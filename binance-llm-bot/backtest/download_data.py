#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载 TRADIFI 股票永续的日线/1h K 线到 backtest/data/*.csv。

用法:
  python3 backtest/download_data.py --tf 1d --bars 600
  python3 backtest/download_data.py --tf 1h --bars 5000

输出列: ts,open,high,low,close,volume
"""
import argparse
import csv
import os
import sys
import time

import ccxt
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
load_dotenv(os.path.join(BASE, '.env'))

PROXY = os.environ.get('PROXY', 'http://127.0.0.1:7897')
OUT = os.path.join(HERE, 'data')

SYMBOLS = [
    'TSLA/USDT:USDT', 'COIN/USDT:USDT', 'PLTR/USDT:USDT', 'MSTR/USDT:USDT', 'HOOD/USDT:USDT',
    'NVDA/USDT:USDT', 'META/USDT:USDT', 'AMZN/USDT:USDT', 'QQQ/USDT:USDT',
    'SPY/USDT:USDT', 'GOOGL/USDT:USDT', 'INTC/USDT:USDT', 'CRCL/USDT:USDT',
]


def make_ex():
    ex = ccxt.binance({
        'enableRateLimit': True,
        'proxies': {'http': PROXY, 'https': PROXY},
        'options': {'defaultType': 'future', 'adjustForTimeDifference': True},
    })
    ex.session.headers['Connection'] = 'close'
    ex.enable_demo_trading(True)
    return ex


def fetch_all(ex, sym, tf, bars):
    """从当前时间往前翻页，返回按时间升序的 OHLCV。"""
    ms_per = ex.parse_timeframe(tf) * 1000
    now = ex.milliseconds()
    since = now - bars * ms_per
    out = []
    cursor = since
    while cursor < now:
        try:
            chunk = ex.fetch_ohlcv(sym, tf, since=cursor, limit=1000)
        except Exception as e:
            print(f'  [warn] {sym} {tf} {cursor}: {str(e)[:100]}', flush=True)
            time.sleep(1.0)
            chunk = ex.fetch_ohlcv(sym, tf, since=cursor, limit=1000)
        if not chunk:
            break
        out.extend(chunk)
        nxt = chunk[-1][0] + ms_per
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(ex.rateLimit / 1000.0)
    seen = {}
    for r in out:
        seen[int(r[0])] = r
    rows = [seen[k] for k in sorted(seen)]
    return rows[-bars:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tf', default='1d', choices=['1d', '1h', '4h'])
    ap.add_argument('--bars', type=int, default=600)
    ap.add_argument('--symbols', default='')
    a = ap.parse_args()
    syms = [s for s in a.symbols.split(',') if s] or SYMBOLS
    os.makedirs(OUT, exist_ok=True)
    ex = make_ex()
    ex.load_markets()
    total = len(syms)
    for i, sym in enumerate(syms, 1):
        name = sym.split('/')[0]
        print(f'[progress] {i}/{total} {name} {a.tf}', flush=True)
        try:
            rows = fetch_all(ex, sym, a.tf, a.bars)
        except Exception as e:
            print(f'  [fail] {name}: {str(e)[:160]}', flush=True)
            continue
        path = os.path.join(OUT, f'{name}_{a.tf}.csv')
        with open(path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['ts', 'open', 'high', 'low', 'close', 'volume'])
            for r in rows:
                w.writerow([int(r[0]), r[1], r[2], r[3], r[4], r[5]])
        if rows:
            first = time.strftime('%Y-%m-%d %H:%M', time.localtime(rows[0][0] / 1000))
            last = time.strftime('%Y-%m-%d %H:%M', time.localtime(rows[-1][0] / 1000))
        else:
            first = last = '-'
        print(f'  [ok] {path} {len(rows)} 根 {first} → {last}', flush=True)
    print('[progress] done', flush=True)


if __name__ == '__main__':
    main()
