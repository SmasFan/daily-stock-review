#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成交前 LLM 风控闸门（币安模块共用）

背景：trader100 / trader_short 原来是纯规则机器人（收盘价 > SMA50 就开多）。
LLM 只在 review.py 里做事后复盘，提的建议（加 SMA20 过滤、别在局部高点追高）
落不到实盘 —— 复盘 25 次、建议 10 批，applied 全空。

本模块把 LLM 放到【成交前】：规则先触发候选 → LLM 复核 → 通过才下单。

- 买入：LLM 可否决（当日/近期弱势、追高、动能转弱 → avoid）
- 卖出：趋势离场是策略核心，默认 hard=True 强制成交，LLM 只能确认；
        调用方也可把某类卖出标为 hard=False 让 LLM 决定
- 缓存：同一标的同方向同一天只问一次，结果存 state['llm_gate']（跨日自动失效）
- 兜底：LLM 不可用/超时/解析失败 → 全部放行（绝不因 LLM 停盘）；连错 3 次熔断 10 分钟
- 开关：BN_LLM_GATE=0 关闭；BN_LLM_GATE_TIMEOUT 改超时（默认 30s）

用法：
    from llm_gate import gate
    dec = gate(st, tasks, ctx_note="池净值 98.2（回撤 4.1%）")
    if dec.get(task_id, {}).get("verdict") == "avoid":  # 放弃下单
"""
import json
import os
import re
import time
from datetime import datetime

DEFAULT_BASE_URL = 'https://api.commandcode.ai/provider/v1'
DEFAULT_MODEL = 'deepseek/deepseek-v4-flash'

GATE_ON = os.environ.get('BN_LLM_GATE', '1').lower() not in ('0', 'false', 'off', 'no')
TIMEOUT = float(os.environ.get('BN_LLM_GATE_TIMEOUT', '30'))
FAIL_LIMIT = 3
COOLDOWN_MIN = 10


def _client():
    from openai import OpenAI
    key = (os.environ.get('COMMAND_CODE_API_KEY') or os.environ.get('DEEPSEEK_API_KEY')
           or os.environ.get('OPENAI_API_KEY'))
    if not key:
        raise RuntimeError('无 API key')
    return OpenAI(api_key=key,
                  base_url=os.environ.get('LLM_BASE_URL', DEFAULT_BASE_URL))


def _json_of(text):
    """从 LLM 返回里抠 JSON（容错 ```json 包裹 / 前后废话）。"""
    if not text or not text.strip():
        raise ValueError('空返回')
    t = text.strip()
    if '```' in t:
        m = re.search(r'```(?:json)?\s*([\s\S]*?)```', t)
        if m:
            t = m.group(1).strip()
    i, j = t.find('{'), t.rfind('}')
    if i < 0 or j <= i:
        raise ValueError('无 JSON')
    return json.loads(t[i:j + 1])


def _hhmm_add(hms, minutes):
    try:
        p = [int(x) for x in hms.split(':')]
    except Exception:
        return hms
    total = (p[0] * 3600 + p[1] * 60 + p[2] + int(minutes * 60)) % 86400
    return '%02d:%02d:%02d' % (total // 3600, total % 3600 // 60, total % 60)


SYS_PROMPT = """你是币安（加密/股票永续）的趋势跟随交易风控员，在系统下单前做最后复核。
系统信号：日线（或1h）收盘价站上SMA50 → 开多；跌破SMA50 → 离场。
另有一类「利润保护」卖出：由止盈规则（回撤/达标）触发，目的是把账面利润变成实际利润。

【市场特性：T+0】这里随时可平、可反手，没有「今天买明天才能卖」的约束，且 7x24 连续交易。
因此默认原则是 **优先保证利润** —— 到手的浮盈回吐，比少赚一段趋势的代价更高。
教训：原来只靠「跌破SMA50再走」，短线池从 +6.3% 一路回吐到 0，日线池从 +4.5% 回吐到 0。

【买入】通常是趋势突破，但历史教训是「入场多集中在局部高点、被噪音止损」（胜率很低）。
遇到下列情况给 avoid：
- 价格虽在SMA50上方，但已跌破SMA20（短期动能转弱）
- 距SMA50偏离过大（>12%）属追高
- 近7日大跌（d7 <= -8%）属下跌反弹诱多
- 该标当前已浮亏且趋势在走弱
其余给 allow。

【卖出·硬规则】趋势离场（收盘跌破SMA50）是策略核心，系统强制执行，你只能确认（给 allow）。

【卖出·利润保护】默认给 allow（T+0 落袋为安）。只有「趋势结构完好」时才给 avoid 继续持有，
需同时满足：
- 现价仍在 SMA20 上方，且距 SMA50 偏离不大（<8%）
- 回撤属正常噪音（从峰值回撤 <1%），或浮盈还很小（不到保本线）
- 账户整体不在明显回撤中
其余一律 allow。宁可少赚，不把已实现的利润还回去。

只依据给定数据判断，不臆造新闻。宁少误杀，不放过明显的追高与弱势入场。
输出严格 JSON：{"decisions":[{"id":"t1","verdict":"allow|avoid","note":"≤20字理由"}]}
id 必须原样照抄（t1/t2/...），不得替换成标的代码；逐一回答所有 id。"""


def _call(tasks, ctx_note=''):
    """一次批量评审，返回 {id: {verdict,note}}；失败返回 None。"""
    short, lines = {}, []
    for i, t in enumerate(tasks, 1):
        sid = 't%d' % i
        short[sid] = t
        if t.get('action') == 'buy':
            lines.append(
                '%s | 买 %s(%s) | 现价%.2f | SMA50 %.2f(偏离%+.1f%%) | SMA20 %.2f(关系:%s) '
                '| 近7日%+.1f%% 近30日%+.1f%% | 杠杆%gx | 触发:%s' % (
                    sid, t.get('name'), t.get('symbol'), t.get('price') or 0,
                    t.get('sma50') or 0, t.get('dev50') or 0, t.get('sma20') or 0,
                    t.get('vs_ma20') or '?', t.get('d7') or 0, t.get('d30') or 0,
                    t.get('lev') or 0, t.get('why') or ''))
        else:
            kind = '硬规则(不可否决)' if t.get('hard') else (
                '利润保护(T+0默认放行)' if t.get('kind') == 'protect' else '可商量')
            extra = ''
            if t.get('peak_pct') is not None:     # 利润保护单：带上峰值/回撤让 LLM 判断
                extra = ' | 峰值%+.1f%% 自峰值回撤%.1f%%' % (
                    t.get('peak_pct') or 0, t.get('dd_peak') or 0)
            lines.append(
                '%s | 卖 %s(%s) | 现价%.2f | 入场%.2f 浮盈%+.1f%% | SMA50 %.2f(偏离%+.1f%%) '
                '| 近7日%+.1f%%%s | %s | 类型:%s' % (
                    sid, t.get('name'), t.get('symbol'), t.get('price') or 0,
                    t.get('entry') or 0, t.get('pnl_pct') or 0,
                    t.get('sma50') or 0, t.get('dev50') or 0, t.get('d7') or 0,
                    extra, t.get('why') or '', kind))
    user = '【账户背景】%s\n\n【待复核触发】\n%s\n\n逐个给出 allow/avoid，id 原样返回。' % (
        ctx_note or '无', '\n'.join(lines))
    client = _client()
    r = client.chat.completions.create(
        model=os.environ.get('LLM_MODEL', DEFAULT_MODEL),
        messages=[{'role': 'system', 'content': SYS_PROMPT},
                  {'role': 'user', 'content': user}],
        max_tokens=1200, timeout=TIMEOUT)
    text = (r.choices[0].message.content or '').strip()
    j = _json_of(text)
    out = {}
    for x in (j.get('decisions') or []):
        raw = str(x.get('id') or '').strip()
        t = short.get(raw)
        if t is None:   # 兜底：模型把 id 写成标的代码
            hits = [v for v in short.values() if v.get('symbol') and v['symbol'] in raw]
            if len(hits) == 1:
                t = hits[0]
        if t is None:
            continue
        out[t['id']] = {
            'verdict': 'avoid' if str(x.get('verdict')).lower() == 'avoid' else 'allow',
            'note': (x.get('note') or '')[:40]}
    return out or None


def gate(state, tasks, ctx_note=''):
    """成交前 LLM 复核。返回 {id: {verdict, note, src, ts}}。

    src: llm=本次评审 / cache=同日已评 / fallback=LLM不可用放行 / off=闸门关闭
    传入的 state 会被就地写入缓存（调用方负责持久化）。
    """
    if not tasks:
        return {}
    date = time.strftime('%Y-%m-%d')
    hms = time.strftime('%H:%M:%S')
    c = state.get('llm_gate')
    if not c or c.get('date') != date:
        c = {'date': date, 'items': {}}
        state['llm_gate'] = c
    items = c.setdefault('items', {})
    out, fresh = {}, []
    for t in tasks:
        hit = items.get(t['id'])
        if hit:
            out[t['id']] = {'verdict': hit.get('verdict'), 'note': hit.get('note'),
                            'src': 'cache', 'ts': hit.get('ts')}
        else:
            fresh.append(t)
    if not fresh:
        return out
    if not GATE_ON:
        for t in fresh:
            out[t['id']] = {'verdict': 'allow', 'note': '闸门关闭', 'src': 'off'}
        return out
    cb = state.get('llm_gate_cb') or {}
    if cb.get('until') and hms < cb['until']:
        for t in fresh:
            out[t['id']] = {'verdict': 'allow', 'note': 'LLM熔断至%s' % cb['until'],
                            'src': 'fallback'}
        return out
    try:
        dec = _call(fresh, ctx_note)
    except Exception as e:
        dec = None
        print('  [llm][gate] 调用失败(放行): %s' % str(e)[:150])
    if dec is None:
        cb['streak'] = int(cb.get('streak') or 0) + 1
        if cb['streak'] >= FAIL_LIMIT:
            cb['until'] = _hhmm_add(hms, COOLDOWN_MIN)
            cb['streak'] = 0
            print('  [llm][gate] 连续失败 → 熔断至 %s' % cb['until'])
        state['llm_gate_cb'] = cb
        for t in fresh:
            out[t['id']] = {'verdict': 'allow', 'note': 'LLM不可用，放行', 'src': 'fallback'}
        return out
    state.pop('llm_gate_cb', None)
    for t in fresh:
        d = dec.get(t['id']) or {'verdict': 'allow', 'note': 'LLM未逐条返回，放行'}
        d['src'] = 'llm'
        d['ts'] = hms
        out[t['id']] = d
        items[t['id']] = {'verdict': d['verdict'], 'note': d.get('note'), 'ts': hms}
    return out


def market_stats(ex, sym, sma_n=50, tf='1d'):
    """取标的趋势上下文（供闸门判断）：SMA50/SMA20、d7/d30、偏离度。

    返回 dict 或 {}（数据不足/异常时返回空，闸门只看得到什么就用什么）。
    """
    try:
        ohlcv = ex.fetch_ohlcv(sym, tf, limit=max(sma_n, 60) + 30)
        now = ex.milliseconds()
        unit = 3600000 if tf.endswith('h') else 86400000
        closes = [c[4] for c in ohlcv if c[0] + unit <= now]   # 只取已收K线
        if len(closes) < 21:
            return {}
        last = closes[-1]
        ma50 = sum(closes[-sma_n:]) / sma_n if len(closes) >= sma_n else None
        ma20 = sum(closes[-20:]) / 20
        out = {
            'price': round(last, 4),
            'sma20': round(ma20, 4),
            'sma50': round(ma50, 4) if ma50 else 0,
            'dev50': round((last / ma50 - 1) * 100, 2) if ma50 else 0,
            'vs_ma20': ('上方' if last > ma20 else '下方'),
            'd7': round((last / closes[-8] - 1) * 100, 2) if len(closes) >= 8 else 0,
            'd30': round((last / closes[-31] - 1) * 100, 2) if len(closes) >= 31 else 0,
        }
        return out
    except Exception:
        return {}


# ---------------- 影子跟踪：否决到底对不对？ ----------------
# 每次 LLM 否决买入，就记一条「影子单」（当时价格 + 理由），跟踪 WINDOW_DAYS 天：
#   否决正确 = 期间价格下跌（没买是对的）
#   否决错误 = 期间价格上涨（错过了）
# 结算后归档到 stats，用来量化闸门到底有没有正贡献。
SHADOW_DAYS = float(os.environ.get('BN_SHADOW_DAYS', '3'))
SHADOW_MAX = 60          # 未结算影子单上限，超了丢最旧（防 state 无限膨胀）


def _shadow(state):
    sh = state.setdefault('shadow', {})
    sh.setdefault('open', [])
    sh.setdefault('done', [])
    sh.setdefault('stats', {'n': 0, 'correct': 0, 'wrong': 0, 'flat': 0,
                            'avg_ret': 0, 'avg_max': 0, 'avg_min': 0,
                            'missed_gain': 0, 'avoided_loss': 0})
    return sh


def shadow_record(state, tasks, decisions):
    """把本轮被否决的买入登记成影子单。返回新增条数。"""
    sh = _shadow(state)
    added = 0
    for t in tasks:
        if t.get('action') != 'buy':
            continue
        d = decisions.get(t['id']) or {}
        if d.get('verdict') != 'avoid':
            continue
        if any(x['symbol'] == t.get('symbol') for x in sh['open']):
            continue          # 同标的已有未结算影子单，不重复记
        px = t.get('price') or 0
        if not px:
            continue
        sh['open'].append({
            'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
            'due': time.time() + SHADOW_DAYS * 86400,
            'symbol': t.get('symbol'), 'name': t.get('name'),
            'price': round(px, 4), 'note': d.get('note') or '',
            'why': (t.get('why') or '')[:60],
            'last': round(px, 4), 'last_ts': time.strftime('%Y-%m-%d %H:%M:%S'),
            'max': round(px, 4), 'min': round(px, 4),
            'dev50': t.get('dev50'), 'vs_ma20': t.get('vs_ma20'), 'd7': t.get('d7'),
        })
        added += 1
    if len(sh['open']) > SHADOW_MAX:
        sh['open'] = sorted(sh['open'], key=lambda x: x['due'])[-SHADOW_MAX:]
    return added


def shadow_update(state, ex):
    """更新影子单价格并结算到期条目。返回 (更新数, 结算数)。"""
    sh = _shadow(state)
    if not sh['open']:
        return 0, 0
    syms = sorted({x['symbol'] for x in sh['open']})
    px = {}
    try:
        for s, t in ex.fetch_tickers(syms).items():
            if t and t.get('last'):
                px[s] = float(t['last'])
    except Exception:
        for s in syms:          # 批量失败 → 逐个兜底
            try:
                t = ex.fetch_ticker(s)
                if t and t.get('last'):
                    px[s] = float(t['last'])
            except Exception:
                continue
    now = time.time()
    nows = time.strftime('%Y-%m-%d %H:%M:%S')
    upd, closed, keep = 0, 0, []
    for x in sh['open']:
        p = px.get(x['symbol'])
        if p:
            x['last'] = round(p, 4)
            x['last_ts'] = nows
            x['max'] = round(max(x['max'], p), 4)
            x['min'] = round(min(x['min'], p), 4)
            upd += 1
        if now >= x.get('due', 0) and x.get('last'):
            base = x['price'] or 1
            # 轮询采样间隔 1h~6h，会低估期间极值 → 用否决期间的 1h K线高低点修正
            try:
                since = int(datetime.strptime(x['ts'], '%Y-%m-%d %H:%M:%S').timestamp() * 1000)
                o = ex.fetch_ohlcv(x['symbol'], '1h', since=since, limit=200)
                if o:
                    x['max'] = round(max([x['max']] + [c[2] for c in o]), 4)
                    x['min'] = round(min([x['min']] + [c[3] for c in o]), 4)
                    x['bars'] = len(o)
            except Exception:
                pass
            ret = (x['last'] / base - 1) * 100
            x['exit_price'] = x['last']
            x['ret'] = round(ret, 2)
            x['max_ret'] = round((x['max'] / base - 1) * 100, 2)
            x['min_ret'] = round((x['min'] / base - 1) * 100, 2)
            x['verdict'] = 'correct' if ret < -0.3 else ('wrong' if ret > 0.3 else 'flat')
            sh['done'].append(x)
            closed += 1
        else:
            keep.append(x)
    sh['open'] = keep
    if closed:
        _shadow_recompute(sh)
    return upd, closed


def _shadow_recompute(sh):
    done = sh.get('done') or []
    st = sh['stats']
    st['n'] = len(done)
    st['correct'] = sum(1 for x in done if x.get('verdict') == 'correct')
    st['wrong'] = sum(1 for x in done if x.get('verdict') == 'wrong')
    st['flat'] = sum(1 for x in done if x.get('verdict') == 'flat')
    if done:
        st['avg_ret'] = round(sum(x.get('ret', 0) for x in done) / len(done), 2)
        st['avg_max'] = round(sum(x.get('max_ret', 0) for x in done) / len(done), 2)
        st['avg_min'] = round(sum(x.get('min_ret', 0) for x in done) / len(done), 2)
        st['missed_gain'] = round(sum(max(x.get('max_ret', 0), 0) for x in done) / len(done), 2)
        st['avoided_loss'] = round(sum(min(x.get('min_ret', 0), 0) for x in done) / len(done), 2)
    st['open_n'] = len(sh.get('open') or [])


def shadow_summary(state):
    """给页面用：未结算明细 + 已结算统计。"""
    sh = state.get('shadow') or {}
    open_ = sh.get('open') or []
    for x in open_:
        base = x.get('price') or 1
        x['ret_now'] = round((x.get('last', base) / base - 1) * 100, 2)
    return {'open': open_, 'done': (sh.get('done') or [])[-20:],
            'stats': sh.get('stats') or {}}
