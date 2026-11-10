#!/usr/bin/env python3
"""Binance USD-M 合约 Algo 单工具 (条件单显式 API)

背景: 2025-12-09 起币安将 STOP/STOP_MARKET/TAKE_PROFIT 等条件单自动路由到
Algo Order API (/fapi/v1/algoOrder)。ccxt 的 create_order(type='stop') 仍能下单
(实际落到 algo 池) 但其 fetch_open_orders / cancel_all_orders 只查普通单,
造成: 1) 查询盲区(以为没挂上) 2) 平仓时漏撤 algo 单(僵尸单残留)

本模块绕过 ccxt 直接走 REST, 保证挂撤查一致。
依赖 .env: BN_API_KEY / BN_SECRET / PROXY
"""
import os, time, hmac, hashlib, base64, requests
from urllib.parse import quote
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


# 时钟偏移校正（ms）：本机时间戳 - 币安服务器时间。
# 背景：WSL 时钟会漂移（实测本机快 ~2.4s），而本模块直接用 time.time() 签名，
# 不做校正会持续报 -1021 "Timestamp ahead"，导致止损单挂不上、旧单撤不掉。
_TIME_OFFSET_MS = 0


def sync_time():
    """请求币安服务器时间，记录本机-服务器偏移；返回偏移(ms)，失败返回 None。"""
    global _TIME_OFFSET_MS
    try:
        r = requests.get(f'{BASE_URL}/fapi/v1/time', proxies=PROXIES, timeout=10)
        server = int(r.json()['serverTime'])
        _TIME_OFFSET_MS = int(time.time() * 1000) - server
        return _TIME_OFFSET_MS
    except Exception:
        return None


def time_offset_ms():
    return _TIME_OFFSET_MS


def _sig(params):
    """币安签名：BN_SECRET 是 Ed25519 PEM 时用 EdDSA，普通串用 HMAC-SHA256。

    坑（2026-11-09 修）：Demo 账号用 Self-generated Ed25519 Key，
    旧版这里一律 HMAC，algo 条件单持续报 -1022 "Signature for this request is not vali"，
    止损单只能靠 guard.py 的 60s 本地检查兜底。签名口径与 ccxt.binance.sign 对齐。
    """
    params['timestamp'] = int(time.time() * 1000) - _TIME_OFFSET_MS
    params['recvWindow'] = 10000
    qs = '&'.join(f'{k}={v}' for k, v in params.items())
    if 'PRIVATE KEY' in SECRET and len(SECRET) <= 120:
        from cryptography.hazmat.primitives import serialization
        key = serialization.load_pem_private_key(SECRET.encode(), password=None)
        sig = base64.b64encode(key.sign(qs.encode())).decode()
        return qs + '&signature=' + quote(sig, safe='')
    return qs + '&signature=' + hmac.new(SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()


def _req(method, path, params=None):
    params = dict(params or {})
    url = f'{BASE_URL}{path}?{_sig(params)}'
    r = requests.request(method, url, headers={'X-MBX-APIKEY': KEY}, proxies=PROXIES, timeout=15)
    if r.status_code != 200:
        # -1021 时间戳偏移 → 自动重新对时并重试一次（自愈）
        if '"code":-1021' in r.text and sync_time() is not None:
            url = f'{BASE_URL}{path}?{_sig(params)}'
            r = requests.request(method, url, headers={'X-MBX-APIKEY': KEY},
                                 proxies=PROXIES, timeout=15)
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


def algo_stops_for(symbol, side='SELL'):
    """该 symbol 的 reduceOnly STOP 单（多头止损 SELL / 空头止损 BUY）。

    side=None 时不区分方向。"""
    out = []
    for o in open_algo_orders(symbol):
        if o.get('symbol') != sym_id(symbol) or o.get('reduceOnly') not in ('true', True):
            continue
        if side and o.get('side') != side.upper():
            continue
        out.append(o)
    return out


if __name__ == '__main__':
    # 自测: 列全部 open algo 单
    import json
    d = open_algo_orders()
    print(f'open algo orders: {len(d)}')
    for o in d:
        print(' ', {k: o.get(k) for k in ['symbol', 'side', 'orderType', 'quantity', 'triggerPrice', 'algoStatus', 'algoId']})
