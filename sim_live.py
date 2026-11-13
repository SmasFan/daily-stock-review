#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实时模拟炒股 · 全池盘中触发引擎（v5，2026-11-10 起）

只保留全池(all)：3 个真实独立账户（激进/稳健/严守纪律，各 5 万）+ 合并·共识账户。
原「6股精选(six)」池已停用：不再建账、不再巡检、不再展示，旧数据里的 six 账本会被
load_state() 自动清掉。合并净值 = 池内三账户加权合成。

数据模型：
  state.pools = { all: {accounts:{...3账户...}, mix:{...}} }
  state.meta.pools = ["all"]；旧 v1~v4 数据自动迁移 → pools.all。

命令（默认作用于全池，--pool 仅接受 all）：
  python3 sim_live.py --init                 # 初始化（全池 × 3 账户）
  python3 sim_live.py --plan                 # 重建计划（收盘 auto_run 调用）
  python3 sim_live.py --intraday-plan         # 盘中整点复盘重建(刷新买点/补新信号, 需全量成分版review)
  python3 sim_live.py --intraday             # 盘中巡检触发成交（cron 每2分钟独立锁）
  python3 sim_live.py --review [--date D]       # 收盘复盘 + 自学习
  python3 sim_live.py --strategy-log "msg"   # 策略版本变更

LLM 层（--plan 时自动）:
  - 宏观: data/macro_llm_data.json（macro_llm.py 生成）空头/防御 → 全局闸门
  - 个股评审: 候选股画像 + 宏观 → LLM 批量评审，回避股不入计划
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
# 宏观利多但市场宽度极弱 → 背离降档（政策面与盘面背离，追多风险高）：
#   不直接 block（利多是真实的，超卖可能是机会），而是抬门槛 + 缩仓位
REDUCE_BREADTH = float(os.environ.get("SIM_REDUCE_BREADTH", "25"))      # 涨占比 ≤ 此值算极弱
REDUCE_MIN_SCORE_BONUS = float(os.environ.get("SIM_REDUCE_SCORE", "6"))  # 买入门槛上调
REDUCE_BUDGET_FACTOR = float(os.environ.get("SIM_REDUCE_BUDGET", "0.6"))  # 仓位打折
MACRO_STALE_HOURS = float(os.environ.get("SIM_MACRO_STALE_H", "20"))     # 宏观数据超过则视为过期
# 闸门分档（v3.12）：把「开/关」二元闸门改成「仓位旋钮」。
#   背景：9/11~9/18 连续 6 日 block（原因逐日退化为单靠一条 LLM「防御」），
#   期间半导体 +5%~14%、模拟盘只成交 1 笔、93.8% 现金闲置 —— 用「开不开仓」控风险
#   等于只会守财。改为：软因素（LLM 防御 / 广度过热）只降档到 caution（半仓），
#   只有「上证空头 + 涨占比极低」这类硬崩盘条件才 block（空仓）。
GATE_POS = {"attack": 1.0, "open": 1.0, "caution": 0.5, "reduce": 0.6, "block": 0.0}
CAUTION_POS = float(os.environ.get("SIM_CAUTION_POS", "0.5"))           # 软因素降档后的仓位系数
CAUTION_SCORE_BONUS = float(os.environ.get("SIM_CAUTION_SCORE", "4"))   # 降档时买入门槛上调分
OVERHEAT_BREADTH = float(os.environ.get("SIM_OVERHEAT_BREADTH", "65"))  # 普涨过热阈值（只降档不空仓）
HARD_FLAT_BREADTH = float(os.environ.get("SIM_HARD_FLAT_BREADTH", "20"))  # 硬空仓：涨占比 ≤ 此值
ATTACK_MAIN_NET = float(os.environ.get("SIM_ATTACK_MAIN_NET", "1.0e10"))  # attack 需主力净流入 ≥ 100亿
ATTACK_BREADTH_LO = float(os.environ.get("SIM_ATTACK_BREADTH_LO", "45"))  # attack 广度下限
ATTACK_BREADTH_HI = float(os.environ.get("SIM_ATTACK_BREADTH_HI", "75"))  # attack 广度上限
# 突破买点（v3.12）：只做回踩买点 → 单边上涨永远等不到单（plan 全是 buy_below）。
#   对「强势多头 + 站上 MA20」的标的额外给一个 buy_above，达到即可追入（限幅防追高）。
#   过热（caution）阶段也允许突破 —— 普涨日往往没有回踩；但追高幅度收紧到 1.5%。
BREAKOUT_GATES = ("open", "attack", "caution")                               # 这三档允许突破买
BREAKOUT_PREM = float(os.environ.get("SIM_BREAKOUT_PREM", "0.005"))         # 触发价 = 现价 ×(1+0.5%)
BREAKOUT_BAND = float(os.environ.get("SIM_BREAKOUT_BAND", "0.03"))          # open/attack：最多追到触发价 ×1.03
BREAKOUT_BAND_CAUTION = float(os.environ.get("SIM_BREAKOUT_BAND_CAUTION", "0.015"))  # caution：×1.015
BREAKOUT_MAX_CHG = float(os.environ.get("SIM_BREAKOUT_MAX_CHG", "0.08"))    # 相对昨收最大 +8%
# ===== 市场环境自适应（v4.0，2026-11-14）=====
# 背景：2026-10/11 创业板 20 日 -12.5%、60 日 -32.4%、上证跌破 MA20，
#   但闸门只看「上证 signal/score + 广度」，score 61「观望」就只降半仓；
#   三套策略的门槛(66/68/76)、回踩(0.5%/2%/3%)、止盈(20%/15%/10%) 完全没变，
#   结果 9/4 起 激进 -6.9%、稳健 -12.3%（≈创业板同期跌幅）、严守纪律 -0.4%。
# 做法：每天用「指数动量 + 波动 + 广度」判定环境，环境参数覆盖到
#   仓位(pos)/门槛(score)/回踩(bias)/止盈(tp)/止损(sl)/是否允许开新仓(new)。
REGIME_LABELS = {"bull": "趋势多头", "chop": "震荡结构", "bear": "趋势下跌",
                 "panic": "高波动急跌", "overheat": "普涨过热"}
REGIME_PARAMS = {
    "bull":     {"pos": 1.00, "score": 0,  "bias": 0.000, "tp": 1.00, "sl": 1.00, "new": True},
    "chop":     {"pos": 0.70, "score": 4,  "bias": 0.010, "tp": 0.70, "sl": 0.85, "new": True},
    "overheat": {"pos": 0.50, "score": 6,  "bias": 0.005, "tp": 0.80, "sl": 0.90, "new": True},
    "bear":     {"pos": 0.30, "score": 10, "bias": 0.015, "tp": 0.60, "sl": 0.70, "new": True},
    "panic":    {"pos": 0.00, "score": 99, "bias": 0.020, "tp": 0.50, "sl": 0.60, "new": False},
}
REGIME_BULL_MOM = float(os.environ.get("SIM_REGIME_BULL_MOM", "2.0"))     # 上证 20 日 ≥ 此值
REGIME_BULL_ATR = float(os.environ.get("SIM_REGIME_BULL_ATR", "3.0"))     # 创业板 ATR20% 上限
REGIME_BEAR_MOM = float(os.environ.get("SIM_REGIME_BEAR_MOM", "-2.5"))
REGIME_PANIC_ATR = float(os.environ.get("SIM_REGIME_PANIC_ATR", "3.5"))
REGIME_PANIC_MOM = float(os.environ.get("SIM_REGIME_PANIC_MOM", "-2.0"))
REGIME_CHOP_MOM = float(os.environ.get("SIM_REGIME_CHOP_MOM", "-6.0"))   # 创业板 20 日
REGIME_CONFIRM = int(os.environ.get("SIM_REGIME_CONFIRM", "2"))          # 连续确认天数
# 组合层面自适应（v4.1）：主开关 = 自己回撤 + 波动目标化；大盘 regime 只留极端保护。
# 120 天回测（2026-07-17~11-13）结论：
#   adaptive（大盘方向驱动）-3.66% —— 逆势选强势股被 chop 档 0.7 仓位 + 门槛+4 削掉 alpha；
#   adaptive2（组合回撤 + 波动目标化）+2.36%/MDD 3.0% —— 熔断保留但只在自己亏损时触发。
# 所以 sim_live 默认：大盘 regime 仅在 panic（ATR≥3.5 且上证20日≤-2%）时 block，
#   bear 只降档到 caution；账户层用 account_regime()（回撤 6%/12% + 连亏5）+ 波动目标化。
ACCT_DD_STOP = float(os.environ.get("SIM_ACCT_DD_STOP", "12.0"))     # 近 20 日回撤 ≥ 此值 → 暂停开仓
ACCT_DD_HALF = float(os.environ.get("SIM_ACCT_DD_HALF", "6.0"))      # 回撤 ≥ 此值 → 仓位 ×0.6
ACCT_LOSS_STREAK = int(os.environ.get("SIM_ACCT_LOSS_STREAK", "5"))   # 连亏笔数 → 暂停开仓
VOL_TARGET = float(os.environ.get("SIM_VOL_TARGET", "2.0"))           # 波动目标：创业板 ATR20%
VOL_POS_MIN = float(os.environ.get("SIM_VOL_POS_MIN", "0.4"))         # 波动目标仓位下限
ACCT_REGIME_ON = os.environ.get("SIM_ACCT_REGIME", "1") != "0"        # 组合自适应总开关
# 闸门错失成本监控（v3.12）：记录 block/caution 日的基准与广度，供「防守过度」复核
MISSED_BENCH = "sh000300"
# 外部市场因子（build_external.py 产出）：原油/黄金/白银/铜/美债收益率/美元/纳指/VIX/标普
# → 板块偏好分。选股时按 板块 或 个股名关键词 折算成加/减分，影响候选排序与门槛。
EXT_WEIGHT = float(os.environ.get("SIM_EXT_WEIGHT", "1.5"))    # 偏好分 → 选股分 的放大系数
EXT_CAP = float(os.environ.get("SIM_EXT_CAP", "8"))            # 单个股外部加减分上限
EXT_MAX_AGE_H = float(os.environ.get("SIM_EXT_STALE_H", "14"))  # 外部数据过期小时数

# 2026-11-10 起只保留全池；6 股精选池（six）已下线
POOLS = ("all",)
POOL_LABEL = {"all": "全池"}

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
    # 2026-11-12：红利策略作为第 4 个策略账户进入全池（5 万口径，派生自 sim_dividend.py 账本）
    "dividend": {
        "label": "红利", "icon": "coins", "badge": "#0f766e", "kind": "dividend",
        "budget_frac": 0.0, "max_pos": 10, "cash_reserve": 0.0,
        "buy_bias": 0.04, "stop_ma": True, "tp_pct": 0.15,
        "trail": None, "be_at": None, "min_score": 0,
        "desc": "红利低波回踩长持：核心 6 只 + 扩池 4 只（半份），现价 ≤ MA20×0.96 且 > MA60×0.85 建一档，最多 3 档；+15% 减档，-10% 或破 MA60-12% 清仓。",
        "rules": ["不追高：只在回踩 MA20 下方 4% 买", "不接飞刀：跌破 MA60-15% 不买",
                  "金字塔 3 档，涨 15% 减 1 档", "破 MA60-12% 或亏 10% 清仓"],
        "derived_from": "data/sim_dividend.json",
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

# 2026-11-10 起 6 股精选池（six）已下线；不再建账、不再巡检、不再展示。
# review_from_kline() 仍保留（scripts/backtest_sprint.py 依赖），但只用于工具脚本。


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
    # v1~v3 单池 → v4 迁移：旧 accounts/mix → pools.all
    # 2026-11-10 起 6 股精选池（six）已停用，统一只保留 all
    if st and "pools" not in st and "accounts" in st:
        st["pools"] = {
            "all": {"accounts": st.get("accounts") or {},
                    "mix": st.get("mix") or {"equity_curve": [], "daily_log": [],
                                             "regime": [], "meta": {"start_cash": CASH_START}}},
        }
        st.pop("accounts", None)
        st.pop("mix", None)
        st.setdefault("meta", {})
        st["meta"]["pools"] = list(POOLS)
        st["meta"].pop("pool_mode", None)
        st["meta"].pop("pool_switched_at", None)
    # 清理旧双池数据里残留的 six 账本（不再记账、不再展示）
    if st and isinstance(st.get("pools"), dict):
        st["pools"].pop("six", None)
    st = normalize_state(st)
    return st


def normalize_state(st):
    """补齐全池结构（新老字段兜底；6 股精选池已停用）。"""
    if not st:
        st = {}
    st.setdefault("meta", {})
    m = st["meta"]
    m.setdefault("created", now_ts())
    if m.get("strategy_version", "v0") < "v4.1":
        m["strategy_version"] = "v4.1"
    m.setdefault("accounts", list(REAL_ACCOUNTS))
    m["pools"] = list(POOLS)
    st.setdefault("version_history", [])
    # ds4.1 审阅：旧文件 pools 非 dict 时先重置，否则 setdefault(...).setdefault 会抛 AttributeError 丢账本
    if not isinstance(st.get("pools"), dict):
        st["pools"] = {}
    st["pools"].pop("six", None)
    for p in POOLS:
        b = st.setdefault("pools", {}).setdefault(p, {})
        acc = b.setdefault("accounts", {})
        for k in REAL_ACCOUNTS:
            if k not in acc or not isinstance(acc[k], dict):
                acc[k] = new_account(k, ACCOUNTS[k])
            else:
                acc[k] = normalize_account(k, acc[k])
        dv = dividend_account()
        if dv is not None:
            acc[DIVIDEND_KEY] = dv
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
    # v4.1：保存前给每个真账户附加当前环境与组合状态（前端可读，不影响交易逻辑）
    try:
        rg = (state.get("meta") or {}).get("regime_now") or {}
        for p in state.get("pools", {}).values():
            for key, acct in (p.get("accounts") or {}).items():
                if key not in REAL_ACCOUNTS:
                    continue
                ar = account_regime(acct)
                ar["vol_pos"] = vol_target_pos((rg.get("inputs") or {}).get("atr20_cyb"))
                acct["regime_info"] = {
                    "market": {"regime": rg.get("regime"), "label": rg.get("label"),
                               "params": rg.get("params"), "why": rg.get("why")},
                    "portfolio": ar,
                }
    except Exception:
        pass
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
        "meta": {"created": now_ts(), "strategy_version": "v4.1",
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


# ---------------- 红利策略账户（2026-11-12：全池第 4 个策略，5 万本金口径） ----------------
# 红利池的真实交易在 sim_dividend.py 里跑（10 万本金、10 只标的、MA20 回踩 + MA60 清仓）。
# 全池页要与其他三个策略同台比较，因此按 5 万本金等比例派生一份只读账本：
#   · 收益率与红利池完全一致（同一笔交易、同一时间点，只按 scale 缩放金额）
#   · 该账户不进入 all_books() 循环，也不会被 sim_live 的下单逻辑写入
DIVIDEND_KEY = "dividend"
DIVIDEND_START = 50000.0
DIVIDEND_FILE = os.path.join(DATA_DIR, "sim_dividend.json")
EXTRA_DISPLAY = (DIVIDEND_KEY,)


def dividend_account():
    """把 data/sim_dividend.json 按 5 万本金缩放成 sim_live 账户结构；读不到返回 None。"""
    if not os.path.exists(DIVIDEND_FILE):
        return None
    try:
        with open(DIVIDEND_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    meta = d.get("meta") or {}
    start = float(meta.get("start_cash") or 100000.0)
    sc = DIVIDEND_START / start if start else 1.0

    def _n(v, dflt=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return dflt

    pos = []
    for p in d.get("positions") or []:
        last = _n(p.get("last"), _n(p.get("avg")))
        pos.append({
            "code": p.get("code"), "name": p.get("name"),
            "shares": int(round(_n(p.get("shares")) * sc)),
            "cost": round(_n(p.get("avg")), 4),
            "last": last, "last_close": last,
            "last_chg": p.get("last_chg"),
            "lots": p.get("lots"),
            "buy_date": p.get("open_date") or p.get("buy_date") or "",
            "kind": "dividend", "strategy": "红利",
        })
    plan = []
    for r in d.get("plan") or []:
        plan.append({
            "code": r.get("code"), "name": r.get("name"),
            "status": r.get("status"), "signal": "红利回踩" if r.get("status") == "wait" else "已满档",
            "close": r.get("last"), "buy_below": r.get("dip_price"),
            "dist_pct": r.get("dist_pct"), "lots": r.get("lots"),
            "stop_ma": r.get("exit_price"), "tp": None, "stop_atr": None,
            "gate": "open", "kind": "dividend",
        })
    curve = []
    for c in d.get("equity_curve") or []:
        curve.append({"date": c.get("date"),
                      "equity": round(_n(c.get("equity")) * sc, 2),
                      "cash": round(_n(c.get("cash")) * sc, 2),
                      "pos_count": c.get("pos_count")})
    trades = []
    for t in d.get("trades") or []:
        row = dict(t)
        for k in ("shares", "amount", "value"):
            if row.get(k) is not None:
                row[k] = round(_n(row.get(k)) * sc, 2)
        row["kind"] = "dividend"
        trades.append(row)
    eq = _n(d.get("cash")) * sc + sum(p["shares"] * p["last"] for p in pos)
    return {
        "key": DIVIDEND_KEY, "label": ACCOUNTS["dividend"]["label"],
        "start_cash": DIVIDEND_START, "cash": round(_n(d.get("cash")) * sc, 2),
        "positions": pos, "plan": plan, "trades": trades, "equity_curve": curve,
        "daily_log": d.get("daily_log") or [], "review_log": [], "miss_log": [], "llm_log": [],
        "equity": round(eq, 2),
        "meta": {"derived": True, "source": "data/sim_dividend.json", "scale": sc,
                 "source_start": start, "note": "红利池账本按 5 万口径等比例派生，交易在 sim_dividend.py 执行"},
    }


# ---------------- LLM 个股评审（计划阶段，一次性批量） ----------------
# 通道（2026-11-08 起）：agent 队列 → llm_router 决策路由（短 JSON 纯本地）
#   → ollama 直连兜底 → commandcode 付费云端（默认关）。见 config/llm_routing.json
OLLAMA = "http://localhost:11434"
LLM_MODEL = os.environ.get("DSR_LLM_MODEL", "qwen3-coder:30b")  # 本机实有模型；原 qwen3-vl:32b 不存在
LLM_MAXTOK = int(os.environ.get("DSR_LLM_MAXTOK", "1024"))
CC_URL = "https://api.commandcode.ai/provider/v1/chat/completions"
CC_UA = "OpenAI/Python 1.99.0"
CC_CLOUD_MODEL = "deepseek/deepseek-v4-flash"


def _cc_key():
    import glob as _g
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    envs = [os.path.join(here, "binance-llm-bot", ".env")]
    envs += _g.glob("/mnt/c/Users/z7280/daily-stock-review/binance-llm-bot/.env")
    for envf in envs:
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
        "options": {"num_predict": LLM_MAXTOK, "temperature": 0.2, "num_ctx": 16384},
    }).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    content = (d.get("message") or {}).get("content") or ""
    if not content.strip():
        raise RuntimeError("ollama 空返回")
    return content


def _agent_ask(kind, system, user, expect_json=True, wait=None):
    """优先问「我」（llm_agent 文件队列，免费）。没等到答案返回 None。"""
    try:
        import llm_agent
        a = llm_agent.ask(kind, system, user, expect_json=expect_json, wait=wait)
        if a is None:
            return None
        return a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)
    except Exception as e:
        print("  [agent-llm] 不可用(%s)，走降级" % str(e)[:80])
        return None


def _cloud_allowed():
    return os.environ.get("AGENT_LLM_ALLOW_CLOUD", "0") in ("1", "true", "yes", "on")


def _llm_chat(system, user, timeout=480):
    """Agent 队列 → 决策路由(本地) → ollama 直连 → [付费云端，默认停用]。"""
    c = _agent_ask("candidates", system, user, expect_json=True,
                   wait=float(os.environ.get("AGENT_LLM_WAIT_CANDIDATES", "0")))
    if c and c.strip():
        return c
    errs = []
    try:
        import llm_router
        c, _bk = llm_router.chat("candidates", system, user, expect_json=True)
        if c and c.strip():
            return c
        errs.append("router 空返回")
    except Exception as e:
        errs.append("router: %s" % str(e)[:100])
    try:
        return _llm_chat_ollama(system, user, timeout=timeout)
    except Exception as e:
        errs.append("ollama: %s" % str(e)[:80])
    if _cloud_allowed():
        import time as _t
        for attempt in range(2):
            try:
                c = _llm_chat_cc(system, user, timeout=min(timeout, 120))
                if c.strip():
                    return c
            except Exception as e:
                errs.append("cc: %s" % e)
            _t.sleep(1.5 * (attempt + 1))
    raise RuntimeError("；".join(errs))


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
GATE_TIMEOUT = float(os.environ.get("SIM_LLM_GATE_TIMEOUT", "30"))
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
    """盘中调用：先问我（等 AGENT_LLM_WAIT_FAST 秒，默认不等待只登记），
    再走 ollama 本地；不重试，失败即熔断放行。

    盘中闸门 2 分钟一轮，不能为等我而卡住 —— 默认 AGENT_LLM_WAIT_FAST=0：
    只把问题登记进队列，我不在线就交给 ollama / 规则放行。
    """
    errs = []
    wait = float(os.environ.get("AGENT_LLM_WAIT_FAST", "0"))
    c = _agent_ask("gate", system, user, expect_json=True, wait=wait)
    if c and c.strip():
        return c
    # 按 2026-11-08 对比结论：短 JSON 复核走纯本地模型（group/local-code）
    try:
        import llm_router
        c, _bk = llm_router.chat("gate", system, user, expect_json=True,
                                 timeout=GATE_TIMEOUT * 2)
        if c and c.strip():
            return c
        errs.append("router 空返回")
    except Exception as e:
        errs.append("router: %s" % str(e)[:100])
    try:
        c = _llm_chat_ollama(system, user, timeout=GATE_TIMEOUT * 2)
        if c.strip():
            return c
        errs.append("ollama 空返回")
    except Exception as e:
        errs.append("ollama: %s" % str(e)[:60])
    if _cloud_allowed():
        try:
            c = _llm_chat_cc(system, user, timeout=GATE_TIMEOUT)
            if c.strip():
                return c
            errs.append("cc 空返回")
        except Exception as e:
            errs.append("cc: %s" % str(e)[:60])
    raise RuntimeError("；".join(errs))


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
        # 失败计数（供统计 LLM 复核覆盖率，避免"静默未复核"）
        st = cache.setdefault("stat", {"llm": 0, "cache": 0, "fallback": 0, "off": 0})
        st["fallback"] = st.get("fallback", 0) + len(fresh)
        return out
    meta.pop("llm_gate_cb", None)
    st = cache.setdefault("stat", {"llm": 0, "cache": 0, "fallback": 0, "off": 0})
    st["llm"] = st.get("llm", 0) + len(fresh)
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
def _macro_age_hours(macro):
    """宏观 LLM 数据距今小时数；无数据返回 None。"""
    gen = ((macro or {}).get("generatedAt") or "")[:19]
    if not gen:
        return None
    try:
        return (time.time() - time.mktime(time.strptime(gen, "%Y-%m-%d %H:%M:%S"))) / 3600.0
    except Exception:
        return None


def gate_pos_of(gate):
    """闸门 → 仓位系数（v3.12）。未知档位按 0.5 保守处理。"""
    if gate == "caution":
        return CAUTION_POS
    return GATE_POS.get(gate, 0.5)


def breakout_band_of(gate):
    """闸门 → 突破追高幅度上限（v3.12）。caution 阶段收紧。"""
    return BREAKOUT_BAND_CAUTION if gate == "caution" else BREAKOUT_BAND


def index_metrics(code="sh000001", count=70):
    """指数 20 日动量 / 距 MA20 / ATR20%（均百分比）。取不到返回 {}。"""
    try:
        from src import data_provider as dp
        k = dp.fetch_daily_kline(code, count=count) or {}
        c = k.get("closes") or []
        h = k.get("highs") or []
        l = k.get("lows") or []
        if len(c) < 25 or len(h) < 25 or len(l) < 25:
            return {}
        ma20 = sum(c[-20:]) / 20.0
        trs = []
        for i in range(len(c) - 20, len(c)):
            trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
        atr = (sum(trs) / len(trs)) / c[-1] * 100.0 if c[-1] else 0.0
        return {"price": c[-1],
                "mom20": (c[-1] / c[-21] - 1.0) * 100.0,
                "dev20": (c[-1] / ma20 - 1.0) * 100.0,
                "atr20": atr}
    except Exception as e:
        print("  [regime] 指数 %s 取数失败: %s" % (code, e))
        return {}


def account_regime(acct):
    """组合层面环境（v4.1）：看自己近 20 日回撤与连亏，不看大盘方向。

    回测结论（2026-07-17~11-13）：
      健康期（chop）稳健基准 +6.9%，组合自适应只 +3.5%（因为波动目标化降了仓）；
      但在自身开始回撤时熔断及时，整体 MDD 3.0% 优于固定参数的 3.5%。
    规则：回撤 ≥ ACCT_DD_HALF → 仓位 ×0.6 + 门槛 +4；
         回撤 ≥ ACCT_DD_STOP 或 连亏 ≥ ACCT_LOSS_STREAK 笔 → 暂停开新仓。
    """
    curve = acct.get("equity_curve") or []
    if len(curve) < 21:
        return {"regime": "warmup", "pos": 1.0, "score": 0, "why": "样本不足"}
    eq = [float(c.get("equity") or 0) for c in curve[-21:]]
    peak = max(eq) or 0.0
    dd = (peak - eq[-1]) / peak * 100.0 if peak else 0.0
    trades = [t for t in (acct.get("trades") or []) if t.get("action") == "sell"]
    streak = 0
    for t in reversed(trades):
        if (t.get("pnl") or 0) < 0:
            streak += 1
        else:
            break
    if dd >= ACCT_DD_STOP or streak >= ACCT_LOSS_STREAK:
        return {"regime": "broken", "pos": 0.0, "score": 99,
                "why": "近20日回撤%.1f%%/连亏%d笔→暂停开仓" % (dd, streak)}
    if dd >= ACCT_DD_HALF:
        return {"regime": "drawdown", "pos": 0.6, "score": 4,
                "why": "近20日回撤%.1f%%→降仓" % dd}
    return {"regime": "healthy", "pos": 1.0, "score": 0, "why": "组合健康"}


def vol_target_pos(atr20):
    """波动目标化仓位：ATR20% 高于目标就缩仓，低于目标不放大（最多 1.0）。"""
    try:
        a = float(atr20 or 0)
    except Exception:
        return 1.0
    if a <= 0:
        return 1.0
    return max(VOL_POS_MIN, min(1.0, VOL_TARGET / a))


def market_regime(review=None, state=None, date=None, sh=None, cyb=None):
    """判定市场环境并给出参数覆盖（v4.0）。

    输入：上证/创业板 20 日动量、距 MA20、创业板 ATR20%、涨占比
    输出 regime ∈ {bull, chop, overheat, bear, panic} + params（覆盖到仓位/门槛/回踩/止盈/止损）
    平滑：新环境需连续 REGIME_CONFIRM 天确认，避免单日抖动；未确认时沿用上一环境。
    """
    review = review or {}
    sh = sh if sh is not None else index_metrics("sh000001")
    cyb = cyb if cyb is not None else index_metrics("sz399006")
    temp = review.get("temperature") or {}
    breadth = temp.get("breadth")
    if breadth is None and temp.get("market_total"):
        breadth = (temp.get("market_up") or 0) * 100.0 / temp["market_total"]
    breadth = float(breadth or 0)
    mom20 = sh.get("mom20")
    dev20 = sh.get("dev20")
    cyb_mom = cyb.get("mom20")
    atr = cyb.get("atr20")
    why = []
    if mom20 is None or dev20 is None:
        regime = "chop"
        why.append("指数数据缺失，按震荡处理")
    elif atr is not None and atr >= REGIME_PANIC_ATR and mom20 <= REGIME_PANIC_MOM:
        regime = "panic"
        why.append("创业板 ATR20 %.2f%%≥%.1f%% 且上证20日 %+.1f%%≤%.1f%%" % (
            atr, REGIME_PANIC_ATR, mom20, REGIME_PANIC_MOM))
    elif mom20 <= REGIME_BEAR_MOM or (cyb_mom is not None and cyb_mom <= REGIME_CHOP_MOM and dev20 < -1.0):
        regime = "bear"
        why.append("上证20日 %+.1f%% / 创业板20日 %s / 距MA20 %+.1f%%" % (
            mom20, ("%+.1f%%" % cyb_mom) if cyb_mom is not None else "?", dev20))
    elif breadth >= OVERHEAT_BREADTH and mom20 >= 4.0:
        regime = "overheat"
        why.append("涨占比 %.0f%%≥%.0f%% 且上证20日 %+.1f%%（普涨过热）" % (
            breadth, OVERHEAT_BREADTH, mom20))
    elif mom20 >= REGIME_BULL_MOM and dev20 > 0.5 and (atr or 0) < REGIME_BULL_ATR:
        regime = "bull"
        why.append("上证20日 %+.1f%%≥%.1f%% 站上MA20 %+.1f%% 创业板ATR %.1f%%<%.1f%%" % (
            mom20, REGIME_BULL_MOM, dev20, atr or 0, REGIME_BULL_ATR))
    else:
        regime = "chop"
        why.append("无明确趋势：上证20日 %+.1f%% 距MA20 %+.1f%% 创业板ATR %.1f%%" % (
            mom20, dev20, atr or 0))

    meta = None
    if state is not None:
        meta = state.setdefault("meta", {})
    prev = (meta or {}).get("regime_now") or {}
    prev_rg = prev.get("regime")
    days = int(prev.get("days") or 0)
    pending = prev.get("pending") or {}
    if not prev_rg:
        final, days, pending = regime, 1, {}
    elif prev_rg == regime:
        final, days, pending = regime, days + 1, {}
    else:
        pd_ = (int(pending.get("days") or 0) + 1) if pending.get("regime") == regime else 1
        if pd_ >= REGIME_CONFIRM:
            final, days, pending = regime, pd_, {}
        else:
            final, days, pending = prev_rg, days, {"regime": regime, "days": pd_}
    out = {"regime": final,
           "label": REGIME_LABELS.get(final, final),
           "params": dict(REGIME_PARAMS.get(final) or {}),
           "raw": regime,
           "why": why,
           "inputs": {"mom20_sh": None if mom20 is None else round(mom20, 2),
                      "mom20_cyb": None if cyb_mom is None else round(cyb_mom, 2),
                      "dev20_sh": None if dev20 is None else round(dev20, 2),
                      "atr20_cyb": None if atr is None else round(atr, 2),
                      "breadth": round(breadth, 1),
                      "price_sh": sh.get("price")},
           "days": days, "pending": pending,
           "date": date, "ts": now_ts()}
    today = date or time.strftime("%Y-%m-%d")
    if meta is not None:
        out["since"] = prev.get("since") if (prev_rg == final) and prev.get("since") else today
        meta["regime_now"] = out
        hist = meta.setdefault("regime_history", [])
        if not hist or hist[-1].get("regime") != final:
            hist.append({"date": today, "regime": final, "label": out["label"],
                         "why": "；".join(why), "inputs": out["inputs"], "ts": out["ts"]})
            if len(hist) > 60:
                del hist[:-60]
    else:
        out["since"] = today
    return out


def market_gate(review, state=None, date=None, sh=None, cyb=None):
    """大盘闸门。返回 (gate, 说明)，gate ∈ {attack, open, caution, reduce, block}。

    v3.12：由二元开关改为「仓位旋钮」，软因素只降档，硬崩盘才空仓。
      attack : 多头结构 + 广度健康(45~75) + 板块主力净流入 ≥ 100亿 → 满仓（含突破买点）
      open   : 其余无风险 → 满仓
      caution: 广度过热(≥65) / LLM 防御 / 上证弱 → 半仓（仍可开仓，但只用回踩买点）
      reduce : 宏观利多 × 宽度极弱背离 → 仓位×0.6
      block  : 上证空头 **且** 涨占比 ≤ 20%（真崩盘）→ 空仓

    理由：2026-09-11~18 连续 6 日 block，原因逐日退化为单靠一条 LLM「防御」
    （同日 LLM 实测会在 多头64 / 防御44 之间抖动），期间主线板块涨 5%~14%、
    模拟盘只成交 1 笔、93.8% 现金闲置。风控应靠「仓位大小」，不是「开不开仓」。

    日内棘轮（传 state 时生效）：仅对 block 维持到收盘（防 LLM 抖动导致反复开关）；
    caution/reduce 不锁定，随时可按最新市场恢复。
    """
    idx_sigs = {x.get("code"): x for x in review.get("indices", [])}
    sh_f = (idx_sigs.get("sh000001") or {}).get("factors") or {}
    if sh is None:
        sh = sh_f
    # mkt_bear 用 review 里的 factors（signal/score 结构）；sh/cyb 参数是给 market_regime 的指标 dict
    mkt_bear = sh_f.get("signal") in ("卖出", "减仓") and (sh_f.get("score") or 0) < 45
    breadth = (review.get("temperature") or {}).get("breadth") or 0
    overheat = breadth >= OVERHEAT_BREADTH
    llm_def, llm_bull, macro_age, llm_stale = False, False, None, False
    try:
        _llm = load_json("macro_llm_data.json") or {}
        _ll = (_llm.get("llm") or {})
        _llm_sent = _ll.get("sentiment")
        _score = _ll.get("score") or 50
        macro_age = _macro_age_hours(_llm)
        fresh = macro_age is None or macro_age <= MACRO_STALE_HOURS
        llm_stale = not fresh
        # v3.12 对称新鲜度：过期数据既不能判多头，也不能判防御
        #   （旧版只有多头要求 fresh，导致过期 90h 的「防御」仍能 block 全天）
        llm_def = fresh and (_llm_sent in ("空头", "防御")
                             or (_llm_sent == "中性" and _score < 40))
        llm_bull = fresh and _llm_sent == "多头" and _score >= 60
    except Exception:
        pass
    # attack 判定：市场资金面（板块主力净流入，取机构数据 overview）
    main_net = 0.0
    try:
        _inst = load_json("institution_data.json") or {}
        main_net = ((_inst.get("overview") or {}).get("main_net")) or 0.0
    except Exception:
        pass
    # 背离：政策面利多，但个股普跌（涨占比极低）→ 降档而非禁开仓
    diverge = llm_bull and breadth <= REDUCE_BREADTH
    # 硬空仓：大盘转空 + 个股几乎全跌（真崩盘）
    hard_flat = mkt_bear and 0 < breadth <= HARD_FLAT_BREADTH
    attack_ok = (not mkt_bear and not overheat and not llm_def
                 and ATTACK_BREADTH_LO <= breadth <= ATTACK_BREADTH_HI
                 and main_net >= ATTACK_MAIN_NET)
    if hard_flat:
        gate = "block"
    elif mkt_bear or overheat or llm_def:
        gate = "caution"
    elif diverge:
        gate = "reduce"
    elif attack_ok:
        gate = "attack"
    else:
        gate = "open"
    why = []
    # ===== 环境自适应（v4.0）：环境优先于单日软因素 =====
    #   panic（高波动急跌）直接空仓；bear（趋势下跌）最多半仓；其余按原 gate 分档。
    rg = market_regime(review, state=state, date=date, sh=sh, cyb=cyb)
    # v4.1：环境 regime 优先于单日软因素 —— panic 直接 block，bear 至多半仓
    if rg["regime"] == "panic":
        gate = "block"
        why.append("高波动急跌(panic，环境优先)")
    elif rg["regime"] == "bear" and gate in ("attack", "open"):
        gate = "caution"
        why.append("趋势下跌(bear，最多半仓)")
    if mkt_bear:
        why.append("上证空头")
    if hard_flat:
        why.append("涨占比%.0f%%≤%.0f%%(崩盘)" % (breadth, HARD_FLAT_BREADTH))
    if overheat:
        why.append("普涨过热(广度%d%%，降半仓)" % int(breadth))
    if llm_def:
        why.append("LLM宏观防御(降半仓)")
    if diverge:
        why.append("宏观利多×宽度极弱(涨占比%.1f%%≤%.0f%%)背离→降档" % (breadth, REDUCE_BREADTH))
    if attack_ok:
        why.append("进攻档(广度%.0f%% 主力净流入%.0f亿)" % (breadth, main_net / 1e8))
    if llm_stale and macro_age is not None:
        why.append("宏观数据过期%.0fh(不参与闸门判断)" % macro_age)

    # 日内棘轮：仅 block 维持到收盘（软因素 caution/reduce 不锁，允许按最新市场恢复）
    _pos = gate_pos_of(gate)
    if state is not None and date:
        try:
            dg = state.setdefault("meta", {}).setdefault("daily_gate", {})
            if dg.get("date") != date:
                dg.clear()
                dg.update({"date": date, "blocked_today": False})
            if gate == "block":
                dg["blocked_today"] = True
                dg["block_ts"] = dg.get("block_ts") or time.strftime("%H:%M:%S")
            if dg.get("blocked_today") and gate != "block":
                why.append("日内棘轮（今日 %s 已触发 block：%s）" % (
                    dg.get("block_ts", ""), dg.get("block_why") or ""))
                gate, _pos = "block", 0.0
            else:
                if gate == "block":
                    dg["block_why"] = "；".join(why) or "block"
                dg.update({"gate": gate, "pos": _pos,
                           "why": "；".join(why) or "open",
                           "ts": time.strftime("%H:%M:%S")})
        except Exception:
            pass
    # 闸门历史（按日，供"连续 block 天数"告警与「防守过度」复核）：
    # 同时记录 gate_pos、广度、基准涨幅 —— 用于回看 block/caution 日的错失幅度
    if state is not None and date:
        try:
            gh = state.setdefault("meta", {}).setdefault("gate_history", {})
            _bench = ((idx_sigs.get(MISSED_BENCH) or {}).get("change_pct"))
            gh[date] = {"gate": gate, "pos": _pos, "why": "；".join(why) or "open",
                        "breadth": round(breadth, 1),
                        "bench": _bench,
                        "ts": time.strftime("%H:%M:%S")}
            if len(gh) > 90:
                for k in sorted(gh)[:-90]:
                    gh.pop(k, None)
            # 防守过度告警：连续 ≥3 日非 open 且基准累计上涨 > 2%
            _seq = [gh[k] for k in sorted(gh) if k <= date][-12:]
            _streak, _bench_cum = 0, 0.0
            for _h in reversed(_seq):
                if _h.get("gate") in ("block", "caution"):
                    _streak += 1
                    _bench_cum += (_h.get("bench") or 0)
                else:
                    break
            if _streak >= 3 and _bench_cum > 2.0:
                print("  [gate-watch] 连续 %d 日降档/防守，基准累计 %+.2f%% → 复核是否防守过度" % (
                    _streak, _bench_cum))
        except Exception:
            pass
    return gate, ("；".join(why) if why else "open")


def load_external():
    """读外部因子数据；过期（>EXT_MAX_AGE_H）视为不可用。返回 dict 或 {}。"""
    try:
        e = load_json("external_data.json") or {}
    except Exception:
        return {}
    gen = (e.get("generatedAt") or "")[:19]
    if not gen:
        return {}
    try:
        age = (time.time() - time.mktime(time.strptime(gen, "%Y-%m-%d %H:%M:%S"))) / 3600.0
    except Exception:
        return {}
    e["_age_h"] = round(age, 2)
    e["_stale"] = age > EXT_MAX_AGE_H
    return e


def _ext_score_of(it, ext):
    """个股的外部因子偏好（原始分，未乘权重）。返回 (score, 理由) 或 (0, '').

    优先用个股名关键词（板块映射不到的石油/矿业股），其次用板块偏好。
    """
    if not ext or ext.get("_stale"):
        return 0.0, ""
    name = it.get("name") or ""
    kw = ext.get("keyword_bias") or {}
    import re as _re
    for pat, sc in kw.items():
        try:
            if _re.search(pat, name):
                return float(sc), "外部:" + pat.split("|")[0]
        except Exception:
            continue
    sec = it.get("sector") or ""
    bias = ext.get("sector_bias") or {}
    if sec in bias:
        return float(bias[sec]), "外部:" + sec
    return 0.0, ""


def ext_bonus(it, ext):
    """外部因子 → 选股加分（已乘权重并限幅）。"""
    sc, _why = _ext_score_of(it, ext)
    if not sc:
        return 0.0
    return max(-EXT_CAP, min(EXT_CAP, sc * EXT_WEIGHT))


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
    gate, gate_why = market_gate(review, state=state, date=asof)
    ext = load_external()
    if ext and not ext.get("_stale"):
        print("  [ext][%s] 风险偏好 %s(%+d)；板块偏好 %s" % (
            pool, (ext.get("risk_pref") or {}).get("tone"),
            (ext.get("risk_pref") or {}).get("score") or 0,
            ", ".join("%s%+.1f" % (k, v) for k, v in list((ext.get("sector_bias") or {}).items())[:5])))
    elif ext:
        print("  [ext] 外部因子过期 %.1fh（>%.0fh），本次不参与选股" % (
            ext.get("_age_h", 0), EXT_MAX_AGE_H))
    _reduce = (gate == "reduce")
    _score_bonus = (REDUCE_MIN_SCORE_BONUS if _reduce else
                    (CAUTION_SCORE_BONUS if gate == "caution" else 0.0))
    _budget_mul = gate_pos_of(gate) if gate != "open" else 1.0
    # v4.0 环境参数覆盖：仓位/门槛/回踩/止盈/止损/是否允许开新仓
    _rg = ((state.get("meta") or {}).get("regime_now") or {})
    _rp = _rg.get("params") or {}
    _bias_add = float(_rp.get("bias") or 0)
    _tp_scale = float(_rp.get("tp") or 1.0)
    _sl_scale = float(_rp.get("sl") or 1.0)
    if _rp:
        _budget_mul *= float(_rp.get("pos") or 0)
        _score_bonus += float(_rp.get("score") or 0)
        print("  [regime][%s] %s（仓位×%.2f 门槛+%.0f 回踩+%.1f%% 止盈×%.2f）：%s" % (
            pool, _rg.get("label"), _rp.get("pos", 1), _rp.get("score", 0),
            _bias_add * 100, _tp_scale, "；".join(_rg.get("why") or [])[:90]))
    # 组合层面自适应（每账户）：回撤/连亏熔断 + 波动目标化（v4.1 主开关）
    acct_reg = {}
    for key in REAL_ACCOUNTS:
        if not ACCT_REGIME_ON:
            acct_reg[key] = {"regime": "off", "pos": 1.0, "score": 0,
                             "vol_pos": 1.0, "why": "已关闭"}
            continue
        _ar = account_regime(acc_b[key])
        _ar["vol_pos"] = vol_target_pos((_rg.get("inputs") or {}).get("atr20_cyb"))
        acct_reg[key] = _ar
        if _ar["regime"] != "healthy":
            print("  [acct][%s] %s：%s（仓位×%.2f×波动%.2f）" % (
                pool, ACCOUNTS[key]["label"], _ar["why"], _ar["pos"], _ar["vol_pos"]))
    if gate != "open":
        print("  [gate][%s] %s（仓位×%.2f）" % (pool, gate_why, _budget_mul))
    # 候选：池过滤
    cand_pool = {}   # code -> item
    per_key = {}     # key -> [item]
    for key in REAL_ACCOUNTS:
        cfg = ACCOUNTS[key]
        acct = acc_b[key]
        held = {p["code"] for p in acct["positions"]}
        cands = []
        _ar = acct_reg[key]
        if _ar["pos"] <= 0:
            per_key[key] = cands
            continue
        if not _rp.get("new", True):   # panic：环境禁止开新仓
            per_key[key] = cands
            continue
        for it in items:
            if it.get("code") in held:
                continue
            if it.get("signal_key") not in ("strong_buy", "buy"):
                continue
            _eb = ext_bonus(it, ext)
            # 外部强烈利空（≤-4.5，如"纳指跌+收益率升+VIX涨"三杀 AI算力）→ 个股门槛额外 +4
            if (it.get("score") or 0) < cfg["min_score"] + _score_bonus + _ar["score"] + (-4 if _eb <= -4.5 else 0):
                continue
            if it.get("trend_status") not in ("强势多头", "多头排列"):
                continue
            cands.append(it)
        # 排序 = 原始分 + 策略偏好分（v3.2）+ 外部市场因子分（v3.9）
        cands.sort(key=lambda x: -(x.get("score", 0) + _strategy_bonus(key, x)
                                   + ext_bonus(x, ext)))
        per_key[key] = cands
        for it in cands:
            cand_pool.setdefault(it.get("code"), it)
    # LLM 个股评审（盘前一次性；失败放行）
    llm_rev = {}
    if not skip_llm:
        _llm_macro = None
        try:
            _llm_macro = load_json("macro_llm_data.json")
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
            if _rv and _rv.get("verdict") == "avoid":
                acct["daily_log"].append({"date": asof, "kind": "plan",
                                          "note": "[%s]%s：%s LLM技术回避(%s)" % (
                                              POOL_LABEL[pool], cfg["label"], it.get("name"),
                                              _rv.get("note", ""))})
                continue
            # 注意：_eb 须在本循环内重算（过滤循环里的值会残留，导致所有计划显示同一分数）
            _eb = ext_bonus(it, ext)
            _eb_why = _ext_score_of(it, ext)[1]
            close = it.get("close") or 0
            ma10 = it.get("ma10") or close
            ideal = it.get("ideal_buy") or it.get("secondary_buy") or close
            # 回踩买点：现价下方 bias 处；且不低于 MA10/ideal 过远
            floor = min(ideal, close * (1 - cfg["buy_bias"] - _bias_add))
            buy_below = min(close * (1 - cfg["buy_bias"] - _bias_add), max(floor, close * 0.96))
            buy_below = round(buy_below, 3)
            # 突破买点（v3.12）：单边上涨时回踩单永远等不到，对「强势多头 + 站上 MA20」
            # 额外给一个现价上方的追入价，仅在 open/attack 档启用（弱市不追）。
            buy_above, breakout_band = None, None
            _ma20 = it.get("ma20") or 0
            if (gate in BREAKOUT_GATES and close and _ma20 and close >= _ma20
                    and it.get("trend_status") in ("强势多头", "多头排列")):
                # 突破价：前高在 3% 以内 → 取「突破前高」（趋势突破，更有意义）；
                # 否则取现价上方 0.5%（日内向上确认，避免 buy_above 离现价太远而永不触发）
                _resist = it.get("resistance") or 0
                if _resist and _resist <= close * 1.03:
                    buy_above = round(max(close * (1 + BREAKOUT_PREM), _resist * 1.001), 3)
                else:
                    buy_above = round(close * (1 + BREAKOUT_PREM), 3)
                breakout_band = breakout_band_of(gate)
            budget = round(CASH_START * cfg["budget_frac"] * _budget_mul
                           * acct_reg[key]["pos"] * acct_reg[key]["vol_pos"], 2)
            plan.append({
                "code": it["code"], "name": it.get("name"), "asof": asof,
                "score": it.get("score"), "signal": it.get("signal"),
                "close": close, "buy_below": buy_below, "buy_above": buy_above,
                "breakout_band": breakout_band,
                "entry": "回踩+突破" if buy_above else "回踩",
                "stop_atr": it.get("atr_stop"),
                "stop_ma": it.get("stop_loss") if cfg["stop_ma"] else None,
                "tp": round(close * (1 + cfg["tp_pct"] * _tp_scale), 3) if cfg["tp_pct"] else None,
                "trail": cfg.get("trail"), "be_at": cfg.get("be_at"),
                "gate": gate, "budget": budget, "status": "wait",
                # 外部因子结构化分（供统计：ext 分档 vs 实际收益）
                "ext": {"score": round(_eb, 2), "why": _eb_why} if _eb else None,
                "reason": "%s(%s分) 回踩≤%.2f%s ATR止损%s%s%s%s%s" % (
                    it.get("signal"), it.get("score"), buy_below,
                    (" / 突破≥%.2f" % buy_above) if buy_above else "",
                    it.get("atr_stop") if it.get("atr_stop") else "--",
                    "（保本+5%%）" if cfg.get("be_at") else "",
                    ("；LLM:" + _rv.get("note", "")) if _rv else "",
                    ("（闸门%d档×%.2f）" % ({"caution": 0, "reduce": 1}.get(gate, 2), _budget_mul))
                    if gate != "open" else "",
                    ("（%s%+.1f）" % (_eb_why, _eb)) if _eb else ""),
            })
        acct["plan"] = plan
        if log:
            _gate_note = "" if gate == "open" else (
                "（%s档：门槛+%.0f分 仓位×%.2f）" % (gate, _score_bonus, _budget_mul))
            acct["daily_log"].append({"date": asof, "kind": "plan",
                                      "note": "[%s]%s：%d 单待盘中触发%s" % (
                                          POOL_LABEL[pool], cfg["label"], len(plan),
                                          _gate_note)})
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
        _bf = gate_pos_of(gate) if gate != "open" else 1.0
        budget = round(CASH_START * MIX_SIZE.get(n, MIX_SIZE[1]) * _bf, 2)
        stops = [pl["stop_atr"] for pl in src if pl.get("stop_atr")]
        tps = [pl["tp"] for pl in src if pl.get("tp")]
        _aboves = [pl["buy_above"] for pl in src if pl.get("buy_above")]
        buy_above = round(min(_aboves), 3) if _aboves else None
        _bands = [pl.get("breakout_band") for pl in src if pl.get("breakout_band")]
        breakout_band = min(_bands) if _bands else None
        blocked = any(gate_pos_of(pl.get("gate") or "open") <= 0 for pl in src)
        plan.append({
            "code": code, "name": src[0].get("name"), "asof": asof,
            "score": max((pl.get("score") or 0) for pl in src),
            "signal": src[0].get("signal"),
            "close": max((pl.get("close") or 0) for pl in src),
            "buy_below": round(max(pl["buy_below"] for pl in src), 3),
            "buy_above": buy_above,
            "breakout_band": breakout_band,
            "entry": "回踩+突破" if buy_above else "回踩",
            "stop_atr": round(max(stops), 3) if stops else None,
            "stop_ma": None,
            "tp": round(min(tps), 3) if tps else None,
            "trail": next((pl.get("trail") for pl in src if pl.get("trail")), None),
            "be_at": next((pl.get("be_at") for pl in src if pl.get("be_at")), None),
            "gate": "block" if blocked else (gate or "open"),
            "budget": budget, "status": "wait", "consensus": n,
            "from": [k for k, _ in lst],
            "reason": "共识%d/3（%s）仓位%.0f%%%s 回踩≤%.2f%s 止損%s 止盈%s" % (
                n, "/".join(ACCOUNTS[k]["label"] for k, _ in lst), MIX_SIZE[n] * 100,
                ("×%.1f降档" % _bf) if gate != "open" else "",
                max(pl["buy_below"] for pl in src),
                (" / 突破≥%.2f" % buy_above) if buy_above else "",
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
                                      ("（大盘闸门挡）" if gate == "block" else
                                       ("（%s降档：仓位×%.1f）" % (gate, gate_pos_of(gate))
                                        if gate != "open" else "")))})
    return len(mix["plan"])


# ---------------- 盘中巡检（全池） ----------------
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
    # 实时闸门：计划项的 gate 是【生成时固化】的，盘中宏观/宽度可能已变
    # （如收盘 gate=reduce，次日开盘前宏观转"防御"→ 应变 block 停止开新仓）。
    # 因此买入前用当前 review + 最新宏观重新评估。
    live_gate, live_why = market_gate(load_json("review_data.json") or {},
                                      state=state, date=date)
    gate_changed = False
    # 第一遍：只读扫描，收集候选（不落账）
    probes, tasks = {}, []
    for pool in POOLS:
        for key, a, cfg in all_books(state, pool):
            sells, buys, pnotes = _probe_account(pool, a, cfg, quotes, date, hms, ctx,
                                                 live_gate=live_gate)
            if gate_pos_of(live_gate) <= 0 and buys:
                # 闸门关闭：丢弃全部买入候选（卖出/止损照常执行）
                n_buys = len(buys)
                stale = sorted({(b["pl"].get("gate") or "?") for b in buys})
                if any(g != "block" for g in stale):
                    gate_changed = True
                for b in buys:
                    pl = b["pl"]
                    pl["gate"] = "block"          # 同步为实时闸门，避免下次再触发
                    pl["gate_live"] = {"date": date, "ts": hms, "why": live_why}
                buys = []
                pnotes = list(pnotes) + ["⛔[%s]%s 实时闸门 block（%s）→ 拦截 %d 个买入候选（计划原 gate=%s）" % (
                    POOL_LABEL[pool], cfg["label"], live_why, n_buys, ",".join(stale))]
            elif buys and live_gate not in BREAKOUT_GATES:
                # v3.12：盘中降档（caution/reduce）→ 关闭突破通道，只留回踩买点（弱市不追）
                _drop = [b for b in buys if b.get("entry_type") == "突破"]
                for b in _drop:
                    b["pl"]["buy_above"] = None
                if _drop:
                    buys = [b for b in buys if b not in _drop]
                    pnotes = list(pnotes) + ["⏸[%s]%s 实时闸门 %s → 关闭 %d 个突破买点（只留回踩）" % (
                        POOL_LABEL[pool], cfg["label"], live_gate, len(_drop))]
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
    print("  [gate] 实时闸门 %s（%s）%s" % (
        live_gate, live_why, "← 与计划不一致，已拦截买入" if gate_changed else ""))
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


def _probe_account(pool, acct, cfg, quotes, date, hms, ctx=None, live_gate=None):
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
        if gate_pos_of(pl.get("gate") or "open") <= 0:
            continue
        if len(acct["positions"]) >= cfg["max_pos"]:
            continue
        q = quotes.get(pl["code"])
        if not q:
            continue
        px = q.get("price")
        if not px:
            continue
        _close = pl.get("close") or 0
        # v3.12：追高上限从 昨收+3% 放宽到 +8%，否则突破买点（现价上方）永远进不来；
        # 回踩线 buy_below 本就 ≤ 昨收，不受影响。
        if _close and px > _close * (1 + BREAKOUT_MAX_CHG):
            continue  # 相对昨收涨幅过大不追
        pc = q.get("prevClose") or _close
        chg = (px / pc - 1) * 100 if pc else 0
        trigger = round(pl["buy_below"] * (1 + TOUCH_TOL), 3)
        # 突破触发（v3.12）：站上 buy_above，但最多追到 buy_above×(1+band)
        #   实时档位比计划更严时收紧 band（例：计划 open → 盘中转 caution）
        _above = pl.get("buy_above")
        _band = pl.get("breakout_band") or breakout_band_of(pl.get("gate") or "open")
        if live_gate:
            _band = min(_band, breakout_band_of(live_gate))
        trig_break = bool(_above) and px >= _above and px <= _above * (1 + _band)
        if px <= trigger or trig_break:
            it = ctx.get(pl["code"]) or {}
            _entry = "突破" if (trig_break and not px <= trigger) else "回踩"
            buys.append({
                "id": "%s|%s|%s|buy" % (pool, acct["key"], pl["code"]),
                "action": "buy", "code": pl["code"], "name": pl["name"],
                "px": px, "chg": chg, "buy_below": pl["buy_below"],
                "buy_above": _above, "entry_type": _entry,
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
            "ext": pos.get("ext"),
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
        if gate_pos_of(pl.get("gate") or "open") <= 0:
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
            "ext": pl.get("ext"),
            "llm": {"verdict": d.get("verdict"), "note": d.get("note"),
                    "src": d.get("src")},
        })
        acct["trades"].append({
            "action": "buy", "date": date, "time": hms, "code": pl["code"],
            "name": pl["name"], "price": round(px, 3), "shares": shares,
            "chg_at_fill": round(b["chg"], 2),
            "entry": b.get("entry_type"),
            "reason": pl.get("reason", ""), "strategy": acct["key"], "pool": pool,
            "ext": pl.get("ext"),
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
                    help="盘中整点用最新全量复盘刷新全池计划(成分版, items>300 才执行)")
    ap.add_argument("--replay", default=None)
    ap.add_argument("--review", action="store_true")
    ap.add_argument("--finalize", action="store_true")
    ap.add_argument("--strategy-log", default=None)
    ap.add_argument("--pool", default=None, help="all=全池（默认；six 已停用）")
    ap.add_argument("--plan-date", default=None, help="历史日计划重建已停用（原仅 6 股池）")
    ap.add_argument("--no-llm", action="store_true", help="跳过LLM个股评审(快速重建用)")
    ap.add_argument("--force", action="store_true",
                    help="--intraday 在非交易时段也执行（默认拒绝，避免拿收盘价当盘中成交）")
    ap.add_argument("--date", default=None)
    args = ap.parse_args()

    if args.pool and args.pool not in POOLS:
        print("未知池模式:", args.pool, "（仅 all；six 已停用，省略=全池）")
        return
    target_pools = list(POOLS)

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
        print("全池初始化：激进/稳健/严守纪律 各 5 万 + 合并·共识，各自独立交易。")
        print("先 --plan 建计划。")
        return
    state = load_state()
    print("当前池: " + " + ".join("%s(%s)" % (p, POOL_LABEL[p]) for p in POOLS))

    if args.plan:
        if args.plan_date:
            print("--plan-date 已停用（6 股精选池已下线），请直接 --plan 重建全池计划")
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
                    print("    %s 分%s 回踩≤%.2f%s %s" % (
                        p["name"], p["score"], p["buy_below"],
                        (" / 突破≥%.2f" % p["buy_above"]) if p.get("buy_above") else "",
                        p.get("gate")))
            mx = books(state, pool)["mix"]
            print("  [%s] %d 单%s" % (MIX_CFG["label"], len(mx["plan"]),
                                    "（%s）" % "/".join(
                                        "共识%s×%d" % (c, sum(1 for p in mx["plan"] if p.get("consensus") == c))
                                        for c in (3, 2, 1) if any(p.get("consensus") == c for p in mx["plan"]))
                                    if mx["plan"] else ""))
            for p in mx["plan"][:5]:
                print("    [共识%d] %s 分%s 回踩≤%.2f%s %s" % (
                    p.get("consensus", 0), p["name"], p["score"], p["buy_below"],
                    (" / 突破≥%.2f" % p["buy_above"]) if p.get("buy_above") else "",
                    p.get("gate")))
        save(state)
        return

    if args.intraday_plan:
        # 盘中整点复盘重建：用最新全量复盘(含ETF成分, items>300)刷新全池待触发计划
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
        print("盘中复盘重建 %s：全池计划已按最新信号刷新，变更账户 %d" % (date, changed))
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
