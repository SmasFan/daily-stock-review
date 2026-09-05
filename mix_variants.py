#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""混合策略变体对比（轻量：用 sim_trade_all 三策略日收益合成，无需重跑回测）。

方案:
  cur   动态 攻/守阈值 ±2.5%（现版）
  guard 只守不攻：斜率<-2.5 才降激进（守档）；上行一律衡 25/50/25（永不踏空）
  wide  阈值放宽 ±5%
  wide_guard 只守 + 阈值 -5%（最保守踏空最少）
对照: equal 等权 1/3
输出: 各窗口 dyn/等权 收益、回撤、dyn-等权。
"""
import json
import sys

DATA = "data/sim_trade_all.json"


def run_mix(strats, mode, th=2.5):
    """strats: {key: equity_curve[{date,daily_return,equity,bench?}]} 按窗口内日期对齐"""
    # 取三策略曲线（同一窗口同长度同日期）
    cur = {k: strats[k]["equity_curve"] for k in ("aggressive", "balanced", "disciplined")}
    n = len(cur["balanced"])
    if n == 0:
        return None
    dates = [p["date"] for p in cur["balanced"]]
    bench = [p.get("bench") for p in cur["balanced"]]
    # 权重函数
    def weights(slope):
        if mode == "cur":
            if slope > th:
                return (0.7, 0.2, 0.1)
            if slope < -th:
                return (0.1, 0.2, 0.7)
            return (0.25, 0.5, 0.25)
        if mode == "guard":      # 只守不攻：< -2.5 守，否则恒衡
            if slope < -2.5:
                return (0.1, 0.2, 0.7)
            return (0.25, 0.5, 0.25)
        if mode == "wide":       # 放宽 ±5 攻守
            if slope > 5:
                return (0.7, 0.2, 0.1)
            if slope < -5:
                return (0.1, 0.2, 0.7)
            return (0.25, 0.5, 0.25)
        if mode == "wide_guard": # 只守 + 宽阈值 -5
            if slope < -5:
                return (0.1, 0.2, 0.7)
            return (0.25, 0.5, 0.25)
        raise ValueError(mode)

    eq_dyn = eq_eq = 50000.0
    dyn_curve, eq_curve, regime = [], [], []
    for i in range(n):
        if i >= 21 and bench[i - 21]:
            slope = (bench[i - 1] / bench[i - 21] - 1) * 100  # 滞后1日
        else:
            slope = 0.0
        wa, wb, wd = weights(slope)
        lab = "攻" if wa >= 0.7 else ("守" if wd >= 0.7 else "衡")
        r_a = cur["aggressive"][i]["daily_return"]
        r_b = cur["balanced"][i]["daily_return"]
        r_d = cur["disciplined"][i]["daily_return"]
        rd = wa * r_a + wb * r_b + wd * r_d
        re_ = (r_a + r_b + r_d) / 3
        eq_dyn *= 1 + rd / 100
        eq_eq *= 1 + re_ / 100
        dyn_curve.append(eq_dyn)
        eq_curve.append(eq_eq)
        regime.append(lab)

    def summ(c):
        peak, mdd = -1e18, 0.0
        for v in c:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        return mdd * 100
    return {
        "dyn_ret": (eq_dyn / 50000 - 1) * 100,
        "eq_ret": (eq_eq / 50000 - 1) * 100,
        "dyn_mdd": summ(dyn_curve), "eq_mdd": summ(eq_curve),
        "regime": {r: regime.count(r) for r in ("攻", "衡", "守")},
        "dates": (dates[0], dates[-1]),
    }


MODES = [("cur", "现版±2.5"), ("guard", "只守不攻<-2.5"), ("wide", "放宽±5"),
         ("wide_guard", "只守+宽-5")]
WINS = [("m2", "近2月"), ("h1", "半年"), ("y2026", "今年"),
        ("y2025", "2025以来"), ("all", "全程"), ("bull", "政策牛")]

d = json.load(open(DATA))
print("%-8s %-6s" % ("窗口", "方案") + "".join("%10s" % x for x in
      ["dyn收益", "等权收益", "dyn-等权", "dyn回撤", "攻/衡/守"]))
for wk, wlab in WINS:
    w = d["windows"][wk]
    print("── %s ──" % wlab)
    for mode, mlabel in MODES:
        r = run_mix(w["strategies"], mode)
        reg = "/".join(str(r["regime"].get(k, 0)) for k in ("攻", "衡", "守"))
        print("%-8s %-6s %9.2f%% %9.2f%% %+9.2f %8.1f%% %12s" % (
            "", mlabel, r["dyn_ret"], r["eq_ret"], r["dyn_ret"] - r["eq_ret"],
            r["dyn_mdd"], reg))
    print()
