#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3：日线定方向 + 1h 择时的混合短线回测（币安 TradFi 股票永续，13 只美股）。

v2 结论（backtest/shortterm_v2_results.csv）：纯 1h 动量加滞回 + SMA200 过滤后
MDD 从 60% 降到 15%，但样本外仍在 -2%~-9%，收益几乎归零——1h 噪声被双边
0.04% 手续费吃掉。v3 检验「顺日线大势、用 1h 找入场/离场」是否能兼顾短线
响应与正期望：日线 20 根动量定方向，1h 只做同向触发，1h 级止损与利润保护。

数据：backtest/data/*_1d.csv + *_1h.csv；费用双边 0.04%；杠杆 5x；
      13 标等额保证金；前 70% 训练 / 后 30% 样本外；ANN=sqrt(252*24)。
"""
import bisect
import csv
import itertools
import math
import os
import time

import numpy as np

from shortterm import (POOL5, POOL13, PG_SHORT, FEE, BE_BUF, LEV, ANN,
                       load, sma_arr, metrics)

HERE = os.path.dirname(os.path.abspath(__file__))


def daily_dir(rows_1d, dth, short_ok):
    """返回 (ts, dir)：已收日线的 20 根动量方向。"""
    ts = np.array([r[0] for r in rows_1d], dtype=np.int64)
    c = np.array([r[4] for r in rows_1d], dtype=float)
    d = np.zeros(len(c), dtype=np.int8)
    for i in range(20, len(c)):
        mom = c[i] / c[i - 20] - 1
        d[i] = 1 if mom > dth else (-1 if (short_ok and mom < -dth) else 0)
    return ts, d


def sim(rows, dts, ddir, n, th, sl, exit_ratio, trend_n, margin):
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    o = np.array([r[1] for r in rows], dtype=float)
    h = np.array([r[2] for r in rows], dtype=float)
    l = np.array([r[3] for r in rows], dtype=float)
    c = np.array([r[4] for r in rows], dtype=float)
    ma20 = sma_arr(c, 20)
    trend = sma_arr(c, trend_n) if trend_n else None
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
        # 用「已经收盘」的日线方向：ts + 86400000 才收盘，故取更早一根
        j = bisect.bisect_right(dts, ts[i] - 86400000) - 1
        dir_ = int(ddir[j]) if j >= 20 else 0
        if qty > 0 and stop > 0:
            hit = (l[i] <= stop) if side > 0 else (h[i] >= stop)
            if hit:
                fill = (o[i] if o[i] < stop else stop) if side > 0 else (o[i] if o[i] > stop else stop)
                close(fill); curve.append(max(0.0, margin + realized)); continue
        if qty > 0 and PG_SHORT:
            peak = max(peak, h[i]) if side > 0 else min(peak, l[i]) if peak else l[i]
            gain = (px / entry - 1) * side * 100
            pgain = (peak / entry - 1) * side * 100
            pg = PG_SHORT
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
        sig = 0
        if dir_ > 0 and mom > th:
            sig = 1
        elif dir_ < 0 and mom < -th:
            sig = -1
        if trend is not None and not np.isnan(trend[i]) and sig:
            if (sig > 0 and px < trend[i]) or (sig < 0 and px > trend[i]):
                sig = 0
        if qty > 0:
            if dir_ != side:                    # 日线方向翻转：立即离场
                close(px)
            elif (sig != 0 and sig != side):
                close(px)
            elif side > 0 and mom < exit_ratio * th:
                close(px)
            elif side < 0 and mom > -exit_ratio * th:
                close(px)
        elif sig != 0:
            notional = margin * LEV * (1.0 if sig > 0 else 0.5)
            qty = notional / px; entry = px; side = sig; peak = px
            stop = entry * (1 - sl) if sig > 0 else entry * (1 + sl)
            trimmed = False; realized -= notional * FEE
        unreal = (px - entry) * qty * side if qty > 0 else 0.0
        curve.append(max(0.0, margin + realized + unreal))
    if qty > 0 and curve:
        close(c[-1]); curve[-1] = max(0.0, margin + realized)
    return ts[1:1 + len(curve)], np.array(curve), trades


def run(p, syms, cache):
    cs, ntr = [], 0
    margin = 100.0 / len(syms)
    for s in syms:
        rows, dts, ddir = cache[s]
        t, e, tr = sim(rows, dts, ddir, p['n'], p['th'], p['sl'],
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
    syms = POOL13
    rows = []
    t0 = time.time()
    for dth, short_ok in itertools.product((0.005, 0.02), (False, True)):
        cache = {}
        for s in syms:
            d = load(s, '1d')
            cache[s] = (load(s, '1h'), *daily_dir(d, dth, short_ok))
        for n, th, sl, er, tn in itertools.product((6, 12), (0.0, 0.005, 0.010),
                                                   (0.02, 0.04), (0.0, 0.4), (0, 200)):
            p = dict(dth=dth, short=int(short_ok), n=n, th=th, sl=sl,
                     exit_ratio=er, trend_n=tn)
            a, tr, oo, ntr = run(p, syms, cache)
            p.update(total=a['total'], mdd=a['mdd'], sharpe=a['sharpe'],
                     train=tr['total'], oos=oo['total'], oos_sh=oo['sharpe'], trades=ntr)
            rows.append(p)
        print('[progress] dth=%.3f short=%d 完成 %d 组 %.0fs' % (
            dth, short_ok, len(rows), time.time() - t0), flush=True)
    out = os.path.join(HERE, 'shortterm_v3_results.csv')
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    valid = [r for r in rows if r['total'] > -0.5 and r['mdd'] < 0.95]
    def score(r):
        return (r['oos'] * 1.2 + r['total'] * 0.4 + r['oos_sh'] * 0.02
                - r['mdd'] * 1.2 - max(0.0, r['train'] - r['oos']) * 0.3)
    best = sorted(valid, key=score, reverse=True)
    print('\n=== v3 TOP 20 ===')
    print('%-6s %-5s %-3s %-6s %-6s %-5s %-6s | %+8s %7s %7s | %+8s %+8s %7s | %5s' % (
        'dth', 'short', 'n', 'th', 'sl', 'exit', 'trend', 'total', 'MDD', 'Sharpe',
        'train', 'OOS', 'OOS_Sh', 'trd'))
    for r in best[:20]:
        print('%-6.1f%% %-5d %-3d %-6.1f%% %-6.1f%% %-5.1f %-6s | %+7.1f%% %6.1f%% %7.2f | %+7.1f%% %+7.1f%% %7.2f | %5d' % (
            r['dth'] * 100, r['short'], r['n'], r['th'] * 100, r['sl'] * 100,
            r['exit_ratio'], r['trend_n'] or 'off',
            r['total'] * 100, r['mdd'] * 100, r['sharpe'],
            r['train'] * 100, r['oos'] * 100, r['oos_sh'], r['trades']))
    print('\n=== 维度边际 ===')
    for key in ('dth', 'short', 'n', 'th', 'sl', 'exit_ratio', 'trend_n'):
        parts = []
        for v in sorted({r[key] for r in valid}, key=str):
            sub = [r for r in valid if r[key] == v]
            parts.append('%s=%.3f(最优%+.1f%%)' % (
                v, sum(score(r) for r in sub) / len(sub), max(r['total'] for r in sub) * 100))
        print('  %-10s %s' % (key, '  '.join(parts)))
    print('\nCSV: %s  有效 %d/%d  %.0fs' % (out, len(valid), len(rows), time.time() - t0))


if __name__ == '__main__':
    main()
