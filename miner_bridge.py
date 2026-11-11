#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""矿机数据桥：WSL/Windows 上的 OneZeroDash (8181) → data/miner_live.json

只做数据搬运和格式化，不下发任何控制指令；产出文件给 dsrbar_card.html 同源读取，
避免 WKWebView 跨端口 fetch 的 CORS 限制。失败时保留上一份数据并标记 ok=false。
"""
import datetime
import json
import os
import tempfile
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, 'data', 'miner_live.json')
API = os.environ.get('MINER_API', 'http://127.0.0.1:8181').rstrip('/')
INTERVAL = float(os.environ.get('MINER_INTERVAL', '20'))
TIMEOUT = float(os.environ.get('MINER_TIMEOUT', '6'))

UNITS = ((1e18, 'EH/s'), (1e15, 'PH/s'), (1e12, 'TH/s'),
         (1e9, 'GH/s'), (1e6, 'MH/s'), (1e3, 'kH/s'), (1.0, 'H/s'))


def hr_text(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return '—'
    if v <= 0:
        return '—'
    for scale, unit in UNITS:
        if v >= scale:
            return '%.2f %s' % (v / scale, unit)
    return '%.0f H/s' % v


def now_str():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def fetch(path):
    req = urllib.request.Request(API + path, headers={'Cache-Control': 'no-store'})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def spark_of(hist, fallback):
    """把 2 小时的 5s 采样压到最多 48 个点，给卡片画 sparkline。"""
    vals = []
    for s in (hist or {}).get('samples') or []:
        try:
            v = float(s.get('hr') or 0)
        except (TypeError, ValueError):
            continue
        if v > 0:
            vals.append(v)
    if len(vals) < 2:
        vals = [fallback, fallback] if fallback else []
    if len(vals) > 48:
        step = len(vals) / 48.0
        vals = [vals[min(len(vals) - 1, int(i * step))] for i in range(48)]
    return [round(v) for v in vals]


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build(live, hist):
    m = live.get('market') or {}
    g = live.get('gpu') or {}
    c = live.get('cpu') or {}
    raw = live.get('raw') or {}
    sample = live.get('sample') or {}
    algos = raw.get('algos') or []
    algo = algos[0] if algos else {}

    hr = num(sample.get('hr') or algo.get('total_hashrate'))
    net = num(m.get('net_hashrate'))
    bt = num(m.get('block_time'))
    reward = num(m.get('block_reward'))
    fee = num(m.get('pool_fee'))
    price = num(m.get('price'))
    share = hr / net if net else 0.0
    gross = share * (86400.0 / bt) * reward if bt else 0.0
    prl = gross * (1.0 - fee)
    usd = prl * price

    acc = int(num(algo.get('total_accepted_shares') or sample.get('acc')))
    rej = int(num(algo.get('total_rejected_shares') or sample.get('rej')))
    inv = int(num(algo.get('total_invalid_shares') or sample.get('inv')))
    total = acc + rej + inv

    return {
        'generatedAt': now_str(),
        'ok': True,
        'bridgeError': None,
        'running': bool(live.get('miner_running')),
        'poolStatus': sample.get('status') or algo.get('pool_status') or '—',
        'staleSeconds': num(live.get('stale_seconds')),
        'worker': live.get('worker') or '—',
        'version': raw.get('version') or '—',
        'algo': algo.get('name') or sample.get('algo') or '—',
        'uptime': raw.get('uptime_str') or '—',
        'hashrate': hr,
        'hashrateText': hr_text(hr),
        'netHashrate': net,
        'netHashrateText': hr_text(net),
        'sharePct': share * 100.0,
        'price': price,
        'change24h': num(m.get('change24h')),
        'mcap': num(m.get('mcap')),
        'height': num(m.get('height')),
        'source': m.get('source') or '—',
        'marketOk': bool(m.get('ok')) and not bool(m.get('stale')),
        'marketError': m.get('error'),
        'blockTime': bt,
        'blockReward': reward,
        'poolFeePct': fee * 100.0,
        'dailyPrl': prl,
        'dailyUsd': usd,
        'shares': {'accepted': acc, 'rejected': rej, 'invalid': inv,
                   'acceptRate': (acc / total * 100.0) if total else 0.0},
        'gpu': {'temp': num(g.get('temp')), 'power': num(g.get('power_draw')),
                'limit': num(g.get('power_limit')), 'util': num(g.get('util_gpu')),
                'fan': num(g.get('fan')), 'smMhz': num(g.get('sm_mhz')),
                'memUsed': num(g.get('mem_used')), 'memTotal': num(g.get('mem_total')),
                'eco': bool(g.get('eco'))},
        'cpu': {'util': num(c.get('util')), 'power': num(c.get('power'))},
        'spark': spark_of(hist, hr),
    }


def atomic_write(obj):
    d = os.path.dirname(OUT)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.miner_live.', dir=d)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))
        os.chmod(tmp, 0o644)
        os.replace(tmp, OUT)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def mark_error(err):
    try:
        with open(OUT) as f:
            prev = json.load(f)
    except Exception:
        prev = {}
    prev['ok'] = False
    prev['bridgeError'] = str(err)
    prev['generatedAt'] = now_str()
    atomic_write(prev)


def main():
    print('%s 矿机数据桥启动: %s → %s, 每 %.0fs' % (now_str(), API, OUT, INTERVAL), flush=True)
    while True:
        try:
            live = fetch('/api/live')
            try:
                hist = fetch('/api/history?minutes=120')
            except Exception as e:
                hist = {'samples': []}
                print('%s 历史曲线取数失败(不影响实时): %s' % (now_str(), e), flush=True)
            d = build(live, hist)
            atomic_write(d)
            print('%s %s · %s · $%.2f/24h · %d 点曲线' % (
                now_str(), d['hashrateText'], d['poolStatus'], d['dailyUsd'], len(d['spark'])), flush=True)
        except Exception as e:
            mark_error(e)
            print('%s 矿机取数失败: %s' % (now_str(), e), flush=True)
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
