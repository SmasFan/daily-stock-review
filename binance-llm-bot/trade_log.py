#!/usr/bin/env python3
"""交易记录模块: 追加写 JSONL, 页面读取渲染
记录: 开/平仓/止损/熔断/错误 等事件
"""
import os, json, time

BASE = '/mnt/c/Users/z7280/daily-stock-review/binance-llm-bot'
TRADE_LOG = os.path.join(BASE, 'trades.jsonl')


def record(event_type, symbol, action, qty=0, price=0, pnl=0, detail='', pool=None, lev=None, notional=None):
    """写一条交易记录。event_type: open/close/sl/cooldown/error/decision"""
    entry = {
        'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
        'type': event_type,
        'symbol': symbol,
        'action': action,
        'qty': round(qty, 8),
        'price': round(price, 4) if price else 0,
        'pnl': round(pnl, 4) if pnl else 0,
        'pool': round(pool, 2) if pool is not None else None,
        'lev': lev,
        'notional': round(notional, 2) if notional else None,
        'detail': detail,
    }
    with open(TRADE_LOG, 'a') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    return entry


def read_trades(limit=200):
    if not os.path.exists(TRADE_LOG):
        return []
    trades = []
    with open(TRADE_LOG, 'r', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                trades.append(json.loads(line))
            except Exception:
                continue
    return trades[-limit:]
