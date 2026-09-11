#!/usr/bin/env python3
"""股票 TRADIFI 永续池回测 v2 — 精确模拟 trader100
关键修正: 保证金不复利全仓。每标初始份额 = 池/标的数 作为保证金
杠杆 2x → 名义 = 保证金*2。开仓用名义/价 算张数
持仓期间: 池净值 = 现金 + 保证金占用 + 浮动盈亏 (c-entry)*张数
止损: 日最低 <= entry*(1-12%) → 按 entry*(1-12%) 平仓
真实费用: 开平各 0.04%
"""
import csv, datetime
import numpy as np

FEE = 0.0004
LEV = 2.0
SL = 0.12
SMA_N = 50

def load(path):
    rows = list(csv.DictReader(open(path)))
    return [(int(r['ts']), float(r['close']), float(r['high']), float(r['low'])) for r in rows]

def sma_arr(closes, w):
    c = np.cumsum(np.insert(closes, 0, 0.0))
    out = np.full(len(closes), np.nan)
    if len(closes) >= w:
        out[w-1:] = (c[w:] - c[:-w]) / w
    return out

def bt_single(path, margin=100/6, start=None, end=None):
    """单标的: 固定保证金 margin, 2x杠杆趋势策略
    返回 (净值曲线, 统计)
    """
    data = load(path)
    if start: data = [d for d in data if d[0] >= start]
    if end:   data = [d for d in data if d[0] <= end]
    closes = np.array([d[1] for d in data])
    ma = sma_arr(closes, SMA_N)
    cash = margin          # 未用资金
    pos = 0.0              # 持仓张数
    entry = 0.0
    trades = 0; wins = 0; stop_hits = 0
    curve = []
    for i in range(1, len(data)):
        ts, c, h, l = data[i]
        # 1. 止损检查
        if pos > 0:
            sl_price = entry * (1 - SL)
            if l <= sl_price:
                # 平仓: 保证金亏 (entry-sl)/entry, 加费用
                pnl = (sl_price - entry) * pos
                cash = margin + pnl - margin * FEE * 2
                trades += 1; stop_hits += 1
                if pnl > 0: wins += 1
                pos = 0; entry = 0
                curve.append(cash)
                continue
        # 2. 信号
        sig = (not np.isnan(ma[i])) and closes[i] > ma[i]
        if sig and pos == 0:
            # 开仓: 全保证金投, 名义 = margin*LEV
            notional = margin * LEV
            pos = notional / c
            entry = c
            cash = margin  # 保证金已投
        elif not sig and pos > 0:
            # 平仓离场
            pnl = (c - entry) * pos
            cash = margin + pnl - margin * FEE * 2
            trades += 1
            if pnl > 0: wins += 1
            pos = 0; entry = 0
        # 3. 净值
        if pos > 0:
            equity = margin + (c - entry) * pos
        else:
            equity = cash
        curve.append(equity)
    if pos > 0:  # 末日平
        trades += 1
        pnl_e = (closes[-1]-entry)*pos - margin*FEE*2
        if pnl_e > 0: wins += 1
        curve[-1] = margin + pnl_e
    eq = np.array(curve)
    total = eq[-1]/margin - 1
    days = len(eq)
    ann = (1+total)**(365.25/days)-1 if total > -1 and days > 0 else -1
    peak = np.maximum.accumulate(eq)
    mdd = ((peak-eq)/peak).max() if len(eq) else 0
    r = np.diff(eq)/eq[:-1]
    sharpe = (r.mean()/r.std()*np.sqrt(365)) if r.std() > 0 and len(r) > 1 else 0
    winr = wins/max(trades,1)*100
    return dict(total=total, ann=ann, mdd=mdd, sharpe=sharpe, trades=trades,
                winr=winr, stops=stop_hits, days=days, curve=eq, name=path.split('/')[-1].replace('_1d.csv',''))

def bt_pool(files, initial=100.0, start=None, end=None):
    n = len(files)
    each = initial / n
    res = []
    curves = []
    for f in files:
        r = bt_single(f, margin=each, start=start, end=end)
        res.append(r); curves.append(r['curve'])
    L = min(len(c) for c in curves)
    pool = np.sum([c[:L] for c in curves], axis=0)
    total = pool[-1]/initial - 1
    peak = np.maximum.accumulate(pool)
    mdd = ((peak-pool)/peak).max()
    r = np.diff(pool)/pool[:-1]
    sharpe = (r.mean()/r.std()*np.sqrt(365)) if r.std() > 0 else 0
    return dict(total=total, ann=ann_from(total, L), mdd=mdd, sharpe=sharpe, days=L, curve=pool, per=res)

def ann_from(total, days):
    return (1+total)**(365.25/days)-1 if total > -1 and days > 0 else -1

if __name__ == '__main__':
    end = int(datetime.datetime(2026,9,6).timestamp()*1000)
    start = int(datetime.datetime(2026,3,6).timestamp()*1000)
    allf = ['TSLA','COIN','PLTR','MSTR','HOOD','XAU']
    files = [f'backtest/{f}_1d.csv' for f in allf]
    print(f'=== 股票池回测 v2 (2026-03-06→09-05 半年) ===')
    print(f'策略: SMA{SMA_N} 日线趋势, {LEV}x, -{SL*100:.0f}%止损 | 池100U 等权{len(files)}标 每标保证金{100/len(files):.1f}U\n')
    print(f'{"标的":8s} {"收益":>8s} {"年化":>8s} {"回撤":>7s} {"夏普":>6s} {"交易":>4s} {"胜率":>6s} {"止损":>4s}')
    for f in files:
        r = bt_single(f, margin=100/len(files), start=start, end=end)
        print(f"{r['name']:8s} {r['total']*100:7.1f}% {r['ann']*100:7.1f}% {r['mdd']*100:6.1f}% {r['sharpe']:6.2f} {r['trades']:4d} {r['winr']:5.1f}% {r['stops']:4d}")
    pool = bt_pool(files, initial=100.0, start=start, end=end)
    print(f"\n=== 组合 (等权6标, 独立信号) ===")
    print(f"总收益 {pool['total']*100:.1f}% | 年化 {pool['ann']*100:.1f}% | 最大回撤 {pool['mdd']*100:.1f}% | 夏普 {pool['sharpe']:.2f} | {pool['days']}天")
    # 对照买入持有
    print(f"\n{'标的':8s} {'半年买入持有':>12s} {'策略收益':>10s}")
    for f in files:
        data = [d for d in load(f) if d[0] >= start and d[0] <= end]
        bh = data[-1][1]/data[0][1]-1 if len(data) >= 2 else float('nan')
        r = bt_single(f, margin=100/len(files), start=start, end=end)
        print(f"{r['name']:8s} {bh*100:11.1f}% {r['total']*100:9.1f}%")
