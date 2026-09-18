#!/usr/bin/env python3
"""杠杆扫描回测 — 过去 N 个月, SMA 日线趋势策略, 对比不同杠杆。

与 stock_pool_bt.py 的区别（关键）:
  1) 先用全量数据算 SMA 再截取回测区间 → 避免预热期被砍掉导致前 50 天无信号
  2) 杠杆/参数全部参数化（命令行或环境变量）
  3) 输出多档杠杆横向对比

策略（与 trader100 / trader_short 一致）:
  收盘价 > SMA(N) → 持仓; 跌破 → 离场; 日最低 <= entry*(1-SL) → 止损
  池等权分配保证金, 名义 = 保证金 * LEV, 开平各 0.04% 费用

用法:
  python3 lev_sweep.py --pool p100 --months 3 --lev 2,3,5
  python3 lev_sweep.py --pool short --months 3 --lev 2,3,5
"""
import argparse
import csv
import datetime
import os

import numpy as np

FEE = 0.0004
SL = 0.12          # 日线池止损 -12%（trader100）
SL_SHORT = 0.03    # 短线池止损 -3%（trader_short）
HERE = os.path.dirname(os.path.abspath(__file__))

POOLS = {
    # trader100.py: TSLA/COIN/PLTR/MSTR/HOOD 各 20%, SMA50, 日线
    "p100": {"files": ["tsla", "coin", "PLTR", "MSTR", "HOOD"], "sma": 50, "sl": SL},
    # trader_short.py: 8 标各 12.5%, SMA50（实际为 1h 线，此处用日线近似）
    "short": {"files": ["NVDA", "META", "AMZN", "QQQ", "SPY", "GOOGL", "INTC", "CRCL"],
              "sma": 50, "sl": SL_SHORT},
    # 合并全池
    "all": {"files": ["tsla", "coin", "PLTR", "MSTR", "HOOD", "NVDA", "META", "AMZN",
                      "QQQ", "SPY", "GOOGL", "INTC", "CRCL"], "sma": 50, "sl": SL},
}


def load(name):
    path = os.path.join(HERE, f"{name}_1d.csv")
    if not os.path.exists(path):
        return None
    rows = list(csv.DictReader(open(path)))
    return [(int(r["ts"]), float(r["close"]), float(r["high"]), float(r["low"])) for r in rows]


def sma_arr(closes, w):
    c = np.cumsum(np.insert(closes, 0, 0.0))
    out = np.full(len(closes), np.nan)
    if len(closes) >= w:
        out[w - 1:] = (c[w:] - c[:-w]) / w
    return out


def bt_single(name, margin, start_ts, end_ts, lev, sma_n, sl):
    """单标的回测：全量算 SMA，仅在 [start,end] 区间内交易。"""
    data = load(name)
    if not data:
        return None
    ts = np.array([d[0] for d in data])
    closes = np.array([d[1] for d in data])
    lows = np.array([d[3] for d in data])
    ma = sma_arr(closes, sma_n)
    live = (ts >= start_ts) & (ts <= end_ts)
    if live.sum() < 5:
        return None

    cash = margin
    pos = 0.0
    entry = 0.0
    trades = wins = stops = 0
    curve = []
    for i in range(len(data)):
        if not live[i]:
            continue
        c, l = closes[i], lows[i]
        if pos > 0:
            slp = entry * (1 - sl)
            if l <= slp:
                pnl = (slp - entry) * pos
                cash = margin + pnl - margin * FEE * 2
                trades += 1
                stops += 1
                if pnl > 0:
                    wins += 1
                pos = entry = 0.0
                curve.append(cash)
                continue
        sig = (not np.isnan(ma[i])) and c > ma[i]
        if sig and pos == 0:
            pos = (margin * lev) / c
            entry = c
            cash = margin
        elif not sig and pos > 0:
            pnl = (c - entry) * pos
            cash = margin + pnl - margin * FEE * 2
            trades += 1
            if pnl > 0:
                wins += 1
            pos = entry = 0.0
        curve.append(margin + (c - entry) * pos if pos > 0 else cash)
    if pos > 0:
        trades += 1
        pnl = (closes[-1] - entry) * pos - margin * FEE * 2
        if pnl > 0:
            wins += 1
        curve[-1] = margin + pnl

    eq = np.array(curve)
    total = eq[-1] / margin - 1
    peak = np.maximum.accumulate(eq)
    mdd = ((peak - eq) / peak).max() if len(eq) else 0.0
    r = np.diff(eq) / eq[:-1] if len(eq) > 1 else np.array([0.0])
    sharpe = (r.mean() / r.std() * np.sqrt(365)) if r.std() > 0 else 0.0
    wiped = total <= -0.999   # 爆仓（权益归零）
    return dict(name=name, total=total, mdd=mdd, sharpe=sharpe, trades=trades,
                winr=wins / max(trades, 1) * 100, stops=stops, curve=eq, wiped=wiped)


def bt_pool(pool, lev, months):
    cfg = POOLS[pool]
    files = [f for f in cfg["files"] if load(f)]
    end_ts = max(load(f)[-1][0] for f in files)
    start_ts = int((datetime.datetime.fromtimestamp(end_ts / 1000, datetime.UTC)
                    - datetime.timedelta(days=int(months * 30.5))).timestamp() * 1000)
    each = 100.0 / len(files)
    res = [bt_single(f, each, start_ts, end_ts, lev, cfg["sma"], cfg["sl"]) for f in files]
    res = [r for r in res if r]
    L = min(len(r["curve"]) for r in res)
    pool_curve = np.sum([r["curve"][:L] for r in res], axis=0)
    total = pool_curve[-1] / 100.0 - 1
    peak = np.maximum.accumulate(pool_curve)
    mdd = ((peak - pool_curve) / peak).max()
    r = np.diff(pool_curve) / pool_curve[:-1]
    sharpe = (r.mean() / r.std() * np.sqrt(365)) if r.std() > 0 else 0.0
    ann = (1 + total) ** (365.25 / L) - 1 if total > -1 and L > 0 else -1
    return dict(pool=pool, lev=lev, total=total, ann=ann, mdd=mdd, sharpe=sharpe,
                days=L, per=res,
                start=datetime.datetime.fromtimestamp(start_ts / 1000, datetime.UTC).date(),
                end=datetime.datetime.fromtimestamp(end_ts / 1000, datetime.UTC).date())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="p100", choices=list(POOLS))
    ap.add_argument("--months", type=float, default=3)
    ap.add_argument("--lev", default="2,3,5", help="逗号分隔的杠杆档位")
    a = ap.parse_args()
    levs = [float(x) for x in a.lev.split(",")]

    print(f"=== 杠杆扫描回测 · 池={a.pool} · 窗口={a.months}个月 ===")
    print(f"策略: SMA50 日线趋势, 止损 -{POOLS[a.pool]['sl']*100:.0f}%, 等权 {len(POOLS[a.pool]['files'])} 标, 池 100U\n")

    rows = []
    for lev in levs:
        r = bt_pool(a.pool, lev, a.months)
        rows.append(r)
    r0 = rows[0]
    print(f"窗口: {r0['start']} → {r0['end']}（{r0['days']} 个交易日）\n")
    print(f"{'杠杆':>5} {'总收益':>9} {'年化':>9} {'最大回撤':>9} {'夏普':>6} {'爆仓':>5}")
    for r in rows:
        print(f"{r['lev']:>4.0f}x {r['total']*100:>8.1f}% {r['ann']*100:>8.1f}% "
              f"{r['mdd']*100:>8.1f}% {r['sharpe']:>6.2f} {'是' if any(x['wiped'] for x in r['per']) else '否':>5}")
    print(f"\n{'标的':8s}" + "".join(f"{'%gx收益' % r['lev']:>10s}{'%gx回撤' % r['lev']:>10s}"
                                    for r in rows))
    for i, f in enumerate([x["name"] for x in r0["per"]]):
        line = f"{f:8s}"
        for r in rows:
            p = r["per"][i]
            line += f"{p['total']*100:>9.1f}%{p['mdd']*100:>9.1f}%"
        print(line)


if __name__ == "__main__":
    main()
