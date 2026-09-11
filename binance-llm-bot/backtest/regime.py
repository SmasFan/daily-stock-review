#!/usr/bin/env python3
"""分 regime 测试: 双均线20/50 + RSI 在 上升/下降/震荡 段的表现
用 200根(33天) SMA 斜率 + 波动率 定义 regime
验证: 状态过滤能否避免横盘磨损
"""
import csv, math
import numpy as np

rows = []
with open('btc_4h.csv') as f:
    for r in csv.DictReader(f):
        rows.append({'t': int(r['ts']), 'o': float(r['open']), 'h': float(r['high']),
                     'l': float(r['low']), 'c': float(r['close']), 'v': float(r['volume'])})
close = np.array([r['c'] for r in rows])
n = len(close)

def sma(x, w):
    c = np.cumsum(np.insert(x, 0, 0.0))
    out = np.full(len(x), np.nan)
    if len(x) >= w: out[w-1:] = (c[w:] - c[:-w])/w
    return out

# regime: 用 长均线斜率 (SMA200 过去 200根变化) 
# slope>0.5%/200根 = 上升, <-0.5% = 下降, 否则震荡
# 波动过滤: 近 96 根 (16天) 波动率
s200 = sma(close, 200)
regime = np.zeros(n)  # 0=震荡 1=上升 -1=下降
for i in range(200, n):
    slope = (s200[i]/s200[i-100] - 1)*100  # 过去100根斜率%
    if slope > 0.6: regime[i] = 1
    elif slope < -0.6: regime[i] = -1
    else: regime[i] = 0

# 统计各 regime 占时
for name, val in [('上升',1), ('震荡',0), ('下降',-1)]:
    mask = regime[200:] == val
    seg_ret = (close[200:][mask][-1] if mask.any() else 1)
    # 该regime累计K线涨幅
    idxs = np.where(regime[200:]==val)[0]
    if len(idxs)>0:
        first = 200+idxs[0]; last = 200+idxs[-1]
        r = close[last]/close[first]-1
        print(f'{name}: 占比 {mask.sum()/max(len(mask),1)*100:.0f}%  期间BTC {r*100:+.1f}%')

# 策略在各 regime: 只在上升/震荡开
def test_in_regime(regime_ok):
    sig = np.zeros(n, bool)
    holding = False
    s20, s50 = sma(close, 20), sma(close, 50)
    for i in range(1, n):
        if regime_ok[i] and not holding and not np.isnan(s20[i]) and s20[i] > s50[i]:
            holding = True
        elif holding and (not np.isnan(s20[i]) and s20[i] < s50[i]):
            holding = False
        sig[i] = holding
    return sig

FEE = 0.0015
def bt(sig, name, initial=10000):
    cash = initial; pos=0; bc=0; trades=0; wins=0
    for i in range(1, n):
        p = close[i]
        t = 1 if sig[i] else 0
        if t==1 and pos==0:
            amt = cash*(1-FEE)/p; bc=cash; cash=0; pos=amt; trades+=1
        elif t==0 and pos>0:
            cash = pos*p*(1-FEE)
            if cash>bc: wins+=1
            pos=0
    eq = cash+pos*close[-1]
    return eq, trades, wins

# 全时段 vs 只在上升段 vs 上升+震荡
buyhold_eq,_,_ = bt(np.ones(n,bool),'x')
print(f'\n基准 买入持有 终值: {buyhold_eq:.0f}')

for label, ok in [('仅上升regime', np.array([r==1 for r in regime])),
                   ('上升+震荡(避开下跌)', np.array([r!= -1 for r in regime]))]:
    sig = test_in_regime(ok)
    eq, tr, wi = bt(sig, label)
    print(f'{label}: 终值 {eq:.0f}  (+{(eq/10000-1)*100:.0f}%)  交易{tr} 胜率{wi/max(tr,1)*100:.0f}%')
