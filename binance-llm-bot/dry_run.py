#!/usr/bin/env python3
"""Dry-run 全链路: 行情 -> DeepSeek LLM 决策 -> 风控。不下单。"""
import os, json
from dotenv import load_dotenv
load_dotenv('/mnt/c/Users/z7280/daily-stock-review/binance-llm-bot/.env')
import importlib.util
spec = importlib.util.spec_from_file_location('bot', '/mnt/c/Users/z7280/daily-stock-review/binance-llm-bot/main.py')
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

ex = bot.make_exchange()
ex.load_markets()

print('1. 拉行情...')
market = bot.get_market(ex)
print('   行情 OK:', {k: len(v) for k, v in market.items()}, '| BTC:', market['1h'][-1][4])

from openai import OpenAI
client = OpenAI(
    api_key=os.environ.get('COMMAND_CODE_API_KEY'),
    base_url=os.environ.get('LLM_BASE_URL', 'https://api.commandcode.ai/provider/v1'),
)
print('2. LLM 决策 (' + os.environ.get('LLM_MODEL', 'deepseek/deepseek-v4-flash') + ')...')
decision = bot.llm_decide(client, market)
print('   决策:', json.dumps(decision, ensure_ascii=False))

risk = bot.RiskGate()
size, msg = risk.check(decision)
print('3. 风控:', msg, '| 仓位比例:', size)
print('\n✅ 链路通。若风控放行即会下单——dry-run 只到检查为止。')
