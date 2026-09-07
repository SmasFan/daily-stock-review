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
  python3 sim_live.py --intraday [--pool ...]   # 盘中巡检触发成交（cron 每5分钟，双池）
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
        mx.setdefault("equity_curve", [])
        mx.setdefault("daily_log", [])
        mx.setdefault("regime", [])
        mx.setdefault("meta", {"start_cash": CASH_START})
    return st


def normalize_account(key, a):
    for f in ("key", "label", "start_cash", "cash", "positions", "plan",
              "trades", "equity_curve", "daily_log", "review_log"):
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
                       "daily_log", "review_log"):
                a[f] = []
    return a


def save(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def new_account(key, cfg):
    return {
        "key": key, "label": cfg["label"], "start_cash": CASH_START,
        "cash": CASH_START, "positions": [], "plan": [], "trades": [],
        "equity_curve": [], "daily_log": [], "review_log": [],
    }


def pool_books():
    """单个池的三账户账本 + 合成账户骨架。"""
    return {
        "accounts": {k: new_account(k, ACCOUNTS[k]) for k in REAL_ACCOUNTS},
        "mix": {"equity_curve": [], "daily_log": [], "regime": [],
                "meta": {"start_cash": CASH_START}},
    }


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


def make_plan(state, review, asof, pool, skip_llm=False):
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
            if _is_etf(it.get("code")) or it.get("code") in held:
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
        cands.sort(key=lambda x: -x.get("score", 0))
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
        acct["daily_log"].append({"date": asof, "kind": "plan",
                                  "note": "[%s]%s：%d 单待盘中触发%s" % (
                                      POOL_LABEL[pool], cfg["label"], len(plan),
                                      ("（大盘闸门挡）" if gate == "block" else ""))})
    return {k: len(acc_b[k]["plan"]) for k in REAL_ACCOUNTS}


# ---------------- 盘中巡检（双池并行） ----------------
def intraday_scan(state, date, hms):
    from src import data_provider as dp
    all_codes = set()
    for pool in POOLS:
        for key in REAL_ACCOUNTS:
            a = accts(state, pool)[key]
            all_codes |= {p["code"] for p in a["plan"] if p.get("status", "wait") == "wait"}
            all_codes |= {p["code"] for p in a["positions"]}
    if not all_codes:
        return 0, ["无计划/持仓"]
    quotes = {}
    try:
        quotes = dp.fetch_quotes(sorted(all_codes))
    except Exception as e:
        return 0, ["快照失败 %s" % e]
    total_fill = 0
    all_notes = []
    for pool in POOLS:
        for key in REAL_ACCOUNTS:
            cfg = ACCOUNTS[key]
            a = accts(state, pool)[key]
            n, notes = _scan_account(pool, a, cfg, quotes, date, hms)
            total_fill += n
            all_notes.extend(notes)
    return total_fill, all_notes


def _scan_account(pool, acct, cfg, quotes, date, hms):
    filled = 0
    notes = []
    # 卖出
    sell_codes = []
    for pos in acct["positions"]:
        q = quotes.get(pos["code"])
        if not q:
            continue
        px = q.get("price")
        if not px:
            continue
        prev = pos.get("prev_close") or pos["cost"]
        chg = (px / prev - 1) * 100 if prev else 0
        # 现价印记：供页面展示个股实时盈亏/当日涨跌
        pos["last"] = px
        pos["last_chg"] = round(chg, 2)
        pos["last_ts"] = hms
        if px > pos.get("peak", pos["cost"]):
            pos["peak"] = px
        gain = (px / pos["cost"] - 1) * 100
        reason = None
        if pos.get("tp") and px >= pos["tp"]:
            reason = "止盈：现价%.2f≥目标%.2f（+%.1f%%）" % (px, pos["tp"], gain)
        elif pos.get("be_at") and gain >= pos["be_at"] * 100 and not pos.get("be_on"):
            pos["be_on"] = True
            pos["stop_atr"] = pos["cost"] * 1.002  # 保本
            notes.append("（[%s]%s %s 浮盈+%.0f%% → 保本锁定）" % (
                POOL_LABEL[pool], acct["label"], pos["name"], gain))
        elif pos.get("stop_atr") and px <= pos["stop_atr"]:
            reason = "破ATR/保本止损 %.2f" % pos["stop_atr"]
        elif pos.get("peak") and pos.get("trail") and pos["peak"] > pos["cost"] * 1.08 \
                and px / pos["peak"] - 1 <= -pos["trail"]:
            reason = "移动止盈（峰值%+.1f%%回落%.0f%%）" % ((pos["peak"] / pos["cost"] - 1) * 100,
                                                      pos["trail"] * 100)
        if reason:
            shares = pos["shares"]
            proceeds = shares * px
            fee = proceeds * (0.0003 + 0.0005)
            pnl = proceeds - shares * pos["cost"]
            acct["cash"] += proceeds - fee
            acct["trades"].append({
                "action": "sell", "date": date, "time": hms, "code": pos["code"],
                "name": pos["name"], "price": round(px, 3), "shares": shares,
                "chg_at_fill": round(chg, 2), "pnl": round(pnl, 2),
                "pnl_pct": round((px / pos["cost"] - 1) * 100, 2),
                "reason": reason, "strategy": acct["key"], "pool": pool,
            })
            sell_codes.append(pos["code"])
            filled += 1
            notes.append("[%s] 卖出 %s @%.2f（%+.2f%%）%s" % (acct["label"], pos["name"], px, chg, reason))
    acct["positions"] = [p for p in acct["positions"] if p["code"] not in sell_codes]
    # 买入
    for pl in acct.get("plan", []):
        if pl.get("status", "wait") != "wait":
            continue
        if pl.get("gate") == "block":
            pl["status"] = "skip_gate"
            continue
        if len(acct["positions"]) >= cfg["max_pos"]:
            pl["status"] = "skip_full"
            continue
        q = quotes.get(pl["code"])
        if not q:
            continue
        px = q.get("price")
        if not px:
            continue
        if px > (pl.get("close") or 0) * 1.03:
            continue  # 高开冲高不追
        if px <= pl["buy_below"]:
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
                "be_on": False, "peak": px, "prev_close": q.get("prevClose"),
                "score": pl.get("score"), "signal": pl.get("signal"),
            })
            acct["trades"].append({
                "action": "buy", "date": date, "time": hms, "code": pl["code"],
                "name": pl["name"], "price": round(px, 3), "shares": shares,
                "chg_at_fill": round((px / (q.get("prevClose") or pl["close"]) - 1) * 100, 2),
                "reason": pl.get("reason", ""), "strategy": acct["key"], "pool": pool,
            })
            pl["status"] = "filled"
            filled += 1
            notes.append("[%s] 买入 %s @%.2f 回踩触发" % (acct["label"], pl["name"], px))
    return filled, notes


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
        eq = equity_of(a)
        a["equity_curve"] = [x for x in a["equity_curve"] if x["date"] != date]
        prev = a["equity_curve"][-1]["equity"] if a["equity_curve"] else CASH_START
        a["equity_curve"].append({"date": date, "equity": round(eq, 2),
                                  "cash": round(a["cash"], 2),
                                  "daily_return": round((eq / prev - 1) * 100, 3),
                                  "pos_count": len(a["positions"])})
    # mix：以上证斜率定档，三账户日收益加权复利（首日直接取三账户加权净值）
    slope = sh_slope(date)
    regs = [0.25, 0.5, 0.25]
    if slope is not None and slope > 2.5:
        regs = [0.7, 0.2, 0.1]
    elif slope is not None and slope < -2.5:
        regs = [0.1, 0.2, 0.7]
    curves = [acc_b[k]["equity_curve"] for k in REAL_ACCOUNTS]
    mc = pool_b["mix"]["equity_curve"]
    if all(c for c in curves):
        # 首日：无 c[-2] → 直接取各账户当前净值加权
        if len(curves[0]) == 1:
            eq_mix = sum(curves[i][-1]["equity"] * regs[i] for i in range(3))
            r_day = 0.0
        else:
            rets = []
            for c in curves:
                if len(c) >= 2 and c[-2].get("equity"):
                    rets.append(c[-1]["equity"] / c[-2]["equity"] - 1)
                else:
                    rets.append(0.0)
            prev_mix = mc[-1]["equity"] if mc else CASH_START
            eq_mix = prev_mix * (1 + sum(r * w for r, w in zip(rets, regs)))
            r_day = sum(r * w for r, w in zip(rets, regs)) * 100
        mc = [x for x in mc if x["date"] != date]  # 幂等
        pool_b["mix"]["equity_curve"] = mc
        mc.append({"date": date, "equity": round(eq_mix, 2), "daily_return": round(r_day, 3),
                   "regime": "攻" if regs[0] >= 0.7 else ("守" if regs[2] >= 0.7 else "衡")})
    return "攻" if regs[0] >= 0.7 else ("守" if regs[2] >= 0.7 else "衡")


def do_review(state, pool, date):
    for key in REAL_ACCOUNTS:
        a = accts(state, pool)[key]
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


def expiry_plan(state, pool):
    """收盘后：当日未触发 wait 单标记 expired（次日 --plan 重建）。"""
    for k in REAL_ACCOUNTS:
        for p in accts(state, pool)[k]["plan"]:
            if p.get("status", "wait") == "wait":
                p["status"] = "expired"


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--intraday", action="store_true")
    ap.add_argument("--replay", default=None)
    ap.add_argument("--review", action="store_true")
    ap.add_argument("--finalize", action="store_true")
    ap.add_argument("--strategy-log", default=None)
    ap.add_argument("--pool", default=None, help="six=6股精选 / all=全池（默认双池并行）")
    ap.add_argument("--plan-date", default=None, help="历史日收盘信号重建计划(如2026-09-03，仅six)")
    ap.add_argument("--no-llm", action="store_true", help="跳过LLM个股评审(快速重建用)")
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
        print("策略 v%s → v%s：%s" % (v, nv, args.strategy_log))
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
        save(state)
        return

    if args.intraday:
        now = time.localtime()
        date = time.strftime("%Y-%m-%d", now)
        hms = time.strftime("%H:%M:%S", now)
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
        for k in REAL_ACCOUNTS:
            a = accts(state, pool)[k]
            cfg = ACCOUNTS[k]
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
            expiry_plan(state, pool)
            for k in REAL_ACCOUNTS:
                a = accts(state, pool)[k]
                ec = a["equity_curve"]
                print("  [%s]%s 净值%.0f（%+.2f%%）持仓%d" % (
                    POOL_LABEL[pool], ACCOUNTS[k]["label"],
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
