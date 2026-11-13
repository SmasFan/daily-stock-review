#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""策略对决回测：一周冲刺 vs 实时模拟盘三策略(激进/稳健/纪律) vs 基准

统一引擎、同资金 5 万、同池(复盘池个股)、同时间窗口(默认近3月全历史)。
每日用前一日收盘信号选股，次日按各自规则成交(日K 高/低/收近似)，
收盘按收盘价估值，滚动净值曲线。公平对比谁在相同行情下赚得多/亏得少。

策略实现:
  冲刺(sprint):  开盘市价买 top1 强票(score+动量), 集中≤2, 单笔50%,
                  目标+18%止盈 / 峰值≥+8%回落6%移动止盈 / -6.5%硬止损
  激进(agg):     回踩买点 buy_below=close×(1-0.5%), 门槛66, 最多5只22%仓
                  峰值≥+8%回落10%移动止盈 / +20%止盈 / ATR止损
  稳健(bal):     回踩 close×(1-2%), 门槛68, 5只18%, +15%止盈, ATR/MA20止损
  纪律(dis):     回踩 close×(1-3%), 门槛76, 4只14%, +10%止盈/保本+5%, ATR止损
  基准(bench):   自选池等权(每次调仓全池平均)

用法: python3 scripts/backtest_sprint.py --compare --days 90
输出: 控制台报告 + data/sprint_vs_strategies.json (含每日净值曲线)
"""
import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "src"))

import sim_live as sl

START_CASH = 50000.0
DATA_DIR = os.path.join(BASE, "data")
OUT = os.path.join(DATA_DIR, "sprint_vs_strategies.json")

# 策略参数（与 sim_live.ACCOUNTS / sim_sprint.RULE 对齐）
CFG = {
    "sprint": {"max_pos": 2, "frac": 0.5, "buy_bias": 0.0, "min_score": 66,
               "tp": 0.18, "stop": -0.065, "trail_peak": 0.08, "trail_drop": 0.06,
               "stop_mode": "hard", "be": None},
    "aggressive": {"max_pos": 5, "frac": 0.22, "buy_bias": 0.005, "min_score": 66,
                   "tp": 0.20, "stop": None, "trail_peak": 0.08, "trail_drop": 0.10,
                   "stop_mode": "atr", "be": None},
    "balanced": {"max_pos": 5, "frac": 0.18, "buy_bias": 0.02, "min_score": 68,
                 "tp": 0.15, "stop": None, "trail_peak": None, "trail_drop": None,
                 "stop_mode": "atr", "be": None},
    "disciplined": {"max_pos": 4, "frac": 0.14, "buy_bias": 0.03, "min_score": 76,
                    "tp": 0.10, "stop": None, "trail_peak": None, "trail_drop": None,
                    "stop_mode": "atr", "be": 0.05},
    # v4.0：环境自适应（基准=稳健参数，每日按 bull/chop/bear/panic 覆盖仓位/门槛/回踩/止盈）
    "adaptive": {"max_pos": 5, "frac": 0.18, "buy_bias": 0.02, "min_score": 68,
                 "tp": 0.15, "stop": None, "trail_peak": None, "trail_drop": None,
                 "stop_mode": "atr", "be": None, "regime_aware": True},
    # v4.1：组合层面自适应（不看大盘方向，看自己赚不赚钱 + 波动目标化）
    "adaptive2": {"max_pos": 5, "frac": 0.18, "buy_bias": 0.02, "min_score": 68,
                  "tp": 0.15, "stop": None, "trail_peak": None, "trail_drop": None,
                  "stop_mode": "atr", "be": None, "portfolio_aware": True,
                  "vol_target": 2.0},
}
STRAT_LABEL = {"sprint": "一周冲刺", "aggressive": "模拟·激进", "balanced": "模拟·稳健",
               "disciplined": "模拟·纪律", "adaptive": "模拟·自适应",
               "adaptive2": "模拟·组合自适应"}

# ---- 环境自适应（与 sim_live.market_regime 同口径）----
RG_PARAMS = {
    "bull":  {"pos": 1.00, "score": 0,  "bias": 0.000, "tp": 1.00, "new": True},
    "chop":  {"pos": 0.70, "score": 4,  "bias": 0.010, "tp": 0.70, "new": True},
    "bear":  {"pos": 0.30, "score": 10, "bias": 0.015, "tp": 0.60, "new": True},
    "panic": {"pos": 0.00, "score": 99, "bias": 0.020, "tp": 0.50, "new": False},
}


def _idx_metrics_at(k, date):
    from bisect import bisect_right
    d = k.get("dates") or []
    c = k.get("closes") or []
    h = k.get("highs") or []
    l = k.get("lows") or []
    i = bisect_right(d, date) - 1
    if i < 25 or i >= len(c) or i >= len(d):
        return {}
    ma20 = sum(c[i - 19:i + 1]) / 20.0
    trs = [max(h[j] - l[j], abs(h[j] - c[j - 1]), abs(l[j] - c[j - 1]))
           for j in range(i - 19, i + 1)]
    return {"mom20": (c[i] / c[i - 20] - 1) * 100, "dev20": (c[i] / ma20 - 1) * 100,
            "atr20": (sum(trs) / len(trs)) / c[i] * 100 if c[i] else 0.0}


def portfolio_regime(curve, trades):
    """组合层面环境：看自己近 20 日回撤与连续亏损，不看大盘方向。
    理由：120 天回测显示「大盘弱→减仓」会削掉逆势选强势股的 alpha。
    """
    if len(curve) < 21:
        return {"pos": 1.0, "score": 0, "new": True, "why": "样本不足"}
    eq = [c["equity"] for c in curve[-21:]]
    peak = max(eq)
    dd = (peak - eq[-1]) / peak * 100 if peak else 0.0
    sells = [t for t in trades if t.get("action") == "sell"]
    streak = 0
    for t in reversed(sells):
        if (t.get("pnl") or 0) < 0:
            streak += 1
        else:
            break
    if dd >= 12 or streak >= 5:
        return {"pos": 0.0, "score": 99, "new": False,
                "why": "近20日回撤%.1f%%/连亏%d笔→暂停开仓" % (dd, streak)}
    if dd >= 6:
        return {"pos": 0.60, "score": 4, "new": True,
                "why": "近20日回撤%.1f%%→降仓" % dd}
    return {"pos": 1.0, "score": 0, "new": True, "why": "组合健康"}


def regime_at(idx, date):
    """用 date 及之前的指数 K 线判定环境（bull/chop/bear/panic）。"""
    sh = _idx_metrics_at(idx.get("sh") or {}, date)
    cyb = _idx_metrics_at(idx.get("cyb") or {}, date)
    if not sh:
        return "chop"
    mom, dev = sh["mom20"], sh["dev20"]
    atr = cyb.get("atr20")
    if atr is not None and atr >= 3.5 and mom <= -2.0:
        return "panic"
    if mom <= -2.5 or (cyb.get("mom20") is not None and cyb["mom20"] <= -6.0 and dev < -1.0):
        return "bear"
    if mom >= 2.0 and dev > 0.5 and (atr or 0) < 3.0:
        return "bull"
    return "chop"


def analyze_on(code, name, date):
    return sl.review_from_kline(code, name, date)


def run_compare(start_date, end_date, pool):
    """pool: [(code,name)]"""
    print("预载K线 %d 只..." % len(pool), flush=True)
    klines = {}
    for code, name in pool:
        try:
            k = sl.load_json("") or None
        except Exception:
            pass
        from src import data_provider as dp
        try:
            kk = dp.fetch_daily_kline_long(code, count=400, min_days=120, use_cache=True)
            if kk and len(kk["dates"]) > 40:
                klines[code] = (name, kk)
        except Exception:
            pass
    print("可用 %d 只" % len(klines), flush=True)
    # 指数（供环境自适应判定）
    print("预载指数...", flush=True)
    idx = {}
    from src import data_provider as dp_i
    for key, code in (("sh", "sh000001"), ("cyb", "sz399006")):
        try:
            kk = dp_i.fetch_daily_kline_long(code, count=400, min_days=150, use_cache=True)
            if kk and len(kk.get("dates") or []) > 40:
                idx[key] = kk
        except Exception as e:
            print("  指数 %s 失败: %s" % (code, e), flush=True)
    print("指数 %d 条" % len(idx), flush=True)
    # 交易日历
    all_dates = set()
    for _, k in klines.values():
        for d in k["dates"]:
            if start_date <= d <= end_date:
                all_dates.add(d)
    days = sorted(all_dates)
    print("交易日 %d 天 %s~%s" % (len(days), days[0], days[-1]), flush=True)

    # 每策略一个账户
    accts = {}
    for sk in CFG:
        accts[sk] = {"cash": START_CASH, "positions": [], "trades": [],
                     "curve": [], "buy_px": {}}

    sig_cache = {}
    def sigs(prev, held):
        key = prev
        if key in sig_cache:
            return [(it, code, name) for it, code, name in sig_cache[key]
                    if code not in held]
        res = []
        for code, (name, k) in klines.items():
            if prev not in k["dates"]:
                continue
            try:
                it = analyze_on(code, name, prev)
            except Exception:
                continue
            if not it or it.get("signal_key") not in ("strong_buy", "buy"):
                continue
            if it.get("trend_status") not in ("强势多头", "多头排列"):
                continue
            if (it.get("score") or 0) < 66:
                continue
            res.append((it, code, name))
        sig_cache[key] = res
        return [(it, code, name) for it, code, name in res if code not in held]

    for di, date in enumerate(days):
        prev = days[di - 1] if di > 0 else None
        rg = regime_at(idx, prev or date) if idx else "chop"

        def _dcfg(sk, acc=None):
            base = CFG[sk]
            cfg = dict(base)
            if base.get("regime_aware"):
                rp = RG_PARAMS.get(rg) or RG_PARAMS["chop"]
                cfg["frac"] = base["frac"] * rp["pos"]
                cfg["min_score"] = base["min_score"] + rp["score"]
                cfg["buy_bias"] = base["buy_bias"] + rp["bias"]
                cfg["tp"] = base["tp"] * rp["tp"]
                cfg["_allow_new"] = rp["new"]
            if base.get("portfolio_aware") and acc is not None:
                pr = portfolio_regime(acc["curve"], acc["trades"])
                atr = (_idx_metrics_at(idx.get("cyb") or {}, date) or {}).get("atr20") or 2.0
                vt = base.get("vol_target") or 2.0
                vol_pos = max(0.4, min(1.0, vt / atr)) if atr else 1.0
                cfg["frac"] = base["frac"] * pr["pos"] * vol_pos
                cfg["min_score"] = base["min_score"] + pr["score"]
                cfg["_allow_new"] = pr["new"]
                cfg["_why"] = "%s · ATR%.1f%%→仓×%.2f" % (pr["why"], atr, vol_pos)
            return cfg

        # ---------- 盘前选股（前日信号，全池每日算一次共享） ----------
        for sk in CFG:
            acc = accts[sk]; cfg = _dcfg(sk, acc)
            if cfg.get("_allow_new") is False:
                continue
            if len(acc["positions"]) >= cfg["max_pos"] or not prev:
                continue
            held = {p["code"] for p in acc["positions"]}
            cands = sigs(prev, held)
            if not cands:
                continue
            # 排序：score + 动量（sprint 更激进追强）
            cands.sort(key=lambda x: (x[0].get("score") or 0) +
                       min(x[0].get("change_60d") or 0, 30) / 8, reverse=True)
            cands = [c for c in cands if (c[0].get("score") or 0) >= cfg["min_score"]]
            if not cands:
                continue
            it, code, name = cands[0]
            k = klines[code][1]
            if date not in k["dates"]:
                continue
            oi = k["dates"].index(date)
            px = k["opens"][oi]
            pc = k["closes"][k["dates"].index(prev)] if prev in k["dates"] else px
            # 涨停近似不可买（开盘>9.5%昨收）
            if pc > 0 and px / pc - 1 > 0.095:
                continue
            if px <= 0:
                continue
            # 买入价：sprint 市价开盘；回踩策略按规则 (现价≤回踩点才买 → 用开盘价近似低吸条件)
            #   简化：回踩策略要求 当日最低 ≤ 回踩点，且开盘不高开>3% → 用 min(open, buy_below) 成交近似
            buy_px = px
            close_ref = it.get("close") or pc
            bias = cfg["buy_bias"]
            if bias > 0:
                buy_line = close_ref * (1 - bias)
                lo = k["lows"][oi]
                # 高开>3%不追
                if px > close_ref * 1.03:
                    continue
                if lo > buy_line:
                    continue
                buy_px = min(px, buy_line) if px >= buy_line else px
                buy_px = max(buy_px, lo)  # 不可能低于当日最低
            budget = min(acc["cash"] * cfg["frac"], acc["cash"] * 0.98)
            shares = int(budget / buy_px / 100) * 100
            if shares <= 0:
                continue
            cost_v = shares * buy_px
            fee = cost_v * 0.0003
            if cost_v + fee > acc["cash"]:
                continue
            acc["cash"] -= cost_v + fee
            acc["positions"].append({"code": code, "name": name, "shares": shares,
                                     "cost": buy_px, "peak": buy_px,
                                     "enter": date, "score": it.get("score"),
                                     "atr_stop": it.get("atr_stop")})
            acc["trades"].append({"action": "buy", "date": date, "code": code,
                                  "name": name, "price": buy_px, "shares": shares,
                                  "strat": sk})

        # ---------- 卖出判断 ----------
        for sk in CFG:
            acc = accts[sk]; cfg = _dcfg(sk, acc)
            for pos in list(acc["positions"]):
                code = pos["code"]
                k = klines.get(code, (None, None))[1]
                if not k or date not in k["dates"]:
                    continue
                i = k["dates"].index(date)
                hi, lo, cl = k["highs"][i], k["lows"][i], k["closes"][i]
                cost = pos["cost"]
                reason = None; px = None
                # 目标止盈
                if cfg["tp"] and hi >= cost * (1 + cfg["tp"]):
                    px = cost * (1 + cfg["tp"]); reason = "止盈+%.0f%%" % (cfg["tp"] * 100)
                # 保本(纪律): 曾+5% 后跌回成本附近 → 简化：峰值>+5%后当日最低≤成本*1.002
                if not reason and cfg.get("be") and pos["peak"] >= cost * (1 + cfg["be"]) \
                        and lo <= cost * 1.002:
                    px = cost * 1.002; reason = "保本锁定"
                # ATR 止损
                if not reason and cfg["stop_mode"] == "atr" and pos.get("atr_stop") and lo <= pos["atr_stop"]:
                    px = pos["atr_stop"]; reason = "ATR止损"
                # 硬止损
                if not reason and cfg.get("stop") and lo <= cost * (1 + cfg["stop"]):
                    px = cost * (1 + cfg["stop"]); reason = "硬止损"
                # 移动止盈
                if not reason and cfg.get("trail_peak") and pos["peak"] >= cost * (1 + cfg["trail_peak"]) \
                        and pos["peak"] * (1 - cfg["trail_drop"]) >= lo:
                    px = max(lo, cost * 1.001); reason = "移动止盈"
                if cl > pos["peak"]:
                    pos["peak"] = cl
                if reason:
                    proceeds = pos["shares"] * px
                    fee = proceeds * 0.0008
                    pnl = proceeds - fee - pos["shares"] * cost
                    acc["cash"] += proceeds - fee
                    acc["trades"].append({"action": "sell", "date": date, "code": code,
                                          "name": pos["name"], "price": px,
                                          "shares": pos["shares"],
                                          "pnl": round(pnl, 2),
                                          "pnl_pct": round((px / cost - 1) * 100, 2),
                                          "reason": reason, "strat": sk})
                    acc["positions"] = [p for p in acc["positions"] if p["code"] != code]

        # ---------- 收盘净值 ----------
        for sk in CFG:
            acc = accts[sk]
            eq = acc["cash"]
            for pos in acc["positions"]:
                k = klines.get(pos["code"], (None, None))[1]
                if not k or date not in k["dates"]:
                    eq += pos["shares"] * pos["cost"]
                    continue
                i = k["dates"].index(date)
                eq += pos["shares"] * k["closes"][i]
            acc["curve"].append({"date": date, "equity": round(eq, 2)})

    # ---------- 汇总对比 ----------
    print("\n======== 策略对决回测 %s ~ %s ========" % (days[0], days[-1]))
    out = {"window": [days[0], days[-1]], "start": START_CASH, "strategies": {}}
    rows = []
    for sk in CFG:
        acc = accts[sk]
        curve = acc["curve"]
        final = curve[-1]["equity"] if curve else START_CASH
        sells = [t for t in acc["trades"] if t["action"] == "sell"]
        wins = sum(1 for t in sells if (t.get("pnl") or 0) > 0)
        tot_pnl = sum(t.get("pnl") or 0 for t in sells)
        peak = START_CASH; mdd = 0
        for c in curve:
            peak = max(peak, c["equity"])
            mdd = max(mdd, (peak - c["equity"]) / peak)
        ret = (final / START_CASH - 1) * 100
        # 周统计：取每周最后一个净值
        weekly = {}
        for c in curve:
            wk = c["date"][:7] + "-W" + str((int(c["date"][8:10]) - 1) // 7 + 1)
            weekly.setdefault(wk, []).append(c["equity"])
        wk_last = {wk: v[-1] for wk, v in weekly.items()}
        rows.append((sk, ret, final, mdd * 100, len(sells), wins, len(sells) - wins, tot_pnl))
        out["strategies"][sk] = {
            "label": STRAT_LABEL[sk], "final": round(final, 2), "ret": round(ret, 2),
            "mdd": round(mdd * 100, 2), "trades": len(sells),
            "win": wins, "loss": len(sells) - wins,
            "pnl": round(tot_pnl, 0), "curve": curve, "weekly": wk_last}
    rows.sort(key=lambda x: -x[1])
    print("%-10s %10s %9s %8s %6s %7s %9s" % ("策略", "期末净值", "总收益", "最大回撤", "交易", "胜/负", "已实现"))
    for sk, ret, final, mdd, ns, w, l, pnl in rows:
        print("%-10s %10.0f %+8.2f%% %7.1f%% %5d %4d/%-3d %+9.0f" %
              (STRAT_LABEL[sk], final, ret, mdd, ns, w, l, pnl))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("\n已存 %s" % OUT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--start", default=None)
    args = ap.parse_args()
    end = time.strftime("%Y-%m-%d")
    start = args.start or time.strftime("%Y-%m-%d", time.localtime(time.time() - args.days * 86400))
    try:
        r = json.load(open(os.path.join(DATA_DIR, "review_data.json")))
        pool = [(x["code"], x["name"]) for x in r.get("items", [])
                if str(x.get("code", "")).isdigit()
                and not str(x["code"]).startswith(("5", "1"))]
    except Exception:
        pool = []
    print("池 %d 只, %s ~ %s" % (len(pool), start, end), flush=True)
    run_compare(start, end, pool)


if __name__ == "__main__":
    main()
