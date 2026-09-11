#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一周冲刺模拟盘（sim_sprint）· 单账户激进短线

目标：一周 ≥ +10%（尽力而为，市场不保证）。
玩法：自动选股重仓（--auto，市价进），cron 每2分钟（sim_intraday_scan.sh）自动 scan 执行
      移动止盈/目标止盈/硬止损；盘前/收盘自动跑 --auto 补仓。
建仓闸门：大盘闸门 block 时不建仓；个股消息面回避、外部强利空不碰；LLM 成交前复核。

规则（冲刺激进档）：
  - 单笔 ≤ 现金 50%，最多 2 只并行（集中）
  - 买：市价现价成交（不等回踩），记真实时分秒
  - 卖：峰值 ≥+8% 后回落 6% → 移动止盈锁定；单票 +18% → 目标止盈；
       现价 ≤ 成本 -6.5% → 硬止损
命令：
  python3 sim_sprint.py --init
  python3 sim_sprint.py --auto                       # 自动选股建仓（推荐，过大盘闸门+LLM复核）
  python3 sim_sprint.py --auto --dry                 # 演练：只打印不下单
  python3 sim_sprint.py --buy 601138 [frac=0.5]     # 市价买（现金×frac，手工）
  python3 sim_sprint.py --sell 601138                # 市价全清
  python3 sim_sprint.py --scan                       # 巡检执行止盈/止损（cron 每5分）
  python3 sim_sprint.py --review [--date D]          # 收盘净值曲线
  python3 sim_sprint.py --status                     # 账户/持仓/净值
账本: data/sim_sprint.json
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
STATE = os.path.join(DATA_DIR, "sim_sprint.json")
START_CASH = 50000.0

RULE = {
    "label": "一周冲刺",
    "max_pos": 2,          # 集中：最多2只
    "max_frac": 0.5,       # 单笔 ≤ 现金50%
    "buy_fee": 0.0003,     # 万3
    "sell_fee": 0.0008,    # 万3+万5
    "trail_peak": 0.08,    # 峰值≥+8%
    "trail_drop": 0.06,    # 回落6% 移动止盈
    "tp_pct": 0.18,        # 单票 +18% 目标止盈
    "hard_stop": -0.065,   # 硬止损 -6.5%
}


# 成交前 LLM 风控闸门：复用 sim_live 的实现（缓存/熔断/失败放行同源）
try:
    from sim_live import llm_trade_gate as _llm_gate
except Exception:          # sim_live 不可用时退化为全放行
    _llm_gate = None


def _ctx(code):
    """review_data 上下文（板块/趋势/评分），供 LLM 闸门判断。"""
    try:
        with open(os.path.join(DATA_DIR, "review_data.json"), encoding="utf-8") as f:
            rev = json.load(f)
        for x in (rev.get("items") or []):
            if x.get("code") == code:
                return x
    except Exception:
        pass
    return {}


def gate(st, date, hms, tasks):
    """跑 LLM 闸门；不可用时全放行。返回 {id: 决策}。"""
    if not tasks or _llm_gate is None:
        return {}
    try:
        return _llm_gate(st, date, hms, tasks)
    except Exception as e:
        print("  [llm][gate] 异常(放行): %s" % e)
        return {}


def now_ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load():
    if not os.path.exists(STATE):
        return None
    with open(STATE, "r", encoding="utf-8") as f:
        return json.load(f)


def save(st):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def new_state():
    return {
        "meta": {"created": now_ts(), "start_cash": START_CASH, "label": RULE["label"],
                 "pool": "sprint-AI选股", "rule": RULE},
        "cash": START_CASH, "positions": [], "trades": [],
        "equity_curve": [], "log": [],
        "signals": [],   # AI 每日决策记录
    }


def log(st, msg):
    st["log"].append({"ts": now_ts(), "msg": msg})


def equity(st):
    total = st["cash"]
    for p in st["positions"]:
        total += p["shares"] * (p.get("last") or p["cost"])
    return total


def _quote(codes):
    from src import data_provider as dp
    try:
        return dp.fetch_quotes(sorted(codes))
    except Exception as e:
        print("快照失败:", e)
        return {}


def do_buy(st, code, frac, llm=None):
    q = _quote([code]).get(code)
    if not q or not q.get("price"):
        print("无行情", code)
        return
    px = q["price"]
    budget = min(st["cash"] * frac, st["cash"] * 0.98)
    shares = int(budget / px / 100) * 100
    if shares <= 0:
        print("预算不足", code)
        return
    cost = shares * px
    fee = cost * RULE["buy_fee"]
    st["cash"] -= cost + fee
    st["positions"].append({
        "code": code, "name": q.get("name") or code,
        "shares": shares, "cost": round(px, 3),
        "buy_time": now_ts(), "peak": px,
        "last": px, "last_chg": q.get("change"),
        "stop": round(px * (1 + RULE["hard_stop"]), 3),
    })
    st["trades"].append({"action": "buy", "ts": now_ts(), "code": code,
                         "name": q.get("name") or code, "price": round(px, 3),
                         "shares": shares, "reason": "AI冲刺建仓", "llm": llm})
    print("买入 %s %s股 @%.2f 现金剩余%.0f" % (code, shares, px, st["cash"]))


def do_sell(st, code, reason, px=None, llm=None):
    pos = next((p for p in st["positions"] if p["code"] == code), None)
    if not pos:
        return
    if px is None:
        q = _quote([code]).get(code)
        px = (q or {}).get("price") or pos["last"] or pos["cost"]
    proceeds = pos["shares"] * px
    fee = proceeds * RULE["sell_fee"]
    pnl = proceeds - fee - pos["shares"] * pos["cost"]
    pnl_pct = (px / pos["cost"] - 1) * 100
    st["cash"] += proceeds - fee
    st["positions"] = [p for p in st["positions"] if p["code"] != code]
    st["trades"].append({"action": "sell", "ts": now_ts(), "code": code,
                         "name": pos["name"], "price": round(px, 3),
                         "shares": pos["shares"], "pnl": round(pnl, 2),
                         "pnl_pct": round(pnl_pct, 2), "reason": reason, "llm": llm})
    print("卖出 %s @%.2f (%+.2f%%) %s" % (pos["name"], px, pnl_pct, reason))


def _gate():
    """大盘闸门（复用 sim_live 的 market_gate）。返回 (gate, why)。"""
    try:
        import sim_live as S
        st = S.load_state()
        return S.market_gate(S.load_json("review_data.json") or {},
                             state=st, date=time.strftime("%Y-%m-%d"))
    except Exception as e:
        print("  [gate] 读取失败(按 open 处理): %s" % str(e)[:80])
        return "open", "闸门不可用"


def _ext_score(name, sector):
    """外部因子分（复用 sim_live）。"""
    try:
        import sim_live as S
        return S.ext_bonus({"name": name, "sector": sector}, S.load_external())
    except Exception:
        return 0.0


def _news_avoid():
    """宏观 LLM 的个股消息面回避名单（利空/防御/score<45）。"""
    try:
        with open(os.path.join(DATA_DIR, "macro_llm_data.json"), encoding="utf-8") as f:
            d = json.load(f)
        out = {}
        for s in (d.get("stocks") or []):
            if s.get("sentiment") in ("利空", "防御") or (s.get("score") or 50) < 45:
                out[s.get("code")] = s
        return out
    except Exception:
        return {}


def auto_pick(st, dry=False):
    """自动选股建仓：冲刺盘此前只有人工 --buy，平仓后永久空转。

    选股口径（激进：取最强）：
      review 候选（strong_buy/buy + 强势多头/多头排列）→ 评分 + 外部因子 排序
      → 过滤消息面回避 → 过大盘闸门 → LLM 成交前复核 → 市价建仓
    约束：最多 max_pos(2) 只、单笔 ≤ 现金 50%
    """
    if len(st["positions"]) >= RULE["max_pos"]:
        print("已有 %d 只持仓（上限%d），不新建" % (len(st["positions"]), RULE["max_pos"]))
        return 0
    gate, gwhy = _gate()
    if gate == "block":
        print("  [gate] 大盘闸门 block（%s）→ 冲刺盘不建仓" % gwhy[:60])
        _msg = "闸门 block（%s）→ 跳过自动建仓" % gwhy[:60]
        _logs = st.setdefault("log", [])
        _today = time.strftime("%Y-%m-%d")
        if not any(l.get("msg") == _msg and str(l.get("ts", "")).startswith(_today) for l in _logs):
            _logs.append({"ts": now_ts(), "msg": _msg})
        return 0
    try:
        with open(os.path.join(DATA_DIR, "review_data.json"), encoding="utf-8") as f:
            items = (json.load(f).get("items") or [])
    except Exception as e:
        print("无 review_data:", str(e)[:60]); return 0
    held = {p["code"] for p in st["positions"]}
    avoid = _news_avoid()
    cands = []
    for it in items:
        c = it.get("code")
        if not c or c in held or c in avoid:
            continue
        if it.get("signal_key") not in ("strong_buy", "buy"):
            continue
        if it.get("trend_status") not in ("强势多头", "多头排列"):
            continue
        if str(c).startswith(("5", "1")):     # 冲刺盘只做个股，跳过 ETF
            continue
        sc = (it.get("score") or 0)
        eb = _ext_score(it.get("name"), it.get("sector"))
        if eb <= -4.5:                        # 外部强利空（如三杀成长）不碰
            continue
        cands.append({**it, "_rank": sc + eb, "_eb": eb})
    cands.sort(key=lambda x: -x["_rank"])
    if not cands:
        print("  [auto] 无合格候选（信号/趋势/消息面/外部因子过滤后）")
        return 0
    slots = RULE["max_pos"] - len(st["positions"])
    picks = cands[:slots]
    print("  [auto] 候选 %d 只 → 取前 %d：%s" % (
        len(cands), len(picks), ", ".join("%s(%d分 外部%+.1f)" % (
            p.get("name"), p.get("score") or 0, p["_eb"]) for p in picks)))
    # LLM 成交前复核（复用 sim_live 闸门）
    _d = time.strftime("%Y-%m-%d"); _t = time.strftime("%H:%M:%S")
    tasks = []
    for p in picks:
        q = _quote([p["code"]]).get(p["code"]) or {}
        tasks.append({"id": "sprint|auto|%s|buy" % p["code"], "action": "buy",
                      "code": p["code"], "name": p.get("name"),
                      "px": q.get("price") or p.get("close") or 0,
                      "chg": q.get("change"),
                      "buy_below": q.get("price") or p.get("close") or 0,
                      "why": "冲刺盘自动选股（%d分 %s 趋势%s 外部%+.1f）" % (
                          p.get("score") or 0, p.get("signal"), p.get("trend_status"), p["_eb"]),
                      "sector": p.get("sector"), "trend": p.get("trend_status"),
                      "score": p.get("score"), "signal": p.get("signal")})
    dec = gate_dec = {}
    try:
        if tasks and _llm_gate is not None:
            dec = _llm_gate(st, _d, _t, tasks)
    except Exception as e:
        print("  [llm][gate] 异常(放行): %s" % str(e)[:80])
    done = 0
    for p in picks:
        d = dec.get("sprint|auto|%s|buy" % p["code"]) or {}
        if d.get("verdict") == "avoid":
            print("  🧠 LLM否决建仓 %s：%s" % (p.get("name"), d.get("note") or ""))
            st.setdefault("log", []).append({"ts": now_ts(),
                                            "msg": "LLM 否决建仓 %s：%s" % (
                                                p.get("name"), d.get("note") or "")})
            continue
        if dry:
            print("  [dry] 建仓 %s（%d分）" % (p.get("name"), p.get("score") or 0))
            done += 1
            continue
        before = st["cash"]
        do_buy(st, p["code"], RULE["max_frac"], llm=d)
        if st["cash"] < before:
            done += 1
            st.setdefault("signals", []).append({
                "ts": now_ts(), "code": p["code"], "name": p.get("name"),
                "score": p.get("score"), "signal": p.get("signal"),
                "trend": p.get("trend_status"), "ext": round(p["_eb"], 2),
                "llm": d.get("note"), "why": "自动选股建仓"})
    return done


def do_scan(st):
    """巡检持仓两阶段：先扫出卖出候选 → LLM 复核 → 落账。

    硬止损强制成交（LLM 只能确认）；目标止盈/移动止盈可被 LLM 否决改为继续持有。
    """
    if not st["positions"]:
        print("sprint 无持仓")
        return
    codes = [p["code"] for p in st["positions"]]
    quotes = _quote(codes)
    date = time.strftime("%Y-%m-%d")
    hms = time.strftime("%H:%M:%S")
    cands = []          # (pos, px, gain, reason, hard)
    for pos in list(st["positions"]):
        q = quotes.get(pos["code"])
        if not q or not q.get("price"):
            continue
        px = q["price"]
        pos["last"] = px
        if px > pos["peak"]:
            pos["peak"] = px
        gain = (px / pos["cost"] - 1) * 100
        peak_gain = (pos["peak"] / pos["cost"] - 1) * 100
        reason, hard = None, False
        if gain >= RULE["tp_pct"] * 100:
            reason = "目标止盈 +%.0f%%" % gain
        elif px <= pos["cost"] * (1 + RULE["hard_stop"]):
            reason = "硬止损 %.1f%%" % gain
            hard = True
        elif peak_gain >= RULE["trail_peak"] * 100 and \
                pos["peak"] - px >= pos["peak"] * RULE["trail_drop"]:
            reason = "移动止盈(峰值%+.1f%%回落)" % peak_gain
        if reason:
            cands.append({"pos": pos, "px": px, "gain": gain, "reason": reason, "hard": hard})
    if not cands:
        save(st)
        print("sprint 巡检 %s：无触发" % hms)
        return
    # LLM 风控复核（一次批量）
    tasks = []
    for c in cands:
        pos = c["pos"]
        it = _ctx(pos["code"])
        tasks.append({
            "id": "sprint|sprint|%s|sell" % pos["code"],
            "action": "sell", "code": pos["code"], "name": pos["name"],
            "px": c["px"], "chg": quotes.get(pos["code"], {}).get("change"),
            "cost": pos["cost"], "gain": c["gain"],
            "peak_gain": (pos["peak"] / pos["cost"] - 1) * 100,
            "why": c["reason"], "hard": c["hard"],
            "sector": it.get("sector"), "trend": it.get("trend_status"),
            "score": it.get("score"),
        })
    dec = gate(st, date, hms, tasks)
    for c in cands:
        pos = c["pos"]
        d = dec.get("sprint|sprint|%s|sell" % pos["code"]) or {}
        if d.get("verdict") == "avoid" and not c["hard"]:
            pos["llm_hold"] = {"date": date, "ts": hms, "note": d.get("note")}
            st.setdefault("log", []).append({
                "ts": now_ts(), "msg": "🧠 LLM 否决卖出 %s：%s（%s）" % (
                    pos["name"], d.get("note") or "", c["reason"])})
            print("🧠 LLM否决卖出 %s：%s（继续持有）" % (pos["name"], d.get("note") or ""))
            continue
        note = "｜LLM:%s" % (d.get("note") or "") if d.get("note") else ""
        do_sell(st, pos["code"], c["reason"] + note, c["px"], llm=d)
    save(st)


def do_review(st, date):
    """收盘净值曲线（市值按最后现价/成本）。"""
    for p in st["positions"]:
        p["last_close"] = p.get("last") or p["cost"]
    eq = st["cash"] + sum(p["shares"] * p["cost"] for p in st["positions"])
    curve = st["equity_curve"]
    curve = [x for x in curve if x["date"] != date]
    prev = curve[-1]["equity"] if curve else START_CASH
    ret = (eq / prev - 1) * 100 if curve else (eq / START_CASH - 1) * 100
    curve.append({"date": date, "equity": round(eq, 2), "cash": round(st["cash"], 2),
                  "daily_return": round(ret, 3), "pos": len(st["positions"])})
    st["equity_curve"] = curve
    save(st)
    print("收盘 %s 净值 %.0f（累计%+.2f%%）持仓%d" % (date, eq, (eq / START_CASH - 1) * 100,
                                                 len(st["positions"])))


def do_status(st):
    print("== 一周冲刺模拟盘 ==")
    eq = equity(st)
    print("净值 %.2f（累计%+.2f%%）现金 %.0f 持仓%d 成交%d笔" % (
        eq, (eq / START_CASH - 1) * 100, st["cash"],
        len(st["positions"]), len(st["trades"])))
    for p in st["positions"]:
        gain = ((p.get("last") or p["cost"]) / p["cost"] - 1) * 100
        print("  %s %s股 成本%.2f 现价%s (%+.1f%%) 峰值%.2f" % (
            p["name"], p["shares"], p["cost"], p.get("last"), gain, p["peak"]))
    for t in st["trades"][-4:]:
        print("  [%s] %s %s @%.2f x%d %s" % (t["ts"][11:19], t["action"],
              t["name"], t["price"], t["shares"], t.get("reason", "")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--buy", default=None, help="code 市价买入")
    ap.add_argument("--frac", type=float, default=0.5, help="买入仓位比例(默认0.5)")
    ap.add_argument("--sell", default=None, help="code 市价卖出")
    ap.add_argument("--auto", action="store_true", help="自动选股建仓（过大盘闸门+LLM复核）")
    ap.add_argument("--dry", action="store_true", help="--auto 演练：只打印不下单")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--review", action="store_true")
    ap.add_argument("--date", default=None)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.init or not load():
        save(new_state())
        print("冲刺模拟盘初始化：5万 单账户 · 集中≤2只 · 移动止盈/目标止盈+18%/硬止损-6.5%")
        return
    st = load()
    if args.buy:
        if len(st["positions"]) >= RULE["max_pos"]:
            print("已达持仓上限%d只，先卖再买" % RULE["max_pos"])
            return
        _d = time.strftime("%Y-%m-%d"); _t = time.strftime("%H:%M:%S")
        _q = _quote([args.buy]).get(args.buy) or {}
        _it = _ctx(args.buy)
        _tid = "sprint|sprint|%s|buy" % args.buy
        _dec = gate(st, _d, _t, [{
            "id": _tid, "action": "buy", "code": args.buy,
            "name": _q.get("name") or args.buy, "px": _q.get("price") or 0,
            "chg": _q.get("change"), "buy_below": _q.get("price") or 0,
            "why": "冲刺盘市价建仓(现金%.0f%%)" % (args.frac * 100),
            "sector": _it.get("sector"), "trend": _it.get("trend_status"),
            "score": _it.get("score"), "signal": _it.get("signal"),
        }]).get(_tid) or {}
        if _dec.get("verdict") == "avoid":
            st.setdefault("log", []).append({
                "ts": now_ts(), "msg": "🧠 LLM 否决买入 %s：%s" % (
                    _q.get("name") or args.buy, _dec.get("note") or "")})
            save(st)
            print("🧠 LLM 否决买入：%s（不建仓）" % (_dec.get("note") or ""))
            return
        do_buy(st, args.buy, args.frac, llm=_dec or None)
        save(st)
        do_status(st)
        return
    if args.sell:
        do_sell(st, args.sell, "AI主动卖出")
        save(st)
        do_status(st)
        return
    if args.auto:
        n = auto_pick(st, dry=args.dry)
        save(st)
        print("自动建仓 %d 笔" % n)
        do_status(st)
        return
    if args.scan:
        do_scan(st)
        return
    if args.review:
        do_review(st, args.date or time.strftime("%Y-%m-%d"))
        return
    if args.status:
        do_status(st)
        return
    print("用法: --init / --buy CODE [--frac .5] / --sell CODE / --scan / --review / --status")


if __name__ == "__main__":
    main()
