#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一多因素方向打分器回测（用户 m03163：统一自由判断多空，不绑死纯多/纯空）。

核心：direction_score(ctx) 用 5 个因素各投 -1/0/+1，按权重加权求和 → 净分
决定做多/做空/空仓。策略不再是「多空规则」，而是「同一打分器的不同权重配置」，
因此每套策略都能对任意标的自由双向。

因素（全部用已收数据，无未来函数）：
  f1 日线20根动量      >+2% → +1，<-2% → -1
  f2 1h 12根动量       >+1% → +1，<-1% → -1
  f3 日线价 vs SMA50d  站上 → +1，跌破 → -1
  f4 1h 价 vs SMA200h  站上 → +1，跌破 → -1
  f5 1h 均值回归       偏离 SMA20 超买 → -1，超跌 → +1（反转因子）

权重配置（策略 = 权重，都能双向）：
  S1 动量型  w=(2,2,0,1,0)   重日线+1h 动量
  S2 趋势型  w=(1,0,2,2,0)   重均线趋势
  S4 反转型  w=(0,0,0,0,3)   纯均值回归
  S5 突破型  w=(1,2,0,1,0)   重短动量+趋势过滤
  S6 均衡型  w=(1,1,1,1,1)   全因素等权

对比基准：现有 arena.py 各策略（纯多/纯空规则）。

用法: python3 backtest/unified_score.py
输出: 控制台排名 + unified_score_results.csv
"""
import bisect
import csv
import os
import time

import numpy as np

from shortterm import POOL13, FEE, BE_BUF, LEV, ANN, load, sma_arr, metrics
from arena import ALL, Book, _flat, PG_SHORT, SHORT_W, stats

HERE = os.path.dirname(os.path.abspath(__file__))

DTH = 0.02   # 日线动量阈值
HTH = 0.01   # 1h 动量阈值
OPEN_TH = 0.35   # 净分归一化后开仓阈值（|score|/sum_w）
EXIT_TH = 0.15   # 滞回：净分衰减到此值以下平仓

# 策略 = 权重配置（都能自由双向）
CONFIGS = [
    ('U1_动量双向', (2, 2, 0, 1, 0), 0.02),
    ('U2_趋势双向', (1, 0, 2, 2, 0), 0.03),
    ('U4_反转双向', (0, 0, 0, 0, 3), 0.025),
    ('U5_突破双向', (1, 2, 0, 1, 0), 0.025),
    ('U6_均衡双向', (1, 1, 1, 1, 1), 0.025),
]


def direction_score(c, d, dts, ts, ddir, i, w, ma20, ma200, dma50):
    """5 因素投票 → 归一化净分 ∈ [-1,1]。正=看多，负=看空。"""
    if i < 200:
        return 0.0
    j = bisect.bisect_right(dts, ts[i] - 86400000) - 1
    if j < 50:
        return 0.0
    px = c[i]
    # f1 日线动量（ddir 已按 dth 判好 -1/0/1）
    f1 = int(ddir[j])
    # f2 1h 12根动量
    hmom = px / c[i - 12] - 1 if c[i - 12] > 0 else 0.0
    f2 = 1 if hmom > HTH else (-1 if hmom < -HTH else 0)
    # f3 日线价 vs SMA50d
    f3 = 0 if np.isnan(dma50[j]) else (1 if d[j] > dma50[j] else -1)
    # f4 1h 价 vs SMA200h
    f4 = 0 if np.isnan(ma200[i]) else (1 if px > ma200[i] else -1)
    # f5 1h 均值回归（偏离 SMA20：超跌看多、超买看空）
    dev = px / ma20[i] - 1 if not np.isnan(ma20[i]) else 0.0
    f5 = -1 if dev > 0.03 else (1 if dev < -0.03 else 0)
    votes = (f1, f2, f3, f4, f5)
    sw = sum(abs(x) for x in w)
    return sum(wk * vk for wk, vk in zip(w, votes)) / sw if sw else 0.0


def sim_score(rows, dclose, dts, ddir, b, sl, w):
    """统一打分器驱动的双向交易（复用 arena.Book 的止损/PG 框架）。"""
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    o = np.array([r[1] for r in rows], dtype=float)
    h = np.array([r[2] for r in rows], dtype=float)
    l = np.array([r[3] for r in rows], dtype=float)
    c = np.array([r[4] for r in rows], dtype=float)
    ma20 = sma_arr(c, 20)
    ma200 = sma_arr(c, 200)
    dma50 = sma_arr(dclose, 50)
    for i in range(200, len(c)):
        if b.eq and b.eq[-1] <= 0:
            break
        px = c[i]
        if _flat(b, o, h, l, px, i, b.side, sl):
            b.step(ts[i], px)
            continue
        sc = direction_score(c, dclose, dts, ts, ddir, i, w, ma20, ma200, dma50)
        if b.qty > 0:
            side = b.side
            # 滞回离场：净分反向或衰减到 EXIT_TH 以下
            if (side > 0 and sc <= EXIT_TH) or (side < 0 and sc >= -EXIT_TH):
                b.close(px)
        elif sc >= OPEN_TH:
            b.open(px, 1, 1.0)
            b.stop = b.entry * (1 - sl)
        elif sc <= -OPEN_TH:
            b.open(px, -1, SHORT_W)
            b.stop = b.entry * (1 + sl)
        b.step(ts[i], px)
    if b.qty > 0 and b.ts:
        b.close(c[-1]); b.eq[-1] = max(0.0, b.margin + b.realized)


def run_one(w, sl, syms, cache):
    curves, pnls, fills, closes = [], [], 0, 0
    margin = 100.0 / len(syms)
    for s in syms:
        c = cache[s]
        b = Book(margin)
        sim_score(c['h'], c['dc'], c['dts'], c['ddir'], b, sl, w)
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
            mom = dc[i] / dc[i - 20] - 1
            ddir[i] = 1 if mom > DTH else (-1 if mom < -DTH else 0)
        cache[s] = dict(h=load(s, '1h'), dc=dc, dts=dts, ddir=ddir)
    print('[data] %d 标加载完成 %.1fs' % (len(syms), time.time() - t0), flush=True)

    out, eqs = [], {}
    for name, w, sl in CONFIGS:
        a, tr, oo, st, tsx, eq, fills, closes = run_one(w, sl, syms, cache)
        eqs[name] = (tsx, eq)
        out.append(dict(strategy=name, w=str(w), sl=sl,
                        total=a['total'], mdd=a['mdd'], sharpe=a['sharpe'],
                        fills=fills, trades=closes, win_rate=st['win'],
                        avg_win=st['avg_w'], avg_loss=st['avg_l'], pf=st['pf'],
                        train=tr['total'], oos=oo['total'], oos_sh=oo['sharpe']))
        print('[progress] %s 完成 %.1fs' % (name, time.time() - t0), flush=True)

    csv_path = os.path.join(HERE, 'unified_score_results.csv')
    with open(csv_path, 'w', newline='') as f:
        wr = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        wr.writeheader(); wr.writerows(out)

    print('\n=== 统一打分器（23 标全池，双向，5x，双边0.04%%）===')
    print('%-16s %+8s %7s %7s %6s %6s %6s | %+8s %+8s' % (
        '策略(权重)', '总收益', 'MDD', 'Sharpe', '成交', '胜率', 'PF', '训练', '样本外'))
    for r in sorted(out, key=lambda x: x['oos'], reverse=True):
        print('%-16s %+7.1f%% %6.1f%% %7.2f %6d %5.1f%% %5s | %+7.1f%% %+7.1f%%' % (
            r['strategy'], r['total'] * 100, r['mdd'] * 100, r['sharpe'],
            r['fills'], r['win_rate'],
            ('%.2f' % r['pf']) if r['pf'] != float('inf') else 'inf',
            r['train'] * 100, r['oos'] * 100))
    print('\n基准（现有 arena.py，23 标）：S1 hybrid +19.2%/MDD18.0%/Sh1.07/胜率57.4%/OOS+17.3%')
    print('结果: %s' % csv_path)


if __name__ == '__main__':
    main()
