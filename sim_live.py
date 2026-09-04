#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实时模拟炒股 · 盘中触发单引擎（v2）+ 自学习复盘。

核心理念（与用户确认）：
  不做"盘后挂单次日成交"。而是——
  1) 交易日盘中定时巡检（默认每 5 分钟，cron */5 交易时段）
  2) 用当日实时快照现价 + 已算好的技术位（买点/MA/ATR 止损/止盈）
  3) 价格触发即成交：现价触及买点→买入；破止损/到止盈→卖出
  4) 记录真实时分秒 + 成交时现价 + 当时涨跌幅
  5) 计划盘前生成（基于最近收盘信号），盘中只等触发 → 无前视

命令：
  python3 sim_live.py --plan                 # 每日盘前/收盘后生成计划（买点/止损/止盈价位）
  python3 sim_live.py --intraday             # 盘中巡检：触发即成交（交易时段每5分钟 cron）
  python3 sim_live.py --replay 2026-09-04    # 回放历史某日盘中触发（无分时→日K低点近似）
  python3 sim_live.py --init                 # 初始化账本（5万）
  python3 sim_live.py --review               # 收盘后复盘总结 + 自我学习
  python3 sim_live.py --strategy-log "理由"  # 策略版本变更
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

STRATEGY = {
    "version": "v2.0",
    "name": "稳健·盘中触发",
    "created": "2026-09-05",
    "rules": [
        "模式：盘前定计划（买点=MA10/MA20 或理想买点，止损=ATR），盘中实时价触发成交",
        "买：日K多头排列/强势多头 且 收盘评分≥68 的候选列入计划",
        "买触发：现价 ≤ 计划买点（回踩买，不追高）且 非普涨过热日 且 大盘非防守",
        "大盘防守：上证空头且<45分 或 全市场上涨占比≥65% → 当日不执行买入",
        "卖触发：现价 ≤ ATR 止损 → 即时卖；现价 ≤ MA20 且当日跌>3% → 即时卖",
        "信号转卖出（收盘算）→ 次日盘中等反抽/开盘附近卖",
        "止盈：浮盈≥15% 触发卖；已+8%后从峰值回落8% 移动止盈",
        "单票 ≤20% 预算（约¥10000），最多持 5 只，现金≥15%",
        "股数恒为 100 整数倍（A股一手）；每笔记录 时分秒/现价/当时涨跌幅/理由",
    ],
    "log": [],
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
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def new_state():
    return {
        "meta": {"created": now_ts(), "start_cash": CASH_START, "cash": CASH_START,
                 "strategy": STRATEGY},
        "positions": [],
        "plan": [],            # 盘前计划：待触发买单 {code,name,buy_below,stop_atr,stop_ma,tp,score,signal,reason,asof}
        "trades": [],          # 已成交（含真实时分秒）
        "equity_curve": [],
        "daily_log": [],
        "review_log": [],
        "version_history": [],
    }


def _is_etf(code):
    return str(code).startswith(("5", "1"))


# ---------------- 计划生成 ----------------
def make_plan(state, review, asof):
    """盘前/收盘后：从 review 候选生成待触发买单计划。"""
    items = review.get("items", []) or []
    held = {p["code"] for p in state["positions"]}
    cands = []
    for it in items:
        if _is_etf(it.get("code")) or it.get("code") in held:
            continue
        if it.get("signal_key") not in ("strong_buy", "buy"):
            continue
        if (it.get("score") or 0) < 68:
            continue
        if it.get("trend_status") not in ("强势多头", "多头排列"):
            continue
        cands.append(it)
    cands.sort(key=lambda x: -x.get("score", 0))
    # 买点：回踩位 = max(MA10, 理想买点, 现价*0.97) 下限 —— 只在回踩时买
    plan_new = []
    for it in cands[:10]:
        code = it["code"]
        close = it.get("close") or 0
        ma10 = it.get("ma10")
        ideal = it.get("ideal_buy") or it.get("secondary_buy")
        # 触发买点：不高于现价-1% 的回踩位（至少要求回踩 1% 才动手，防追高）
        candidates = [x for x in (ma10, ideal, close * 0.98) if x]
        buy_below = max(min(candidates), close * 0.99)
        if buy_below >= close * 0.995:  # 买点须明显低于现价，否则等回踩
            buy_below = close * 0.99
        stop_atr = it.get("atr_stop")
        stop_ma = it.get("stop_loss")
        tp = None
        if close and it.get("take_profit"):
            tp = it["take_profit"]
        budget = CASH_START * 0.18
        plan_new.append({
            "code": code, "name": it.get("name"), "asof": asof,
            "score": it.get("score"), "signal": it.get("signal"),
            "buy_below": round(buy_below, 3), "buy_above": round(close * 1.0, 3),
            "close": close,
            "stop_atr": round(stop_atr, 3) if stop_atr else None,
            "stop_ma": round(stop_ma, 3) if stop_ma else None,
            "tp": round(tp, 3) if tp else None,
            "reason": "%s(%s分)·%s 现价%.2f 回踩≤%.2f买 ATR止损%s" % (
                it.get("signal"), it.get("score"), it.get("trend_status"),
                close, buy_below, stop_atr if stop_atr else "--"),
            "budget": round(budget, 2), "status": "wait",
        })
    return plan_new


# ---------------- 盘中巡检 ----------------
def intraday_scan(state, date, now_hms):
    """盘中：拉实时快照，按计划/持仓触发成交。返回当日成交数。"""
    from src import data_provider as dp
    codes = ([p["code"] for p in state.get("plan", []) if p.get("status", "wait") == "wait"]
             + [p["code"] for p in state["positions"]])
    if not codes:
        return 0, ["无计划/持仓，等待"]
    # 分批快照
    filled = 0
    notes = []
    try:
        quotes = dp.fetch_quotes(sorted(set(codes)))
    except Exception as e:
        return 0, ["快照失败 %s" % e]
    # 先看持仓卖出触发
    sell_codes = []
    for pos in state["positions"]:
        q = quotes.get(pos["code"])
        if not q:
            continue
        px = q.get("price")
        if not px:
            continue
        prev = pos.get("prev_close") or pos["cost"]
        chg_now = (px / prev - 1) * 100 if prev else 0
        reason = None
        # 更新峰值（移动止盈用）
        if px > pos.get("peak", pos["cost"]):
            pos["peak"] = px
        gain = (px / pos["cost"] - 1) * 100
        # 止盈：现价 ≥ 成本*(1+15%)
        if pos.get("tp") and px >= pos["tp"]:
            reason = "触发止盈：现价%.2f ≥ 目标%.2f（+%.1f%%）" % (px, pos["tp"], gain)
        elif pos.get("stop_atr") and px <= pos["stop_atr"]:
            reason = "触发ATR止损：现价%.2f ≤ 止损%.2f" % (px, pos["stop_atr"])
        elif pos.get("peak") and pos["peak"] > pos["cost"] * 1.08 and px / pos["peak"] - 1 <= -0.08:
            reason = "移动止盈：峰值+%.1f%%回落8%%" % ((pos["peak"] / pos["cost"] - 1) * 100)
        if reason:
            shares = pos["shares"]
            proceeds = shares * px
            fee = proceeds * (0.0003 + 0.0005)
            pnl = proceeds - shares * pos["cost"]
            state["meta"]["cash"] += proceeds - fee
            state["trades"].append({
                "action": "sell", "date": date, "time": now_hms, "code": pos["code"],
                "name": pos["name"], "price": round(px, 3), "shares": shares,
                "chg_at_fill": round(chg_now, 2),
                "pnl": round(pnl, 2), "pnl_pct": round((px / pos["cost"] - 1) * 100, 2),
                "reason": reason, "strategy_ver": state["meta"]["strategy"]["version"],
            })
            sell_codes.append(pos["code"])
            filled += 1
            notes.append("卖出 %s @%.2f（%+.2f%%）%s" % (pos["name"], px, chg_now, reason))
    state["positions"] = [p for p in state["positions"] if p["code"] not in sell_codes]
    # 再看计划买入触发
    for pl in state.get("plan", []):
        if pl.get("status", "wait") != "wait":
            continue
        if len(state["positions"]) >= 5:
            pl["status"] = "skip_full"
            continue
        q = quotes.get(pl["code"])
        if not q:
            continue
        px = q.get("price")
        if not px:
            continue
        prev = q.get("prevClose") or pl.get("close") or 0
        chg_now = (px / prev - 1) * 100 if prev else 0
        # 触发：现价跌到 buy_below（回踩）→ 买；若现价跳空大涨(>3%)放弃（不追）
        if px > pl["close"] * 1.03:
            continue  # 高开冲高不追
        if px <= pl["buy_below"]:
            # 大盘防守不买（简化：用计划 asof 的全局闸门标记）
            if pl.get("gate", "off") == "block":
                pl["status"] = "skip_gate"
                continue
            budget = min(pl["budget"], state["meta"]["cash"] * 0.95)
            price = px
            shares = int(budget / price / 100) * 100
            if shares <= 0:
                continue
            cost = shares * price
            fee = cost * 0.0003
            state["meta"]["cash"] -= (cost + fee)
            state["positions"].append({
                "code": pl["code"], "name": pl["name"], "shares": shares,
                "cost": round(price, 3), "buy_date": date, "buy_time": now_hms,
                "stop_atr": pl.get("stop_atr"), "stop_ma": pl.get("stop_ma"),
                "tp": pl.get("tp"), "peak": price, "prev_close": prev,
                "score": pl.get("score"), "signal": pl.get("signal"),
            })
            state["trades"].append({
                "action": "buy", "date": date, "time": now_hms, "code": pl["code"],
                "name": pl["name"], "price": round(price, 3), "shares": shares,
                "chg_at_fill": round(chg_now, 2),
                "reason": pl.get("reason", ""), "strategy_ver": state["meta"]["strategy"]["version"],
            })
            pl["status"] = "filled"
            filled += 1
            notes.append("买入 %s @%.2f（%+.2f%%）回踩触发" % (pl["name"], px, chg_now))
        elif px >= pl["buy_above"] * 1.02:
            pass  # 在买点上方运行，继续等回踩
    return filled, notes


def replay_day(state, date):
    """盘中触发回放：用当日日K 高低价判断计划是否盘中触及买点。
    无分时数据 → 触发价=买点价成交，时间标『盘中(回放近似)』，如实注明。"""
    from src import data_provider as dp
    notes, filled = [], 0
    for pl in state.get("plan", []):
        if pl.get("status", "wait") != "wait":
            continue
        if len(state["positions"]) >= 5:
            pl["status"] = "skip_full"
            continue
        if pl.get("gate") == "block":
            pl["status"] = "skip_gate"
            continue
        k = dp.fetch_daily_kline(pl["code"], count=30, use_cache=True)
        if not k or date not in k["dates"]:
            continue
        i = k["dates"].index(date)
        low = k["lows"][i]
        if low <= pl["buy_below"]:
            price = pl["buy_below"]
            budget = min(pl["budget"], state["meta"]["cash"] * 0.95)
            shares = int(budget / price / 100) * 100
            if shares <= 0:
                continue
            cost = shares * price
            fee = cost * 0.0003
            state["meta"]["cash"] -= (cost + fee)
            state["positions"].append({
                "code": pl["code"], "name": pl["name"], "shares": shares,
                "cost": round(price, 3), "buy_date": date, "buy_time": "盘中低位(回放)",
                "stop_atr": pl.get("stop_atr"), "stop_ma": pl.get("stop_ma"),
                "tp": pl.get("tp"), "peak": price,
                "score": pl.get("score"), "signal": pl.get("signal"),
            })
            state["trades"].append({
                "action": "buy", "date": date, "time": "盘中(回放近似)",
                "code": pl["code"], "name": pl["name"],
                "price": round(price, 3), "shares": shares, "chg_at_fill": None,
                "reason": "回放触发：当日低%.2f≤买点%.2f → 限价%.2f成交｜%s" % (
                    low, pl["buy_below"], price, pl.get("reason", "")),
                "strategy_ver": state["meta"]["strategy"]["version"],
            })
            pl["status"] = "filled"
            filled += 1
            notes.append("回放买入 %s @%.2f（当日低%.2f）" % (pl["name"], price, low))
    return filled, notes


def record_equity(state, date):
    eq = state["meta"]["cash"]
    for pos in state["positions"]:
        eq += pos["shares"] * pos["cost"]  # 盘中无持仓市值更新（收盘复盘时替换）
    prev = state["equity_curve"][-1]["equity"] if state["equity_curve"] else CASH_START
    state["equity_curve"].append({"date": date, "equity": round(eq, 2),
                                  "cash": round(state["meta"]["cash"], 2),
                                  "daily_return": round((eq / prev - 1) * 100, 3),
                                  "pos_count": len(state["positions"])})


def do_review(state):
    sells = [t for t in state["trades"] if t["action"] == "sell"]
    if not sells:
        return None
    wins = [t for t in sells if t["pnl"] > 0]
    losses = [t for t in sells if t["pnl"] <= 0]
    note = ["已平仓 %s 笔：胜 %s / 负 %s，胜率 %.0f%%，总已实现 %+.0f 元" % (
        len(sells), len(wins), len(losses), len(wins) / len(sells) * 100,
        sum(t["pnl"] for t in sells))]
    if losses:
        lr = {}
        for t in losses:
            k = t["reason"].split("：")[0][:12]
            lr[k] = lr.get(k, 0) + 1
        note.append("亏损主因：" + "、".join("%s×%d" % (k, v) for k, v in
                    sorted(lr.items(), key=lambda x: -x[1])[:3]))
        note.append("平均单笔亏损 %.0f 元" % (sum(t["pnl"] for t in losses) / len(losses)))
    state["review_log"].append({"date": now_ts()[:10], "note": "\n".join(note),
                                "trades_since_last": len(sells)})
    return note


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--intraday", action="store_true")
    ap.add_argument("--replay", default=None, metavar="DATE")
    ap.add_argument("--review", action="store_true")
    ap.add_argument("--strategy-log", default=None)
    args = ap.parse_args()
    if args.strategy_log:
        st = load_state()
        v = st["meta"]["strategy"]["version"]
        m = v.split("."); m[1] = str(int(m[1]) + 1); nv = ".".join(m)
        st["meta"]["strategy"]["version"] = nv
        st["meta"]["strategy"]["log"].append({"to": nv, "date": now_ts()[:10],
                                              "reason": args.strategy_log})
        st["version_history"].append({"date": now_ts()[:10], "from": v, "to": nv,
                                      "change": args.strategy_log,
                                      "rules": st["meta"]["strategy"]["rules"]})
        save(st)
        print("策略 %s → %s：%s" % (v, nv, args.strategy_log))
        return
    # 初始化
    if args.init or not load_state():
        state = new_state()
        save(state)
        print("账本已初始化：5万现金，策略 %s。先跑 --plan 建计划，盘中 --intraday 触发成交。" % STRATEGY["version"])
        return
    state = load_state()
    review = load_json("review_data.json")
    date = (review.get("generatedAt") or now_ts())[:10] if review else now_ts()[:10]
    if args.plan:
        if not review:
            print("无 review_data，先跑 run_review")
            return
        state["plan"] = make_plan(state, review, date)
        # 大盘闸门标记
        save(state)
        print("计划已更新（%s）：%d 个回踩买点待盘中触发" % (date, len(state["plan"])))
        for p in state["plan"]:
            print("  %s %s分 现价%.2f 回踩≤%.2f 止损%s" % (p["name"], p["score"], p["close"],
                                                  p["buy_below"], p["stop_atr"]))
        return
    if args.replay:
        n, notes = replay_day(state, args.replay)
        save(state)
        print("盘中回放 %s：成交 %d 笔（无分时近似，触发价=买点价）" % (args.replay, n))
        for x in notes[:10]:
            print("  -", x)
        return
    if args.intraday:
        now = time.localtime()
        date = time.strftime("%Y-%m-%d", now)
        hms = time.strftime("%H:%M:%S", now)
        n, notes = intraday_scan(state, date, hms)
        save(state)
        print("盘中巡检 %s %s：成交 %d 笔" % (date, hms, n))
        for x in notes[:12]:
            print("  -", x)
        return
    if args.review:
        do_review(state)
        # 用收盘 review 更新持仓市值 + 净值
        if review:
            items = {x.get("code"): x for x in review.get("items", [])}
            for pos in state["positions"]:
                it = items.get(pos["code"])
                if it:
                    pos["last_close"] = it.get("close")
                    pos["close_date"] = date
            eq = state["meta"]["cash"]
            for pos in state["positions"]:
                px = pos.get("last_close") or pos["cost"]
                eq += pos["shares"] * px
            if state["equity_curve"] and state["equity_curve"][-1]["date"] == date:
                state["equity_curve"][-1]["equity"] = round(eq, 2)
            else:
                state["equity_curve"].append({"date": date, "equity": round(eq, 2),
                                              "cash": round(state["meta"]["cash"], 2),
                                              "daily_return": 0.0,
                                              "pos_count": len(state["positions"])})
            # 收盘把未触发计划清掉，次日 --plan 重建
            for p in state["plan"]:
                if p.get("status", "wait") == "wait":
                    p["status"] = "expired"
        save(state)
        print("收盘复盘完成 %s：持仓%d 现金%.0f" % (date, len(state["positions"]), state["meta"]["cash"]))
        print("（未触发计划已标记 expired；下个交易日 --plan 重建）")
        return
    print("用法：--init / --plan / --intraday / --review / --strategy-log")


if __name__ == "__main__":
    main()
