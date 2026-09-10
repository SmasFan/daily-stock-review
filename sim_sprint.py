#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一周冲刺模拟盘（sim_sprint）· 单账户激进短线

目标：一周 ≥ +10%（尽力而为，市场不保证）。
玩法：AI 主动选股重仓（市价进），cron 每2分钟（sim_intraday_scan.sh）自动 scan 执行
      移动止盈/目标止盈/硬止损；AI 每日收盘后主动调仓。

规则（冲刺激进档）：
  - 单笔 ≤ 现金 50%，最多 2 只并行（集中）
  - 买：市价现价成交（不等回踩），记真实时分秒
  - 卖：峰值 ≥+8% 后回落 6% → 移动止盈锁定；单票 +18% → 目标止盈；
       现价 ≤ 成本 -6.5% → 硬止损
命令：
  python3 sim_sprint.py --init
  python3 sim_sprint.py --buy 601138 [frac=0.5]     # 市价买（现金×frac）
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


def do_buy(st, code, frac):
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
                         "shares": shares, "reason": "AI冲刺建仓"})
    print("买入 %s %s股 @%.2f 现金剩余%.0f" % (code, shares, px, st["cash"]))


def do_sell(st, code, reason, px=None):
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
                         "pnl_pct": round(pnl_pct, 2), "reason": reason})
    print("卖出 %s @%.2f (%+.2f%%) %s" % (pos["name"], px, pnl_pct, reason))


def do_scan(st):
    """巡检持仓：移动止盈/目标止盈/硬止损。"""
    if not st["positions"]:
        print("sprint 无持仓")
        return
    codes = [p["code"] for p in st["positions"]]
    quotes = _quote(codes)
    for pos in list(st["positions"]):
        q = quotes.get(pos["code"])
        if not q or not q.get("price"):
            continue
        px = q["price"]
        pos["last"] = px
        if px > pos["peak"]:
            pos["peak"] = px
        gain = (px / pos["cost"] - 1) * 100
        # 目标止盈
        if gain >= RULE["tp_pct"] * 100:
            do_sell(st, pos["code"], "目标止盈 +%.0f%%" % gain, px)
            continue
        # 硬止损
        if px <= pos["cost"] * (1 + RULE["hard_stop"]):
            do_sell(st, pos["code"], "硬止损 %.1f%%" % gain, px)
            continue
        # 移动止盈：曾 +8% 且从峰值回落 6%
        peak_gain = (pos["peak"] / pos["cost"] - 1) * 100
        if peak_gain >= RULE["trail_peak"] * 100:
            if pos["peak"] - px >= pos["peak"] * RULE["trail_drop"]:
                do_sell(st, pos["code"], "移动止盈(峰值%+.1f%%回落)" % peak_gain, px)
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
        do_buy(st, args.buy, args.frac)
        save(st)
        do_status(st)
        return
    if args.sell:
        do_sell(st, args.sell, "AI主动卖出")
        save(st)
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
