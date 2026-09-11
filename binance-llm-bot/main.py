#!/usr/bin/env python3
"""LLM 驱动的 Binance Demo Trading 机器人
架构: 行情 -> LLM 决策(JSON) -> 确定性风控 -> 下单 -> 循环
仅用于 demo 测试。风险自负。
"""
import ccxt, os, json, time, logging, sys
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(), logging.FileHandler('bot.log', encoding='utf-8')],
)
log = logging.getLogger('bot')

SYMBOL = 'BTC/USDT'
INTERVALS = [('15m', 48), ('1h', 48), ('4h', 24)]
LOOP_SECONDS = 300  # 5 分钟一轮
PROXY = os.environ.get('PROXY', 'socks5h://172.25.16.1:10808')  # v2rayN socks5 经 Windows 网关


# ---------- 1. 交易所接入 (Demo API) ----------
def make_exchange():
    ex = ccxt.binance({
        'apiKey': os.environ.get('BN_API_KEY', ''),
        'secret': os.environ.get('BN_SECRET', ''),
        'enableRateLimit': True,
        'proxies': {'http': PROXY, 'https': PROXY},
        'options': {'adjustForTimeDifference': True},  # 时钟偏差自动校准
    })
    ex.enable_demo_trading(True)  # 新版 Binance Spot Demo API, 不是 set_sandbox_mode
    return ex


# ---------- 2. 风控门 (确定性代码, LLM 无权绕过) ----------
class RiskGate:
    def __init__(self, max_daily_loss_pct=0.05, max_trade_pct=0.10, min_confidence=0.7):
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_trade_pct = max_trade_pct
        self.min_confidence = min_confidence
        self.day_start_equity = None
        self.halted = False

    def update_equity(self, equity_usdt):
        if self.day_start_equity is None:
            self.day_start_equity = equity_usdt
        elif self.day_start_equity - equity_usdt >= self.max_daily_loss_pct * self.day_start_equity:
            self.halted = True
            log.warning('日亏熔断触发, 停机')

    def check(self, decision):
        if self.halted:
            return 0, '日亏熔断, 停机'
        if not isinstance(decision, dict) or 'action' not in decision:
            return 0, 'LLM 输出非法'
        action = str(decision['action']).upper()
        if action == 'HOLD':
            return 0, '观望'
        if action not in ('BUY', 'SELL'):
            return 0, f'未知动作 {action}'
        conf = float(decision.get('confidence', 0))
        if conf < self.min_confidence:
            return 0, f'置信度 {conf:.2f} < {self.min_confidence}'
        size = float(decision.get('size_pct', 0))
        size = max(0.0, min(size, self.max_trade_pct))  # 封顶
        if size <= 0:
            return 0, '仓位为 0'
        return size, f'放行 {action} 仓位 {size*100:.1f}%'


# ---------- 3. 行情 ----------
def get_market(ex):
    out = {}
    for tf, limit in INTERVALS:
        ohlcv = ex.fetch_ohlcv(SYMBOL, tf, limit=limit)
        out[tf] = [[x[0], x[1], x[2], x[3], x[4], x[5]] for x in ohlcv]
    return out


# ---------- 4. LLM 决策 ----------
SYSTEM_PROMPT = """你是加密货币交易员, 分析 BTC/USDT 多周期 K 线 (15m/1h/4h)。
数据格式: [时间戳, 开, 高, 低, 收, 量]。
输出 JSON 且只能输出 JSON, 严格格式:
{"action":"BUY|SELL|HOLD","confidence":0~1,"size_pct":0~0.1,"reason":"50字以内"}
原则: 宁可 HOLD 不要乱动; 无明确趋势就观望。"""


def llm_decide(client, market):
    import re as _re
    for attempt in range(3):  # 网关偶发空返回, 重试
        try:
            r = client.chat.completions.create(
                model=os.environ.get('LLM_MODEL', 'deepseek/deepseek-v4-flash'),
                # 网关不支持 response_format=json_object, 靠 prompt 强约束 + 解析兜底
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(market)},
                ],
                max_tokens=300,
                timeout=120,
            )
            text = (r.choices[0].message.content or '').strip()
            # 提取 JSON 块 (模型可能包 ```json ... ```)
            if '```' in text:
                m = _re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
                if m: text = m.group(1).strip()
            return json.loads(text)
        except Exception as e:
            if attempt == 2:
                raise
            log.warning('LLM 返回异常 (第%s次): %s, 重试...', attempt+1, str(e)[:200])
            time.sleep(3)


# ---------- 5. 执行 ----------
def place_order(ex, action, size_usdt, price):
    amount = size_usdt / price
    if action == 'BUY':
        order = ex.create_market_buy_order(SYMBOL, amount)
    else:
        order = ex.create_market_sell_order(SYMBOL, amount)
    log.info('下单 %s %s @ %s | order %s', action, f'{amount:.6f}', f'{price:.0f}', order.get('id'))


def equity_usdt(ex):
    bal = ex.fetch_balance()
    total = 0.0
    for asset, v in bal['total'].items():
        if v and v > 0:
            total += ex.fetch_ticker(f'{asset}/USDT')['last'] * v if asset != 'USDT' else v
    return total


# ---------- 6. 主循环 ----------
def main():
    ex = make_exchange()
    log.info('连接 Demo API 并加载市场...')
    ex.load_markets()

    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=os.environ.get('COMMAND_CODE_API_KEY') or os.environ.get('DEEPSEEK_API_KEY'),
            base_url=os.environ.get('LLM_BASE_URL', 'https://api.commandcode.ai/provider/v1'),
        )
    except Exception:
        log.error('缺少 API key 或 openai 包未装。pip install openai')
        sys.exit(1)

    risk = RiskGate()
    log.info('机器人启动。轮询间隔 %ss', LOOP_SECONDS)

    while True:
        try:
            market = get_market(ex)
            decision = llm_decide(client, market)
            size, msg = risk.check(decision)
            log.info('LLM: %s | %s', json.dumps(decision, ensure_ascii=False), msg)

            if size > 0:
                eq = equity_usdt(ex)
                risk.update_equity(eq)
                price = market['1h'][-1][4]
                place_order(ex, decision['action'], size * eq, price)
        except Exception as e:
            log.error('轮次失败 (不下单): %s', e)
        time.sleep(LOOP_SECONDS)


if __name__ == '__main__':
    main()
