#!/usr/bin/env python3
"""时分短线趋势 bot (Demo 合约) — 独立池, 与 trader100(日线) 并行
1h K线 SMA50 趋势 + 3x + -3% 止损
8 股票 TRADIFI 永续等权, 独立 100U 池记账 (state_short.json)
每 1h 检查 (信号基于 1h K线, 未收 K 过滤)

T+0 利润保护(profit_guard): 币安随时可平, 只等 SMA 破位会把浮盈原样还回去
（短线池曾从 +6.3% 回吐到 0）。规则：峰值浮盈 2% 止损上移保本；浮盈 3% 先平一半；
从峰值回撤 1.5% 或赚够 5% 破 SMA20 → 落袋。平仓类交 LLM 复核（默认放行）。
"""
import os, sys, json, time, logging
import ccxt
from dotenv import load_dotenv
import algo_tools
import llm_gate     # 成交前 LLM 风控复核（买入可否决；趋势离场硬规则）
import profit_guard  # T+0 利润保护：保本上移 / 分批止盈 / 移动止盈

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
for _n in ('httpx', 'httpx2', 'httpcore', 'openai'):   # 屏蔽 SDK 每次请求的 INFO 噪音
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger('tshort')

BASE = os.path.dirname(os.path.abspath(__file__))
POOL_FILE = os.path.join(BASE, 'state_short.json')
POOL_START = float(os.environ.get('SHORT_POOL', '100'))
LEV = float(os.environ.get('SHORT_LEV', '5'))
FEE = 0.0004
PROXY = os.environ.get('PROXY', 'socks5h://172.25.16.1:10808')
LOOP_MIN = float(os.environ.get('SHORT_LOOP_MIN', '60'))   # 每 60 分钟检查
DD_STOP = float(os.environ.get('SHORT_DD_STOP', '0.20'))
SL_PCT = float(os.environ.get('SHORT_SL_PCT', '0.03'))      # 短线止损 -3%
TIMEFRAME = os.environ.get('SHORT_TF', '1h')
SMA_N = int(os.environ.get('SHORT_SMA', '50'))

# 8 股票 TRADIFI (与 trader100 的 5 标不重叠)
SYMBOLS = {
    'NVDA/USDT:USDT': {'alloc': 0.125, 'name': 'NVDA'},
    'META/USDT:USDT': {'alloc': 0.125, 'name': 'META'},
    'AMZN/USDT:USDT': {'alloc': 0.125, 'name': 'AMZN'},
    'QQQ/USDT:USDT':  {'alloc': 0.125, 'name': 'QQQ'},
    'SPY/USDT:USDT':  {'alloc': 0.125, 'name': 'SPY'},
    'GOOGL/USDT:USDT': {'alloc': 0.125, 'name': 'GOOGL'},
    'INTC/USDT:USDT': {'alloc': 0.125, 'name': 'INTC'},
    'CRCL/USDT:USDT': {'alloc': 0.125, 'name': 'CRCL'},
}
MIN_NOTIONAL = 5  # TRADIFI 最小名义


def make_fex():
    ex = ccxt.binance({
        'apiKey': os.environ.get('BN_API_KEY', ''),
        'secret': os.environ.get('BN_SECRET', ''),
        'enableRateLimit': True,
        'proxies': {'http': PROXY, 'https': PROXY},
        'options': {'defaultType': 'future', 'adjustForTimeDifference': True},
    })
    ex.session.headers['Connection'] = 'close'
    ex.enable_demo_trading(True)
    return ex


def load_pool():
    if os.path.exists(POOL_FILE):
        try:
            return json.load(open(POOL_FILE))
        except Exception:
            pass
    return {'pool_equity': POOL_START, 'cash': POOL_START,
            'peak': POOL_START, 'cooldown': False, 'real_pos': {}, 'log': []}


def save_pool(st):
    json.dump(st, open(POOL_FILE, 'w'), indent=2)


def sync_from_real(ex, st):
    """真实持仓 -> 池记账。保留利润保护字段；入场价变了=新仓，peak/trimmed/sl 重置"""
    algo_tools.set_symbol_map(ex.markets)
    positions = ex.fetch_positions(list(SYMBOLS.keys()))
    for p in positions:
        sym = p['symbol']
        amt = float(p['contracts'])
        if amt != 0:
            old = st['real_pos'].get(sym) or {}
            entry = float(p['entryPrice'])
            rec = {'amt': amt, 'entry': entry}
            for k in ('peak', 'trimmed', 'sl', 'ts'):
                if k in old:
                    rec[k] = old[k]
            if old.get('entry') and abs(float(old['entry']) - entry) > 1e-9:
                for k in ('peak', 'trimmed', 'sl'):
                    rec.pop(k, None)
                rec['ts'] = time.time()
            st['real_pos'][sym] = rec
        else:
            st['real_pos'].pop(sym, None)


def closed_bars(ex, sym, tf, need):
    """过滤未收 K 线, 返回收盘价序列"""
    ohlcv = ex.fetch_ohlcv(sym, tf, limit=need + 30)
    now = ex.milliseconds()
    tf_ms = {'1h': 3600000, '15m': 900000, '4h': 14400000, '1d': 86400000}[tf]
    return [c[4] for c in ohlcv if c[0] + tf_ms <= now]


def sma(closes, w):
    return sum(closes[-w:]) / w if len(closes) >= w else None


def set_sl(ex, sym, qty, entry, sl_price=None):
    """挂止损；sl_price 给定时按给定价挂（利润保护上移保本用）"""
    sl_price = round(sl_price if sl_price else entry * (1 - SL_PCT), 2)
    pct = (1 - sl_price / entry) * 100 if entry else SL_PCT * 100
    try:
        r = algo_tools.place_stop_market(sym, 'sell', qty, sl_price)
        log.info('挂止损 %s @ %.2f (%+.1f%%) algoId=%s', sym, sl_price, -pct, r.get('algoId'))
        return r.get('algoId')
    except Exception as e:
        log.warning('挂止损失败 %s: %s', sym, str(e)[:120])
        return None


def cancel_sl(ex, sym):
    try:
        algo_tools.cancel_all_algo(sym)
    except Exception as e:
        log.warning('撤 algo 失败 %s: %s', sym, str(e)[:80])


def buy_open(ex, sym, notional_usd, llm=None):
    ex.set_leverage(int(LEV), sym)
    cancel_sl(ex, sym)
    t = ex.fetch_ticker(sym)
    qty = ex.amount_to_precision(sym, notional_usd / t['last'])
    o = ex.create_order(sym, 'market', 'buy', qty)
    filled = float(o.get('filled', qty))
    avg = float(o.get('average') or t['last'])
    aid = set_sl(ex, sym, filled, avg)
    from trade_log import record
    _n = (llm or {}).get('note')
    record('open', sym, 'BUY', filled, avg,
           detail=f'短线{tf_tag()} 名义~{notional_usd:.0f}' + (f'｜LLM:{_n}' if _n else '｜LLM:未复核'),
           pool=None, lev=LEV, notional=notional_usd)
    return filled


def tf_tag():
    return TIMEFRAME


def sell_close(ex, sym, amt=None, llm=None, why=None):
    """撤 algo 单 + 市价平仓 (why 区分趋势离场/利润保护)"""
    cancel_sl(ex, sym)
    try:
        ex.cancel_all_orders(sym)
    except Exception:
        pass
    entry_px = 0.0
    if amt is None:
        for p in ex.fetch_positions([sym]):
            if float(p['contracts']) > 0:
                amt = float(p['contracts'])
                entry_px = float(p['entryPrice'])
    else:
        try:
            for p in ex.fetch_positions([sym]):
                if float(p['contracts']) > 0:
                    entry_px = float(p['entryPrice'])
        except Exception:
            pass
    if amt:
        o = ex.create_order(sym, 'market', 'sell', ex.amount_to_precision(sym, amt))
        avg = float(o.get('average') or 0)
        # 用成交均价 vs 入场价算真实盈亏 (修正: 平仓后才查浮盈已归零的 bug)
        pnl = (avg - entry_px) * amt if (avg and entry_px) else 0
        from trade_log import record
        _n = (llm or {}).get('note')
        record('close', sym, 'SELL', amt, avg, pnl,
               detail=(why or f'短线{tf_tag()} SMA离场') + (f'｜LLM:{_n}' if _n else ''), lev=LEV)
        return amt
    return 0


def pool_equity_real(ex, st):
    pnl = 0.0
    for sym in SYMBOLS:
        for pp in ex.fetch_positions([sym]):
            amt = float(pp['contracts'])
            if amt:
                pnl += float(pp['unrealizedPnl'])
    return POOL_START + pnl


def decide(ex, st, dry=False):
    """一轮决策：1h 信号 → LLM 复核 → 下单（与 trader100 同构的两阶段流程）。

    买入可被 LLM 否决（避免短期动能转弱时追高）；趋势离场（跌破 SMA50）标 hard=True。
    """
    sync_from_real(ex, st)
    # 影子跟踪：更新历史「LLM 否决」单的现价并结算到期条目
    try:
        _u, _c = llm_gate.shadow_update(st, ex)
        if _c:
            _s = (st.get('shadow') or {}).get('stats') or {}
            log.info('[shadow] 结算%d条 → 累计%d条 否决正确%d/错误%d 均收益%+.2f%%',
                     _c, _s.get('n',0), _s.get('correct',0), _s.get('wrong',0), _s.get('avg_ret',0))
    except Exception as _e:
        log.warning('[shadow] 更新失败: %s', str(_e)[:120])
    # 各标信号
    sigs = {}
    for sym, cfg in SYMBOLS.items():
        closes = closed_bars(ex, sym, TIMEFRAME, SMA_N + 5)
        if len(closes) < SMA_N:
            log.warning('%s %s历史不足(%d)', cfg['name'], TIMEFRAME, len(closes))
            continue
        ma = sma(closes, SMA_N)
        sigs[sym] = {'above': closes[-1] > ma, 'price': closes[-1], 'ma': ma}
        log.info('%s %s收%.2f > SMA%d %.2f? %s', cfg['name'], TIMEFRAME, closes[-1], SMA_N, ma,
                 '✓多' if sigs[sym]['above'] else '✗空')

    # 回撤风控: 深回撤暂停开新仓 (持仓保留), 新高解除
    eq = pool_equity_real(ex, st)
    if eq > st['peak']:
        st['peak'] = eq
    dd = 1 - eq / st['peak'] if st['peak'] else 0
    log.info('短线池净值 %.2f (峰%.2f, 回撤%.1f%%)', eq, st['peak'], dd * 100)
    if dd > DD_STOP:
        st['cooldown'] = True
        log.warning('回撤%.0f%% 暂停开新仓 (持仓保留)', dd * 100)
    if eq >= st['peak']:
        st['cooldown'] = False

    # ---- 阶段一：收集候选 ----
    cands = []
    sl_tasks = []        # 保本上移任务 [(sym, sl_price)]
    for sym, cfg in SYMBOLS.items():
        if sym not in sigs:
            continue
        sig = sigs[sym]
        holding = sym in st['real_pos']
        ms = llm_gate.market_stats(ex, sym, sma_n=SMA_N, tf=TIMEFRAME)
        base = {'symbol': sym, 'name': cfg['name'], 'lev': LEV,
                'price': ms.get('price') or sig['price'],
                'sma50': ms.get('sma50') or sig['ma'], 'sma20': ms.get('sma20'),
                'dev50': ms.get('dev50'), 'vs_ma20': ms.get('vs_ma20'),
                'd7': ms.get('d7'), 'd30': ms.get('d30')}
        if holding and not sig['above']:
            pos = st['real_pos'].get(sym) or {}
            entry = float(pos.get('entry') or 0)
            cands.append({**base, 'id': 'short|%s|sell' % sym, 'action': 'sell',
                          'entry': entry,
                          'pnl_pct': round((base['price'] / entry - 1) * 100, 2) if entry else 0,
                          'why': '%s收盘跌破SMA%d %.2f（趋势离场）' % (TIMEFRAME, SMA_N, sig['ma']),
                          'hard': True})
        elif holding:
            # T+0：趋势未破，但先把账面利润保护起来
            try:
                pg = profit_guard.scan(ex, st, sym, profile='short', ms=ms,
                                       min_notional=MIN_NOTIONAL, peak_tf=TIMEFRAME)
            except Exception as e:
                log.warning('[利润保护] %s 扫描失败: %s', cfg['name'], str(e)[:120])
                pg = None
            if pg:
                log.info('[%s] %s（现价%.2f 浮盈%+.2f%% 峰值%+.2f%%）',
                         cfg['name'], pg['why'], pg['price'], pg['pnl_pct'], pg['peak_pct'])
                if pg['act'] == 'be_lock':
                    sl_tasks.append((sym, pg['sl']))
                elif pg['act'] in ('trim', 'close'):
                    cands.append({**base, 'price': pg['price'],
                                  'id': 'short|%s|pg' % sym, 'action': 'sell',
                                  'entry': pg['entry'], 'pnl_pct': pg['pnl_pct'],
                                  'peak_pct': pg['peak_pct'], 'dd_peak': pg['dd_peak'],
                                  'why': '利润保护：' + pg['why'],
                                  'hard': False, 'kind': 'protect', 'pg': pg})
        elif not holding and not st['cooldown'] and sig['above']:
            margin = eq * cfg['alloc']
            notional = margin * LEV
            if notional < MIN_NOTIONAL:
                log.info('%s 名义%.0f < %dU 跳过', cfg['name'], notional, MIN_NOTIONAL)
                continue
            cands.append({**base, 'id': 'short|%s|buy' % sym, 'action': 'buy',
                          'why': '%s收盘站上SMA%d %.2f（趋势突破）名义~%.0f' % (
                              TIMEFRAME, SMA_N, sig['ma'], notional),
                          'hard': False, 'notional': notional})

    # ---- 阶段二：LLM 复核 ----
    if cands:      # dry 模式也走 LLM 全链路（只是不下单），便于演练
        dec = llm_gate.gate(st, cands, ctx_note='短线池净值 %.2f（峰值 %.2f，回撤 %.1f%%）%s，持仓 %d 只' % (
            eq, st['peak'], dd * 100, TIMEFRAME, len(st['real_pos'])))
        srcs = ','.join(sorted({(d or {}).get('src', '?') for d in dec.values()})) or '-'
        log.info('[llm][gate] 候选%d → 来源:%s', len(cands), srcs)
        try:
            _added = llm_gate.shadow_record(st, cands, dec)
            if _added:
                log.info('[shadow] 登记 %d 条被否决买入，跟踪 %.0f 天', _added, llm_gate.SHADOW_DAYS)
        except Exception as _e:
            log.warning('[shadow] 登记失败: %s', str(_e)[:120])
        for c in cands:
            d = dec.get(c['id']) or {}
            c['llm'] = d
            if d.get('verdict') == 'avoid' and not c.get('hard'):
                log.warning('🧠 LLM否决买入 %s：%s', c['name'], d.get('note') or '')
                st.setdefault('log', []).append({
                    'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'msg': 'LLM 否决买入(短线) %s：%s（%s）' % (
                        c['name'], d.get('note') or '', c['why'])})
    else:
        for c in cands:
            c['llm'] = {}

    # ---- 阶段三：执行 ----
    def _pg_set_sl(_ex, _sym, _qty, _sl):
        _e = float((st['real_pos'].get(_sym) or {}).get('entry') or 0)
        return set_sl(_ex, _sym, _qty, _e, sl_price=_sl)

    def _pg_close(_ex, _sym, _amt, _llm, _why=None):
        return sell_close(_ex, _sym, _amt, _llm, why=_why)

    PG_HOOKS = {'cancel_sl': cancel_sl, 'set_sl': _pg_set_sl, 'close': _pg_close}

    # 3a. 保本上移（纯降险，直接执行，不占 LLM 额度）
    for sym, sl in sl_tasks:
        pos = st['real_pos'].get(sym) or {}
        try:
            cancel_sl(ex, sym)
            set_sl(ex, sym, float(pos.get('amt') or 0),
                   float(pos.get('entry') or 0), sl_price=sl)
            pos['sl'] = sl
            log.info('[利润保护] %s 止损上移保本 → %.2f', sym, sl)
        except Exception as e:
            log.warning('[利润保护] %s 上移止损失败: %s', sym, str(e)[:120])

    for c in cands:
        sym, cfg = c['symbol'], SYMBOLS[c['symbol']]
        if c['action'] == 'sell':
            # 硬规则强制执行；利润保护类 LLM 说继续持有就听它的
            if not c.get('hard') and c['llm'].get('verdict') == 'avoid':
                log.warning('🧠 LLM建议继续持有 %s：%s', cfg['name'], c['llm'].get('note') or '')
                st.setdefault('log', []).append({
                    'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'msg': 'LLM 建议继续持有(短线) %s：%s（%s）' % (
                        cfg['name'], c['llm'].get('note') or '', c['why'])})
                continue
            if c.get('pg'):
                try:
                    profit_guard.apply(ex, st, sym, c['pg'], PG_HOOKS, llm=c['llm'])
                except Exception as e:
                    log.error('[利润保护] %s 执行失败: %s', cfg['name'], str(e)[:150])
                continue
            amt = sell_close(ex, sym, llm=c['llm'])
            if amt:
                log.info('平 %s %f 张%s', cfg['name'], amt,
                         '｜LLM:%s' % c['llm'].get('note') if c['llm'].get('note') else '')
                st['real_pos'].pop(sym, None)
        else:
            if c['llm'].get('verdict') == 'avoid':
                continue
            if dry:
                log.info('[dry] 开多 %s 名义~%.0f', cfg['name'], c['notional'])
                continue
            amt = buy_open(ex, sym, c['notional'], llm=c['llm'])
            log.info('开多 %s %.6f 张 (名义~%.0f)%s', cfg['name'], amt, c['notional'],
                     '｜LLM:%s' % c['llm'].get('note') if c['llm'].get('note') else '')
    save_pool(st)


def main():
    ex = make_fex()
    ex.load_markets()
    st = load_pool()
    mode = sys.argv[1] if len(sys.argv) > 1 else 'loop'
    if mode == 'once':
        decide(ex, st)
        return
    log.info('短线bot启动: %s刀池 %d标 %gx SMA%d %s, 每%.0fmin, 止损-%d%%',
             POOL_START, len(SYMBOLS), LEV, SMA_N, TIMEFRAME, LOOP_MIN, SL_PCT * 100)
    while True:
        # WSL 时钟漂移（实测本机比币安快 ~2.4s）：ccxt 的 adjustForTimeDifference 只在
        # load_markets 时校准一次，之后漂移会让请求报 -1021（timeout/挂止损失败）。每轮重新校准。
        try:
            ex.load_time_difference()
        except Exception:
            pass
        try:
            import algo_tools as _at
            _at.sync_time()      # algo 条件单走自建 REST，需独立对时（-1021 自愈）
        except Exception:
            pass
        try:
            decide(ex, st)
        except Exception as e:
            cause = repr(e.__cause__) if e.__cause__ else ''
            log.error('本轮失败: %s | cause=%s', e, cause)
            try:
                time.sleep(5)
                decide(ex, st)
                log.info('重试成功')
            except Exception as e2:
                log.error('重试也失败: %s', str(e2)[:150], exc_info=True)
        time.sleep(LOOP_MIN * 60)


if __name__ == '__main__':
    main()
