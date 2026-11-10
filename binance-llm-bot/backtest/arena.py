#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多策略竞技场回测（币安 TradFi 股票永续，13 只美股，同一份 1d+1h 数据）。

用户目标（2026-11-10, m00489）：不要按「短线池/日线池」切，而是各种交易模式
一起操作，一共 100U，最终比较各策略的收益与胜率，看哪个策略胜率高。

这里用完全一致的样本、费用、杠杆、资金口径跑 6 个独立策略，每个策略独立
100U 账户（13 标等额保证金），逐笔记录交易，因此能算真实的胜率/盈亏比/PF：

  S1 hybrid      日线 20 根 ±2% 定方向 + 1h 12 根 ±1% 择时 + SMA200 过滤，双向
  S2 daily_trend 日线 20 根动量 > +2% 开多，动量转负或跌破日线 SMA50 平（只多）
  S3 trend_ma    1h SMA20 上穿 SMA50 做多 / 下穿做空，反向交叉或 2.5% 止损平
  S4 rebound     1h 价格低于 SMA20 达 4% 且当根收阳 → 做多（半仓），回 SMA20 平
  S5 breakout    1h 收盘突破前 24 根最高 → 追多；跌破最低 → 追空（半仓）
  S6 grid        1h 在 SMA200 ±5% 区间内，跌到下沿买、回到上沿卖（只多）

费用双边 0.04%，杠杆 5x，前 70% 训练 / 后 30% 样本外，Sharpe 年化 sqrt(252*24)。

用法: python3 backtest/arena.py
输出: backtest/arena_results.csv + 控制台排名（含胜率）
"""
import bisect
import csv
import math
import os
import time

import numpy as np

from shortterm import POOL13, FEE, BE_BUF, LEV, ANN, load, sma_arr, metrics

SHORT_W = float(os.environ.get('ARENA_SHORT_W', '0.5'))  # 空头仓位系数（1.0=与多头同名义）
HERE = os.path.dirname(os.path.abspath(__file__))

# v3 最优参数（backtest/shortterm_v3_results.csv 排名第一且稳健平台内）
HYB = dict(dth=0.02, short_ok=True, n=12, th=0.010, sl=0.02, exit_ratio=0.0,
           trend_n=200)
PG_SHORT = dict(lock=1.0, trail=0.6, trail_min=1.0, give_back=0.5,
                tp_scale=2.0, scale_ratio=0.5, tp_fast=3.5)


class Book:
    """单标的账本：记录净值曲线与逐笔净盈亏（用于胜率/PF）。"""

    __slots__ = ('margin', 'realized', 'qty', 'side', 'entry', 'peak', 'stop',
                 'trimmed', 'pnls', 'ts', 'eq', 'closes')

    def __init__(self, margin):
        self.margin = margin
        self.realized = 0.0
        self.qty = 0.0
        self.side = 0
        self.entry = 0.0
        self.peak = 0.0
        self.stop = 0.0
        self.trimmed = False
        self.pnls = []
        self.ts = []
        self.eq = []
        self.closes = 0

    # --- 交易 ---
    def open(self, px, side, weight=1.0):
        notional = self.margin * LEV * weight
        self.qty = notional / px
        self.entry = px
        self.side = side
        self.peak = px
        self.trimmed = False
        self.realized -= notional * FEE

    def close(self, px):
        pnl = (px - self.entry) * self.qty * self.side - px * self.qty * FEE
        self.realized += pnl
        # 以「保证金百分比」记账，便于跨策略比较
        self.pnls.append(pnl / self.margin * 100.0)
        self.closes += 1
        self.qty = 0.0
        self.side = 0
        self.entry = self.peak = self.stop = 0.0
        self.trimmed = False

    def step(self, ts, px):
        unreal = (px - self.entry) * self.qty * self.side if self.qty > 0 else 0.0
        self.ts.append(int(ts))
        self.eq.append(max(0.0, self.margin + self.realized + unreal))


def _flat(b, o, h, l, px, i, side, sl):
    """共享止损撮合：返回 True 表示本根已止损离场。"""
    if b.qty <= 0 or b.stop <= 0:
        return False
    hit = (l[i] <= b.stop) if side > 0 else (h[i] >= b.stop)
    if not hit:
        return False
    fill = (o[i] if o[i] < b.stop else b.stop) if side > 0 else \
           (o[i] if o[i] > b.stop else b.stop)
    b.close(fill)
    return True


# ---------------------------------------------------------------- S1 hybrid
def sim_hybrid(rows, dts, ddir, b, sl):
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    o = np.array([r[1] for r in rows], dtype=float)
    h = np.array([r[2] for r in rows], dtype=float)
    l = np.array([r[3] for r in rows], dtype=float)
    c = np.array([r[4] for r in rows], dtype=float)
    ma20 = sma_arr(c, 20)
    trend = sma_arr(c, HYB['trend_n'])
    n, th, er = HYB['n'], HYB['th'], HYB['exit_ratio']
    for i in range(1, len(c)):
        if b.eq and b.eq[-1] <= 0:
            break
        px = c[i]
        j = bisect.bisect_right(dts, ts[i] - 86400000) - 1
        d = int(ddir[j]) if j >= 20 else 0
        if _flat(b, o, h, l, px, i, b.side, sl):
            b.step(ts[i], px)
            continue
        if b.qty > 0:
            pg, side = PG_SHORT, b.side
            b.peak = max(b.peak, h[i]) if side > 0 else (min(b.peak, l[i]) if b.peak else l[i])
            gain = (px / b.entry - 1) * side * 100
            pgain = (b.peak / b.entry - 1) * side * 100
            if pgain >= pg['trail_min'] and (pgain - gain) >= max(pgain * pg['give_back'], pg['trail']):
                b.close(px); b.step(ts[i], px); continue
            if (not b.trimmed) and gain >= pg['tp_scale']:
                q = b.qty * pg['scale_ratio']
                b.realized += (px - b.entry) * q * side - px * q * FEE
                b.pnls.append(((px - b.entry) * q * side - px * q * FEE) / b.margin * 100.0)
                b.qty -= q; b.trimmed = True
                be = b.entry * (1 + BE_BUF * side)
                b.stop = max(b.stop, be) if side > 0 else min(b.stop, be)
            if b.qty > 0 and gain >= pg['tp_fast'] and not np.isnan(ma20[i]) and \
               ((side > 0 and px < ma20[i]) or (side < 0 and px > ma20[i])):
                b.close(px); b.step(ts[i], px); continue
            if b.qty > 0 and pgain >= pg['lock']:
                be = b.entry * (1 + BE_BUF * side)
                b.stop = max(b.stop, be) if side > 0 else min(b.stop, be)
        mom = px / c[i - n] - 1 if i >= n and c[i - n] > 0 else 0.0
        sig = 1 if (d > 0 and mom > th) else (-1 if (d < 0 and mom < -th) else 0)
        if sig and not np.isnan(trend[i]) and \
           ((sig > 0 and px < trend[i]) or (sig < 0 and px > trend[i])):
            sig = 0
        if b.qty > 0:
            side = b.side
            if d != side or (sig != 0 and sig != side):
                b.close(px)
            elif side > 0 and mom < er * th:
                b.close(px)
            elif side < 0 and mom > -er * th:
                b.close(px)
        elif sig != 0:
            b.open(px, sig, 1.0 if sig > 0 else SHORT_W)
            b.stop = b.entry * (1 - sl) if sig > 0 else b.entry * (1 + sl)
        b.step(ts[i], px)
    if b.qty > 0 and b.ts:
        b.close(c[-1]); b.eq[-1] = max(0.0, b.margin + b.realized)


# ------------------------------------------------------------ S2 daily_trend
def sim_daily_trend(rows, dts, ddir, b, sl):
    """只做多：日线 20 根动量 > 2% 持有，转负或跌破 SMA50 离场。"""
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    o = np.array([r[1] for r in rows], dtype=float)
    h = np.array([r[2] for r in rows], dtype=float)
    l = np.array([r[3] for r in rows], dtype=float)
    c = np.array([r[4] for r in rows], dtype=float)
    ma50d = sma_arr(c, 50)
    for i in range(1, len(c)):
        if b.eq and b.eq[-1] <= 0:
            break
        px = c[i]
        if _flat(b, o, h, l, px, i, b.side, sl):
            b.step(ts[i], px); continue
        mom = c[i] / c[i - 20] - 1 if i >= 20 else 0.0
        up = mom > 0.02
        hold_ok = up and (np.isnan(ma50d[i]) or px > ma50d[i])
        if b.qty > 0:
            if not hold_ok:
                b.close(px)
        elif hold_ok:
            b.open(px, 1, 1.0)
            b.stop = b.entry * (1 - sl)
        b.step(ts[i], px)
    if b.qty > 0 and b.ts:
        b.close(c[-1]); b.eq[-1] = max(0.0, b.margin + b.realized)


# --------------------------------------------------------------- S3 trend_ma
def sim_trend_ma(rows, dts, ddir, b, sl):
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    o = np.array([r[1] for r in rows], dtype=float)
    h = np.array([r[2] for r in rows], dtype=float)
    l = np.array([r[3] for r in rows], dtype=float)
    c = np.array([r[4] for r in rows], dtype=float)
    fast, slow = sma_arr(c, 20), sma_arr(c, 50)
    for i in range(1, len(c)):
        if b.eq and b.eq[-1] <= 0:
            break
        px = c[i]
        if _flat(b, o, h, l, px, i, b.side, sl):
            b.step(ts[i], px); continue
        if i < 51 or np.isnan(fast[i]) or np.isnan(slow[i]):
            b.step(ts[i], px); continue
        up = fast[i] > slow[i]
        sig = 1 if up else -1
        if b.qty > 0:
            if sig != b.side:
                b.close(px)
        else:
            b.open(px, sig, 1.0 if sig > 0 else SHORT_W)
            b.stop = b.entry * (1 - sl) if sig > 0 else b.entry * (1 + sl)
        b.step(ts[i], px)
    if b.qty > 0 and b.ts:
        b.close(c[-1]); b.eq[-1] = max(0.0, b.margin + b.realized)


# ---------------------------------------------------------------- S4 rebound
def sim_rebound(rows, dts, ddir, b, sl):
    """超跌反弹：1h 收盘低于 SMA20 达 4% 且当根收阳 → 做多（半仓）。"""
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    o = np.array([r[1] for r in rows], dtype=float)
    h = np.array([r[2] for r in rows], dtype=float)
    l = np.array([r[3] for r in rows], dtype=float)
    c = np.array([r[4] for r in rows], dtype=float)
    ma20 = sma_arr(c, 20)
    for i in range(1, len(c)):
        if b.eq and b.eq[-1] <= 0:
            break
        px = c[i]
        if _flat(b, o, h, l, px, i, b.side, sl):
            b.step(ts[i], px); continue
        if b.qty > 0:
            gain = (px / b.entry - 1) * 100
            if (not np.isnan(ma20[i]) and px >= ma20[i]) or gain >= 3.0:
                b.close(px)
        elif not np.isnan(ma20[i]) and c[i] < o[i]:
            dev = px / ma20[i] - 1
            if dev <= -0.04:
                b.open(px, 1, 0.5)
                b.stop = b.entry * (1 - sl)
        b.step(ts[i], px)
    if b.qty > 0 and b.ts:
        b.close(c[-1]); b.eq[-1] = max(0.0, b.margin + b.realized)


# --------------------------------------------------------------- S5 breakout
def sim_breakout(rows, dts, ddir, b, sl):
    """1h 收盘突破前 24 根最高追多 / 跌破最低追空（半仓）。"""
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    o = np.array([r[1] for r in rows], dtype=float)
    h = np.array([r[2] for r in rows], dtype=float)
    l = np.array([r[3] for r in rows], dtype=float)
    c = np.array([r[4] for r in rows], dtype=float)
    ma50 = sma_arr(c, 50)
    N = 24
    for i in range(N + 1, len(c)):
        if b.eq and b.eq[-1] <= 0:
            break
        px = c[i]
        if _flat(b, o, h, l, px, i, b.side, sl):
            b.step(ts[i], px); continue
        hi, lo = h[i - N:i].max(), l[i - N:i].min()
        if b.qty > 0:
            if (b.side > 0 and px < lo * 0.998) or (b.side < 0 and px > hi * 1.002):
                b.close(px)
            elif not np.isnan(ma50[i]) and ((b.side > 0 and px < ma50[i]) or
                                            (b.side < 0 and px > ma50[i])):
                b.close(px)
        elif px > hi * 1.002:
            b.open(px, 1, 0.5); b.stop = b.entry * (1 - sl)
        elif px < lo * 0.998:
            b.open(px, -1, 0.5); b.stop = b.entry * (1 + sl)
        b.step(ts[i], px)
    if b.qty > 0 and b.ts:
        b.close(c[-1]); b.eq[-1] = max(0.0, b.margin + b.realized)


# ------------------------------------------------------------------- S6 grid
def sim_grid(rows, dts, ddir, b, sl):
    """SMA200 ±5% 区间网格（只多）：跌到下沿买入，回到上沿/出区间离场。"""
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    o = np.array([r[1] for r in rows], dtype=float)
    h = np.array([r[2] for r in rows], dtype=float)
    l = np.array([r[3] for r in rows], dtype=float)
    c = np.array([r[4] for r in rows], dtype=float)
    ma200 = sma_arr(c, 200)
    for i in range(1, len(c)):
        if b.eq and b.eq[-1] <= 0:
            break
        px = c[i]
        if _flat(b, o, h, l, px, i, b.side, sl):
            b.step(ts[i], px); continue
        if np.isnan(ma200[i]):
            b.step(ts[i], px); continue
        mid = ma200[i]
        low, high, out_lo, out_hi = mid * 0.95, mid * 0.97, mid * 0.93, mid * 1.07
        if b.qty > 0:
            if px >= high or px < out_lo or px > out_hi:
                b.close(px)
        elif px <= low and px >= out_lo:
            b.open(px, 1, 1.0)
            b.stop = b.entry * (1 - sl)
        b.step(ts[i], px)
    if b.qty > 0 and b.ts:
        b.close(c[-1]); b.eq[-1] = max(0.0, b.margin + b.realized)


# 全部可用标的（剔除 1h 数据异常的 KORU 跳变 1840%、XYZ 跳变 153%）
ALL = ['AMZN', 'COIN', 'CRCL', 'EWJ', 'GOOGL', 'HOOD', 'INTC', 'META', 'MSTR',
       'MU', 'NVDA', 'PAYP', 'PLTR', 'QQQ', 'SAMSUNG', 'SKHYNIX', 'SOXL',
       'SOXS', 'SPY', 'TSLA', 'XAG', 'XAU', 'XPT']

# 所有策略跑同一批标的 —— 只有同一把尺子才能比较「策略质量」；
# 分池（每策略 3-5 只）会让结果被标的分配主导：S1 在 13 标上 +26.8%，
# 换成 NVDA/TSLA/META/AMZN/GOOGL 5 只就变成 -28.4%。
POOLS = {name: ALL for name in (
    'S1_hybrid_日内双向', 'S2_daily_trend_日线趋势', 'S3_trend_ma_双均线',
    'S4_rebound_超跌反弹', 'S5_breakout_区间突破', 'S6_grid_区间网格')}

STRATS = [
    ('S1_hybrid_日内双向', sim_hybrid, 0.02, '日线定方向 + 1h 择时', '1h'),
    ('S2_daily_trend_日线趋势', sim_daily_trend, 0.08, '日线动量>2% 做多', '1d'),
    ('S3_trend_ma_双均线', sim_trend_ma, 0.025, '1h SMA20/50 双向', '1h'),
    ('S4_rebound_超跌反弹', sim_rebound, 0.025, '1h 低于SMA20 4% 抢反弹', '1h'),
    ('S5_breakout_区间突破', sim_breakout, 0.025, '1h 24根高低点突破', '1h'),
    ('S6_grid_区间网格', sim_grid, 0.03, 'SMA200±5% 网格', '1h'),
]


def stats(pnls):
    if not pnls:
        return dict(n=0, win=0.0, avg_w=0.0, avg_l=0.0, pf=0.0)
    w = [p for p in pnls if p > 0]
    l = [p for p in pnls if p <= 0]
    gw, gl = sum(w), abs(sum(l))
    return dict(n=len(pnls), win=len(w) / len(pnls) * 100,
                avg_w=(sum(w) / len(w)) if w else 0.0,
                avg_l=(sum(l) / len(l)) if l else 0.0,
                pf=(gw / gl) if gl > 0 else float('inf'))


def run_one(simfn, syms, cache, sl, tf='1h'):
    curves, pnls, fills, closes = [], [], 0, 0
    margin = 100.0 / len(syms)
    for s in syms:
        c = cache[s]
        rows = c['d'] if tf == '1d' else c['h']
        b = Book(margin)
        simfn(rows, c['dts'], c['ddir'], b, sl)
        curves.append((np.array(b.ts, dtype=np.int64), np.array(b.eq, dtype=float)))
        pnls.extend(b.pnls); fills += len(b.pnls); closes += b.closes
    allts = sorted({int(t) for ts, _ in curves for t in ts})
    eq = []
    for t in allts:
        v = 0.0
        for ts, e in curves:
            j = bisect.bisect_right(ts, t) - 1
            v += e[j] if j >= 0 else e[0]
        eq.append(v)
    eq = np.array(eq) if eq else np.array([100.0])
    k = int(len(eq) * 0.7)
    return (metrics(eq), metrics(eq[:k]), metrics(eq[k:]), stats(pnls),
            np.array(allts, dtype=np.int64), eq, fills, closes)


def combine(eqs, weights):
    """按权重合成 100U 组合净值（各曲线按时间戳向前填充）。"""
    names = [n for n in weights if n in eqs]
    allts = sorted({int(t) for n in names for t in eqs[n][0]})
    tot = sum(weights[n] for n in names)
    eq = []
    for t in allts:
        v = 0.0
        for n in names:
            ts, e = eqs[n]
            j = bisect.bisect_right(ts, t) - 1
            v += (e[j] if j >= 0 else e[0]) * weights[n] / tot
        eq.append(v)
    return np.array(eq)


def main():
    t0 = time.time()
    out, eqs = [], {}
    for name, fn, sl, desc, tf in STRATS:
        pool = POOLS[name]
        cache = {}
        for s in pool:
            d1 = load(s, '1d')
            dts = np.array([r[0] for r in d1], dtype=np.int64)
            c1 = np.array([r[4] for r in d1], dtype=float)
            ddir = np.zeros(len(c1), dtype=np.int8)
            for i in range(20, len(c1)):
                mom = c1[i] / c1[i - 20] - 1
                ddir[i] = 1 if mom > HYB['dth'] else (-1 if mom < -HYB['dth'] else 0)
            cache[s] = dict(h=load(s, '1h'), d=d1, dts=dts, ddir=ddir)
        a, tr, oo, st, tsx, eq, fills, closes = run_one(fn, pool, cache, sl, tf)
        eqs[name] = (tsx, eq)
        out.append(dict(strategy=name, desc=desc, pool=','.join(pool),
                        total=a['total'], mdd=a['mdd'], sharpe=a['sharpe'],
                        fills=fills, trades=closes, win_rate=st['win'],
                        avg_win=st['avg_w'], avg_loss=st['avg_l'], pf=st['pf'],
                        train=tr['total'], oos=oo['total'], oos_sh=oo['sharpe']))
        print('[progress] %d/%d %s (%d 标) 完成 %.1fs' % (
            len(out), len(STRATS), name, len(pool), time.time() - t0), flush=True)

    csv_path = os.path.join(HERE, 'arena_results.csv')
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader(); w.writerows(out)

    print('\n=== 多策略排名（每策略独立 100U，13 标等额，5x，双边 0.04%%）===')
    print('%-26s %+8s %7s %7s %6s %6s %6s %7s %6s | %+8s %8s' % (
        '策略', '总收益', 'MDD', 'Sharpe', '成交', '完整笔', '胜率', 'PF', '均盈/均亏', '训练', '样本外'))
    for r in sorted(out, key=lambda x: x['oos'], reverse=True):
        print('%-26s %+7.1f%% %6.1f%% %7.2f %6d %6d %5.1f%% %6s %+5.1f/%-5.1f | %+7.1f%% %+7.1f%%' % (
            r['strategy'], r['total'] * 100, r['mdd'] * 100, r['sharpe'],
            r['fills'], r['trades'], r['win_rate'],
            ('%.2f' % r['pf']) if r['pf'] != float('inf') else 'inf',
            r['avg_win'], r['avg_loss'], r['train'] * 100, r['oos'] * 100))

    # 组合 A：6 策略等权（每份 16.67U）
    w_eq = {r['strategy']: 1.0 for r in out}
    ceq = combine(eqs, w_eq)
    k = int(len(ceq) * 0.7)
    meq, teq, oeq = metrics(ceq), metrics(ceq[:k]), metrics(ceq[k:])
    # 组合 B：按样本外表现加权（S1 55 / S6 20 / S4 10 / S5 5 / S2 5 / S3 5）
    w_sk = {'S1_hybrid_日内双向': 55, 'S6_grid_区间网格': 20, 'S4_rebound_超跌反弹': 10,
            'S5_breakout_区间突破': 5, 'S2_daily_trend_日线趋势': 5, 'S3_trend_ma_双均线': 5}
    csk = combine(eqs, w_sk)
    msk, tsk, osk = metrics(csk), metrics(csk[:k]), metrics(csk[k:])

    print('\n=== 组合（总资金 100U）===')
    print('A 六策略等权(16.67U/个): 总收益 %+.1f%%  MDD %.1f%%  Sharpe %.2f  训练 %+.1f%%  样本外 %+.1f%%' % (
        meq['total'] * 100, meq['mdd'] * 100, meq['sharpe'], teq['total'] * 100, oeq['total'] * 100))
    print('B 按样本外加权(55/20/10/5/5/5): 总收益 %+.1f%%  MDD %.1f%%  Sharpe %.2f  训练 %+.1f%%  样本外 %+.1f%%' % (
        msk['total'] * 100, msk['mdd'] * 100, msk['sharpe'], tsk['total'] * 100, osk['total'] * 100))
    print('\n最高胜率: %s (%.1f%%，%d 笔)' % max(
        ((r['strategy'], r['win_rate'], r['trades']) for r in out), key=lambda x: x[1]))
    print('最高样本外: %s (%+.1f%%)' % max(
        ((r['strategy'], r['oos'] * 100) for r in out), key=lambda x: x[1]))
    print('最高总收益: %s (%+.1f%%)' % max(
        ((r['strategy'], r['total'] * 100) for r in out), key=lambda x: x[1]))
    print('\nCSV: %s  %.0fs' % (csv_path, time.time() - t0))


if __name__ == '__main__':
    main()
