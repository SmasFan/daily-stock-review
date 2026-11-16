#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一多因素方向打分器（2026-11-17）

用户诉求（m03163）：不要按策略的「纯多/纯空」属性死扛，要统一多因素自由判断多空。

设计：5 个因素各投 -1/0/+1，按权重加权归一到 -1..1：
  f1 日线 20 根动量 > +2% / < -2%
  f2 1h 12 根动量 > +1% / < -1%
  f3 日线收盘价 vs 日线 SMA50（站上 / 跌破）
  f4 1h 现价 vs 1h SMA200（站上 / 跌破）
  f5 1h 现价偏离 SMA20 超买(-1) / 超跌(+1)，方向由 f5sign 决定

策略 = 权重配置（都能自由多空）：
  S2 趋势型 (1,0,2,2,0)  f5sign=+1  开仓 oth=0.65 离场 eth=0.15  sl=0.03
  S4 反转型 (0,0,0,0,3)  f5sign=-1  开仓 oth=0.65 离场 eth=0.25  sl=0.025
  S6 均衡型 (1,1,1,1,1)  f5sign=-1  开仓 oth=0.65 离场 eth=0.25  sl=0.02

来源：backtest/unified_sweep.py 96 组扫描 + backtest/unified_compare.py 三方案对比。
方案B（S1/S5 保留原参数，S2/S4/S6 换打分器）组合 +13.2%/MDD15.9%/样本外+15.6%，
优于现状 +11.2%/+13.3%；三策略胜率 S2 35%→70%、S6 38.5%→66%、S4 45.8%→52%。

注意：币种打分器与 backtest/unified_sweep.py 的 score() 严格同口径，改这里必须同步改回测。
"""
import os

DTH = float(os.environ.get('BN_DIR_DAY_TH', '0.02'))    # 日线动量阈值
HTH = float(os.environ.get('BN_DIR_HOUR_TH', '0.01'))   # 1h 动量阈值
DEV = float(os.environ.get('BN_DIR_DEV_TH', '0.03'))    # 偏离 SMA20 阈值

# sid -> (weights, open_th, exit_th, f5sign, sl)
SCORE_CFG = {
    'S2': ((1, 0, 2, 2, 0), 0.65, 0.15, 1, 0.03),
    'S4': ((0, 0, 0, 0, 3), 0.65, 0.25, -1, 0.025),
    'S6': ((1, 1, 1, 1, 1), 0.65, 0.25, -1, 0.02),
}


def sma(vals, w):
    if not vals or len(vals) < w:
        return None
    return sum(vals[-w:]) / w


def direction_score(ctx, w, f5sign=1):
    """算 -1..1 的方向净分。ctx 需含 c/d/price/ma20/ma200（与 arena_engine.fetch_ctx 一致）。"""
    c, d = ctx.get('c') or [], ctx.get('d') or []
    ma20, ma200, px = ctx.get('ma20'), ctx.get('ma200'), ctx.get('price')
    if len(d) < 51 or len(c) < 13 or px is None or ma20 is None or ma200 is None:
        return 0.0
    m1 = d[-1] / d[-21] - 1 if d[-21] > 0 else 0.0
    f1 = 1 if m1 > DTH else (-1 if m1 < -DTH else 0)
    m2 = px / c[-13] - 1 if c[-13] > 0 else 0.0
    f2 = 1 if m2 > HTH else (-1 if m2 < -HTH else 0)
    dma50 = sma(d, 50)
    f3 = 0 if dma50 is None else (1 if d[-1] > dma50 else -1)
    f4 = 1 if px > ma200 else -1
    dev = px / ma20 - 1 if ma20 > 0 else 0.0
    f5 = f5sign * (-1 if dev > DEV else (1 if dev < -DEV else 0))
    v = (f1, f2, f3, f4, f5)
    sw = sum(abs(x) for x in w)
    return sum(a * b for a, b in zip(w, v)) / sw if sw else 0.0


def make_score_signal(sid):
    """返回 (fn, sl)：fn(ctx, holding) -> 'long'/'short'/None。"""
    if sid not in SCORE_CFG:
        raise KeyError('unknown score strategy: %s' % sid)
    w, oth, eth, f5sign, sl = SCORE_CFG[sid]

    def fn(ctx, holding):
        sc = direction_score(ctx, w, f5sign)
        if holding == 'long':
            return 'long' if sc > eth else None
        if holding == 'short':
            return 'short' if sc < -eth else None
        if sc >= oth:
            return 'long'
        if sc <= -oth:
            return 'short'
        return None

    return fn, sl
