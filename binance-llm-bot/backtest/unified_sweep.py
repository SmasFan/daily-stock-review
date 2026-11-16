#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一打分器参数扫描：加利润保护(PG) + 扫阈值/权重/f5符号，找能跑赢 S1 的配置。

第一版（无 PG、OPEN_TH=0.35）跑输基准：换手过高、手续费吃光。本脚本补上
arena.sim_hybrid 的 PG（trailing/分批止盈/保本）后重扫，验证「统一多因素
双向」是否真能替代现有「纯多策略」。

用法: python3 backtest/unified_sweep.py
输出: unified_sweep_results.csv + TOP15
"""
import bisect
import csv
import itertools
import os
import time

import numpy as np

from shortterm import FEE, BE_BUF, LEV, load, sma_arr, metrics
from arena import ALL, Book, _flat, PG_SHORT, SHORT_W, stats

HERE = os.path.dirname(os.path.abspath(__file__))
DTH, HTH = 0.02, 0.01


def score(c, d, dts, ts, ddir, i, w, ma20, ma200, dma50, f5sign):
    if i < 200:
        return 0.0
    j = bisect.bisect_right(dts, ts[i] - 86400000) - 1
    if j < 50:
        return 0.0
    px = c[i]
    f1 = int(ddir[j])
    hm = px / c[i - 12] - 1 if c[i - 12] > 0 else 0.0
    f2 = 1 if hm > HTH else (-1 if hm < -HTH else 0)
    f3 = 0 if np.isnan(dma50[j]) else (1 if d[j] > dma50[j] else -1)
    f4 = 0 if np.isnan(ma200[i]) else (1 if px > ma200[i] else -1)
    dev = px / ma20[i] - 1 if not np.isnan(ma20[i]) else 0.0
    f5 = f5sign * (-1 if dev > 0.03 else (1 if dev < -0.03 else 0))
    v = (f1, f2, f3, f4, f5)
    sw = sum(abs(x) for x in w)
    return sum(a * b for a, b in zip(w, v)) / sw if sw else 0.0


def sim(rows, dc, dts, ddir, b, sl, w, oth, eth, f5sign):
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    o = np.array([r[1] for r in rows], dtype=float)
    h = np.array([r[2] for r in rows], dtype=float)
    l = np.array([r[3] for r in rows], dtype=float)
    c = np.array([r[4] for r in rows], dtype=float)
    ma20, ma200, dma50 = sma_arr(c, 20), sma_arr(c, 200), sma_arr(dc, 50)
    for i in range(200, len(c)):
        if b.eq and b.eq[-1] <= 0:
            break
        px = c[i]
        if _flat(b, o, h, l, px, i, b.side, sl):
            b.step(ts[i], px); continue
        if b.qty > 0:
            side = b.side
            b.peak = max(b.peak, h[i]) if side > 0 else (min(b.peak, l[i]) if b.peak else l[i])
            gain = (px / b.entry - 1) * side * 100
            pgain = (b.peak / b.entry - 1) * side * 100
            pg = PG_SHORT
            if pgain >= pg['trail_min'] and (pgain - gain) >= max(pgain * pg['give_back'], pg['trail']):
                b.close(px); b.step(ts[i], px); continue
            if not b.trimmed and gain >= pg['tp_scale']:
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
        sc = score(c, dc, dts, ts, ddir, i, w, ma20, ma200, dma50, f5sign)
        if b.qty > 0:
            side = b.side
            if (side > 0 and sc <= eth) or (side < 0 and sc >= -eth):
                b.close(px)
        elif sc >= oth:
            b.open(px, 1, 1.0); b.stop = b.entry * (1 - sl)
        elif sc <= -oth:
            b.open(px, -1, SHORT_W); b.stop = b.entry * (1 + sl)
        b.step(ts[i], px)
    if b.qty > 0 and b.ts:
        b.close(c[-1]); b.eq[-1] = max(0.0, b.margin + b.realized)


def run(w, sl, oth, eth, f5sign, syms, cache):
    curves, pnls = [], []
    margin = 100.0 / len(syms)
    for s in syms:
        c = cache[s]; b = Book(margin)
        sim(c['h'], c['dc'], c['dts'], c['ddir'], b, sl, w, oth, eth, f5sign)
        curves.append((np.array(b.ts, dtype=np.int64), np.array(b.eq, dtype=float)))
        pnls.extend(b.pnls)
    at = sorted({int(t) for ts, _ in curves for t in ts})
    eq = []
    for t in at:
        v = 0.0
        for ts, e in curves:
            j = bisect.bisect_right(ts, t) - 1
            v += e[j] if j >= 0 else e[0]
        eq.append(v)
    eq = np.array(eq)
    k = int(len(eq) * 0.7)
    a, tr, oo = metrics(eq), metrics(eq[:k]), metrics(eq[k:])
    st = stats(pnls)
    return dict(total=a['total'], mdd=a['mdd'], sharpe=a['sharpe'],
                fills=len(pnls), trades=st['n'], win=st['win'], pf=st['pf'],
                train=tr['total'], oos=oo['total'], oos_sh=oo['sharpe'])


def main():
    t0 = time.time()
    syms = ALL
    cache = {}
    for s in syms:
        d1 = load(s, '1d')
        dts = np.array([r[0] for r in d1], dtype=np.int64)
        dc = np.array([r[4] for r in d1], dtype=float)
        ddir = np.zeros(len(dc), dtype=np.int8)
        for i in range(20, len(dc)):
            m = dc[i] / dc[i - 20] - 1
            ddir[i] = 1 if m > DTH else (-1 if m < -DTH else 0)
        cache[s] = dict(h=load(s, '1h'), dc=dc, dts=dts, ddir=ddir)
    print('[data] %d 标 %.1fs' % (len(syms), time.time() - t0), flush=True)

    W = [(2, 2, 0, 1, 0), (1, 0, 2, 2, 0), (1, 2, 0, 1, 0), (1, 1, 1, 1, 1)]
    OTH = [0.35, 0.50, 0.65]
    ETH = [0.15, 0.25]
    F5 = [1, -1]
    SL = [0.02, 0.03]
    rows = []
    combos = list(itertools.product(range(len(W)), OTH, ETH, F5, SL))
    for n, (wi, oth, eth, f5, sl) in enumerate(combos, 1):
        r = run(W[wi], sl, oth, eth, f5, syms, cache)
        r.update(w=str(W[wi]), oth=oth, eth=eth, f5=f5, sl=sl, wi=wi)
        rows.append(r)
        if n % 12 == 0:
            print('[progress] %d/%d %.1fs' % (n, len(combos), time.time() - t0), flush=True)

    path = os.path.join(HERE, 'unified_sweep_results.csv')
    with open(path, 'w', newline='') as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wr.writeheader(); wr.writerows(rows)

    # 评分：样本外为主 + 稳健（MDD 小、训练/样本外差距小）
    def score_of(r):
        return r['oos'] * 1.2 + r['total'] * 0.4 + r['oos_sh'] * 0.02 \
               - r['mdd'] * 1.2 - max(0, r['train'] - r['oos']) * 0.3
    rows.sort(key=score_of, reverse=True)
    print('\n=== TOP15（评分=样本外1.2+总0.4+样本外Sh0.02-MDD1.2-过拟合惩罚0.3）===')
    print('%3s %-16s %5s %5s %4s %5s | %+8s %6s %6s %6s | %+8s %+8s' % (
        '#', 'w', 'oth', 'eth', 'f5', 'sl', '总收益', 'MDD', '胜率', '成交', '训练', '样本外'))
    for i, r in enumerate(rows[:15], 1):
        print('%3d %-16s %5.2f %5.2f %4d %5.2f | %+7.1f%% %5.1f%% %5.1f%% %6d | %+7.1f%% %+7.1f%%' % (
            i, r['w'], r['oth'], r['eth'], r['f5'], r['sl'],
            r['total'] * 100, r['mdd'] * 100, r['win'], r['fills'],
            r['train'] * 100, r['oos'] * 100))
    print('\n基准 S1 hybrid: +19.2%/MDD18.0%/Sh1.07/胜率57.4%/OOS+17.3%')
    print('结果: %s' % path)


if __name__ == '__main__':
    main()
