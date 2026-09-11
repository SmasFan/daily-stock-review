#!/usr/bin/env python3
"""10 刀混合趋势 bot (Demo 合约)

标的: BTC 40% + ETH 30% + TSLA 30% (各自独立趋势, 2x 杠杆)
策略: 收盘价 > SMA(N) 持仓; 跌破离场 (日线)
杠杆: 2x (爆仓需 -45%+, 日线可扛)
池子: 独立 10 刀池追踪, state_pool.json
注意: demo 合约资金 5000U 实际放着, 但只在池净值允许范围内下单
      (用池净值*分配%*2x 算名义, 与 demo 大余额无关 = 等效独立 10 刀池)
"""
import os, sys, json, time, logging
from datetime import datetime
import ccxt
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('t10')

BASE = os.path.dirname(os.path.abspath(__file__))
POOL_FILE = os.path.join(BASE, 'state_pool.json')
SYMBOLS = {
    'BTC/USDT:USDT': {'alloc': 0.40, 'sma': 50, 'name': 'BTC'},
    'ETH/USDT:USDT': {'alloc': 0.30, 'sma': 50, 'name': 'ETH'},
    'TSLA/USDT:USDT': {'alloc': 0.30, 'sma': 30, 'name': 'TSLA'},
}
LEV = 2.0
FEE = 0.0005   # 合约 maker 费率约 0.02%, 保守 0.05%
PROXY = os.environ.get('PROXY', 'socks5h://172.25.16.1:10808')
LOOP_HOURS = float(os.environ.get('LOOP_HOURS', '6'))

TEMPLATE = {
    'pool_equity': 10.0,       # 池净值 (初始 10 刀)
    'positions': {},           # sym -> {pos: bool, entry: price}
    'peak_pool': 10.0,
    'last_date': None,
}


def make_fex():
    ex = ccxt.binance({
        'apiKey': os.environ.get('BN_API_KEY', ''),
        'secret': os.environ.get('BN_SECRET', ''),
        'enableRateLimit': True,
        'proxies': {'http': PROXY, 'https': PROXY},
        'options': {'defaultType': 'future', 'adjustForTimeDifference': True},
    })
    ex.enable_demo_trading(True)
    return ex


def load_pool():
    if os.path.exists(POOL_FILE):
        try:
            st = json.load(open(POOL_FILE))
            for k, v in TEMPLATE.items():
                st.setdefault(k, v)
            return st
        except Exception:
            pass
    return dict(TEMPLATE)


def save_pool(st):
    json.dump(st, open(POOL_FILE, 'w'), indent=2)


def closed_daily(ex, sym, need):
    """返回已收盘日线收盘价列表 (最新未收K剔除)"""
    ohlcv = ex.fetch_ohlcv(sym, '1d', limit=need + 30)
    now = ex.milliseconds()
    closes = [c[4] for c in ohlcv if c[0] + 86400000 <= now]
    return closes


def sma(closes, w):
    if len(closes) < w:
        return None
    return sum(closes[-w:]) / w


def mark_price(ex, sym):
    t = ex.fetch_ticker(sym)
    return t['last']


def pool_mark_value(ex, st):
    """按池内各标的当前持仓市值 + 假设比例, 重估池净值"""
    # 简化: 每标的名义 = 池净值*alloc*lev, 2x 只押一半保证金
    # 真实池净值由持仓盈亏决定, 这里用持仓浮盈亏算
    val = 0.0
    for sym, cfg in SYMBOLS.items():
        p = st['positions'].get(sym, {})
        if p.get('pos'):
            # 持仓中: 名义 = pool_at_entry * alloc * lev, 盈亏按价格变动
            entry = p['entry']
            cur = mark_price(ex, sym)
            alloc_base = p.get('base', 0)
            pnl = alloc_base * (cur / entry - 1)  # alloc_base = 投入保证金
            val += p['base'] + pnl
    return val + st.get('cash', 0.0)


def decide_once(ex, st, dry=False):
    today = datetime.utcnow().date().isoformat()
    log.info('=== %s 池净值追踪开始 ===', today)

    # 1. 每标的出信号
    sigs = {}
    for sym, cfg in SYMBOLS.items():
        closes = closed_daily(ex, sym, cfg['sma'] + 5)
        last = closes[-1]
        ma = sma(closes, cfg['sma'])
        above = last > ma
        sigs[sym] = {'above': above, 'price': last, 'ma': ma}
        log.info('%s 收%.2f SMA%d=%.2f -> %s', cfg['name'], last, cfg['sma'],
                 ma, '做多' if above else '离场')

    # 2. 每标的: 持仓则检查离场, 空仓且信号多则按分配开仓
    for sym, cfg in SYMBOLS.items():
        p = st['positions'].setdefault(sym, {'pos': False, 'entry': 0, 'base': 0})
        sig = sigs[sym]
        cur_price = sig['price']
        if p['pos']:
            if not sig['above']:   # 跌破均线离场
                pnl = p['base'] * (cur_price / p['entry'] - 1)
                st['cash'] = st.get('cash', 0) + p['base'] + pnl - FEE * abs(p['base'])
                log.info('离场 %s: 盈亏 %+.2f刀', cfg['name'], pnl)
                p['pos'] = False; p['entry'] = 0; p['base'] = 0
            else:
                log.info('持有 %s (入场%.0f 现%.0f)', cfg['name'], p['entry'], cur_price)
        else:
            # 空仓: 分配资金开仓 (池净值 * alloc, 2x = 名义*2, 保证金=池净值*alloc)
            pool = st['pool_equity']
            margin = pool * cfg['alloc']            # 投入保证金
            if margin >= 1.0:                        # 最小保证金门槛
                if dry:
                    log.info('[dry] 开仓 %s 保证金%.2f刀 (名义%.2f)', cfg['name'], margin, margin * LEV)
                p['pos'] = True; p['entry'] = cur_price; p['base'] = margin
                st['cash'] = st.get('cash', 0) - margin
    save_pool(st)
    return sigs


def main():
    ex = make_fex()
    ex.load_markets()
    st = load_pool()
    mode = sys.argv[1] if len(sys.argv) > 1 else 'loop'
    if mode == 'once':
        decide_once(ex, st)
        return
    log.info('trader10 启动: 3标混合 2x, 每%.0fh一轮', LOOP_HOURS)
    while True:
        try:
            decide_once(ex, st)
        except Exception as e:
            log.error('本轮失败: %s', e)
        time.sleep(LOOP_HOURS * 3600)


if __name__ == '__main__':
    main()
