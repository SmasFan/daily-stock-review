#!/usr/bin/env python3
"""A方案: 日线级趋势跟随 (长周期均线 + 通道)
对比买入持有, 含成本。重点看能否跑赢并控回撤
"""
import csv, math
import numpy as np

rows = []
with open('btc_1d.csv') as f:
    for r in csv.DictReader(f):
        rows.append({'t': int(r['ts']), 'c': float(r['close']), 'h': float(r['high']), 'l': float(r['low'])})
close = np.array([r['c'] for r in rows])
n = len(close)
FEE = 0.0015

def sma(x, w):
    c = np.cumsum(np.insert(x, 0, 0.0)); out = np.full(len(x), np.nan)
    if len(x) >= w: out[w-1:] = (c[w:] - c[:-w])/w
    return out

def bt(sig_long, name, initial=10000):
    cash = initial; pos = 0; bc = 0; trades = 0; wins = 0
    eqc = []
    for i in range(1, n):
        p = close[i]
        t = 1 if sig_long[i] else 0
        if t == 1 and pos == 0:
            amt = cash*(1-FEE)/p; bc = cash; cash = 0; pos = amt; trades += 1
        elif t == 0 and pos > 0:
            cash = pos*p*(1-FEE)
            if cash > bc: wins += 1
            pos = 0
        eqc.append(cash + pos*p)
    eq = np.array(eqc)
    total = eq[-1]/initial - 1
    years = (rows[-1]['t']-rows[0]['t'])/(365.25*24*3600*1000)
    ann = (1+total)**(1/years)-1 if total > -1 else -1
    peak = np.maximum.accumulate(eq); mdd = ((peak-eq)/peak).max()
    rr = np.diff(eq)/eq[:-1]
    sharpe = 0 if rr.std()==0 else rr.mean()/rr.std()*math.sqrt(365)
    return dict(name=name, ret=total, ann=ann, mdd=mdd, sharpe=sharpe, tr=trades, win=wins/max(trades,1)*100)

res = []
# 基准
bh = np.ones(n, bool)
res.append(bt(bh, '买入持有'))

# 双均线 日线
for f, s in [(20,50),(20,100),(50,100),(50,200)]:
    sf, ss = sma(close, f), sma(close, s)
    sig = np.zeros(n, bool); holding = False
    for i in range(1, n):
        if not holding and not np.isnan(sf[i]) and not np.isnan(ss[i]) and sf[i] > ss[i]: holding = True
        elif holding and not np.isnan(sf[i]) and not np.isnan(ss[i]) and sf[i] < ss[i]: holding = False
        sig[i] = holding
    res.append(bt(sig, f'均线{f}/{s}'))

# 价格 vs 长均线 (定投式: 价格在200日均线上才持有)
for s in [100, 200]:
    ss = sma(close, s)
    sig = np.zeros(n, bool)
    for i in range(1, n):
        if not np.isnan(ss[i]):
            sig[i] = close[i] > ss[i]
    res.append(bt(sig, f'价>均线{s}'))

print(f"\n{'策略':<16}{'总收益':>9}{'年化':>8}{'回撤':>9}{'夏普':>7}{'交易':>5}{'胜率':>7}")
print('-'*62)
for r in sorted(res, key=lambda x: -x['sharpe']):
    print(f"{r['name']:<16}{r['ret']*100:>8.1f}%{r['ann']*100:>7.1f}%{r['mdd']*100:>8.1f}%{r['sharpe']:>7.2f}{r['tr']:>5}{r['win']:>6.1f}%")
