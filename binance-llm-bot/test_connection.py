#!/usr/bin/env python3
"""连通性验证: 只读, 不下单。"""
import ccxt, os
from dotenv import load_dotenv

load_dotenv()

PROXY = os.environ.get('PROXY', 'socks5h://172.25.16.1:10808')

ex = ccxt.binance({
    'apiKey': os.environ.get('BN_API_KEY', ''),
    'secret': os.environ.get('BN_SECRET', ''),
    'enableRateLimit': True,
    'proxies': {'http': PROXY, 'https': PROXY},
    'options': {'adjustForTimeDifference': True},  # 自动校准时钟偏差
})
ex.enable_demo_trading(True)

print('ccxt 版本:', ccxt.__version__)
try:
    print('接口地址:', ex.urls['api']['rest'])
except Exception:
    print('接口地址: (见 urls)', getattr(ex, 'urls', {}))

# 1. 公开行情 (不需要 key)
t = ex.fetch_ticker('BTC/USDT')
print('BTC 最新价:', t['last'])

# 2. 认证接口 (需要 key) —— 验 key 是否有效
bal = ex.fetch_balance()
print('Demo 账户余额:')
for asset, v in bal['total'].items():
    if v and v > 0:
        print(f'  {asset}: {v}')
