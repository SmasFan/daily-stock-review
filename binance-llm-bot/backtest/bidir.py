#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双向动量 vs 纯做多 的快速检验（复用 sweep.py 的数据与费用模型）。

用户 2026-11-09 要求真实 Demo 盘也支持做空。先验证「动量反转做空」是不是正期望，
避免把只做多回测 +78% 的策略直接改成双向后反而亏钱。
用法: python3 backtest/bidir.py
"""
import csv, math, os
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
FEE = 0.0004
BE_BUF = 0.0015
SYMS = ['TSLA', 'COIN', 'PLTR', 'MSTR', 'HOOD']
LEV = 5.0


def load(sym):
    p = os.path.join(DATA, f'{sym}_1d.csv')
    rows = []
    with open(p) as f:
        for r in csv.DictReader(f):
            rows.append((int(r['ts']), float(r['open']), float(r['high']),
                         float(r['low']), float(r['close'])))
    return rows


def ma20_arr(c):
    s = np.cumsum(np.insert(c, 0, 0.0))
    out = np.full(len(c), np.nan)
    if len(c) >= 20:
        out[19:] = (s[20:] - s[:-20]) / 20
    return out


def sim(rows, n, th, sl, pg, allow_short, th_short=None, short_risk=1.0, sl_short=None):
    """返回 (ts, equity)。side: +1 多 / -1 空 / 0 空仓。"""
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    o = np.array([r[1] for r in rows], dtype=float)
    h = np.array([r[2] for r in rows], dtype=float)
    l = np.array([r[3] for r in rows], dtype=float)
    c = np.array([r[4] for r in rows], dtype=float)
    ma20 = ma20_arr(c)
    margin = 100.0 / len(SYMS)
    side, qty, entry, peak, stop, trimmed, realized = 0, 0.0, 0.0, 0.0, 0.0, False, 0.0
    curve = []

    def close(px, why):
        nonlocal qty, side, entry, peak, stop, trimmed, realized
        realized += (px - entry) * qty * side - px * qty * FEE
        qty = side = 0
        entry = peak = stop = 0.0
        trimmed = False

    for i in range(1, len(c)):
        if curve and curve[-1] <= 0:
            break
        px = c[i]
        # 硬止损：多 low<=stop，空 high>=stop；跳空按 open
        if qty > 0 and stop > 0:
            hit = (l[i] <= stop) if side > 0 else (h[i] >= stop)
            if hit:
                fill = (o[i] if o[i] < stop else stop) if side > 0 else (o[i] if o[i] > stop else stop)
                close(fill, 'stop')
                curve.append(max(0.0, margin + realized))
                continue
        # 利润保护（按方向镜像）
        if qty > 0 and pg:
            peak = max(peak, h[i]) if side > 0 else min(peak, l[i]) if peak else l[i]
            gain = (px / entry - 1) * side * 100
            pgain = (peak / entry - 1) * side * 100
            if pgain >= pg['trail_min'] and (pgain - gain) >= max(pgain * pg['give_back'], pg['trail']):
                close(px, 'trail'); curve.append(max(0.0, margin + realized)); continue
            if (not trimmed) and gain >= pg['tp_scale']:
                q = qty * pg['scale_ratio']
                realized += (px - entry) * q * side - px * q * FEE
                qty -= q; trimmed = True
                be = entry * (1 + BE_BUF * side)
                stop = max(stop, be) if side > 0 else min(stop, be)
            if qty > 0 and gain >= pg['tp_fast'] and not np.isnan(ma20[i]) and \
               ((side > 0 and px < ma20[i]) or (side < 0 and px > ma20[i])):
                close(px, 'momentum'); curve.append(max(0.0, margin + realized)); continue
            if qty > 0 and pgain >= pg['lock']:
                be = entry * (1 + BE_BUF * side)
                stop = max(stop, be) if side > 0 else min(stop, be)
        # 信号
        mom = px / c[i - n] - 1 if i >= n and c[i - n] > 0 else 0.0
        th_s = th_short if th_short is not None else th
        sig = 1 if mom > th else (-1 if (allow_short and mom < -th_s) else 0)
        if sig != 0 and qty == 0:
            sl_eff = sl if sig > 0 else (sl_short if sl_short is not None else sl)
            notional = margin * LEV * (1.0 if sig > 0 else short_risk)
            qty = notional / px; entry = px; side = sig; peak = px
            stop = entry * (1 - sl_eff) if sig > 0 else entry * (1 + sl_eff)
            trimmed = False; realized -= notional * FEE
        elif sig == 0 and qty > 0:
            close(px, 'flat')
        elif qty > 0 and sig != side:   # 反向信号：先平，下根再开
            close(px, 'flip')
        unreal = (px - entry) * qty * side if qty > 0 else 0.0
        curve.append(max(0.0, margin + realized + unreal))
    if qty > 0:
        close(c[-1], 'eod')
        curve[-1] = max(0.0, margin + realized)
    return ts[1:1 + len(curve)], np.array(curve)


def metrics(curve):
    if len(curve) < 2:
        return dict(total=0, mdd=0, sharpe=0)
    total = curve[-1] / curve[0] - 1
    peak = np.maximum.accumulate(curve)
    mdd = float(((peak - curve) / peak).max())
    r = np.diff(curve) / curve[:-1]
    sd = r.std()
    return dict(total=float(total), mdd=mdd, sharpe=float(r.mean() / sd * math.sqrt(252)) if sd else 0)


def run(n, th, sl, pg, allow_short, **kw):
    import bisect
    cs = []
    for s in SYMS:
        t, e = sim(load(s), n, th, sl, pg, allow_short, **kw)
        cs.append((t, e))
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
    return metrics(eq), metrics(eq[:k]), metrics(eq[k:]), len(eq)


if __name__ == '__main__':
    PG = dict(lock=4.0, trail=3.0, trail_min=4.0, give_back=0.5,
              tp_scale=7.0, scale_ratio=0.5, tp_fast=12.0)
    print('%-6s %-5s %-6s %-6s | %-8s %-7s %-7s | %-8s %-7s | %-8s %-7s' % (
        'short', 'n', 'th', 'sl', 'total', 'MDD', 'Sharpe', 'train', 'OOS', 'OOS_tot', 'OOS_Sh'))
    for allow_short in (False, True):
        for n in (10, 20, 30):
            for th in (0.01, 0.02, 0.03, 0.05):
                for sl in (0.06, 0.08, 0.12):
                    a, tr, oo, ln = run(n, th, sl, PG, allow_short)
                    print('%-6s %-5d %-6.0f%% %-6.0f%% | %+7.1f%% %6.1f%% %7.2f | %+7.1f%% %7.2f | %+7.1f%% %7.2f' % (
                        'Y' if allow_short else 'N', n, th * 100, sl * 100,
                        a['total'] * 100, a['mdd'] * 100, a['sharpe'],
                        tr['total'] * 100, tr['sharpe'],
                        oo['total'] * 100, oo['sharpe']))
