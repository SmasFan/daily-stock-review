#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实时模拟炒股 · 双池并行盘中触发引擎（v4）

双池 = 6股精选(six) + 全池(all)，两池各持 3 个真实独立账户（激进/稳健/严守纪律，各 5 万），
互不共享资金/计划/持仓，盘中并行巡检同时成交。合并净值 = 池内三账户加权合成。

数据模型 v4：
  state.pools = { six: {accounts:{...3账户...}, mix:{...}}, all: {...} }
  state.meta.pools = ["six","all"]；旧 v1~v3 单池数据自动迁移 → pools.all，six 全新开账。

命令（默认作用于双池，--pool 可限定单池）：
  python3 sim_live.py --init                 # 初始化（双池 × 3 账户）
  python3 sim_live.py --plan [--pool six|all]   # 重建计划（默认双池；收盘 auto_run 调用）
  python3 sim_live.py --intraday-plan         # 盘中整点复盘重建(刷新买点/补新信号, 需全量成分版review)
  python3 sim_live.py --intraday [--pool ...]   # 盘中巡检触发成交（cron 每2分钟独立锁，双池）
  python3 sim_live.py --review [--date D]       # 收盘复盘 + 自学习
  python3 sim_live.py --plan-date 2026-09-04 --pool six   # 6股池历史日K信号重建
  python3 sim_live.py --strategy-log "msg"   # 策略版本变更

LLM 层（--plan 时自动）:
  - 宏观: data/macro_llm_data.json（macro_llm.py 生成）空头/防御 → 全局闸门
  - 个股评审: 候选股画像 + 宏观 → LLM 批量评审，回避股不入计划（6股池另加个股消息面回避）
"""
import argparse
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "sim_live.json")
CASH_START = 50000.0
# 触碰容差：现价 ≤ 买点×(1+容差) 即视为回踩触发（避免差一分钱漏单），可用 SIM_TOUCH_TOL 覆盖
TOUCH_TOL = float(os.environ.get("SIM_TOUCH_TOL", "0.002"))

# 双池
POOLS = ("six", "all")
POOL_LABEL = {"six": "6股精选", "all": "全池"}

# 账户配置：与历史回测 sim.html 的三种风格对齐
ACCOUNTS = {
    "aggressive": {
        "label": "激进", "icon": "fire", "badge": "#dc2626",
        "budget_frac": 0.22, "max_pos": 5, "cash_reserve": 0.05,
        "buy_bias": 0.005,     # 买点相对现价回踩幅度（激进追强，回踩小）
        "stop_ma": False, "tp_pct": 0.20,
        "trail": 0.10, "be_at": None, "min_score": 66,
        "desc": "仓位大(22%×5)、门槛低(≥66分)、回踩少就买，+20%止盈、峰值回落10%移动止盈，容忍回撤博主升。",
        "rules": ["大盘多头结构才开仓（上证强势/多头排列）", "普涨过热不回避，弱市靠大盘闸门空仓",
                  "ATR 宽止损", "峰值涨超8%后回落10%移动止盈"],
    },
    "balanced": {
        "label": "稳健", "icon": "scale-balanced", "badge": "#d97706",
        "budget_frac": 0.18, "max_pos": 5, "cash_reserve": 0.10,
        "buy_bias": 0.02,      # 回踩 2% 才买（不追高）
        "stop_ma": True, "tp_pct": 0.15,
        "trail": None, "be_at": None, "min_score": 68,
        "desc": "仓位中(18%×5)、门槛68、回踩2%买、破ATR或MA20跌3%止损、+15%止盈。攻守平衡（默认主力账户）。",
        "rules": ["大盘卖出/减仓且<45分不开新仓", "普涨过热日(广度≥65%)不追新",
                  "ATR 或破MA20且跌>3%止损", "+15% 止盈"],
    },
    "disciplined": {
        "label": "严守纪律", "icon": "shield-halved", "badge": "#2563eb",
        "budget_frac": 0.14, "max_pos": 4, "cash_reserve": 0.16,
        "buy_bias": 0.03,      # 更挑剔，回踩 3%
        "stop_ma": False, "tp_pct": 0.10,
        "trail": None, "be_at": 0.05, "min_score": 76,
        "desc": "仓小(14%×4)、只买最强(≥76分)、回踩3%才买；+5%后止损抬成本（保本）、+10%止盈。宁可少赚不亏。",
        "rules": ["只做 ≥76分 多头/强势多头", "+5%后保本（止损抬到成本）",
                  "+10% 止盈", "大盘非多头或广度弱不进"],
    },
}

REAL_ACCOUNTS = ["aggressive", "balanced", "disciplined"]

# 合并账户（共识策略 A）：真账户 5 万独立下单，不自建计划，而是汇总三个子策略的计划：
#   入场：任一子策略出价即挂单（取最浅回踩线）；仓位按「共识数」加权（1个8% / 2个13% / 3个20%）
#   风控：止損取最严（最高）、止盈取最先到（最低）、任一子策略大盘闸门 block 则不开新仓
MIX_KEY = "mix"
MIX_CFG = {"key": MIX_KEY, "label": "合并·共识", "max_pos": 6}
MIX_SIZE = {1: 0.08, 2: 0.13, 3: 0.20}
SUB_ACCOUNTS = list(REAL_ACCOUNTS)

# 精选 6 股池（6c：资源/制造龙头+银行+防守）
SIX_POOL = {
    "601138": "工业富联", "600900": "长江电力", "601899": "紫金矿业",
    "600309": "万华化学", "002142": "宁波银行", "600177": "雅戈尔",
}


def now_ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load_json(name):
    p = os.path.join(DATA_DIR, name)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        st = json.load(f)
    # v1~v3 单池 → v4 双池迁移：旧 accounts/mix → pools.all，six 全新开账
    if st and "pools" not in st and "accounts" in st:
        st["pools"] = {
            "all": {"accounts": st.get("accounts") or {},
                    "mix": st.get("mix") or {"equity_curve": [], "daily_log": [],
                                             "regime": [], "meta": {"start_cash": CASH_START}}},
            "six": pool_books(),
        }
        st.pop("accounts", None)
        st.pop("mix", None)
        st.setdefault("meta", {})
        st["meta"]["pools"] = list(POOLS)
        st["meta"].pop("pool_mode", None)
        st["meta"].pop("pool_switched_at", None)
    st = normalize_state(st)
    return st


def normalize_state(st):
    """补齐双池结构（新老字段兜底）。"""
    if not st:
        st = {}
    st.setdefault("meta", {})
    m = st["meta"]
    m.setdefault("created", now_ts())
    m.setdefault("strategy_version", "v4.0")
    m.setdefault("accounts", list(REAL_ACCOUNTS))
    m.setdefault("pools", list(POOLS))
    st.setdefault("version_history", [])
    for p in POOLS:
        b = st.setdefault("pools", {}).setdefault(p, {})
        acc = b.setdefault("accounts", {})
        for k in REAL_ACCOUNTS:
            if k not in acc or not isinstance(acc[k], dict):
                acc[k] = new_account(k, ACCOUNTS[k])
            else:
                acc[k] = normalize_account(k, acc[k])
        mx = b.setdefault("mix", {})
        # 旧版 mix = 被动加权指数（只有 equity_curve/daily_log/regime/meta，无现金）
        # → 归档为 meta.index_archive，本账户改为真实 5 万共识账户（与三子账户平行独立）
        if "cash" not in mx:
            archive = {"equity_curve": mx.get("equity_curve") or [],
                       "daily_log": mx.get("daily_log") or [],
                       "regime": mx.get("regime") or [],
                       "note": "旧版被动加权指数（三账户按档位权重合成），非真实账户；新版起独立 5 万共识账户"}
            fresh = new_account(MIX_KEY, MIX_CFG)
            fresh["label"] = MIX_CFG["label"]
            fresh["meta"] = dict(mx.get("meta") or {})
            fresh["meta"]["index_archive"] = archive
            fresh["regime"] = list(mx.get("regime") or [])
            b["mix"] = fresh
        else:
            mx = normalize_account(MIX_KEY, mx)
            mx["label"] = MIX_CFG["label"]
            mx.setdefault("regime", [])
            mx.setdefault("meta", {"start_cash": CASH_START})
            b["mix"] = mx
    return st


def normalize_account(key, a):
    for f in ("key", "label", "start_cash", "cash", "positions", "plan",
              "trades", "equity_curve", "daily_log", "review_log", "miss_log", "llm_log"):
        if f not in a:
            if f == "key":
                a[f] = key
            elif f == "label":
                a[f] = ACCOUNTS[key]["label"]
            elif f == "start_cash":
                a[f] = CASH_START
            elif f == "cash":
                a[f] = CASH_START
            elif f in ("positions", "plan", "trades", "equity_curve",
                       "daily_log", "review_log", "miss_log", "llm_log"):
                a[f] = []
    return a


def save(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def new_account(key, cfg):
    return {
        "key": key, "label": cfg["label"], "start_cash": CASH_START,
        "cash": CASH_START, "positions": [], "plan": [], "trades": [],
        "equity_curve": [], "daily_log": [], "review_log": [], "miss_log": [], "llm_log": [],
    }


def pool_books():
    """单个池的三账户账本 + 合并共识账户（真账户，同构）。"""
    mx = new_account(MIX_KEY, MIX_CFG)
    mx["label"] = MIX_CFG["label"]
    mx["regime"] = []
    return {"accounts": {k: new_account(k, ACCOUNTS[k]) for k in REAL_ACCOUNTS},
            "mix": mx}


def new_state():
    return {
        "meta": {"created": now_ts(), "strategy_version": "v4.0",
                 "accounts": list(REAL_ACCOUNTS), "pools": list(POOLS)},
        "pools": {p: pool_books() for p in POOLS},
        "version_history": [],
    }


def books(state, pool):
    return state["pools"][pool]


def accts(state, pool):
    return books(state, pool)["accounts"]


def all_books(state, pool):
    """巡检/结算用：三子账户 + 合并共识账户（键、账本、配置）列表。"""
    ac = accts(state, pool)
    out = [(k, ac[k], ACCOUNTS[k]) for k in REAL_ACCOUNTS]
    out.append((MIX_KEY, books(state, pool)["mix"], MIX_CFG))
    return out


def _is_etf(code):
    return str(code).startswith(("5", "1"))


def equity_of(acct):
    return acct["cash"] + sum(p["shares"] * (p.get("last_close") or p["cost"]) for p in acct["positions"])


# ---------------- LLM 个股评审（计划阶段，一次性批量） ----------------
# 通道：commandcode (deepseek-v4-flash) 优先 → ollama qwen3-vl 降级（macro_llm 同策略）
OLLAMA = "http://localhost:11434"
LLM_MODEL = "qwen3-vl:32b"
CC_URL = "https://api.commandcode.ai/provider/v1/chat/completions"
CC_UA = "OpenAI/Python 1.99.0"
CC_CLOUD_MODEL = "deepseek/deepseek-v4-flash"


def _cc_key():
    import glob as _g
    for envf in _g.glob("/mnt/c/Users/z7280/binance-llm-bot/.env"):
        try:
            for line in open(envf):
                if line.startswith("COMMAND_CODE_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return ""


def _llm_chat_cc(system, user, timeout=120):
    import urllib.request
    key = _cc_key()
    if not key:
        raise RuntimeError("无 commandcode key")
    body = json.dumps({
        "model": CC_CLOUD_MODEL, "stream": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.2, "max_tokens": 2400,
    }).encode()
    req = urllib.request.Request(CC_URL, data=body, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json",
        "User-Agent": CC_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    return (d["choices"][0]["message"]["content"] or "").strip()


def _llm_chat_ollama(system, user, timeout=480):
    import urllib.request
    body = json.dumps({
        "model": LLM_MODEL, "stream": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "format": "json",
        "options": {"num_predict": 4096, "temperature": 0.2, "num_ctx": 16384},
    }).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    content = (d.get("message") or {}).get("content") or ""
    if not content.strip():
        raise RuntimeError("ollama 空返回")
    return content


def _llm_chat(system, user, timeout=480):
    """commandcode 优先 → ollama 降级；空返回自动重试。"""
    errs = []
    import time as _t
    for attempt in range(3):
        try:
            c = _llm_chat_cc(system, user, timeout=min(timeout, 120))
            if c.strip():
                return c
            errs.append("cc 空返回(第%d次)" % (attempt + 1))
        except Exception as e:
            errs.append("cc: %s" % e)
        _t.sleep(1.5 * (attempt + 1))
    return _llm_chat_ollama(system, user, timeout=timeout)


def llm_review_candidates(cands, macro_llm=None):
    """候选股批量评审 → {code: "allow"/"avoid", note}
    cands: [{code,name,score,signal,trend,bias,chg60,sector}]
    纯基于 宏观+量化画像 的逻辑评审；不臆造个股新闻。
    失败/超时 → 全 allow（不影响主流程）。"""
    if not cands:
        return {}
    macro_txt = "无"
    if macro_llm:
        ll = macro_llm.get("llm") or {}
        macro_txt = "%s score=%s | %s | 利好:%s | 风险:%s | 板块:%s" % (
            ll.get("sentiment"), ll.get("score"), ll.get("summary"),
            "、".join(ll.get("drivers") or []) or "-",
            "、".join(ll.get("risks") or []) or "-",
            "、".join(ll.get("sectors") or []) or "-")
    lines = []
    for c in cands:
        lines.append("%s %s | %s %s分 | %s | 乖离MA5 %s%% | 60日 %s%% | %s" % (
            c["code"], c["name"], c.get("signal"), c.get("score"),
            c.get("trend"), c.get("bias", "?"), c.get("chg60", "?"), c.get("sector", "")))
    sysp = """你是A股量化策略的风控评审。系统信号给出买入候选，你要结合【宏观判断】与【个股技术画像】评审：
- 宏观逆风(空头/防御/score<45) 时：回避高位追强(乖离大、60日涨幅大)、回避宏观重灾方向
- 宏观顺风 时：只剔除明显高危(极端乖离追高/放量下跌后反弹)
- 纯技术面已过滤，不要重复挑技术毛病；重点是【宏观与个股方向冲突】
输出严格 JSON: {"reviews":[{"code":"600000","verdict":"allow|avoid","note":"一句话理由(≤25字)"}]}"""
    user = "【宏观】%s\n【候选】\n%s\n评审哪些应 avoid（宏观冲突/高位风险），其余 allow。" % (macro_txt, "\n".join(lines))
    try:
        content = _llm_chat(sysp, user)
        j = json.loads(content)
        out = {}
        for r in j.get("reviews", []):
            code = str(r.get("code", "")).strip()
            if code:
                out[code] = {"verdict": r.get("verdict") == "avoid" and "avoid" or "allow",
                             "note": (r.get("note") or "")[:40]}
        return out
    except Exception as e:
        print("  [llm] 个股评审失败(放行): %s" % e)
        return {}


# ---------------- 盘中 LLM 风控闸门（成交前批量评审） ----------------
# 每次真正成交前（买入触发 / 卖出触发）先问 LLM：allow 执行，avoid 放弃。
# 约束：
#   - 一次巡环只发 1 个请求（本轮所有买卖候选合并批量），短超时（默认 25s）
#   - 同日同标的同方向结果缓存（state.meta.llm_gate），不重复问
#   - 失败/超时 → 全部放行（不能因 LLM 挂掉而停盘）；连错 3 次熔断 10 分钟
#   - 硬止損（ATR/保本保命单）不被 avoid 否决，只记录 LLM 意见（风控优先）
LLM_GATE_ON = os.environ.get("SIM_LLM_GATE", "1").lower() not in ("0", "false", "off", "no")
GATE_TIMEOUT = float(os.environ.get("SIM_LLM_GATE_TIMEOUT", "25"))
GATE_FAIL_LIMIT = 3
GATE_COOLDOWN_MIN = 10


def _json_of(text):
    """从 LLM 返回里提取 JSON（容错 ```json 包围 / 前后废话）。"""
    if not text or not text.strip():
        raise ValueError("空返回")
    t = text.strip()
    if t.startswith("```"):
        parts = t.split("```")
        if len(parts) > 1:
            t = parts[1]
            if t.lower().startswith("json"):
                t = t[4:]
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("无 JSON")
    return json.loads(t[i:j + 1])


def _llm_chat_fast(system, user):
    """盘中短超时调用：cc 一次 → ollama 一次，不重试。"""
    errs = []
    try:
        c = _llm_chat_cc(system, user, timeout=GATE_TIMEOUT)
        if c.strip():
            return c
        errs.append("cc 空返回")
    except Exception as e:
        errs.append("cc: %s" % e)
    c = _llm_chat_ollama(system, user, timeout=GATE_TIMEOUT * 2)
    if c.strip():
        return c
    raise RuntimeError("全部通道空返回 %s" % errs)


def _hhmm_add(hms, minutes):
    """'HH:MM:SS' + N 分钟 → 'HH:MM:SS'（超 24h 截断）。"""
    try:
        p = [int(x) for x in hms.split(":")]
    except Exception:
        return hms
    total = (p[0] * 3600 + p[1] * 60 + p[2] + int(minutes * 60)) % 86400
    return "%02d:%02d:%02d" % (total // 3600, total % 3600 // 60, total % 60)


def _llm_gate_call(tasks):
    """一次批量评审买卖候选，返回 {id: {verdict,note}}；失败/未解析 → None。

    发给 LLM 的 id 用短号 t1/t2...（长 id 会被模型改写成代码/别名），返回后按短号映射，
    认不出短号时再用股票代码兜底匹配。
    """
    short, lines = {}, []
    for i, t in enumerate(tasks, 1):
        sid = "t%d" % i
        short[sid] = t
        if t["action"] == "buy":
            lines.append(
                "%s | 买 %s(%s) | 现价%.2f 当日%+.2f%% | 买点%.2f | %s分 %s | %s | 板块%s | 已触发:%s" % (
                    sid, t["name"], t["code"], t["px"], t.get("chg") or 0,
                    t.get("buy_below") or 0, t.get("score") or "?", t.get("signal") or "",
                    t.get("trend") or "", t.get("sector") or "-", t.get("why") or ""))
        else:
            lines.append(
                "%s | 卖 %s(%s) | 现价%.2f 当日%+.2f%% | 成本%.2f 浮盈%+.2f%% 峰值%+.2f%% | %s | 类型=%s" % (
                    sid, t["name"], t["code"], t["px"], t.get("chg") or 0,
                    t.get("cost") or 0, t.get("gain") or 0, t.get("peak_gain") or 0,
                    t.get("why") or "", "硬止损(不可否决)" if t.get("hard") else "软止盈(可否决)"))
    sysp = """你是A股盘中实时交易风控员，在成交前复核系统触发。
买入判断：回踩买点触发通常是机会；但当日大幅杀跌(跌幅≤-5%)、明显弱势下杀、宏观逆风追高时给 avoid。
卖出判断：硬止损由系统强制执行（你只能确认）；软止盈/移动止盈若个股当日强势上攻(红盘)且趋势未破，可给 avoid 表示继续持有。
只依据给定数据判断，不臆造新闻。宁少误杀，不放过明显风险。
输出严格 JSON：{"decisions":[{"id":"t1","verdict":"allow|avoid","note":"≤20字理由"}]}
id 必须原样照抄（t1/t2/...），不得替换成股票代码；逐一回答所有 id。"""
    user = "【待复核触发】\n%s\n\n逐个给出 allow/avoid，id 原样返回。" % "\n".join(lines)
    try:
        j = _json_of(_llm_chat_fast(sysp, user))
    except Exception as e:
        print("  [llm][gate] 评审失败(放行): %s" % e)
        return None
    out = {}
    for r in (j.get("decisions") or []):
        raw = str(r.get("id") or "").strip()
        t = short.get(raw)
        if t is None:
            hits = [x for x in short.values() if x.get("code") and x["code"] in raw]
            if len(hits) == 1:
                t = hits[0]
        if t is None:
            continue
        out[t["id"]] = {"verdict": "avoid" if str(r.get("verdict")).lower() == "avoid" else "allow",
                        "note": (r.get("note") or "")[:40]}
    return out or None


def llm_trade_gate(state, date, hms, tasks):
    """成交前 LLM 闸门。返回 {id: {verdict, note, src}}。

    src: llm=本次评审 / cache=同日已评 / fallback=LLM不可用放行 / off=闸门关闭
    """
    meta = state.setdefault("meta", {})
    cache = meta.get("llm_gate")
    if not cache or cache.get("date") != date:
        cache = {"date": date, "items": {}}
        meta["llm_gate"] = cache
    items = cache.setdefault("items", {})
    out, fresh = {}, []
    for t in tasks:
        c = items.get(t["id"])
        if c:
            out[t["id"]] = {"verdict": c.get("verdict"), "note": c.get("note"),
                            "src": "cache", "ts": c.get("ts")}
        else:
            fresh.append(t)
    if not fresh:
        return out
    if not LLM_GATE_ON:
        for t in fresh:
            out[t["id"]] = {"verdict": "allow", "note": "闸门关闭", "src": "off"}
        return out
    cb = meta.get("llm_gate_cb") or {}
    if cb.get("until") and hms < cb["until"]:
        for t in fresh:
            out[t["id"]] = {"verdict": "allow",
                            "note": "LLM熔断至%s" % cb["until"], "src": "fallback"}
        return out
    dec = _llm_gate_call(fresh)
    if dec is None:
        cb["streak"] = int(cb.get("streak") or 0) + 1
        if cb["streak"] >= GATE_FAIL_LIMIT:
            cb["until"] = _hhmm_add(hms, GATE_COOLDOWN_MIN)
            cb["streak"] = 0
            print("  [llm][gate] 连续失败 → 熔断至 %s" % cb["until"])
        meta["llm_gate_cb"] = cb
        for t in fresh:
            out[t["id"]] = {"verdict": "allow", "note": "LLM不可用，放行", "src": "fallback"}
        return out
    meta.pop("llm_gate_cb", None)
    for t in fresh:
        d = dec.get(t["id"]) or {"verdict": "allow", "note": "LLM未逐条返回，放行"}
        d["src"] = "llm"
        d["ts"] = hms
        out[t["id"]] = d
        items[t["id"]] = {"verdict": d["verdict"], "note": d.get("note"), "ts": hms}
    return out


def review_from_kline(code, name, date):
    """用日K对指定日收盘算信号（无前视 idx=date），返回 make_plan 用的 item dict。
    用于历史补录（如 9/3 收盘信号 → 9/4 盘中触发）。"""
    from src import data_provider as dp
    from src import analyzer as az
    k = dp.fetch_daily_kline_long(code, count=320, min_days=200, use_cache=True)
    if not k or date not in k["dates"]:
        return None
    i = k["dates"].index(date)
    r = az.analyze_stock(name, k["dates"], k["opens"], k["closes"],
                         k["highs"], k["lows"], k["volumes"], code, idx=i)
    if not r:
        return None
    return {
        "name": name, "code": code, "date": date,
        "close": r.close, "open": r.open, "change_pct": r.change_pct,
        "sector": "", "trend_status": r.trend_status,
        "ma5": r.ma5, "ma10": r.ma10, "ma20": r.ma20, "ma60": r.ma60,
        "bias_ma5": r.bias_ma5, "score": r.score,
        "signal_key": r.signal_key, "signal": r.signal,
        "ideal_buy": r.ideal_buy, "secondary_buy": r.secondary_buy,
        "stop_loss": r.stop_loss, "atr_stop": r.atr_stop,
        "take_profit": r.take_profit, "high20": r.high20, "low20": r.low20,
        "change_60d": r.change_60d,
    }


# ---------------- 计划（每池每账户独立） ----------------
def market_gate(review):
    """大盘闸门：上证空头 / 普涨过热 / LLM宏观防御 → block。返回 (gate, 说明)。"""
    idx_sigs = {x.get("code"): x for x in review.get("indices", [])}
    sh = (idx_sigs.get("sh000001") or {}).get("factors") or {}
    mkt_bear = sh.get("signal") in ("卖出", "减仓") and (sh.get("score") or 0) < 45
    breadth = (review.get("temperature") or {}).get("breadth") or 0
    overheat = breadth >= 65
    llm_def = False
    try:
        _llm = load_json("macro_llm_data.json") or {}
        _ll = (_llm.get("llm") or {})
        _llm_sent = _ll.get("sentiment")
        llm_def = _llm_sent in ("空头", "防御")
        _llm_weak = _llm_sent == "中性" and (_ll.get("score") or 50) < 40
        llm_def = llm_def or _llm_weak
    except Exception:
        pass
    gate = "block" if (mkt_bear or overheat or llm_def) else "open"
    why = []
    if mkt_bear:
        why.append("上证空头")
    if overheat:
        why.append("普涨过热(广度%d%%)" % int(breadth))
    if llm_def:
        why.append("LLM宏观防御")
    return gate, ("；".join(why) if why else "open")


def _strategy_bonus(key, it):
    """策略偏好评分（v3.2）: 各策略候选排序差异化。
    激进=收益最大化（强动量/高趋势强度/量能/温和正乖离）;
    稳健=胜率优先（趋势多头+回调到位+主力进场，不追高不接飞刀）;
    严守=最保守（深度回踩到位才认可，过热/破位重罚）。
    只影响入池排序，不改 review 全局分。
    """
    def num(x):
        return x if isinstance(x, (int, float)) else 0.0
    b = 0.0
    chg60 = num(it.get("change_60d"))
    bias5 = num(it.get("bias_ma5"))
    rsi6 = num(it.get("rsi6"))
    ts = num(it.get("trend_strength"))
    vr = num(it.get("volume_ratio"))
    ff = it.get("fund_flow")
    main_net = num(ff.get("main_net")) if isinstance(ff, dict) else 0.0
    if key == "aggressive":
        # 收益优先：动量 + 趋势强度 + 放量 + 温和正乖离；但主力大幅出逃/极端涨幅重罚（防接盘）
        if chg60 > 0:
            b += min(chg60 * 0.12, 10.0)
            if chg60 > 80:
                b -= 5                      # 翻倍高位=离场风险
        b += max(ts - 70, 0) * 0.15
        if vr >= 1.0:
            b += 2.0
        if 0 < bias5 < 6:
            b += bias5 * 0.4
        if main_net > 0:
            b += 1.5
        elif main_net < -10000e4:
            b -= 4.0
        b -= max(rsi6 - 85, 0) * 0.3
    elif key == "balanced":
        # 胜率优先：回调到位 + 主力进场；过热/破位/主力出逃重罚
        if 5 <= chg60 <= 45:
            b += 4
        elif chg60 > 60:
            b -= 6
        elif chg60 < -10:
            b -= 4
        if -3 <= bias5 <= 1:
            b += 4
        elif bias5 > 5:
            b -= 5
        elif bias5 < -4:
            b -= 3
        if main_net > 0:
            b += 2.5
        elif main_net < -8000e4:
            b -= 6.0                       # 主力大幅出逃(万元)
        elif main_net < -2000e4:
            b -= 3.0
        if ts >= 60:
            b += 1.5
        if rsi6 > 75:
            b -= 3
        if vr > 2.5:
            b -= 1.5
    else:  # disciplined 严守纪律：只认可深度回踩企稳+主力不撤，过热重罚
        if 5 <= chg60 <= 35:
            b += 5
        elif chg60 > 50:
            b -= 8
        elif chg60 < -5:
            b -= 6
        if -4 <= bias5 <= 0:
            b += 6                        # 深度回踩至均线(买点质量最高)
        elif bias5 <= 2:
            b += 2
        else:
            b -= 9                        # 偏高乖离拒买(严守不追)
        if main_net > 0:
            b += 3
        elif main_net < -5000e4:
            b -= 7
        elif main_net < -1000e4:
            b -= 3.5
        if rsi6 > 75:
            b -= 8                        # 纪律账户显著厌恶过热
        elif rsi6 > 68:
            b -= 4
        if ts < 65:
            b -= 4
        if vr > 2.0:
            b -= 2
    return b


def make_plan(state, review, asof, pool, skip_llm=False, log=True):
    """对单个池生成 3 账户回踩买点计划。"""
    items = review.get("items", []) or []
    pool_b = books(state, pool)
    acc_b = pool_b["accounts"]
    gate, gate_why = market_gate(review)
    # 候选：池过滤
    cand_pool = {}   # code -> item
    per_key = {}     # key -> [item]
    for key in REAL_ACCOUNTS:
        cfg = ACCOUNTS[key]
        acct = acc_b[key]
        held = {p["code"] for p in acct["positions"]}
        cands = []
        for it in items:
            if it.get("code") in held:
                continue
            if pool == "six" and it.get("code") not in SIX_POOL:
                continue
            if it.get("signal_key") not in ("strong_buy", "buy"):
                continue
            if (it.get("score") or 0) < cfg["min_score"]:
                continue
            if it.get("trend_status") not in ("强势多头", "多头排列"):
                continue
            cands.append(it)
        # 策略差异化排序：原始分 + 策略偏好分（v3.2）
        cands.sort(key=lambda x: -(x.get("score", 0) + _strategy_bonus(key, x)))
        per_key[key] = cands
        for it in cands:
            cand_pool.setdefault(it.get("code"), it)
    # LLM 个股评审（盘前一次性；失败放行）
    llm_rev = {}
    _news_rev = {}
    if not skip_llm:
        _llm_macro = None
        try:
            _llm_macro = load_json("macro_llm_data.json")
            # 个股消息面评审（macro_llm.py --pool six 产出 stocks）：利空/低分 → 回避
            for _s in (_llm_macro.get("stocks") or []):
                if _s.get("sentiment") in ("利空", "防御") or (_s.get("score") or 50) < 45:
                    _news_rev[_s.get("code")] = "消息面:%s(%s)" % (
                        _s.get("sentiment"), _s.get("note", ""))
        except Exception:
            pass
        rev_cands = [{"code": it["code"], "name": it.get("name", ""),
                      "score": it.get("score"), "signal": it.get("signal"),
                      "trend": it.get("trend_status"),
                      "bias": it.get("bias_ma5"), "chg60": it.get("change_60d"),
                      "sector": it.get("sector")}
                     for it in sorted(cand_pool.values(), key=lambda x: -x.get("score", 0))[:12]]
        if rev_cands:
            llm_rev = llm_review_candidates(rev_cands, _llm_macro)
            n_avoid = sum(1 for v in llm_rev.values() if v.get("verdict") == "avoid")
            print("  [llm][%s] 个股评审 %d 只 → avoid %d" % (pool, len(rev_cands), n_avoid))
    for key in REAL_ACCOUNTS:
        cfg = ACCOUNTS[key]
        acct = acc_b[key]
        cands = per_key.get(key, [])
        plan = []
        for it in cands[:8]:
            _rv = llm_rev.get(it.get("code"))
            _nr = _news_rev.get(it.get("code")) if pool == "six" else None
            if _rv and _rv.get("verdict") == "avoid":
                acct["daily_log"].append({"date": asof, "kind": "plan",
                                          "note": "[%s]%s：%s LLM技术回避(%s)" % (
                                              POOL_LABEL[pool], cfg["label"], it.get("name"),
                                              _rv.get("note", ""))})
                continue
            if _nr:
                acct["daily_log"].append({"date": asof, "kind": "plan",
                                          "note": "[%s]%s：%s %s" % (
                                              POOL_LABEL[pool], cfg["label"],
                                              it.get("name"), _nr)})
                continue
            close = it.get("close") or 0
            ma10 = it.get("ma10") or close
            ideal = it.get("ideal_buy") or it.get("secondary_buy") or close
            # 回踩买点：现价下方 bias 处；且不低于 MA10/ideal 过远
            floor = min(ideal, close * (1 - cfg["buy_bias"]))
            buy_below = min(close * (1 - cfg["buy_bias"]), max(floor, close * 0.96))
            buy_below = round(buy_below, 3)
            budget = round(CASH_START * cfg["budget_frac"], 2)
            plan.append({
                "code": it["code"], "name": it.get("name"), "asof": asof,
                "score": it.get("score"), "signal": it.get("signal"),
                "close": close, "buy_below": buy_below,
                "stop_atr": it.get("atr_stop"),
                "stop_ma": it.get("stop_loss") if cfg["stop_ma"] else None,
                "tp": round(close * (1 + cfg["tp_pct"]), 3) if cfg["tp_pct"] else None,
                "trail": cfg.get("trail"), "be_at": cfg.get("be_at"),
                "gate": gate, "budget": budget, "status": "wait",
                "reason": "%s(%s分) 回踩≤%.2f ATR止损%s%s%s" % (
                    it.get("signal"), it.get("score"), buy_below,
                    it.get("atr_stop") if it.get("atr_stop") else "--",
                    "（保本+5%%）" if cfg.get("be_at") else "",
                    ("；LLM:" + _rv.get("note", "")) if _rv else ""),
            })
        acct["plan"] = plan
        if log:
            acct["daily_log"].append({"date": asof, "kind": "plan",
                                      "note": "[%s]%s：%d 单待盘中触发%s" % (
                                          POOL_LABEL[pool], cfg["label"], len(plan),
                                          ("（大盘闸门挡）" if gate == "block" else ""))})
    make_mix_plan(state, pool, asof, gate, log)
    return {k: len(acc_b[k]["plan"]) for k in REAL_ACCOUNTS}


def make_mix_plan(state, pool, asof, gate, log=True):
    """合并共识账户计划 = 汇总三子策略计划（A 方案）。

    - 候选：任一子策略 wait 单中的股票（合并账户已持仓的排除）
    - 共识数 n = 同时给出该股 wait 单的子策略个数 → 仓位 MIX_SIZE[n]（1→8% / 2→13% / 3→20%）
    - 入场线取「最浅」（max buy_below，任一策略回踩即接）；
      止損取最严（max stop_atr，最早离场）；止盈取最先到（min tp）；保本/移动止盈取最保守（取有值的）
    - 闸门：任一子策略该股 gate=block → 合并也不开（最保守）
    """
    mix = books(state, pool)["mix"]
    held = {p["code"] for p in mix["positions"]}
    groups = {}
    for k in REAL_ACCOUNTS:
        for pl in accts(state, pool)[k].get("plan", []):
            if pl.get("status", "wait") != "wait":
                continue
            if pl["code"] in held:
                continue
            groups.setdefault(pl["code"], []).append((k, pl))
    plan = []
    for code, lst in groups.items():
        n = len(lst)
        src = [pl for _, pl in lst]
        budget = round(CASH_START * MIX_SIZE.get(n, MIX_SIZE[1]), 2)
        stops = [pl["stop_atr"] for pl in src if pl.get("stop_atr")]
        tps = [pl["tp"] for pl in src if pl.get("tp")]
        blocked = any(pl.get("gate") == "block" for pl in src)
        plan.append({
            "code": code, "name": src[0].get("name"), "asof": asof,
            "score": max((pl.get("score") or 0) for pl in src),
            "signal": src[0].get("signal"),
            "close": max((pl.get("close") or 0) for pl in src),
            "buy_below": round(max(pl["buy_below"] for pl in src), 3),
            "stop_atr": round(max(stops), 3) if stops else None,
            "stop_ma": None,
            "tp": round(min(tps), 3) if tps else None,
            "trail": next((pl.get("trail") for pl in src if pl.get("trail")), None),
            "be_at": next((pl.get("be_at") for pl in src if pl.get("be_at")), None),
            "gate": "block" if blocked else (gate or "open"),
            "budget": budget, "status": "wait", "consensus": n,
            "from": [k for k, _ in lst],
            "reason": "共识%d/3（%s）仓位%.0f%% 回踩≤%.2f 止損%s 止盈%s" % (
                n, "/".join(ACCOUNTS[k]["label"] for k, _ in lst), MIX_SIZE[n] * 100,
                max(pl["buy_below"] for pl in src),
                "%.2f" % max(stops) if stops else "--",
                "%.2f" % min(tps) if tps else "--"),
        })
    # 共识数降序（三策略共振优先），再按分
    plan.sort(key=lambda x: (-x["consensus"], -(x.get("score") or 0)))
    mix["plan"] = plan[:12]
    if log:
        mix["daily_log"].append({"date": asof, "kind": "plan",
                                  "note": "[%s]%s：%d 单待触发（共识3:%d 共识2:%d 共识1:%d）%s" % (
                                      POOL_LABEL[pool], MIX_CFG["label"], len(mix["plan"]),
                                      sum(1 for p in mix["plan"] if p["consensus"] == 3),
                                      sum(1 for p in mix["plan"] if p["consensus"] == 2),
                                      sum(1 for p in mix["plan"] if p["consensus"] == 1),
                                      "（大盘闸门挡）" if gate == "block" else "")})
    return len(mix["plan"])


# ---------------- 盘中巡检（双池并行） ----------------
def _review_ctx():
    """review_data 索引（板块/趋势/评分），供 LLM 闸门补充上下文。"""
    rev = load_json("review_data.json") or {}
    return {x.get("code"): x for x in (rev.get("items") or [])}


def intraday_scan(state, date, hms):
    """盘中巡检两阶段：先只读扫描出买卖候选 → 一次批量 LLM 风控复核 → 落账。"""
    from src import data_provider as dp
    all_codes = set()
    for pool in POOLS:
        for _k, a, _cfg in all_books(state, pool):
            all_codes |= {p["code"] for p in a["plan"] if p.get("status", "wait") == "wait"}
            all_codes |= {p["code"] for p in a["positions"]}
    if not all_codes:
        return 0, ["无计划/持仓"]
    quotes = {}
    try:
        quotes = dp.fetch_quotes(sorted(all_codes))
    except Exception as e:
        return 0, ["快照失败 %s" % e]
    ctx = _review_ctx()
    # 第一遍：只读扫描，收集候选（不落账）
    probes, tasks = {}, []
    for pool in POOLS:
        for key, a, cfg in all_books(state, pool):
            sells, buys, pnotes = _probe_account(pool, a, cfg, quotes, date, hms, ctx)
            probes[(pool, key)] = (a, cfg, sells, buys, pnotes)
            tasks.extend(sells)
            tasks.extend(buys)
    # 第二遍：成交前 LLM 风控（本轮候选合并 1 次请求）
    decisions = llm_trade_gate(state, date, hms, tasks) if tasks else {}
    total_fill, all_notes, veto_n = 0, [], 0
    for pool in POOLS:
        for key, a, cfg in all_books(state, pool):
            ac, cfg2, sells, buys, pnotes = probes[(pool, key)]
            n, notes, v = _apply_account(pool, ac, cfg2, quotes, date, hms,
                                         sells, buys, decisions)
            total_fill += n
            veto_n += v
            all_notes.extend(pnotes)
            all_notes.extend(notes)
    if tasks:
        srcs = ",".join(sorted({(d or {}).get("src", "?") for d in decisions.values()})) or "-"
        print("  [llm][gate] 候选%d → 成交%d 否决%d（来源:%s）" % (
            len(tasks), total_fill, veto_n, srcs))
    return total_fill, all_notes


def _mark_miss(pool, acct, pl, low, date, hms, tol):
    """漏单留痕：盘中最低已触及买点，但巡检采样点没抓到（采样间隔/锁排队）。

    首次发现写 acct['miss_log'] + 返回 True（用于打日志）；同日重复触碰只累加计数。
    """
    m = pl.get("miss") or {}
    if m.get("date") != date:
        m = {"date": date, "first_ts": hms, "low": low, "count": 1, "caught": None}
        first = True
    else:
        m["count"] = int(m.get("count", 0)) + 1
        m["low"] = min(m.get("low") or low, low)
        m["last_ts"] = hms
        first = False
    pl["miss"] = m
    if first:
        acct.setdefault("miss_log", []).append({
            "date": date, "code": pl["code"], "name": pl.get("name"),
            "buy_below": pl["buy_below"],
            "trigger": round(pl["buy_below"] * (1 + tol), 3),
            "day_low": low, "first_ts": hms,
            "note": ("盘中最低%.2f 已≤买点%.2f（含%.1f%%容差），巡检未采到 → 漏单留痕"
                     % (low, pl["buy_below"], tol * 100)),
        })
    return first


def _probe_account(pool, acct, cfg, quotes, date, hms, ctx=None):
    """第一遍：只读扫描（刷新持仓现价印记/漏单留痕），产出卖出与买入候选。"""
    ctx = ctx or {}
    sells, buys, notes = [], [], []
    for pos in acct["positions"]:
        q = quotes.get(pos["code"])
        if not q:
            continue
        px = q.get("price")
        if not px:
            continue
        prev = pos.get("prev_close") or pos["cost"]
        # 当日涨跌基准随行情刷新：pos.prev_close 建仓时记的是「买入日的前收」，
        # 跨日不更新会把多日涨跌当成当日（曾出现涨跌符号都反了的显示）。
        live_pc = q.get("prevClose")
        if live_pc and live_pc > 0:
            pos["prev_close"] = live_pc
            prev = live_pc
        chg = (px / prev - 1) * 100 if prev else 0
        pos["last"] = px
        pos["last_chg"] = round(chg, 2)
        pos["last_ts"] = hms
        if px > pos.get("peak", pos["cost"]):
            pos["peak"] = px
        gain = (px / pos["cost"] - 1) * 100
        reason = None
        hard = False
        if pos.get("tp") and px >= pos["tp"]:
            reason = "止盈：现价%.2f≥目标%.2f（+%.1f%%）" % (px, pos["tp"], gain)
        elif pos.get("be_at") and gain >= pos["be_at"] * 100 and not pos.get("be_on"):
            pos["be_on"] = True
            pos["stop_atr"] = pos["cost"] * 1.002  # 保本
            notes.append("（[%s]%s %s 浮盈+%.0f%% → 保本锁定）" % (
                POOL_LABEL[pool], acct["label"], pos["name"], gain))
        elif pos.get("stop_atr") and px <= pos["stop_atr"]:
            reason = "破ATR/保本止损 %.2f" % pos["stop_atr"]
            hard = True
        elif pos.get("peak") and pos.get("trail") and pos["peak"] > pos["cost"] * 1.08 \
                and px / pos["peak"] - 1 <= -pos["trail"]:
            reason = "移动止盈（峰值%+.1f%%回落%.0f%%）" % ((pos["peak"] / pos["cost"] - 1) * 100,
                                                      pos["trail"] * 100)
        if reason:
            it = ctx.get(pos["code"]) or {}
            sells.append({
                "id": "%s|%s|%s|sell" % (pool, acct["key"], pos["code"]),
                "action": "sell", "code": pos["code"], "name": pos["name"],
                "px": px, "chg": chg, "cost": pos["cost"], "gain": gain,
                "peak_gain": (pos.get("peak", px) / pos["cost"] - 1) * 100,
                "why": reason, "hard": hard, "pos": pos,
                "sector": it.get("sector"), "trend": it.get("trend_status"),
                "score": it.get("score") or pos.get("score"),
            })
    for pl in acct.get("plan", []):
        if pl.get("status", "wait") != "wait":
            continue
        if pl.get("gate") == "block":
            continue
        if len(acct["positions"]) >= cfg["max_pos"]:
            continue
        q = quotes.get(pl["code"])
        if not q:
            continue
        px = q.get("price")
        if not px:
            continue
        if px > (pl.get("close") or 0) * 1.03:
            continue  # 高开冲高不追
        pc = q.get("prevClose") or pl.get("close")
        chg = (px / pc - 1) * 100 if pc else 0
        trigger = round(pl["buy_below"] * (1 + TOUCH_TOL), 3)
        if px <= trigger:
            it = ctx.get(pl["code"]) or {}
            buys.append({
                "id": "%s|%s|%s|buy" % (pool, acct["key"], pl["code"]),
                "action": "buy", "code": pl["code"], "name": pl["name"],
                "px": px, "chg": chg, "buy_below": pl["buy_below"],
                "why": pl.get("reason", ""), "pl": pl,
                "sector": it.get("sector"),
                "trend": it.get("trend_status") or pl.get("signal"),
                "score": pl.get("score"), "signal": pl.get("signal"),
            })
        else:
            low = q.get("low") or 0
            if low and low <= trigger:
                if _mark_miss(pool, acct, pl, low, date, hms, TOUCH_TOL):
                    notes.append("⚠️[%s] %s 错过买点：盘中最低%.2f≤%.2f（买点%s），巡检未采到" % (
                        acct["label"], pl["name"], low, trigger, pl["buy_below"]))
    return sells, buys, notes


def _apply_account(pool, acct, cfg, quotes, date, hms, sells, buys, decisions):
    """第二遍：按 LLM 决策落账。返回 (成交数, 日志, 被否决策数)。"""
    filled, notes, veto_n = 0, [], 0
    # ---- 卖出 ----
    sell_codes = []
    for s in sells:
        pos = s["pos"]
        d = decisions.get(s["id"]) or {}
        if d.get("verdict") == "avoid" and not s["hard"]:
            # 软止盈被 LLM 否决 → 继续持有（改由移动止盈/止损兜底）
            veto_n += 1
            pos["llm_hold"] = {"date": date, "ts": hms, "note": d.get("note")}
            acct.setdefault("llm_log", []).append({
                "date": date, "ts": hms, "pool": pool, "action": "hold",
                "code": s["code"], "name": s["name"],
                "note": "LLM 否决卖出，继续持有：%s（%s）" % (d.get("note") or "", s["why"])})
            notes.append("🧠[%s] %s LLM否决卖出→继续持有：%s" % (
                acct["label"], s["name"], d.get("note") or ""))
            continue
        px = s["px"]
        shares = pos["shares"]
        proceeds = shares * px
        fee = proceeds * (0.0003 + 0.0005)
        pnl = proceeds - shares * pos["cost"]
        acct["cash"] += proceeds - fee
        acct["trades"].append({
            "action": "sell", "date": date, "time": hms, "code": pos["code"],
            "name": pos["name"], "price": round(px, 3), "shares": shares,
            "chg_at_fill": round(s["chg"], 2), "pnl": round(pnl, 2),
            "pnl_pct": round((px / pos["cost"] - 1) * 100, 2),
            "reason": s["why"], "strategy": acct["key"], "pool": pool,
            "llm": {"verdict": d.get("verdict"), "note": d.get("note"),
                    "src": d.get("src")},
        })
        sell_codes.append(pos["code"])
        filled += 1
        notes.append("[%s] 卖出 %s @%.2f（%+.2f%%）%s%s" % (
            acct["label"], pos["name"], px, s["chg"], s["why"],
            "｜LLM:%s" % (d.get("note") or "") if d.get("note") else ""))
    acct["positions"] = [p for p in acct["positions"] if p["code"] not in sell_codes]
    # ---- 买入 ----
    for b in buys:
        pl = b["pl"]
        if pl.get("status", "wait") != "wait":
            continue
        d = decisions.get(b["id"]) or {}
        if d.get("verdict") == "avoid":
            veto_n += 1
            pl["llm_veto"] = {"date": date, "ts": hms, "note": d.get("note")}
            acct.setdefault("llm_log", []).append({
                "date": date, "ts": hms, "pool": pool, "action": "skip_buy",
                "code": b["code"], "name": b["name"],
                "note": "LLM 否决买入：%s" % (d.get("note") or "")})
            notes.append("🧠[%s] %s LLM否决买入：%s" % (
                acct["label"], b["name"], d.get("note") or ""))
            continue
        if pl.get("gate") == "block":
            pl["status"] = "skip_gate"
            continue
        if len(acct["positions"]) >= cfg["max_pos"]:
            pl["status"] = "skip_full"
            continue
        px = b["px"]
        budget = min(pl["budget"], acct["cash"] * 0.98)
        shares = int(budget / px / 100) * 100
        if shares <= 0:
            continue
        cost = shares * px
        fee = cost * 0.0003
        acct["cash"] -= cost + fee
        acct["positions"].append({
            "code": pl["code"], "name": pl["name"], "shares": shares,
            "cost": round(px, 3), "buy_date": date, "buy_time": hms,
            "stop_atr": pl.get("stop_atr"), "stop_ma": pl.get("stop_ma"),
            "tp": pl.get("tp"), "trail": pl.get("trail"), "be_at": pl.get("be_at"),
            "be_on": False, "peak": px, "prev_close": quotes.get(pl["code"], {}).get("prevClose"),
            "score": pl.get("score"), "signal": pl.get("signal"),
            # 合并共识账户专用：共识数与来源子策略（页面展示买入依据）
            "consensus": pl.get("consensus"), "from": pl.get("from"),
            "llm": {"verdict": d.get("verdict"), "note": d.get("note"),
                    "src": d.get("src")},
        })
        acct["trades"].append({
            "action": "buy", "date": date, "time": hms, "code": pl["code"],
            "name": pl["name"], "price": round(px, 3), "shares": shares,
            "chg_at_fill": round(b["chg"], 2),
            "reason": pl.get("reason", ""), "strategy": acct["key"], "pool": pool,
            "llm": {"verdict": d.get("verdict"), "note": d.get("note"),
                    "src": d.get("src")},
        })
        pl["status"] = "filled"
        if pl.get("miss"):
            pl["miss"]["caught"] = hms
        filled += 1
        notes.append("[%s] 买入 %s @%.2f 回踩触发%s" % (
            acct["label"], pl["name"], px,
            "｜LLM:%s" % (d.get("note") or "") if d.get("note") else ""))
    return filled, notes, veto_n


def _scan_account(pool, acct, cfg, quotes, date, hms, decisions=None):
    """兼容封装：单账户一轮巡检（无 LLM 决策时等价于全放行）。"""
    sells, buys, pnotes = _probe_account(pool, acct, cfg, quotes, date, hms)
    n, notes, _v = _apply_account(pool, acct, cfg, quotes, date, hms, sells, buys, decisions or {})
    return n, pnotes + notes


def chg(px, q):
    pc = q.get("prevClose")
    return (px / pc - 1) * 100 if pc else 0


# ---------------- 收盘结算（每池独立） ----------------
def sh_slope(date):
    """上证 20 日斜率（无前视）。"""
    try:
        from src import data_provider as dp
        sh_k = dp.fetch_index_kline("sh000001", 900)
    except Exception:
        return None
    if not sh_k or len(sh_k["dates"]) < 22 or date not in sh_k["dates"]:
        return None
    i = sh_k["dates"].index(date)
    if i < 21 or not sh_k["dates"][i - 21] or not sh_k["closes"][i - 21]:
        return None
    return (sh_k["closes"][i] / sh_k["closes"][i - 21] - 1) * 100


def finalize_pool(state, pool, date):
    """单池收盘：各账户市值按收盘价更新 + 净值曲线；池内 mix 合成。返回 regime。"""
    from src import data_provider as dp
    review = load_json("review_data.json")
    items = {x.get("code"): x for x in (review.get("items") or [])} if review else {}
    pool_b = books(state, pool)
    acc_b = pool_b["accounts"]
    for key in REAL_ACCOUNTS:
        a = acc_b[key]
        for pos in a["positions"]:
            it = items.get(pos["code"])
            if it:
                pos["last_close"] = it.get("close")
                pos["last"] = it.get("close")
                # 收盘后当日涨跌基准 = 上一交易日收盘（review item: close 与 change_pct 反推）
                c0, cpct = it.get("close"), it.get("change_pct")
                if c0 and cpct is not None and (1 + cpct / 100) > 0:
                    pc = round(c0 / (1 + cpct / 100), 3)
                    if pc > 0:
                        pos["prev_close"] = pc
                        pos["last_chg"] = round(cpct, 2)
                        pos["last_ts"] = "close"
        eq = equity_of(a)
        a["equity_curve"] = [x for x in a["equity_curve"] if x["date"] != date]
        prev = a["equity_curve"][-1]["equity"] if a["equity_curve"] else CASH_START
        a["equity_curve"].append({"date": date, "equity": round(eq, 2),
                                  "cash": round(a["cash"], 2),
                                  "daily_return": round((eq / prev - 1) * 100, 3),
                                  "pos_count": len(a["positions"])})
    # 合并共识账户：真实账户，与三子账户同法结算；同时记当日档位（供页面展示）
    slope = sh_slope(date)
    regs = [0.25, 0.5, 0.25]
    if slope is not None and slope > 2.5:
        regs = [0.7, 0.2, 0.1]
    elif slope is not None and slope < -2.5:
        regs = [0.1, 0.2, 0.7]
    regime = "攻" if regs[0] >= 0.7 else ("守" if regs[2] >= 0.7 else "衡")
    mx = pool_b["mix"]
    for pos in mx["positions"]:
        it = items.get(pos["code"])
        if it:
            pos["last_close"] = it.get("close")
            pos["last"] = it.get("close")
            c0, cpct = it.get("close"), it.get("change_pct")
            if c0 and cpct is not None and (1 + cpct / 100) > 0:
                pc = round(c0 / (1 + cpct / 100), 3)
                if pc > 0:
                    pos["prev_close"] = pc
                    pos["last_chg"] = round(cpct, 2)
                    pos["last_ts"] = "close"
    eq_mx = equity_of(mx)
    mx["equity_curve"] = [x for x in mx["equity_curve"] if x["date"] != date]
    prev_mx = mx["equity_curve"][-1]["equity"] if mx["equity_curve"] else CASH_START
    mx["equity_curve"].append({"date": date, "equity": round(eq_mx, 2),
                               "cash": round(mx["cash"], 2),
                               "daily_return": round((eq_mx / prev_mx - 1) * 100, 3),
                               "pos_count": len(mx["positions"]), "regime": regime})
    mx.setdefault("regime", [])
    mx["regime"] = [x for x in mx["regime"] if x.get("date") != date]
    mx["regime"].append({"date": date, "regime": regime})
    return regime


def do_review(state, pool, date):
    for key, a, _cfg in all_books(state, pool):
        sells = [t for t in a["trades"] if t["action"] == "sell"]
        if not sells:
            continue
        if a["review_log"] and a["review_log"][-1]["date"] == date:
            continue
        wins = [t for t in sells if t["pnl"] > 0]
        losses = [t for t in sells if t["pnl"] <= 0]
        note = "[%s]%s：平仓%s笔 胜%s/负%s 胜率%.0f%% 已实现%+.0f" % (
            POOL_LABEL[pool], a["label"], len(sells), len(wins), len(losses),
            len(wins) / len(sells) * 100, sum(t["pnl"] for t in sells))
        if losses:
            lr = {}
            for t in losses:
                k = t["reason"].split("：")[0][:10]
                lr[k] = lr.get(k, 0) + 1
            top = sorted(lr.items(), key=lambda x: -x[1])[:2]
            note += "｜亏因：" + "、".join("%s×%d" % x for x in top)
            note += "｜均亏%.0f" % (sum(t["pnl"] for t in losses) / len(losses))
        a["review_log"].append({"date": date, "note": note})
        a["daily_log"].append({"date": date, "kind": "review", "note": note})


def expiry_plan(state, pool, date):
    """收盘后：过期旧计划单标 expired。
    判据：asof < 结算日 —— 即该计划对应的信号日是过去交易日、当日未触发，已无用。
    刚生成的次日计划 asof == date（同轮 plan→review）或未来日会保留到次日盘中触发。
    """
    for _k, a, _cfg in all_books(state, pool):
        for p in a["plan"]:
            if p.get("status", "wait") == "wait" and (p.get("asof") or "") < date:
                p["status"] = "expired"


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--intraday", action="store_true")
    ap.add_argument("--intraday-plan", action="store_true",
                    help="盘中整点用最新全量复盘刷新双池计划(成分版, items>300 才执行)")
    ap.add_argument("--replay", default=None)
    ap.add_argument("--review", action="store_true")
    ap.add_argument("--finalize", action="store_true")
    ap.add_argument("--strategy-log", default=None)
    ap.add_argument("--pool", default=None, help="six=6股精选 / all=全池（默认双池并行）")
    ap.add_argument("--plan-date", default=None, help="历史日收盘信号重建计划(如2026-09-03，仅six)")
    ap.add_argument("--no-llm", action="store_true", help="跳过LLM个股评审(快速重建用)")
    ap.add_argument("--force", action="store_true",
                    help="--intraday 在非交易时段也执行（默认拒绝，避免拿收盘价当盘中成交）")
    ap.add_argument("--date", default=None)
    args = ap.parse_args()

    if args.pool and args.pool not in POOLS:
        print("未知池模式:", args.pool, "（six|all，省略=双池并行）")
        return
    target_pools = [args.pool] if args.pool else list(POOLS)

    if args.strategy_log:
        st = load_state()
        if not st:
            print("先 --init")
            return
        v = st["meta"].get("strategy_version", "v?")
        m = v.split("."); m[1] = str(int(m[1]) + 1); nv = ".".join(m)
        st["meta"]["strategy_version"] = nv
        st["version_history"].append({"date": now_ts()[:10], "from": v, "to": nv,
                                      "change": args.strategy_log})
        save(st)
        print("策略 %s → %s：%s" % (v, nv, args.strategy_log))
        return

    if args.init or not load_state():
        state = new_state()
        save(state)
        print("双池初始化：six(6股精选) + all(全池) × 激进/稳健/严守纪律 各 5万，各自独立并行交易。")
        print("先 --plan 建计划（默认双池）。")
        return
    state = load_state()
    print("当前双池: " + " + ".join("%s(%s)" % (p, POOL_LABEL[p]) for p in POOLS))

    if args.plan:
        if args.plan_date:
            # 历史日：6股池用日K重建信号（无前视）
            date = args.plan_date
            if "six" not in target_pools:
                print("--plan-date 仅支持 6股精选池（--pool six）")
                return
            items = []
            for c, n in SIX_POOL.items():
                it = review_from_kline(c, n, date)
                if it:
                    items.append(it)
            review = {"generatedAt": date + " 15:00:00", "items": items,
                      "indices": [], "temperature": {"breadth": 0}}
            print("6股池历史日信号重建 %s：%d 只有效" % (date, len(items)))
            for it in items:
                print("   %s %s %s(%s分) %s" % (it["code"], it["name"], it["signal"],
                                               it["score"], it["trend_status"]))
            res = make_plan(state, review, date, "six", skip_llm=bool(args.no_llm))
            save(state)
            print("6股池计划更新 %s：%s" % (date, {ACCOUNTS[k]["label"] + ":" + str(v)
                                                   for k, v in res.items()}))
            return
        review = load_json("review_data.json")
        if not review:
            print("无 review_data")
            return
        date = (review.get("generatedAt") or "")[:10]
        for pool in target_pools:
            res = make_plan(state, review, date, pool, skip_llm=bool(args.no_llm))
            print("计划更新 %s [%s]：%s" % (date, POOL_LABEL[pool],
                                          {ACCOUNTS[k]["label"] + ":" + str(v) for k, v in res.items()}))
            for k in REAL_ACCOUNTS:
                a = accts(state, pool)[k]
                print("  [%s] %d 单" % (ACCOUNTS[k]["label"], len(a["plan"])))
                for p in a["plan"][:5]:
                    print("    %s 分%s 回踩≤%.2f %s" % (p["name"], p["score"], p["buy_below"],
                                                       p.get("gate")))
            mx = books(state, pool)["mix"]
            print("  [%s] %d 单%s" % (MIX_CFG["label"], len(mx["plan"]),
                                    "（%s）" % "/".join(
                                        "共识%s×%d" % (c, sum(1 for p in mx["plan"] if p.get("consensus") == c))
                                        for c in (3, 2, 1) if any(p.get("consensus") == c for p in mx["plan"]))
                                    if mx["plan"] else ""))
            for p in mx["plan"][:5]:
                print("    [共识%d] %s 分%s 回踩≤%.2f %s" % (
                    p.get("consensus", 0), p["name"], p["score"], p["buy_below"], p.get("gate")))
        save(state)
        return

    if args.intraday_plan:
        # 盘中整点复盘重建：用最新全量复盘(含ETF成分, items>300)刷新双池待触发计划
        review = load_json("review_data.json")
        if not review:
            print("无 review_data")
            return
        items = review.get("items") or []
        if len(items) < 300:
            print("盘中 review 非全量(成分版, items=%d)，跳过盘中重建" % len(items))
            return
        date = (review.get("generatedAt") or "")[:10]
        changed = 0
        for pool in target_pools:
            before = {k: {p["code"] for p in accts(state, pool)[k].get("plan", [])}
                      for k in REAL_ACCOUNTS}
            before_mix = {p["code"] for p in books(state, pool)["mix"].get("plan", [])}
            res = make_plan(state, review, date, pool, skip_llm=True, log=False)
            after = {k: {p["code"] for p in accts(state, pool)[k].get("plan", [])}
                     for k in REAL_ACCOUNTS}
            for k in REAL_ACCOUNTS:
                newc = after[k] - before[k]
                if newc:
                    nm = {p["code"]: p.get("name") for p in accts(state, pool)[k]["plan"]}
                    accts(state, pool)[k]["daily_log"].append({
                        "date": date, "kind": "plan",
                        "note": "[%s]%s 盘中复盘刷新: 新增 %s" % (
                            POOL_LABEL[pool], ACCOUNTS[k]["label"],
                            ",".join(nm.get(c, c) for c in sorted(newc)))})
                    changed += 1
            mx = books(state, pool)["mix"]
            newm = {p["code"] for p in mx["plan"]} - before_mix
            if newm:
                nm = {p["code"]: p.get("name") for p in mx["plan"]}
                mx["daily_log"].append({"date": date, "kind": "plan",
                                         "note": "[%s]%s 盘中复盘刷新: 新增 %s" % (
                                             POOL_LABEL[pool], MIX_CFG["label"],
                                             ",".join(nm.get(c, c) for c in sorted(newm)))})
                changed += 1
        save(state)
        print("盘中复盘重建 %s：双池计划已按最新信号刷新，变更账户 %d" % (date, changed))
        return

    if args.intraday:
        now = time.localtime()
        date = time.strftime("%Y-%m-%d", now)
        hms = time.strftime("%H:%M:%S", now)
        hhmm = now.tm_hour * 100 + now.tm_min
        in_session = now.tm_wday < 5 and ((930 <= hhmm <= 1130) or (1300 <= hhmm <= 1500))
        if not in_session and not args.force:
            print("非交易时段(%s %s)，跳过盘中巡检（需强制执行加 --force）" % (date, hms))
            return
        n, notes = intraday_scan(state, date, hms)
        save(state)
        print("盘中巡检 %s %s：成交%d" % (date, hms, n))
        for x in notes[:15]:
            print("  -", x)
        return

    if args.replay:
        # 简化：对每账户用日K低点回放（仅目标池；默认全池）
        from src import data_provider as dp
        pool = args.pool or "all"
        tot = 0
        for k, a, cfg in all_books(state, pool):
            for pl in a.get("plan", []):
                if pl.get("status", "wait") != "wait" or pl.get("gate") == "block":
                    continue
                # 1) 优先用 1 分钟K精确定位触发时刻（真实时分秒 + 当时涨跌）
                m1 = dp.fetch_minute_kline(pl["code"], scale=1, datalen=480, use_cache=True)
                trig = None  # (time_str, price, chg)
                pc = None
                if m1 and m1.get("dates"):
                    prev_closes = [c for d, c in zip(m1["dates"], m1["closes"]) if not d.startswith(args.replay)]
                    pc = prev_closes[-1] if prev_closes else None
                    for d, c in zip(m1["dates"], m1["closes"]):
                        if d.startswith(args.replay) and c <= pl["buy_below"]:
                            trig = (d[11:16], c, (c / pc - 1) * 100 if pc else None)
                            break
                px = None; low = None
                if trig:
                    px = trig[1]; low = trig[1]
                else:
                    # 2) 无分钟数据回退日K low
                    kd = dp.fetch_daily_kline(pl["code"], count=30)
                    if not kd or args.replay not in kd["dates"]:
                        continue
                    i = kd["dates"].index(args.replay)
                    low = kd["lows"][i]
                    if low > pl["buy_below"]:
                        continue
                    px = pl["buy_below"]
                budget = min(pl["budget"], a["cash"] * 0.98)
                shares = int(budget / px / 100) * 100
                if shares <= 0:
                    continue
                cost = shares * px
                a["cash"] -= cost + cost * 0.0003
                time_str = trig[0] + ":00" if trig else "盘中(回放近似)"
                a["positions"].append({
                    "code": pl["code"], "name": pl["name"], "shares": shares,
                    "cost": round(px, 3), "buy_date": args.replay, "buy_time": time_str,
                    "stop_atr": pl.get("stop_atr"), "tp": pl.get("tp"),
                    "trail": pl.get("trail"), "be_at": pl.get("be_at"),
                    "be_on": False, "peak": px, "score": pl.get("score"),
                    "signal": pl.get("signal"),
                })
                a["trades"].append({
                    "action": "buy", "date": args.replay, "time": time_str,
                    "code": pl["code"], "name": pl["name"], "price": round(px, 3),
                    "shares": shares,
                    "chg_at_fill": round(trig[2], 2) if trig and trig[2] is not None else None,
                    "reason": "回放触发(1分K首触@%s 价%.2f)：%s" % (trig[0], px, pl["reason"]) if trig
                              else "回放触发(日K低%.2f≤%.2f)：%s" % (low, pl["buy_below"], pl["reason"]),
                    "strategy": k, "pool": pool,
                })
                pl["status"] = "filled"
                tot += 1
                print("  [%s] 回放买入 %s @%.2f %s" % (cfg["label"], pl["name"], px, time_str))
        save(state)
        print("回放完成 %s [%s]：%d 笔" % (args.replay, POOL_LABEL[pool], tot))
        return

    if args.review or args.finalize:
        now = time.localtime()
        date = args.date or time.strftime("%Y-%m-%d", now)
        regims = []
        for pool in target_pools:
            regims.append((pool, finalize_pool(state, pool, date)))
            do_review(state, pool, date)
            expiry_plan(state, pool, date)
            for k, a, cfg in all_books(state, pool):
                ec = a["equity_curve"]
                print("  [%s]%s 净值%.0f（%+.2f%%）持仓%d" % (
                    POOL_LABEL[pool], cfg["label"],
                    ec[-1]["equity"] if ec else 0,
                    ec[-1].get("daily_return", 0) if ec else 0,
                    len(a["positions"])))
        save(state)
        print("收盘 %s 完成。" % date)
        for pool, reg in regims:
            print("  %s regime=%s" % (POOL_LABEL[pool], reg))
        return

    print("用法：--init / --plan / --intraday / --replay D / --review / --strategy-log")


if __name__ == "__main__":
    main()
