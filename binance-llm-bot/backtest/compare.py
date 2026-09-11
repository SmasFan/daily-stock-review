#!/usr/bin/env python3
"""BTC/USDT 4h 多策略回测对比
成本模型: 手续费 0.1%/边 (maker+taker 均按 0.1% 保守) + 滑点 0.05%/边 = 0.15%/边
策略: 金叉/均线趋势, RSI均值回归, 布林带突破, MACD, 唐奇安通道突破, 动量
指标: 收益/年化/最大回撤/夏普/胜率/交易次数
"""
import csv, math
import numpy as np

# ---------- 加载数据 ----------
rows = []
with open('btc_4h.csv') as f:
    for r in csv.DictReader(f):
        rows.append({'t': int(r['ts']), 'o': float(r['open']), 'h': float(r['high']),
                     'l': float(r['low']), 'c': float(r['close']), 'v': float(r['volume'])})
close = np.array([r['c'] for r in rows])
high = np.array([r['h'] for r in rows])
low = np.array([r['l'] for r in rows])
n = len(close)
print(f'数据: {n} 根 4h K线, {rows[0]["t"]} ~ {rows[-1]["t"]}')

FEE = 0.0015  # 每边 0.15% (手续费0.1%+滑点0.05%)

def sma(x, w):
    c = np.cumsum(np.insert(x, 0, 0.0))
    out = np.full(len(x), np.nan)
    if len(x) >= w:
        out[w-1:] = (c[w:] - c[:-w]) / w
    return out

def ema(x, w):
    alpha = 2/(w+1)
    out = np.full(len(x), np.nan)
    if len(x) == 0: return out
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = alpha*x[i] + (1-alpha)*out[i-1] if not np.isnan(out[i-1]) else x[i]
    return out

def rsi(x, w=14):
    out = np.full(len(x), 50.0)
    for i in range(1, len(x)):
        chg = x[i]-x[i-1]
        gain = max(chg, 0); loss = max(-chg, 0)
        # 简化 Wilder
        if i == 1:
            ag, al = gain, loss
        else:
            ag = (ag*(w-1)+gain)/w; al = (al*(w-1)+loss)/w
        out[i] = 100 - 100/(1+ (ag/al if al>0 else 100))
    return out

def macd(x, fast=12, slow=26, sig=9):
    ef, es = ema(x, fast), ema(x, slow)
    line = ef - es
    return line, ema(line[~np.isnan(line)] if False else np.nan_to_num(line), sig)

# ---------- 回测引擎 (配对统计胜率) ----------
def backtest(sig_long, sig_short, name, initial=10000.0):
    """简化: 全仓做多/空仓。买卖配对算胜率。"""
    cash = initial; pos = 0.0; buy_cost = 0.0
    trades = 0; wins = 0; open_pnl = []
    equity_curve = []
    for i in range(1, n):
        price = close[i]
        target = 1.0 if sig_long[i] else 0.0
        if target == 1 and pos == 0:  # 开多
            amt = cash*(1-FEE)/price
            buy_cost = cash           # 记录投入本金
            cash = 0; pos = amt; trades += 1
        elif target == 0 and pos > 0:  # 平多
            cash = pos*price*(1-FEE)
            if cash > buy_cost: wins += 1
            pos = 0; buy_cost = 0
        eq = cash + pos*price
        equity_curve.append(eq)
    eq = np.array(equity_curve)
    total_ret = eq[-1]/initial - 1
    years = (rows[-1]['t'] - rows[0]['t'])/ (365.25*24*3600*1000)
    ann = (1+total_ret)**(1/years) - 1 if total_ret > -1 else -1
    peak = np.maximum.accumulate(eq)
    mdd = ((peak-eq)/peak).max()
    r = np.diff(eq)/eq[:-1]
    sharpe = 0 if r.std()==0 else r.mean()/r.std()*math.sqrt(6*365)
    wins_pct = wins/max(trades,1)
    return {'name': name, 'ret': total_ret, 'ann': ann, 'mdd': mdd,
            'sharpe': sharpe, 'trades': trades, 'win%': wins_pct, 'final': eq[-1]}

# ---------- 基准: 买入持有 ----------
buyhold = backtest(np.ones(n, bool), np.zeros(n, bool), '买入持有')
print(f"\n基准 买入持有: 总收益 {buyhold['ret']*100:.1f}% 年化 {buyhold['ann']*100:.1f}% 回撤 {buyhold['mdd']*100:.1f}%")

results = [buyhold]

# 1. 双均线金叉死叉 (20/50)
s20, s50 = sma(close, 20), sma(close, 50)
sig = np.zeros(n, bool)
for i in range(n):
    if i > 0 and not np.isnan(s20[i]) and not np.isnan(s50[i]):
        if s20[i] > s50[i] and s20[i-1] <= s50[i-1]: sig[i] = True
sig_l = np.zeros(n, bool); above = False
for i in range(n):
    if not np.isnan(s20[i]) and not np.isnan(s50[i]):
        above = s20[i] > s50[i]
    sig_l[i] = above
results.append(backtest(sig_l, ~sig_l, '双均线20/50'))

# 2. RSI 均值回归 (<30买 >70卖)
r = rsi(close)
sig_l = np.zeros(n, bool); holding = False
for i in range(1, n):
    if not holding and r[i] < 30 and r[i-1] >= 30: sig_l[i] = True; holding = True
    elif holding and r[i] > 70 and r[i-1] <= 70: sig_l[i] = False; holding = False
    sig_l[i] = holding
results.append(backtest(sig_l, ~sig_l, 'RSI均值回归'))

# 3. 布林带 (20, 2σ) 突破/回归
m20 = sma(close, 20); sd = np.full(n, np.nan)
for i in range(19, n):
    sd[i] = close[i-19:i+1].std()
sig_l = np.zeros(n, bool); holding = False
for i in range(1, n):
    if not holding and not np.isnan(m20[i]) and close[i] < m20[i]-2*sd[i]: holding = True  # 下轨买
    elif holding and not np.isnan(m20[i]) and close[i] > m20[i]: holding = False  # 中轨卖
    sig_l[i] = holding
results.append(backtest(sig_l, ~sig_l, '布林带回归'))

# 4. MACD
line, sigline = macd(close)
sig_l = np.zeros(n, bool); holding = False
for i in range(1, n):
    if not holding and line[i] > sigline[i] and line[i-1] <= sigline[i-1]: holding = True
    elif holding and line[i] < sigline[i] and line[i-1] >= sigline[i-1]: holding = False
    sig_l[i] = holding
results.append(backtest(sig_l, ~sig_l, 'MACD金叉死叉'))

# 5. 唐奇安通道 20/10 突破
sig_l = np.zeros(n, bool); holding = False
for i in range(1, n):
    if not holding and i >= 20 and close[i] > max(high[i-20:i]): holding = True
    elif holding and i >= 10 and close[i] < min(low[i-10:i]): holding = False
    sig_l[i] = holding
results.append(backtest(sig_l, ~sig_l, '唐奇安突破'))

# 6. 动量 (过去 96 根 4h=16天 涨跌)
mom = np.full(n, np.nan)
for i in range(96, n):
    mom[i] = close[i]/close[i-96] - 1
sig_l = np.zeros(n, bool)
for i in range(1, n):
    if not np.isnan(mom[i]):
        sig_l[i] = mom[i] > 0.05 or (sig_l[i-1] and mom[i] > -0.02)  # 突破5%进, 回落2%出
results.append(backtest(sig_l, ~sig_l, '动量16日'))

# ---------- 输出对比 ----------
print(f"\n{'策略':<14}{'总收益':>9}{'年化':>8}{'最大回撤':>10}{'夏普':>7}{'交易':>6}{'胜率':>7}")
print('-'*65)
for r in sorted(results, key=lambda x: -x['sharpe']):
    print(f"{r['name']:<14}{r['ret']*100:>8.1f}%{r['ann']*100:>7.1f}%{r['mdd']*100:>9.1f}%{r['sharpe']:>7.2f}{r['trades']:>6}{r['win%']*100:>6.1f}%")
