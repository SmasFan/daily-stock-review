#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TRADIFI 股票永续策略参数扫描 / 样本外检验。

数据: backtest/data/<SYM>_1d.csv / <SYM>_1h.csv（download_data.py 生成）

设计要点:
  * 每个标的独立保证金、独立信号，组合净值 = 各标的净值之和（与 bot 的池记账一致）
  * 手续费双边各 0.04%，按名义计
  * 止损用 bar 的 low 触发；跳空低开按 open 成交
  * 可选 T+0 利润保护（与 profit_guard.py 同一套规则）
  * 前 70% 训练、后 30% 样本外；同时报告全期 / 训练 / 样本外
  * 不追求单一“最优”，而是看参数邻域是否稳定（避免过拟合）

用法:
  python3 backtest/sweep.py --pool daily --family sma
  python3 backtest/sweep.py --pool short --family sma
  python3 backtest/sweep.py --pool daily --family dual
"""
import argparse
import bisect
import csv
import itertools
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
FEE = 0.0004
BE_BUF = 0.0015

POOLS = {
    'daily': {
        'tf': '1d', 'lev': 5.0, 'sl': 0.12, 'n': 50, 'min_notional': 5,
        'symbols': ['TSLA', 'COIN', 'PLTR', 'MSTR', 'HOOD'],
        'pg': dict(lock=4.0, trail=3.0, trail_min=4.0, give_back=0.5,
                   tp_scale=7.0, scale_ratio=0.5, tp_fast=12.0),
    },
    'short': {
        'tf': '1h', 'lev': 5.0, 'sl': 0.03, 'n': 50, 'min_notional': 5,
        'symbols': ['NVDA', 'META', 'AMZN', 'QQQ', 'SPY', 'GOOGL', 'INTC', 'CRCL'],
        'pg': dict(lock=2.5, trail=1.5, trail_min=2.5, give_back=0.5,
                   tp_scale=3.0, scale_ratio=0.5, tp_fast=5.0),
    },
}


def load(sym, tf):
    path = os.path.join(DATA, f'{sym}_{tf}.csv')
    if not os.path.exists(path):
        return None
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append((int(r['ts']), float(r['open']), float(r['high']),
                         float(r['low']), float(r['close'])))
    return rows


def sma_arr(x, w):
    c = np.cumsum(np.insert(x, 0, 0.0))
    out = np.full(len(x), np.nan)
    if len(x) >= w:
        out[w - 1:] = (c[w:] - c[:-w]) / w
    return out


def rolling_max(x, w):
    out = np.full(len(x), np.nan)
    for i in range(w, len(x)):
        out[i] = x[i - w:i].max()
    return out


def rolling_min(x, w):
    out = np.full(len(x), np.nan)
    for i in range(w, len(x)):
        out[i] = x[i - w:i].min()
    return out


def metrics(curve, ppy):
    arr = np.asarray(curve, dtype=float)
    if len(arr) < 2:
        return dict(total=0.0, ann=0.0, mdd=0.0, sharpe=0.0)
    total = arr[-1] / arr[0] - 1
    peak = np.maximum.accumulate(arr)
    mdd = float(((peak - arr) / peak).max()) if peak.max() > 0 else 0.0
    r = np.diff(arr) / arr[:-1]
    sd = float(r.std())
    sharpe = float(r.mean() / sd * math.sqrt(ppy)) if sd > 0 else 0.0
    ann = (1 + total) ** (ppy / len(arr)) - 1 if total > -1 else -1.0
    return dict(total=float(total), ann=float(ann), mdd=mdd, sharpe=sharpe)


def simulate(rows, cfg):
    """单标的回测。返回 (ts_list, equity_list, trades)。"""
    if not rows or len(rows) < 5:
        return [], [], []
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    o = np.array([r[1] for r in rows], dtype=float)
    h = np.array([r[2] for r in rows], dtype=float)
    l = np.array([r[3] for r in rows], dtype=float)
    c = np.array([r[4] for r in rows], dtype=float)
    n = len(c)
    margin = cfg['margin']
    lev = cfg['lev']
    fee = FEE
    fam = cfg['family']

    ma = sma_arr(c, cfg['n']) if fam in ('sma', 'dual', 'mom', 'sma_filter') else None
    fast = sma_arr(c, cfg.get('fast', 20)) if fam == 'dual' else None
    slow = sma_arr(c, cfg.get('slow', 50)) if fam == 'dual' else None
    ma20 = sma_arr(c, 20)
    hi_n = rolling_max(h, cfg['n']) if fam == 'donchian' else None
    lo_m = rolling_min(l, cfg.get('m', max(5, cfg['n'] // 2))) if fam == 'donchian' else None

    qty = 0.0
    entry = 0.0
    peak = 0.0
    stop = 0.0
    trimmed = False
    realized = 0.0
    trades = []
    curve = []
    pg = cfg.get('pg')

    for i in range(1, n):
        if curve and curve[-1] <= 0:
            break
        price = c[i]
        # 1) 硬止损（bar 内触发，跳空按开盘价）
        if qty > 0 and stop > 0 and l[i] <= stop:
            fill = o[i] if o[i] < stop else stop
            realized += (fill - entry) * qty - fill * qty * fee
            trades.append(dict(ts=int(ts[i]), pnl=(fill - entry) * qty, why='stop'))
            qty = 0.0
            entry = 0.0
            peak = 0.0
            stop = 0.0
            trimmed = False
            curve.append(max(0.0, margin + realized))
            continue

        # 2) T+0 利润保护（收盘价决策）
        if qty > 0 and pg:
            peak = max(peak, h[i])
            gain = (price / entry - 1) * 100
            pgain = (peak / entry - 1) * 100
            gave = pgain - gain
            be = entry * (1 + BE_BUF)
            if pgain >= pg['trail_min'] and gave >= max(pgain * pg['give_back'], pg['trail']):
                realized += (price - entry) * qty - price * qty * fee
                trades.append(dict(ts=int(ts[i]), pnl=(price - entry) * qty, why='trail'))
                qty = 0.0
                entry = 0.0
                peak = 0.0
                stop = 0.0
                trimmed = False
                curve.append(max(0.0, margin + realized))
                continue
            if (not trimmed) and pg['scale_ratio'] > 0 and gain >= pg['tp_scale']:
                q = qty * pg['scale_ratio']
                if price * q >= cfg.get('min_notional', 0):
                    realized += (price - entry) * q - price * q * fee
                    qty -= q
                    trimmed = True
                    stop = max(stop, be)
                    trades.append(dict(ts=int(ts[i]), pnl=(price - entry) * q, why='trim'))
            if qty > 0 and gain >= pg['tp_fast'] and not np.isnan(ma20[i]) and price < ma20[i]:
                realized += (price - entry) * qty - price * qty * fee
                trades.append(dict(ts=int(ts[i]), pnl=(price - entry) * qty, why='momentum'))
                qty = 0.0
                entry = 0.0
                peak = 0.0
                stop = 0.0
                trimmed = False
                curve.append(max(0.0, margin + realized))
                continue
            if qty > 0 and pgain >= pg['lock']:
                stop = max(stop, be)

        # 3) 趋势信号
        long_sig = False
        if fam == 'sma':
            long_sig = (not np.isnan(ma[i])) and price > ma[i]
        elif fam == 'dual':
            long_sig = (not np.isnan(fast[i]) and not np.isnan(slow[i])
                        and fast[i] > slow[i])
        elif fam == 'donchian':
            long_sig = (not np.isnan(hi_n[i])) and price > hi_n[i]
        elif fam == 'mom':
            if not np.isnan(ma[i]) and i >= cfg['n'] and c[i - cfg['n']] > 0:
                mom = price / c[i - cfg['n']] - 1
                long_sig = mom > cfg.get('th', 0.05)
            else:
                long_sig = False
        elif fam == 'hold':
            long_sig = True
        elif fam == 'sma_filter':
            long_sig = ((not np.isnan(ma[i])) and price > ma[i]
                        and (not np.isnan(ma20[i])) and ma[i] > ma20[i])

        if long_sig and qty == 0:
            notional = margin * lev
            qty = notional / price
            entry = price
            peak = price
            stop = entry * (1 - cfg['sl'])
            trimmed = False
            realized -= notional * fee
            trades.append(dict(ts=int(ts[i]), pnl=0.0, why='open'))
        elif (not long_sig) and qty > 0:
            realized += (price - entry) * qty - price * qty * fee
            trades.append(dict(ts=int(ts[i]), pnl=(price - entry) * qty, why='signal'))
            qty = 0.0
            entry = 0.0
            peak = 0.0
            stop = 0.0
            trimmed = False
        curve.append(max(0.0, margin + realized + ((price - entry) * qty if qty > 0 else 0.0)))

    if qty > 0:
        price = c[-1]
        realized += (price - entry) * qty - price * qty * fee
        trades.append(dict(ts=int(ts[-1]), pnl=(price - entry) * qty, why='eod'))
        curve[-1] = max(0.0, margin + realized)
    return [int(x) for x in ts[1:1 + len(curve)]], curve, trades


def align_pool(sym_curves, initial):
    """按时间戳对齐多标的净值曲线，返回 (ts, equity)。"""
    all_ts = sorted({t for ts, _ in sym_curves for t in ts})
    if not all_ts:
        return [], []
    out_ts, out_eq = [], []
    for t in all_ts:
        total = 0.0
        ok = True
        for ts, eq in sym_curves:
            j = bisect.bisect_right(ts, t) - 1
            if j < 0:
                ok = False
                break
            total += eq[j]
        if ok:
            out_ts.append(t)
            out_eq.append(total)
    return out_ts, out_eq


def run_pool(pool_name, cfg):
    pool = POOLS[pool_name]
    each = 100.0 / len(pool['symbols'])
    sym_curves = []
    all_trades = []
    used = []
    for sym in pool['symbols']:
        rows = load(sym, pool['tf'])
        if not rows:
            continue
        c = dict(cfg)
        c['margin'] = each
        ts, eq, tr = simulate(rows, c)
        if not eq:
            continue
        sym_curves.append((ts, eq))
        used.append(sym)
        all_trades.extend(tr)
    if not sym_curves:
        return None
    ts, eq = align_pool(sym_curves, 100.0)
    if len(eq) < 10:
        return None
    ppy = 365 if pool['tf'] == '1d' else 365 * 24
    full = metrics(eq, ppy)
    # 70/30 样本外（连续净值切片）
    cut = int(len(eq) * 0.7)
    train = metrics(eq[:cut + 1], ppy)
    if cut < len(eq) - 1:
        oos_curve = [eq[cut]] + eq[cut + 1:]
        oos = metrics(oos_curve, ppy)
    else:
        oos = dict(total=0.0, ann=0.0, mdd=0.0, sharpe=0.0)
    n_tr = len([t for t in all_trades if t['why'] != 'open'])
    return dict(pool=pool_name, cfg=cfg, used=used, n_sym=len(used),
                full=full, train=train, oos=oos, trades=n_tr,
                start=ts[0], end=ts[-1], curve=eq, ts=ts)


def fmt_cfg(cfg):
    parts = [cfg['family'], f"n={cfg['n']}", f"sl={cfg['sl']*100:.0f}%",
             f"lev={cfg['lev']:g}x", f"pg={'on' if cfg.get('pg') else 'off'}"]
    if cfg['family'] == 'dual':
        parts.insert(1, f"fast={cfg['fast']}/slow={cfg['slow']}")
    if cfg['family'] == 'donchian':
        parts.insert(1, f"exit={cfg['m']}")
    if cfg['family'] == 'mom':
        parts.insert(1, f"th={cfg['th']*100:.1f}%")
    return ' '.join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pool', default='daily', choices=list(POOLS))
    ap.add_argument('--family', default='sma',
                    choices=['sma', 'dual', 'donchian', 'mom', 'hold', 'sma_filter'])
    ap.add_argument('--top', type=int, default=8)
    a = ap.parse_args()
    pool = POOLS[a.pool]
    base_pg = pool['pg']

    configs = []
    if a.family in ('sma', 'sma_filter'):
        for n, sl, lev, pgon in itertools.product([20, 30, 40, 50, 60, 80, 100],
                                                  [0.05, 0.08, 0.10, 0.12, 0.15],
                                                  [3.0, 5.0], [False, True]):
            configs.append(dict(family=a.family, n=n, sl=sl, lev=lev,
                                pg=base_pg if pgon else None,
                                min_notional=pool['min_notional']))
    elif a.family == 'dual':
        for fast, slow, sl, lev, pgon in itertools.product([10, 20, 30, 50],
                                                           [50, 80, 100, 150],
                                                           [0.08, 0.12, 0.15],
                                                           [3.0, 5.0], [False, True]):
            if fast >= slow:
                continue
            configs.append(dict(family='dual', n=slow, fast=fast, slow=slow,
                                sl=sl, lev=lev, pg=base_pg if pgon else None,
                                min_notional=pool['min_notional']))
    elif a.family == 'donchian':
        for n, m, sl, lev, pgon in itertools.product([20, 30, 50, 80],
                                                     [10, 15, 20, 30],
                                                     [0.08, 0.12, 0.15],
                                                     [3.0, 5.0], [False, True]):
            configs.append(dict(family='donchian', n=n, m=m, sl=sl, lev=lev,
                                pg=base_pg if pgon else None,
                                min_notional=pool['min_notional']))
    elif a.family == 'mom':
        for n, th, sl, lev, pgon in itertools.product([10, 20, 30, 50],
                                                      [0.02, 0.05, 0.08],
                                                      [0.08, 0.12, 0.15],
                                                      [3.0, 5.0], [False, True]):
            configs.append(dict(family='mom', n=n, th=th, sl=sl, lev=lev,
                                pg=base_pg if pgon else None,
                                min_notional=pool['min_notional']))
    else:
        configs.append(dict(family='hold', n=50, sl=0.99, lev=pool['lev'],
                            pg=None, min_notional=pool['min_notional']))

    print(f'=== {a.pool} 池 · {a.family} · {len(configs)} 组参数 ===')
    print(f"标的: {', '.join(pool['symbols'])}")
    print(f'成本: 双边 {FEE*100:.2f}% · 前 70% 训练 / 后 30% 样本外\n')
    results = []
    total = len(configs)
    for i, cfg in enumerate(configs, 1):
        r = run_pool(a.pool, cfg)
        if r:
            results.append(r)
        if i % 20 == 0 or i == total:
            print(f'[progress] {i}/{total}', flush=True)
    if not results:
        print('没有可用数据，先跑 download_data.py')
        return 1

    # 选择依据：全期夏普为主，训练/样本外都要正；过拟合或爆仓的配置直接降权。
    for r in results:
        f = r['full']
        r['ruin'] = f['total'] <= -0.90
        r['score'] = (-999.0 if r['ruin'] else
                      0.5 * f['sharpe'] + 0.5 * min(r['train']['sharpe'], r['oos']['sharpe']))
    results.sort(key=lambda r: (r['score'], r['full']['sharpe']), reverse=True)

    r0 = results[0]
    import datetime
    st = datetime.datetime.fromtimestamp(r0['start'] / 1000).strftime('%Y-%m-%d')
    en = datetime.datetime.fromtimestamp(r0['end'] / 1000).strftime('%Y-%m-%d')
    print(f'数据窗口: {st} → {en}（{r0["n_sym"]} 个标的）\n')
    print(f"{'参数':46s} {'全期收益':>9s} {'回撤':>7s} {'夏普':>6s} "
          f"{'训练夏普':>8s} {'样本外收益':>10s} {'样本外夏普':>9s} {'交易':>5s}")
    print('-' * 118)
    for r in results[:a.top]:
        f, tr, oo = r['full'], r['train'], r['oos']
        print(f"{fmt_cfg(r['cfg']):46s} {f['total']*100:>8.1f}% {f['mdd']*100:>6.1f}% "
              f"{f['sharpe']:>6.2f} {tr['sharpe']:>8.2f} {oo['total']*100:>9.1f}% "
              f"{oo['sharpe']:>9.2f} {r['trades']:>5d}")

    # 参数邻域稳定性：按 n / sl 聚合，看中位数而不是单个最优
    print('\n=== 参数邻域稳定性（按 n 聚合，中位夏普）===')
    for n in sorted({r['cfg']['n'] for r in results}):
        sub = [r for r in results if r['cfg']['n'] == n]
        med_full = np.median([r['full']['sharpe'] for r in sub])
        med_oos = np.median([r['oos']['sharpe'] for r in sub])
        best = max(sub, key=lambda x: x['score'])
        print(f"n={n:<3d} 组数={len(sub):<3d} 全期中位夏普={med_full:>5.2f} "
              f"样本外中位夏普={med_oos:>5.2f} 最佳={fmt_cfg(best['cfg'])}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
