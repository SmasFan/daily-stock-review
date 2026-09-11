#!/usr/bin/env python3
"""Binance USD-M 合约 Algo 单工具 (条件单显式 API)

背景: 2025-12-09 起币安将 STOP/STOP_MARKET/TAKE_PROFIT 等条件单自动路由到
Algo Order API (/fapi/v1/algoOrder)。ccxt 的 create_order(type='stop') 仍能下单
(实际落到 algo 池) 但其 fetch_open_orders / cancel_all_orders 只查普通单,
造成: 1) 查询盲区(以为没挂上) 2) 平仓时漏撤 algo 单(僵尸单残留)

本模块绕过 ccxt 直接走 REST, 保证挂撤查一致。
依赖 .env: BN_API_KEY / BN_SECRET / PROXY
"""
import os, time, hmac, hashlib, requests
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

KEY = os.environ.get('BN_API_KEY', '')
SECRET = os.environ.get('BN_SECRET', '')
PROXY = os.environ.get('PROXY', '')
BASE_URL = os.environ.get('FAPI_URL', 'https://demo-fapi.binance.com')  # 测试网默认
PROXIES = {'http': PROXY, 'https': PROXY} if PROXY else None

# 普通 symbol (TSLA/USDT:USDT) -> 币安 id (TSLAUSDT)
_SYM_ID = {}


def _sig(params):
    params['timestamp'] = int(time.time() * 1000)
    params['recvWindow'] = 10000
    qs = '&'.join(f'{k}={v}' for k, v in params.items())
    return qs + '&signature=' + hmac.new(SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()


def _req(method, path, params=None):
    params = dict(params or {})
    url = f'{BASE_URL}{path}?{_sig(params)}'
    r = requests.request(method, url, headers={'X-MBX-APIKEY': KEY}, proxies=PROXIES, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f'{method} {path}: HTTP {r.status_code} {r.text[:200]}')
    return r.json()


def set_symbol_map(markets):
    """传入 ccxt 的 ex.markets 以建 symbol 映射"""
    global _SYM_ID
    for s, m in markets.items():
        _SYM_ID[s] = m['id']


def sym_id(symbol):
    return _SYM_ID.get(symbol, symbol.replace('/', '').replace(':USDT', 'USDT'))


def place_stop_market(symbol, side, qty, trigger_price):
    """挂 STOP_MARKET 条件单 (reduceOnly)。返回 algoId"""
    params = {
        'symbol': sym_id(symbol), 'side': side.upper(), 'type': 'STOP_MARKET',
        'quantity': str(qty), 'triggerPrice': str(trigger_price),
        'reduceOnly': 'true', 'algoType': 'CONDITIONAL',
        'workingType': 'CONTRACT_PRICE', 'timeInForce': 'GTC',
    }
    return _req('POST', '/fapi/v1/algoOrder', params)


def cancel_algo(symbol, algo_id=None, client_algo_id=None):
    """撤 algo 单。algo_id 或 client_algo_id 二选一"""
    params = {'symbol': sym_id(symbol)}
    if algo_id:
        params['algoId'] = algo_id
    if client_algo_id:
        params['clientAlgoId'] = client_algo_id
    return _req('DELETE', '/fapi/v1/algoOrder', params)


def cancel_all_algo(symbol=None):
    """撤该 symbol 全部 algo 单 (或全部)"""
    params = {}
    if symbol:
        params['symbol'] = sym_id(symbol)
    return _req('DELETE', '/fapi/v1/algoOpenOrders', params)


def open_algo_orders(symbol=None):
    """列出未成交 algo 单。可选按 symbol 过滤"""
    params = {}
    if symbol:
        params['symbol'] = sym_id(symbol)
    return _req('GET', '/fapi/v1/openAlgoOrders', params)


def algo_stops_for(symbol):
    """该 symbol 所有 SELL reduceOnly STOP 单 (止损用途)"""
    out = []
    for o in open_algo_orders(symbol):
        if o.get('symbol') == sym_id(symbol) and o.get('side') == 'SELL' and o.get('reduceOnly') in ('true', True):
            out.append(o)
    return out


if __name__ == '__main__':
    # 自测: 列全部 open algo 单
    import json
    d = open_algo_orders()
    print(f'open algo orders: {len(d)}')
    for o in d:
        print(' ', {k: o.get(k) for k in ['symbol', 'side', 'orderType', 'quantity', 'triggerPrice', 'algoStatus', 'algoId']})
