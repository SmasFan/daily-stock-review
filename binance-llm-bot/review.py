#!/usr/bin/env python3
"""进化脑: 复盘分析器
读 trades.jsonl + 实时持仓 -> 统计指标 -> LLM 深度分析 -> 写 review 报告
每轮生成 {date}_review.md + evolution.json (进化状态)

用法:
  review.py now        # 立即复盘一次
  review.py auto       # 常驻: 每6h + 每次平仓后触发
"""
import os, sys, json, time, glob, re
from datetime import datetime, timedelta
import ccxt
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
BASE = os.path.dirname(os.path.abspath(__file__))
TRADES = os.path.join(BASE, 'trades.jsonl')
EVO_FILE = os.path.join(BASE, 'evolution.json')
PROXY = os.environ.get('PROXY', 'socks5h://172.25.16.1:10808')


def make_fex():
    ex = ccxt.binance({'apiKey': os.environ.get('BN_API_KEY', ''), 'secret': os.environ.get('BN_SECRET', ''),
        'enableRateLimit': True, 'proxies': {'http': PROXY, 'https': PROXY},
        'options': {'defaultType': 'future', 'adjustForTimeDifference': True}})
    # 禁 keep-alive: 低频轮询下复用连接会被代理回收导致 RemoteDisconnected
    ex.session.headers['Connection'] = 'close'
    ex.enable_demo_trading(True)
    return ex


def read_trades():
    if not os.path.exists(TRADES):
        return []
    out = []
    for line in open(TRADES, errors='ignore'):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def position_snapshot(ex):
    """当前持仓 + 入场价, 算浮动表现"""
    pos = []
    total_pnl = 0.0
    for s in ['TSLA/USDT:USDT', 'COIN/USDT:USDT', 'PLTR/USDT:USDT', 'MSTR/USDT:USDT', 'HOOD/USDT:USDT']:
        for p in ex.fetch_positions([s]):
            if float(p['contracts']) != 0:
                amt = float(p['contracts'])
                entry = float(p['entryPrice'])
                mark = float(p.get('markPrice') or entry)
                pnl = float(p['unrealizedPnl'])
                pct = (mark / entry - 1) * 100
                total_pnl += pnl
                pos.append({'sym': s, 'amt': amt, 'entry': round(entry, 2),
                            'mark': round(mark, 2), 'pnl': round(pnl, 4), 'pct': round(pct, 2)})
    return pos, total_pnl


def market_context(ex):
    """各标的价格 vs SMA50, 涨跌幅趋势"""
    ctx = {}
    for s, name in [('TSLA/USDT:USDT', 'TSLA'), ('COIN/USDT:USDT', 'COIN'), ('PLTR/USDT:USDT', 'PLTR'), ('MSTR/USDT:USDT', 'MSTR'), ('HOOD/USDT:USDT', 'HOOD')]:
        try:
            ohlcv = ex.fetch_ohlcv(s, '1d', limit=60)
            closes = [c[4] for c in ohlcv]
            now = ex.milliseconds()
            closes = [c for c, t in zip([x[4] for x in ohlcv], [x[0] for x in ohlcv]) if t + 86400000 <= now]
            last = closes[-1]
            ma50 = sum(closes[-50:]) / 50
            ma20 = sum(closes[-20:]) / 20
            ctx[name] = {
                'price': round(last, 2), 'sma20': round(ma20, 2), 'sma50': round(ma50, 2),
                'above50': last > ma50,
                'd7': round((last / closes[-8] - 1) * 100, 2),
                'd30': round((last / closes[-31] - 1) * 100, 2),
            }
        except Exception:
            continue
    return ctx


def stats(trades):
    """从交易记录提取统计"""
    closed = [t for t in trades if t['type'] in ('close', 'sl')]
    opens = [t for t in trades if t['type'] == 'open']
    wins = [t for t in closed if t.get('pnl', 0) > 0]
    losses = [t for t in closed if t.get('pnl', 0) <= 0]
    avg_win = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['pnl'] for t in losses) / len(losses) if losses else 0
    # 按标的
    by_sym = {}
    for t in closed:
        k = t['symbol'].split('/')[0]
        by_sym.setdefault(k, []).append(t['pnl'])
    win_sum = sum(t['pnl'] for t in wins)
    loss_sum = abs(sum(t['pnl'] for t in losses))
    return {
        'total_trades': len(closed), 'open_trades': len(opens),
        'wins': len(wins), 'losses': len(losses),
        'win_rate': len(wins) / max(len(closed), 1),
        'total_pnl': sum(t['pnl'] for t in closed),
        'avg_win': avg_win, 'avg_loss': avg_loss,
        'profit_factor': (round(win_sum / loss_sum, 3) if loss_sum else None),
        'by_symbol': {k: {'n': len(v), 'pnl': round(sum(v), 2)} for k, v in by_sym.items()},
    }


def load_evo():
    if os.path.exists(EVO_FILE):
        try:
            return json.load(open(EVO_FILE))
        except Exception:
            pass
    return {'version': 1, 'reviews': [], 'adjustments': [], 'last_review': None}


def save_evo(evo):
    json.dump(evo, open(EVO_FILE, 'w'), indent=2)


REVIEW_SYSTEM = """你是量化交易策略复盘分析师。基于交易统计和当前市场状态, 输出深刻复盘。

分析要点:
1. 策略执行是否正常 (信号/风控/止损是否按设计工作)
2. 当前持仓的浮盈浮亏反映什么 (趋势是否延续?)
3. 交易统计中胜率/盈亏比/盈利因子说明什么问题
4. 市场状态判断 (TSLA/COIN/PLTR/MSTR/HOOD 是否仍在 SMA50 上方趋势?)
5. 具体改进建议 (参数/风控/时机), 要克制——只建议有数据支撑的改动, 不频繁折腾

只输出 JSON:
{"summary":"3-5行复盘总结","market_state":"趋势/震荡/下跌 判断",
 "execution_ok":true或false,"execution_issue":"执行问题描述或空",
 "strengths":["..."],"weaknesses":["..."],
 "adjustments":[{"param":"如 SMA周期/止损线","from":"当前值","to":"建议值","reason":"理由"}],
 "risk_note":"风险提示","action":"hold|adjust|review" }"""


REVIEW_FAIL = {'summary': 'LLM 复盘失败, 跳过', 'action': 'review',
               'market_state': '未知', 'execution_ok': True,
               'strengths': [], 'weaknesses': [], 'adjustments': []}

# LLM 通道：Agent 文件队列（免费，优先）→ 付费云端（默认关闭）
# 事后复盘每 6h 一次，等得起 —— 默认 35 分钟，等我来答（等不到就是跳过复盘，不影响交易）
AGENT_WAIT = float(os.environ.get('BN_REVIEW_WAIT', os.environ.get('AGENT_LLM_WAIT', '2100')))
ALLOW_CLOUD = os.environ.get('AGENT_LLM_ALLOW_CLOUD', '0').lower() in ('1', 'true', 'yes', 'on')


def _agent_mod():
    """导入仓库根的 llm_agent（本模块在 binance-llm-bot/ 子目录）。"""
    root = os.path.dirname(BASE)
    if root not in sys.path:
        sys.path.insert(0, root)
    import llm_agent
    return llm_agent


def _parse_review(text):
    """从返回里抠出复盘 JSON（容错 ```json 包裹 / 前后废话）。"""
    if isinstance(text, dict):
        return text if 'summary' in text else None
    t = str(text or '').strip()
    if '```' in t:
        m = re.search(r'```(?:json)?\s*([\s\S]*?)```', t)
        if m:
            t = m.group(1).strip()
    m = re.search(r'\{[\s\S]*\}', t)
    if not m:
        return None
    d = json.loads(m.group(0))
    return d if 'summary' in d else None


def _review_agent(user, meta=None):
    try:
        la = _agent_mod()
    except Exception as e:
        print('  [llm][review] Agent 队列不可用: %s' % str(e)[:80])
        return None
    try:
        ans = la.ask('bn_review', REVIEW_SYSTEM, user, expect_json=True,
                     wait=AGENT_WAIT, meta=meta, schema=REVIEW_SYSTEM.split('只输出 JSON:')[-1].strip())
    except Exception as e:
        print('  [llm][review] Agent 队列异常: %s' % str(e)[:80])
        return None
    if ans is None:
        return None
    try:
        return _parse_review(ans)
    except Exception as e:
        print('  [llm][review] Agent 答案解析失败: %s' % str(e)[:80])
        return None


def _review_cloud(user):
    try:
        client = _get_llm()
    except Exception as e:
        print('  [llm][review] 云端不可用: %s' % str(e)[:80])
        return None
    for _ in range(3):
        try:
            r = client.chat.completions.create(
                model=os.environ.get('LLM_MODEL', 'deepseek/deepseek-v4-flash'),
                messages=[{'role': 'system', 'content': REVIEW_SYSTEM},
                          {'role': 'user', 'content': user}],
                max_tokens=8000, timeout=180)
            d = _parse_review((r.choices[0].message.content or '').strip())
            if d:
                return d
        except Exception as e:
            print('  [llm][review] 云端调用失败: %s' % str(e)[:80])
            time.sleep(2)
    return None


def llm_review(st, pos, ctx):
    """复盘分析：先问 Agent 队列，等不到再按开关降级到付费云端。"""
    payload = {'stats': st, 'positions': pos, 'market': ctx}
    user = json.dumps(payload, ensure_ascii=False)
    d = _review_agent(user, meta={'n_trades': st.get('total_trades'),
                                  'n_pos': len(pos or [])})
    if d is None and ALLOW_CLOUD:
        d = _review_cloud(user)
    if d is None:
        return dict(REVIEW_FAIL, summary='Agent 队列无答案，复盘跳过')
    return d


def run_review():
    ex = make_fex()
    ex.load_markets()
    trades = read_trades()
    st = stats(trades)
    pos, u_pnl = position_snapshot(ex)
    ctx = market_context(ex)
    review = llm_review(st, pos, ctx)

    evo = load_evo()
    entry = {
        'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'stats': st, 'positions': pos, 'market': ctx, 'review': review,
        'realized_pnl': st['total_pnl'], 'unrealized_pnl': round(u_pnl, 2),
    }
    evo['reviews'].append(entry)
    evo['last_review'] = entry['ts']
    if review.get('action') == 'adjust' and review.get('adjustments'):
        evo['adjustments'].append({'ts': entry['ts'], 'items': review['adjustments']})
    save_evo(evo)

    # 写可读报告
    fname = os.path.join(BASE, 'reviews', datetime.now().strftime('%Y%m%d_%H%M') + '_review.md')
    os.makedirs(os.path.dirname(fname), exist_ok=True)
    md = _render_md(entry)
    open(fname, 'w').write(md)
    print(f'复盘完成: {fname}')
    print(json.dumps(review, ensure_ascii=False, indent=1)[:800])


def _render_md(e):
    r = e['review']
    lines = [f"# 量化复盘 {e['ts']}", '',
             f"**已实现盈亏**: {e['realized_pnl']:+.2f} U | **浮动盈亏**: {e['unrealized_pnl']:+.2f} U",
             '', '## 市场状态', f"{r.get('market_state','')}",
             '', '## 复盘总结', r.get('summary', ''), '',
             '## 优势', *[f"- {x}" for x in r.get('strengths', [])], '',
             '## 问题', *[f"- {x}" for x in r.get('weaknesses', [])], '',
             '## 调整建议', *[f"- {a.get('param')}: {a.get('from')} → {a.get('to')} ({a.get('reason','')})" for a in r.get('adjustments', [])], '',
             '## 风险提示', r.get('risk_note', ''), '',
             f"**决策**: {r.get('action','')}"]
    return '\n'.join(lines)


def _get_llm():
    from openai import OpenAI
    return OpenAI(api_key=os.environ['COMMAND_CODE_API_KEY'],
                  base_url=os.environ.get('LLM_BASE_URL', 'https://api.commandcode.ai/provider/v1'))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'now'
    if mode == 'auto':
        while True:
            try:
                run_review()
            except Exception as e:
                print(f'复盘失败: {e}')
            time.sleep(6 * 3600)
    else:
        run_review()


if __name__ == '__main__':
    main()
