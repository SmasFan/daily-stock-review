#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1h 短线动量参数扫描 v2（币安 TradFi 股票永续，13 只美股）。

背景：2026-11-10 用户要求「美股要开 23h 交易了，策略上更短线一点，因为可以上
杠杆，弹性大」。原日线池用 20 根日线动量（阈值 2%），一轮信号要一天；这里改到
1h K 线，把回看根数/阈值/止损/利润保护全部缩到日内尺度。

v1 教训（backtest/shortterm.py 首轮 96 组）：把「动量落回阈值区间内就平仓」直接
搬到 1h 会每 20~30 根换手一次，双边 0.04% 手续费把收益吃光，样本外几乎全负
（最优组全期 +39% / 样本外 -12.5% / MDD 63%）。v2 加两个关键修正：
  1) 滞回平仓：持仓中动量衰减到 exit_ratio*th 才平（不是 |mom|<th 就平）；
  2) 长周期趋势过滤：1h SMA200 之上只做多、之下只做空（下跌行情不追多）。
另外把做空阈值做成独立维度（日线回测的 4×th 在 1h 尺度太迟钝）。

数据：backtest/data/*_1h.csv（2026-04 → 11，约 5000 根/标，主网公开 K 线）。
费用：双边 0.04%；杠杆 5x；每标等额保证金；组合按时间戳并集对齐；
      前 70% 训练 / 后 30% 样本外。Sharpe 年化 sqrt(252*24)。

用法: python3 backtest/shortterm.py [--syms 5|13] [--full]
"""
import argparse
import bisect
import csv
import itertools
import math
import os
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
FEE = 0.0004
BE_BUF = 0.0008
LEV = 5.0
ANN = math.sqrt(252 * 24)
POOL5 = ['TSLA', 'COIN', 'PLTR', 'MSTR', 'HOOD']
POOL13 = POOL5 + ['NVDA', 'META', 'AMZN', 'QQQ', 'SPY', 'GOOGL', 'INTC', 'CRCL']
PG_SHORT = dict(lock=1.0, trail=0.6, trail_min=1.0, give_back=0.5,
                tp_scale=2.0, scale_ratio=0.5, tp_fast=3.5)


def load(sym, tf='1h'):
    rows = []
    with open(os.path.join(DATA, f'{sym}_{tf}.csv')) as f:
        for r in csv.DictReader(f):
            rows.append((int(r['ts']), float(r['open']), float(r['high']),
                         float(r['low']), float(r['close'])))
    return rows


def sma_arr(c, w):
    s = np.cumsum(np.insert(c, 0, 0.0))
    out = np.full(len(c), np.nan)
    if len(c) >= w:
        out[w - 1:] = (s[w:] - s[:-w]) / w
    return out


def sim(rows, n, th, sl, pg, th_short, short_risk, sl_short,
        exit_ratio, trend_n, margin, cache=None):
    """单标 1h 收盘价撮合。返回 (ts, equity, trades)。side: +1 多 / -1 空 / 0 空仓。"""
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    o = np.array([r[1] for r in rows], dtype=float)
    h = np.array([r[2] for r in rows], dtype=float)
    l = np.array([r[3] for r in rows], dtype=float)
    c = np.array([r[4] for r in rows], dtype=float)
    ma20 = sma_arr(c, 20)
    trend = sma_arr(c, trend_n) if trend_n else None
    exit_th = exit_ratio * th
    exit_short = exit_ratio * th_short
    side, qty, entry, peak, stop, trimmed, realized = 0, 0.0, 0.0, 0.0, 0.0, False, 0.0
    trades = 0
    curve = []

    def close(px):
        nonlocal qty, side, entry, peak, stop, trimmed, realized, trades
        realized += (px - entry) * qty * side - px * qty * FEE
        qty = side = 0
        entry = peak = stop = 0.0
        trimmed = False
        trades += 1

    for i in range(1, len(c)):
        if curve and curve[-1] <= 0:
            break
        px = c[i]
        if qty > 0 and stop > 0:
            hit = (l[i] <= stop) if side > 0 else (h[i] >= stop)
            if hit:
                fill = (o[i] if o[i] < stop else stop) if side > 0 else (o[i] if o[i] > stop else stop)
                close(fill)
                curve.append(max(0.0, margin + realized))
                continue
        if qty > 0 and pg:
            peak = max(peak, h[i]) if side > 0 else min(peak, l[i]) if peak else l[i]
            gain = (px / entry - 1) * side * 100
            pgain = (peak / entry - 1) * side * 100
            if pgain >= pg['trail_min'] and (pgain - gain) >= max(pgain * pg['give_back'], pg['trail']):
                close(px); curve.append(max(0.0, margin + realized)); continue
            if (not trimmed) and gain >= pg['tp_scale']:
                q = qty * pg['scale_ratio']
                realized += (px - entry) * q * side - px * q * FEE
                qty -= q; trimmed = True
                be = entry * (1 + BE_BUF * side)
                stop = max(stop, be) if side > 0 else min(stop, be)
            if qty > 0 and gain >= pg['tp_fast'] and not np.isnan(ma20[i]) and \
               ((side > 0 and px < ma20[i]) or (side < 0 and px > ma20[i])):
                close(px); curve.append(max(0.0, margin + realized)); continue
            if qty > 0 and pgain >= pg['lock']:
                be = entry * (1 + BE_BUF * side)
                stop = max(stop, be) if side > 0 else min(stop, be)
        mom = px / c[i - n] - 1 if i >= n and c[i - n] > 0 else 0.0
        sig = 1 if mom > th else (-1 if mom < -th_short else 0)
        if trend is not None and not np.isnan(trend[i]) and sig:
            # 长周期趋势过滤：均线上只多、均线下只空
            if (sig > 0 and px < trend[i]) or (sig < 0 and px > trend[i]):
                sig = 0
        if qty > 0:
            if sig != 0 and sig != side:            # 反向信号：先平，下根再开
                close(px)
            elif side > 0 and mom < exit_th:        # 滞回：动量衰减到位才平
                close(px)
            elif side < 0 and mom > -exit_short:
                close(px)
        elif sig != 0:
            sl_eff = sl if sig > 0 else sl_short
            notional = margin * LEV * (1.0 if sig > 0 else short_risk)
            qty = notional / px; entry = px; side = sig; peak = px
            stop = entry * (1 - sl_eff) if sig > 0 else entry * (1 + sl_eff)
            trimmed = False; realized -= notional * FEE
        unreal = (px - entry) * qty * side if qty > 0 else 0.0
        curve.append(max(0.0, margin + realized + unreal))
    if qty > 0 and curve:
        close(c[-1])
        curve[-1] = max(0.0, margin + realized)
    return ts[1:1 + len(curve)], np.array(curve), trades


def metrics(curve):
    if len(curve) < 2:
        return dict(total=0.0, mdd=0.0, sharpe=0.0)
    total = curve[-1] / curve[0] - 1
    peak = np.maximum.accumulate(curve)
    mdd = float(((peak - curve) / peak).max())
    r = np.diff(curve) / curve[:-1]
    sd = r.std()
    return dict(total=float(total), mdd=mdd,
                sharpe=float(r.mean() / sd * ANN) if sd else 0.0)


def run(p, syms, cache):
    cs, ntr = [], 0
    margin = 100.0 / len(syms)
    for s in syms:
        t, e, tr = sim(cache[s], p['n'], p['th'], p['sl'], PG_SHORT,
                       p['th_short'], 0.5, p['sl'] * 0.75,
                       p['exit_ratio'], p['trend_n'], margin)
        cs.append((t, e)); ntr += tr
    allts = sorted({int(x) for t, _ in cs for x in t})
    eq = []
    for t in allts:
        v, ok = 0.0, True
        for ts, e in cs:
            j = bisect.bisect_right(ts, t) - 1
            if j < 0:
                ok = False; break
            v += e[j]
        if ok:
            eq.append(v)
    eq = np.array(eq)
    k = int(len(eq) * 0.7)
    return metrics(eq), metrics(eq[:k]), metrics(eq[k:]), ntr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--syms', choices=('5', '13'), default='13')
    ap.add_argument('--full', action='store_true', help='细网格（含 15m 尺度代理 n=3）')
    args = ap.parse_args()

    syms = POOL5 if args.syms == '5' else POOL13
    cache = {s: load(s) for s in syms}
    print('数据: %d 标, %s ~ %s, 最少 %d 根 1h K线' % (
        len(syms),
        time.strftime('%Y-%m-%d', time.gmtime(min(c[0][0] for c in cache.values()) / 1000)),
        time.strftime('%Y-%m-%d', time.gmtime(max(c[-1][0] for c in cache.values()) / 1000)),
        min(len(c) for c in cache.values())), flush=True)

    ns = (3, 6, 12, 24) if args.full else (6, 12, 24)
    ths = (0.005, 0.010, 0.015)
    sls = (0.020, 0.040)
    exits = (0.0, 0.4)
    trends = (0, 200)
    # short 模式: off=纯多, sym=对称阈值, cons=2×阈值（比 v1 的 4× 灵敏）
    shorts = ('off', 'sym', 'cons')
    combos = []
    for n, th, sl, er, tn, sm in itertools.product(ns, ths, sls, exits, trends, shorts):
        th_short = None if sm == 'off' else (th if sm == 'sym' else 2 * th)
        combos.append(dict(n=n, th=th, sl=sl, exit_ratio=er, trend_n=tn,
                           short=sm, th_short=th_short))
    out = os.path.join(HERE, 'shortterm_v2_results.csv')
    rows = []
    t0 = time.time()
    for idx, p in enumerate(combos, 1):
        if p['th_short'] is None:
            # 纯多：把做空阈值设成不可能达到的大数
            p = dict(p, th_short=9.9)
        a, tr, oo, ntr = run(p, syms, cache)
        r = dict(p)
        r.update(total=a['total'], mdd=a['mdd'], sharpe=a['sharpe'],
                 train=tr['total'], train_sh=tr['sharpe'],
                 oos=oo['total'], oos_sh=oo['sharpe'], trades=ntr)
        rows.append(r)
        if idx % 20 == 0 or idx == len(combos):
            print('[progress] %d/%d  %.0fs' % (idx, len(combos), time.time() - t0), flush=True)
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)

    # 过滤破产路径（曲线被打到 0 后样本外会出现虚假暴涨）
    valid = [r for r in rows if r['total'] > -0.5 and r['mdd'] < 0.95]
    def score(r):
        return (r['oos'] * 1.2 + r['total'] * 0.4 + r['oos_sh'] * 0.02
                - r['mdd'] * 1.2 - max(0.0, r['train'] - r['oos']) * 0.3)
    best = sorted(valid, key=score, reverse=True)
    hdr = '%-3s %-6s %-6s %-5s %-6s %-5s | %+8s %7s %7s | %+8s %7s | %+8s %7s | %5s' % (
        'n', 'th', 'sl', 'exit', 'trend', 'short', 'total', 'MDD', 'Sharpe',
        'train', 'trSh', 'OOS', 'OOS_Sh', 'trd')
    print('\n=== TOP 25（过滤破产路径，按样本外+全期+回撤综合） ===')
    print(hdr)
    for r in best[:25]:
        print('%-3d %-6.1f%% %-6.1f%% %-5.1f %-6s %-5s | %+7.1f%% %6.1f%% %7.2f | %+7.1f%% %7.2f | %+7.1f%% %7.2f | %5d' % (
            r['n'], r['th'] * 100, r['sl'] * 100, r['exit_ratio'],
            r['trend_n'] or 'off', r['short'],
            r['total'] * 100, r['mdd'] * 100, r['sharpe'],
            r['train'] * 100, r['train_sh'], r['oos'] * 100, r['oos_sh'], r['trades']))
    print('\n=== 各维度边际（score 均值 / 最优全期） ===')
    for key in ('n', 'th', 'sl', 'exit_ratio', 'trend_n', 'short'):
        vals = sorted({r[key] for r in valid}, key=str)
        parts = []
        for v in vals:
            sub = [r for r in valid if r[key] == v]
            parts.append('%s=%.3f(最优%+.1f%%)' % (
                v, sum(score(r) for r in sub) / len(sub),
                max(r['total'] for r in sub) * 100))
        print('  %-10s %s' % (key, '  '.join(parts)))
    print('\n结果 CSV: %s 有效 %d/%d 组 耗时 %.0fs' % (out, len(valid), len(rows), time.time() - t0))


if __name__ == '__main__':
    main()
