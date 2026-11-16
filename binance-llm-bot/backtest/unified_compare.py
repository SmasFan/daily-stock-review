#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三方案对比：现状(纯多策略) vs 混合(S1/S5保留+S2/S4/S6打分器双向) vs 全打分器。

回答用户 m03163：统一多因素自由判断多空。核心问题 = S2/S4/S6 三个纯多策略
在下跌趋势里死扛多单。方案：把它们换成统一打分器的不同权重配置（都能双向），
S1（本就是多因素双向、收益最高）与 S5（双向突破）保留。

组合口径：按实盘权重 60/20/8/7/5 在净值层面加权（symbol 仲裁是实盘概念，
回测各策略独立跑全池），5x、双边 0.04%、23 标。

用法: python3 backtest/unified_compare.py
"""
import bisect
import os
import time

import numpy as np

from shortterm import load, sma_arr, metrics
from arena import ALL, Book, sim_hybrid, sim_breakout, sim_daily_trend, sim_rebound, sim_grid, stats
from unified_sweep import sim as sim_score, DTH

HERE = os.path.dirname(os.path.abspath(__file__))
W = 0.60  # S1
W5 = 0.20  # S5

# 打分器配置（来自 unified_sweep TOP，oth=0.65 换手低更稳）
SCORE_CFG = {
    'S2': dict(w=(1, 0, 2, 2, 0), sl=0.03, oth=0.65, eth=0.15, f5=1),   # 趋势型
    'S4': dict(w=(0, 0, 0, 0, 3), sl=0.025, oth=0.65, eth=0.25, f5=-1),  # 反转型
    'S6': dict(w=(1, 1, 1, 1, 1), sl=0.02, oth=0.65, eth=0.25, f5=-1),   # 均衡型
}
SL_BASE = {'S1': 0.02, 'S2': 0.08, 'S4': 0.025, 'S5': 0.025, 'S6': 0.03}


def run_strat(name, fn, syms, cache, score_cfg=None, sl=0.02):
    curves, pnls = [], []
    margin = 100.0 / len(syms)
    for s in syms:
        c = cache[s]; b = Book(margin)
        if score_cfg:
            sim_score(c['h'], c['dc'], c['dts'], c['ddir'], b, sl,
                      score_cfg['w'], score_cfg['oth'], score_cfg['eth'], score_cfg['f5'])
        else:
            fn(c['h'], c['dts'], c['ddir'], b, sl)
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
    return np.array(at), np.array(eq), stats(pnls)


def combine(curves, weights):
    names = list(weights)
    at = sorted({int(t) for n in names for ts, _ in [curves[n]] for t in ts})
    tot = sum(weights.values())
    eq = []
    for t in at:
        v = 0.0
        for n in names:
            ts, e = curves[n]
            j = bisect.bisect_right(ts, t) - 1
            v += (e[j] if j >= 0 else e[0]) * weights[n] / tot
        eq.append(v)
    return np.array(eq)


def report(label, eq):
    k = int(len(eq) * 0.7)
    a, tr, oo = metrics(eq), metrics(eq[:k]), metrics(eq[k:])
    print('%-28s 总%+7.1f%%  MDD%5.1f%%  Sh%5.2f  训练%+7.1f%%  样本外%+7.1f%%  OOS_Sh%5.2f' % (
        label, a['total'] * 100, a['mdd'] * 100, a['sharpe'],
        tr['total'] * 100, oo['total'] * 100, oo['sharpe']))
    return a['total'], oo['total'], a['mdd']


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

    # 现状策略函数
    base_fn = {'S1': sim_hybrid, 'S5': sim_breakout, 'S2': sim_daily_trend,
               'S4': sim_rebound, 'S6': sim_grid}
    weights = {'S1': 0.60, 'S5': 0.20, 'S2': 0.08, 'S6': 0.07, 'S4': 0.05}

    print('\n--- 方案A 现状：全用原策略（S2/S4/S6 纯多）---')
    ca, sa = {}, {}
    for n in ['S1', 'S5', 'S2', 'S4', 'S6']:
        tsx, eq, st = run_strat(n, base_fn[n], syms, cache, sl=SL_BASE[n])
        ca[n] = (tsx, eq); sa[n] = st
        print('  %s 成交%d 胜率%.1f%%' % (n, st['n'], st['win']))
    report('A 现状组合(60/20/8/7/5)', combine(ca, weights))

    print('\n--- 方案B 混合：S1/S5 保留，S2/S4/S6 换打分器双向 ---')
    cb, sb = {}, {}
    for n in ['S1', 'S5']:
        tsx, eq, st = run_strat(n, base_fn[n], syms, cache, sl=SL_BASE[n])
        cb[n] = (tsx, eq); sb[n] = st
    for n in ['S2', 'S4', 'S6']:
        cfg = SCORE_CFG[n]
        tsx, eq, st = run_strat(n, None, syms, cache, score_cfg=cfg, sl=cfg['sl'])
        cb[n] = (tsx, eq); sb[n] = st
        print('  %s(打分器%s) 成交%d 胜率%.1f%%' % (n, cfg['w'], st['n'], st['win']))
    report('B 混合组合(60/20/8/7/5)', combine(cb, weights))

    print('\n--- 方案C 全打分器：5 个策略全换不同权重配置 ---')
    cc = {}
    allcfg = {
        'S1': dict(w=(2, 2, 0, 1, 0), sl=0.02, oth=0.65, eth=0.15, f5=1),
        'S5': dict(w=(1, 2, 0, 1, 0), sl=0.025, oth=0.65, eth=0.25, f5=1),
        **SCORE_CFG,
    }
    for n in ['S1', 'S5', 'S2', 'S4', 'S6']:
        cfg = allcfg[n]
        tsx, eq, st = run_strat(n, None, syms, cache, score_cfg=cfg, sl=cfg['sl'])
        cc[n] = (tsx, eq)
        print('  %s(打分器%s) 成交%d 胜率%.1f%%' % (n, cfg['w'], st['n'], st['win']))
    report('C 全打分器组合(60/20/8/7/5)', combine(cc, weights))

    print('\n耗时 %.1fs' % (time.time() - t0))


if __name__ == '__main__':
    main()
