#!/usr/bin/env python3
"""生产版交易 bot (Demo 资金)

架构:
  主线 A: 日线趋势跟随 —— 收盘价 > 100日均线 持仓, < 均线 离场
  辅线 C: LLM 新闻风控 —— 抓 CoinDesk/CoinTelegraph 头条,
         DeepSeek 判风险等级; HIGH 强制离场+禁开仓
  附加:   峰值回撤 15% 硬止损; 状态落盘 state.json, 重启不丢

用法:
  python3 trader.py          # 常驻循环 (默认 6h 一轮)
  python3 trader.py once     # 只跑一轮
  python3 trader.py news     # 只测新闻风控模块
"""
import os, sys, json, time, logging, csv
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import ccxt
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('trader')

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, 'state.json')
SYMBOL = 'BTC/USDT'
PROXY = os.environ.get('PROXY', 'socks5h://172.25.16.1:10808')
LOOP_HOURS = float(os.environ.get('LOOP_HOURS', '6'))
SMA_N = int(os.environ.get('SMA_N', '100'))
DD_STOP = 0.15        # 峰值回撤 15% 硬离场
REENTER_CUSHION = 0.08  # 止损后需回到峰值 92% 内才可重进
MAX_TRADE_PCT = 0.98  # 单次最多用 98% 可用资金
FEE = 0.001

STATE_TEMPLATE = {'peak_equity': 0, 'pos': False, 'last_signal': None, 'cooldown': False}

# ============================================================
# 1. 交易所
# ============================================================
def make_exchange():
    ex = ccxt.binance({
        'apiKey': os.environ.get('BN_API_KEY', ''),
        'secret': os.environ.get('BN_SECRET', ''),
        'enableRateLimit': True,
        'proxies': {'http': PROXY, 'https': PROXY},
        'options': {'adjustForTimeDifference': True},
    })
    ex.enable_demo_trading(True)
    return ex

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return {**STATE_TEMPLATE, **json.load(open(STATE_FILE))}
        except Exception:
            pass
    return dict(STATE_TEMPLATE)

def save_state(st):
    json.dump(st, open(STATE_FILE, 'w'), indent=2)

# ============================================================
# 2. A 策略: 日线收盘 vs SMA100
# ============================================================
def daily_signal(ex):
    """返回 (持仓信号bool, 诊断dict)。
    用已收盘日线 (最新未收K不算), 避免未来函数。"""
    ohlcv = ex.fetch_ohlcv(SYMBOL, '1d', limit=SMA_N + 50)
    now = ex.milliseconds()
    closes = []
    diag = {}
    for c in ohlcv:
        if c[0] + 86400000 <= now:      # 该日K已收盘
            closes.append(c[4])
    diag['closed_days'] = len(closes)
    if len(closes) < SMA_N + 1:
        raise RuntimeError(f'日线样本不足: {len(closes)} < {SMA_N+1}')
    last = closes[-1]
    sma = sum(closes[-SMA_N:]) / SMA_N
    diag['last_close'] = last
    diag['sma'] = round(sma, 0)
    diag['above'] = last > sma
    return last > sma, diag

# ============================================================
# 3. C 副线: 新闻 -> LLM 风险分级
# ============================================================
RSS_FEEDS = [
    'https://www.coindesk.com/arc/outboundfeeds/rss/',
    'https://cointelegraph.com/rss',
]

def fetch_headlines():
    heads = []
    for url in RSS_FEEDS:
        try:
            r = requests.get(url, timeout=25, proxies={'http': PROXY, 'https': PROXY},
                             headers={'User-Agent': 'Mozilla/5.0'})
            root = ET.fromstring(r.content)
            for item in root.iter('item'):
                t = (item.findtext('title') or '').strip()
                if t:
                    heads.append(t)
        except Exception as e:
            log.warning('RSS 抓取失败 %s: %s', url, str(e)[:100])
    return heads[:24]

NEWS_SYSTEM = """你是加密货币风险分析师。基于最新新闻标题评估 BTC 短期(数日内)下行风险。
分级标准:
- HIGH:  重大利空 —— 交易所被黑/挤兑、监管禁令、央行/美联储极端鹰派、战争/地缘、稳定币脱锚、创始人被捕
- MED:   中等利空 —— ETF 大量流出、监管调查、大额链上转移、名人唱空
- LOW:   常规/利多/无相关 —— 日常波动、技术升级、正常回调
只输出 JSON: {"level":"LOW|MED|HIGH","reason":"一句话"}"""

def llm_risk(client, headlines, diag):
    ctx = {
        'btc_close': round(diag.get('last_close', 0), 0),
        'sma100': diag.get('sma', 0),
        'above_sma': diag.get('above'),
        'headlines': headlines,
    }
    for attempt in range(3):
        try:
            r = client.chat.completions.create(
                model='deepseek/deepseek-v4-flash',
                messages=[{'role': 'system', 'content': NEWS_SYSTEM},
                          {'role': 'user', 'content': json.dumps(ctx, ensure_ascii=False)}],
                max_tokens=400, timeout=90)
            text = (r.choices[0].message.content or '').strip()
            if '```' in text:
                import re as _re
                m = _re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
                if m:
                    text = m.group(1).strip()
            d = json.loads(text)
            if d.get('level') in ('LOW', 'MED', 'HIGH'):
                return d
        except Exception as e:
            log.warning('风险分级重试 %s/3: %s', attempt + 1, str(e)[:150])
            time.sleep(2)
    return {'level': 'MED', 'reason': 'LLM 不可用, 保守按中风险'}

# ============================================================
# 4. 账户 / 执行
# ============================================================
def portfolio(ex):
    bal = ex.fetch_balance()
    ticker = ex.fetch_ticker(SYMBOL)
    price = ticker['last']
    usdt = float(bal['USDT']['free']) + float(bal['USDT'].get('used', 0))
    btc = float(bal['BTC']['free']) + float(bal['BTC'].get('used', 0))
    eq = usdt + btc * price
    return {'usdt': usdt, 'btc': btc, 'price': price, 'equity': eq}

def buy(ex, pf, pct=MAX_TRADE_PCT):
    spend = pf['usdt'] * pct * (1 - FEE)
    if spend < 5:
        log.info('资金过少跳过买入: %.2f USDT', spend)
        return False
    ex.create_order(SYMBOL, 'market', 'buy', None, None, params={'quoteOrderQty': spend})
    log.info('市价买入 ~%.0f USDT', spend)
    return True

def sell_all(ex, pf):
    if pf['btc'] * pf['price'] < 5:
        log.info('持仓过少跳过卖出')
        return False
    ex.create_order(SYMBOL, 'market', 'sell', pf['btc'] * 0.999, None)
    log.info('市价清仓 %f BTC', pf['btc'])
    return True

# ============================================================
# 5. 主决策
# ============================================================
def decide_once(ex, client, st, dry=False):
    sig, diag = daily_signal(ex)
    pf = portfolio(ex)
    eq = pf['equity']

    # 峰值追踪 + 回撤止损
    if eq > st['peak_equity']:
        st['peak_equity'] = eq
    dd = 1 - eq / st['peak_equity'] if st['peak_equity'] else 0
    if dd >= DD_STOP:
        st['cooldown'] = True
        log.warning('峰值回撤 %.1f%% 触发硬止损, 冷却中', dd * 100)

    headlines = fetch_headlines()
    risk = llm_risk(client, headlines, diag)
    log.info('新闻 %d 条 | 风险: %s - %s', len(headlines), risk['level'], risk['reason'])
    log.info('信号: 收盘 %s vs SMA%d=%s -> %s | 回撤 %.1f%% | 净值 %.0f',
             round(diag['last_close'], 0), SMA_N, round(diag['sma'], 0),
             '做多' if sig else '离场', dd * 100, eq)

    action = None
    if st['cooldown']:
        # 冷却解除: 回撤收窄至峰值的 REENTER 缓冲内, 且信号转多, 且风险非HIGH
        if dd <= REENTER_CUSHION and sig and risk['level'] != 'HIGH':
            st['cooldown'] = False
            action = 'buy'
        else:
            action = 'hold_cooldown'
    elif risk['level'] == 'HIGH':
        if st['pos']:
            action = 'sell'          # 利空强制离场
        else:
            action = 'hold_news'     # 禁止新开仓
    elif sig:
        action = 'buy' if not st['pos'] else 'hold'
    else:
        action = 'sell' if st['pos'] else 'hold_flat'

    log.info('决策: %s', action)
    if dry:
        log.info('[dry-run] 不下单. 结论=%s', action)
        return action

    if action == 'buy':
        if buy(ex, pf):
            st['pos'] = True
    elif action == 'sell':
        if sell_all(ex, pf):
            st['pos'] = False
    save_state(st)
    return action

# ============================================================
# 6. 入口
# ============================================================
def get_llm():
    from openai import OpenAI
    return OpenAI(api_key=os.environ['COMMAND_CODE_API_KEY'],
                  base_url=os.environ.get('LLM_BASE_URL', 'https://api.commandcode.ai/provider/v1'))

def main():
    ex = make_exchange()
    ex.load_markets()
    client = get_llm()
    st = load_state()

    mode = sys.argv[1] if len(sys.argv) > 1 else 'loop'
    if mode == 'news':
        pf = portfolio(ex)
        _, diag = daily_signal(ex)
        heads = fetch_headlines()
        print('头条:')
        for h in heads:
            print(' -', h)
        print('\n风险:', llm_risk(client, heads, diag))
        return
    if mode == 'once':
        decide_once(ex, client, st)
        return

    log.info('trader 启动, 每 %.0f 小时一轮', LOOP_HOURS)
    while True:
        try:
            decide_once(ex, client, st)
        except Exception as e:
            log.error('本轮失败(不影响持仓): %s', e)
        time.sleep(LOOP_HOURS * 3600)

if __name__ == '__main__':
    main()
