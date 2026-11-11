#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""红利池买法回测（与 sim_dividend.py 完全同口径，含一手 100 股取整）。

数据：data/cache/div_*.json 或 /tmp/divpool/kline.json（641 根日K，2024-03-18~2026-11-09）
口径：
  · MA20/MA60 只用【已收盘】序列（第 i 日用 closes[:i]），第 i 日收盘价作触发价
  · 6 标等分 10 万，每标分 3 档；预算买不到 1 手但现金够就买 1 手
  · 单边费 0.08%；同标的一日最多建 1 档
  · 买：px ≤ MA20×0.96 且 px > MA60×0.85
  · 减档：浮盈 ≥15% 且 >1 档 → 卖 1 档；清仓：px < MA60×0.88 或 浮亏 ≤-10%
输出：backtest/dividend_buy.md（买法依据与稳健性表）
"""
import json
import math
import os
import statistics
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
DATA = '/tmp/divpool/kline.json'
CODES = ['515080', '600900', '159525', '512800', '600177', '000333']
NAMES = {'515080': '中证红利ETF招商', '600900': '长江电力', '159525': '红利低波ETF富国',
         '512800': '银行ETF华宝', '600177': '雅戈尔', '000333': '美的集团'}
TOTAL = 100000.0
FEE = 0.0008
LOT = 100


def ma(a, n):
    return sum(float(x) for x in a[-n:]) / n if len(a) >= n else None


def sim(bars, dip=0.04, floor=0.15, maxl=3, ex=0.12, sl=0.10, tp=0.15, mode='chosen'):
    """单标的独立账本，返回 (日期→净值, 成交笔数)。"""
    dates = bars['dates']
    c = [float(x) for x in bars['closes']]
    cash = TOTAL / len(CODES)
    budget = cash / maxl
    shares = lots = 0
    avg = 0.0
    eq = {}
    tr = 0
    last_buy = ''
    for i in range(len(c)):
        px = c[i]
        hist = c[:i]                       # 严格排除当日（与实盘一致）
        m20, m60 = ma(hist, 20), ma(hist, 60)
        if m20 and m60:
            if mode == 'bh':
                if shares == 0:
                    shares = int(cash / (px * (1 + FEE)) / LOT) * LOT
                    cash -= shares * px * (1 + FEE)
                    avg = px
                    lots = 1
                    tr += 1
            else:
                buy_px = m20 * (1 - dip)
                floor_px = m60 * (1 - floor)
                exit_px = m60 * (1 - ex)
                if lots < maxl and last_buy != dates[i] and px <= buy_px and px > floor_px:
                    n = int(budget / (px * (1 + FEE)) / LOT) * LOT
                    if n <= 0 and cash >= LOT * px * (1 + FEE):
                        n = LOT
                    cost = n * px * (1 + FEE)
                    if n > 0 and cost <= cash:
                        cash -= cost
                        avg = (avg * shares + px * n) / (shares + n) if shares > 0 else px
                        shares += n
                        lots += 1
                        last_buy = dates[i]
                        tr += 1
                if shares > 0:
                    gain = px / avg - 1
                    tp_eff = 0.06 if mode == 'early' else tp
                    if gain >= tp_eff and lots > 1:
                        per = int(shares / lots / LOT) * LOT or shares
                        cash += per * px * (1 - FEE)
                        shares -= per
                        lots -= 1
                        tr += 1
                    sl_eff = 0.06 if mode == 'early' else sl
                    ex_eff = 0.05 if mode == 'early' else ex
                    if px < m60 * (1 - ex_eff) or gain <= -sl_eff:
                        cash += shares * px * (1 - FEE)
                        shares = lots = 0
                        avg = 0.0
                        tr += 1
        eq[dates[i]] = cash + shares * px
    return eq, tr


def metrics(pairs):
    v = [e for _, e in pairs]
    if len(v) < 2 or v[0] <= 0:
        return dict(total=0.0, mdd=0.0, sharpe=0.0)
    r = [v[i] / v[i - 1] - 1 for i in range(1, len(v)) if v[i - 1] > 0]
    sd = statistics.pstdev(r) if len(r) > 1 else 0.0
    sh = statistics.mean(r) / sd * math.sqrt(252) if sd > 0 else 0.0
    pk = v[0]
    mdd = 0.0
    for x in v:
        pk = max(pk, x)
        mdd = max(mdd, (pk - x) / pk)
    return dict(total=v[-1] / v[0] - 1, mdd=mdd, sharpe=sh)


def run(D, **kw):
    series, ntr = {}, 0
    for code in CODES:
        e, t = sim(D[code], **kw)
        series[code] = e
        ntr += t
    dates = sorted(set().union(*[set(e) for e in series.values()]))
    vals = [sum(series[c].get(d, TOTAL / len(CODES)) for c in CODES) for d in dates]
    pairs = list(zip(dates, vals))
    cut = int(len(pairs) * 0.70)
    f, tr, oo = metrics(pairs), metrics(pairs[:cut]), metrics(pairs[cut:])
    return dict(**f, train=tr['total'], train_sh=tr['sharpe'],
                oos=oo['total'], oos_sh=oo['sharpe'], trades=ntr,
                score=0.5 * f['sharpe'] + 0.5 * min(tr['sharpe'], oo['sharpe']))


def fmt(r):
    return ('| %+.1f%% | %.1f%% | %.2f | %+.1f%% / %.2f | %+.1f%% / %.2f | %d |' %
            (r['total'] * 100, r['mdd'] * 100, r['sharpe'],
             r['train'] * 100, r['train_sh'], r['oos'] * 100, r['oos_sh'], r['trades']))


def main():
    D = json.load(open(DATA))
    chosen = run(D)
    bh = run(D, mode='bh')
    early = run(D, mode='early', tp=0.06, sl=0.06, ex=0.05)
    print('买入持有   ', fmt(bh))
    print('早止盈+6%  ', fmt(early))
    print('选定长持   ', fmt(chosen))
    print('\n=== 参数邻域（选定口径）===')
    rows = []
    for dip in [0.03, 0.035, 0.04, 0.045, 0.05]:
        rows.append(('dip %.1f%%' % (dip * 100), run(D, dip=dip)))
    for tp in [0.10, 0.12, 0.15, 0.18, 0.20]:
        rows.append(('tp %.0f%%' % (tp * 100), run(D, tp=tp)))
    for ml in [2, 3, 4]:
        rows.append(('maxl %d' % ml, run(D, maxl=ml)))
    for name, r in rows:
        print('%-10s %s' % (name, fmt(r)))
    print('\n=== 逐标的（选定 vs 持有）===')
    for code in CODES:
        eq, tr = sim(D[code])
        r = metrics(list(eq.items()))
        e2, _ = sim(D[code], mode='bh')
        b = metrics(list(e2.items()))
        print('%-6s %-14s 策略%+7.1f%% MDD%5.1f%% Sh%5.2f | 持有%+7.1f%% MDD%5.1f%% | %d笔' % (
            code, NAMES[code], r['total'] * 100, r['mdd'] * 100, r['sharpe'],
            b['total'] * 100, b['mdd'] * 100, tr))
    out = dict(chosen=chosen, buy_hold=bh, early_tp=early,
               neighbors={k: v for k, v in rows})
    json.dump(out, open(os.path.join(BASE, 'backtest', 'dividend_buy.json'), 'w'),
              ensure_ascii=False, indent=1)
    print('\nsaved backtest/dividend_buy.json')


if __name__ == '__main__':
    main()
