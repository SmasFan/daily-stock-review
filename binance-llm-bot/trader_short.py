#!/usr/bin/env python3
"""日内混合趋势 bot (Demo 合约) — 独立池, 与 trader100 并行

8 股票 TRADIFI 永续等权, 独立 100U 池记账 (state_short.json)
杠杆 5x（可配） | 日线定方向 + 1h 择时 | 每 15min 巡检 | 止损 2%（多空同口径）

2026-11-10 短线化：旧的「1h SMA50 趋势」全部负期望（回测 -45%/回撤 57%），
已换成与 trader100 同一套 intraday_signal：
  日线 20 根动量 > +2% 定多头方向、< -2% 定空头方向（半仓），中间观望；
  方向确定后，1h 12 根动量 > +1% 且价在 1h SMA200 上方才开多，
  < -1% 且在 SMA200 下方才开空；动量衰减到 0 或日线方向翻转即离场。
  v3 回测（13 标、双边 0.04% 费）：全期 +22.0%/回撤 11.5%/样本外 +2.9%。
  详见 backtest/SHORTTERM.md；双向做空是样本外转正的关键，故启用开空。

T+0 利润保护(profit_guard 'intraday'): 峰值浮盈 1% 止损上移保本；2% 先平一半；
从峰值回撤 0.6% 或赚够 3.5% 破 SMA20 → 落袋。平仓类交 LLM 复核（默认放行）。
"""
import os, sys, json, time, logging
import ccxt
from dotenv import load_dotenv

# 先加载 .env，llm_gate 的模块级开关才读得到（见 trader100.py 同处注释）
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

import algo_tools
import llm_gate     # 成交前 LLM 风控复核（买入可否决；趋势离场硬规则）
import profit_guard  # T+0 利润保护：保本上移 / 分批止盈 / 移动止盈
import pos_guard     # 方向/规模守卫：反向仓与超仓自动市价纠正（2026-09-30 META 事故）
import intraday_signal as isig  # 日线定方向 + 1h 择时（必须在 load_dotenv 之后导入）

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
for _n in ('httpx', 'httpx2', 'httpcore', 'openai'):   # 屏蔽 SDK 每次请求的 INFO 噪音
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger('tshort')

BASE = os.path.dirname(os.path.abspath(__file__))
POOL_FILE = os.path.join(BASE, 'state_short.json')
POOL_START = float(os.environ.get('SHORT_POOL', '100'))
LEV = float(os.environ.get('SHORT_LEV', '5'))
FEE = 0.0004
PROXY = os.environ.get('PROXY', '' if sys.platform == 'darwin' else 'socks5h://172.25.16.1:10808')
LOOP_MIN = float(os.environ.get('LOOP_MIN', os.environ.get('SHORT_LOOP_MIN', '15')))
DD_STOP = float(os.environ.get('SHORT_DD_STOP', '0.20'))
SL_PCT = float(os.environ.get('SHORT_SL_PCT', '0.02'))      # 多头止损 -2%（v3 回测口径）
TIMEFRAME = os.environ.get('SHORT_TF', isig.EXEC_TF)         # 1h 执行层
SMA_N = int(os.environ.get('SHORT_SMA', '50'))               # 仅用于 LLM 上下文（50 小时均线）
# 2026-11-10 起默认开新仓：旧 1h SMA 负期望的问题已由日内混合信号解决。
SHORT_ENABLED = os.environ.get('SHORT_ENABLED', '1').lower() in ('1', 'true', 'yes', 'on')
# 双向：与 trader100 / guard.py 共用开关；空头名义 × SHORT_RISK、止损 +2%。
ALLOW_SHORT = os.environ.get('BN_ALLOW_SHORT', '0').lower() not in ('0', 'false', 'off', 'no')
SHORT_STOP_PCT = float(os.environ.get('BN_SHORT_SL', '0.02'))
SHORT_RISK = float(os.environ.get('BN_SHORT_RISK', '0.5'))
SHORT_MOM_TH = float(os.environ.get('BN_SHORT_MOM_TH', '0.02'))
DAILY_SIGNAL = os.environ.get('DAILY_SIGNAL', 'mom').lower()
DAILY_N = int(os.environ.get('DAILY_MOM_N', '20'))
DAILY_TH = float(os.environ.get('DAILY_MOM_TH', '0.02'))

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
    max_notional = POOL_START * LEV * 3      # 单仓名义上限：池子满杠杆的 3 倍，超出即为异常
    for p in positions:
        sym = p['symbol']
        # 方向守卫：未登记/方向不符/超仓先市价清掉，绝不当成「持有中」参与决策
        expect = pos_guard.registered_side(st, sym)
        if pos_guard.flatten_unexpected(ex, sym, p, st, max_notional=max_notional,
                                        name=(SYMBOLS.get(sym) or {}).get('name'), why='短线sync',
                                        allow_short=ALLOW_SHORT, expected_side=expect):
            continue
        amt = abs(float(p['contracts']))
        if amt != 0:
            old = st['real_pos'].get(sym) or {}
            entry = float(p['entryPrice'])
            rec = {'amt': amt, 'entry': entry, 'side': pos_guard.side_of(p)}
            # ds4.1 审阅：多↔空翻转时清利润保护状态，多头峰值不能带进空头
            if old.get('side') and old['side'] != rec['side']:
                rec.pop('peak', None); rec.pop('trimmed', None); rec.pop('sl', None)
                rec['ts'] = time.time()
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
    tf_ms = {'1m': 60000, '5m': 300000, '15m': 900000,
             '1h': 3600000, '4h': 14400000, '1d': 86400000}.get(tf, 3600000)
    return [c[4] for c in ohlcv if c[0] + tf_ms <= now]


def sma(closes, w):
    return sum(closes[-w:]) / w if len(closes) >= w else None


def set_sl(ex, sym, qty, entry, sl_price=None, side='long'):
    """挂止损；多头 SELL STOP 在下方，空头 BUY STOP 在上方。"""
    is_short = side == 'short'
    if sl_price:
        sl_price = round(sl_price, 2)
    elif is_short:
        sl_price = round(entry * (1 + SHORT_STOP_PCT), 2)
    else:
        sl_price = round(entry * (1 - SL_PCT), 2)
    pct = (sl_price / entry - 1) * 100 if entry else SL_PCT * 100
    try:
        r = algo_tools.place_stop_market(sym, 'buy' if is_short else 'sell', qty, sl_price)
        log.info('挂止损 %s @ %.2f (%+.1f%%) %s algoId=%s', sym, sl_price, pct,
                 '空' if is_short else '多', r.get('algoId'))
        return r.get('algoId')
    except Exception as e:
        log.warning('挂止损失败 %s: %s', sym, str(e)[:120])
        return None


def cancel_sl(ex, sym):
    try:
        algo_tools.cancel_all_algo(sym)
    except Exception as e:
        log.warning('撤 algo 失败 %s: %s', sym, str(e)[:80])


def buy_open(ex, sym, notional_usd, llm=None, st=None):
    # ds4.1 审阅：先核对仓位再撤止损，避免「中止开仓却把已有仓止损撤掉」的裸仓窗口。
    for _p in ex.fetch_positions([sym]):
        if float(_p.get('contracts') or 0) and pos_guard.side_of(_p) != 'long':
            log.error('开多前发现 %s 非多头仓位(%s)，已中止本次开仓', sym, pos_guard.side_of(_p))
            return 0.0
    ex.set_leverage(int(LEV), sym)
    cancel_sl(ex, sym)
    t = ex.fetch_ticker(sym)
    qty = ex.amount_to_precision(sym, notional_usd / t['last'])
    o = ex.create_order(sym, 'market', 'buy', qty, None, {'reduceOnly': False})
    filled = float(o.get('filled', qty))
    avg = float(o.get('average') or t['last'])
    # ds4.1 审阅 P0：成交后立即用真实成交均价落盘登记方向，再挂止损。
    # set_sl 抛异常 / 进程被杀时，guard 才不会把刚开的仓当未登记事故残留平掉。
    if st is not None and filled:
        st.setdefault('real_pos', {})[sym] = {
            'amt': float(filled), 'entry': avg, 'side': 'long', 'ts': time.time()}
        save_pool(st)
    aid = set_sl(ex, sym, filled, avg)
    from trade_log import record
    _n = (llm or {}).get('note')
    record('open', sym, 'BUY', filled, avg,
           detail=f'短线{tf_tag()} 名义~{notional_usd:.0f}' + (f'｜LLM:{_n}' if _n else '｜LLM:未复核'),
           pool='short', lev=LEV, notional=notional_usd)
    return filled


def sell_open(ex, sym, notional_usd, llm=None, st=None):
    """市价开空（与 buy_open 对称，名义按 SHORT_RISK 缩过，成交后立刻登记方向）。"""
    for _p in ex.fetch_positions([sym]):
        if float(_p.get('contracts') or 0):
            log.error('开空前发现 %s 已有仓位(%s)，已中止本次开空', sym, pos_guard.side_of(_p))
            return 0.0
    ex.set_leverage(int(LEV), sym)
    cancel_sl(ex, sym)
    t = ex.fetch_ticker(sym)
    qty = ex.amount_to_precision(sym, notional_usd / t['last'])
    o = ex.create_order(sym, 'market', 'sell', qty, None, {'reduceOnly': False})
    filled = float(o.get('filled', qty))
    avg = float(o.get('average') or t['last'])
    if st is not None and filled:
        st.setdefault('real_pos', {})[sym] = {
            'amt': float(filled), 'entry': avg, 'side': 'short', 'ts': time.time()}
        save_pool(st)
    set_sl(ex, sym, filled, avg, side='short')
    from trade_log import record
    _n = (llm or {}).get('note')
    record('open', sym, 'SELL_SHORT', filled, avg,
           detail=(f'短线{tf_tag()} 空头 名义~{notional_usd:.0f} 止损+{SHORT_STOP_PCT*100:.0f}%'
                   + (f'｜LLM:{_n}' if _n else '｜LLM:未复核')),
           pool='short', lev=LEV, notional=notional_usd)
    return filled


def tf_tag():
    return TIMEFRAME


def buy_cover(ex, sym, amt=None, llm=None, why=None):
    """买入平空（reduceOnly）。短线池默认不开空，但已登记空头必须能正确买回。"""
    cancel_sl(ex, sym)
    try:
        ex.cancel_all_orders(sym)
    except Exception:
        pass
    real_short, entry_px, side = 0.0, 0.0, 'flat'
    try:
        for p in ex.fetch_positions([sym]):
            if not float(p.get('contracts') or 0):
                continue
            side = pos_guard.side_of(p)
            if side == 'short':
                real_short = abs(float(p['contracts']))
                entry_px = float(p['entryPrice'])
    except Exception as e:
        log.warning('查持仓失败 %s: %s', sym, str(e)[:100])
    if side == 'long':
        log.error('%s 是多头仓，拒绝买入平空（会继续加多）→ 交方向守卫处理', sym)
        return 0
    qty = real_short if amt is None else min(float(amt), real_short)
    if qty and qty > 0:
        o = ex.create_order(sym, 'market', 'buy', ex.amount_to_precision(sym, qty),
                            None, {'reduceOnly': True})
        avg = float(o.get('average') or (o.get('info') or {}).get('avgPrice') or 0)
        if not avg:
            try:
                avg = float(ex.fetch_ticker(sym).get('last') or 0)
            except Exception:
                avg = 0.0
        pnl = float((o.get('info') or {}).get('realizedPnl') or 0)
        if not pnl:
            pnl = (entry_px - avg) * qty if (avg and entry_px) else 0
        from trade_log import record
        _n = (llm or {}).get('note')
        record('close', sym, 'BUY_COVER', qty, avg, pnl,
               detail=(why or '空头趋势离场') + (f'｜LLM:{_n}' if _n else ''), pool='short', lev=LEV)
        return qty
    return 0


def close_pos(ex, sym, side, amt=None, llm=None, why=None):
    """按持仓方向选平仓函数：多头卖出 / 空头买回。

    ds4.1 审阅：传入方向与交易所真实方向不符时一律拒绝，防 9-30 事故
    （stale state='long' + 实际 short → 卖出平多 = 继续加空）。
    """
    if side not in ('long', 'short'):
        log.error('%s close_pos 方向未指定，拒绝平仓', sym)
        return 0
    try:
        real = 'flat'
        for _p in ex.fetch_positions([sym]):
            if float(_p.get('contracts') or 0):
                real = pos_guard.side_of(_p)
                break
        if real != 'flat' and real != side:
            log.error('%s close_pos 方向不符: 请求%s / 实际%s → 拒绝，交方向守卫',
                      sym, side, real)
            return 0
    except Exception as e:
        log.warning('%s close_pos 查持仓失败: %s', sym, str(e)[:100])
    if side == 'short':
        return buy_cover(ex, sym, amt=amt, llm=llm, why=why)
    return sell_close(ex, sym, amt=amt, llm=llm, why=why)


def sell_close(ex, sym, amt=None, llm=None, why=None):
    """撤 algo 单 + 市价平仓 (why 区分趋势离场/利润保护)"""
    cancel_sl(ex, sym)
    try:
        ex.cancel_all_orders(sym)
    except Exception:
        pass
    # 只平多头：拿真实多头数量，空头/反向仓一律不卖（卖了就是加空，历史事故根因）
    real_long, entry_px, side, other_amt = 0.0, 0.0, 'flat', 0.0
    try:
        for p in ex.fetch_positions([sym]):
            if not float(p.get('contracts') or 0):
                continue
            side = pos_guard.side_of(p)
            if side == 'long':
                real_long = float(p['contracts'])
                entry_px = float(p['entryPrice'])
            else:
                other_amt = float(p['contracts'])
    except Exception as e:
        log.warning('查持仓失败 %s: %s', sym, str(e)[:100])
    if side == 'short':
        log.error('%s 是空头仓(%.4f)，拒绝卖出平仓（会继续加空）→ 交方向守卫处理', sym, other_amt)
        return 0
    qty = real_long if amt is None else min(float(amt), real_long)
    if qty and qty > 0:
        o = ex.create_order(sym, 'market', 'sell', ex.amount_to_precision(sym, qty), None, {'reduceOnly': True})
        # 成交均价兜底：market 单 average 偶尔为空 → info.avgPrice → ticker，避免 pnl 恒为 0
        avg = float(o.get('average') or (o.get('info') or {}).get('avgPrice') or 0)
        if not avg:
            try:
                avg = float(ex.fetch_ticker(sym).get('last') or 0)
            except Exception:
                avg = 0.0
        # 盈亏优先用交易所口径 realizedPnl（含手续费，最准）；拿不到再用均价差算
        pnl = float((o.get('info') or {}).get('realizedPnl') or 0)
        if not pnl:
            pnl = (avg - entry_px) * qty if (avg and entry_px) else 0
        from trade_log import record
        _n = (llm or {}).get('note')
        record('close', sym, 'SELL', qty, avg, pnl,
               detail=(why or f'短线{tf_tag()} SMA离场') + (f'｜LLM:{_n}' if _n else ''), pool='short', lev=LEV)
        return qty
    return 0


def pool_equity_real(ex, st):
    pnl = 0.0
    for sym in SYMBOLS:
        for pp in ex.fetch_positions([sym]):
            amt = float(pp['contracts'])
            if amt:
                pnl += float(pp['unrealizedPnl'])
    return POOL_START + pnl


def hybrid_signal(ex, sym):
    """日内混合信号：日线定方向 + 1h 择时（与 trader100 同一套参数）。"""
    daily = isig.daily_dir(closed_bars(ex, sym, '1d', DAILY_N + 5),
                           mode=DAILY_SIGNAL, cfg={'sma': SMA_N},
                           n=DAILY_N, th=DAILY_TH, short_th=SHORT_MOM_TH,
                           short_ok=ALLOW_SHORT)
    need = max(isig.TREND_N, isig.EXEC_N + 1) + 5
    exec_sig = isig.exec_signal(closed_bars(ex, sym, TIMEFRAME, need))
    return daily, exec_sig, isig.target(daily, exec_sig)


def decide(ex, st, dry=False):
    """一轮决策：日线定方向 + 1h 择时 → LLM 复核 → 下单（与 trader100 同构）。

    买入可被 LLM 否决；趋势离场（日线方向翻转或 1h 动量衰减）标 hard=True。
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
    # 各标信号：日线定方向，1h 择时（两段时间框架分别只取已收 K）
    sigs = {}
    for sym, cfg in SYMBOLS.items():
        try:
            daily, exec_sig, side = hybrid_signal(ex, sym)
        except Exception as e:
            log.warning('%s 行情获取失败: %s', cfg['name'], str(e)[:120]); continue
        if daily['mom'] is None or exec_sig['mom'] is None:
            log.warning('%s 日线/1h 历史不足', cfg['name']); continue
        sigs[sym] = {'daily': daily, 'exec': exec_sig, 'side': side}
        log.info('%s %s', cfg['name'], isig.describe(daily, exec_sig, side))

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

    # SHORT_ENABLED=0 时只平不开（旧 1h SMA 策略的保险丝，新策略下默认已开启）
    if not SHORT_ENABLED:
        st['cooldown'] = True

    # ---- 阶段一：收集候选 ----
    cands = []
    sl_tasks = []        # 保本上移任务 [(sym, sl_price, side)]
    for sym, cfg in SYMBOLS.items():
        if sym not in sigs:
            continue
        sig = sigs[sym]
        holding = sym in st['real_pos']
        pos_side = pos_guard.registered_side(st, sym) if holding else 'long'
        # 持仓时用滞回规则判断「继续持有 or 离场」；空仓时才判断要不要开仓。
        keep = (isig.target(sig['daily'], sig['exec'], holding_side=pos_side)
                if holding else None)
        ms = llm_gate.market_stats(ex, sym, sma_n=SMA_N, tf=TIMEFRAME)
        base = {'symbol': sym, 'name': cfg['name'], 'lev': LEV,
                'price': ms.get('price') or sig['exec']['price'],
                'sma50': ms.get('sma50') or sig['exec']['trend'], 'sma20': ms.get('sma20'),
                'dev50': ms.get('dev50'), 'vs_ma20': ms.get('vs_ma20'),
                'd7': ms.get('d7'), 'd30': ms.get('d30')}
        if holding and keep is None:
            pos = st['real_pos'].get(sym) or {}
            entry = float(pos.get('entry') or 0)
            sign = 1 if pos_side == 'long' else -1
            cands.append({**base, 'id': 'short|%s|sell' % sym, 'action': 'sell', 'side': pos_side,
                          'entry': entry,
                          'pnl_pct': round((base['price'] / entry - 1) * 100 * sign, 2) if entry else 0,
                          'why': '%s（%s趋势离场）' % (
                              isig.describe(sig['daily'], sig['exec'], None),
                              '空头' if pos_side == 'short' else '多头'),
                          'hard': True})
        elif holding:
            # T+0：趋势未破，但先把账面利润保护起来
            try:
                pg = profit_guard.scan(ex, st, sym, profile='intraday', ms=ms,
                                       min_notional=MIN_NOTIONAL, peak_tf=TIMEFRAME, side=pos_side)
            except Exception as e:
                log.warning('[利润保护] %s 扫描失败: %s', cfg['name'], str(e)[:120])
                pg = None
            if pg:
                log.info('[%s] %s（现价%.2f 浮盈%+.2f%% 峰值%+.2f%%）',
                         cfg['name'], pg['why'], pg['price'], pg['pnl_pct'], pg['peak_pct'])
                if pg['act'] == 'be_lock':
                    sl_tasks.append((sym, pg['sl'], pos_side))
                elif pg['act'] in ('trim', 'close'):
                    cands.append({**base, 'price': pg['price'], 'side': pos_side,
                                  'id': 'short|%s|pg' % sym, 'action': 'sell',
                                  'entry': pg['entry'], 'pnl_pct': pg['pnl_pct'],
                                  'peak_pct': pg['peak_pct'], 'dd_peak': pg['dd_peak'],
                                  'why': '利润保护：' + pg['why'],
                                  'hard': False, 'kind': 'protect', 'pg': pg})
        elif not holding and not st['cooldown'] and sig['side']:
            sig_side = sig['side']
            margin = eq * cfg['alloc']
            notional = margin * LEV * (SHORT_RISK if sig_side == 'short' else 1.0)
            if notional < MIN_NOTIONAL:
                log.info('%s 名义%.0f < %dU 跳过', cfg['name'], notional, MIN_NOTIONAL)
                continue
            cands.append({**base, 'id': 'short|%s|buy' % sym, 'action': 'buy',
                          'side': sig_side,
                          'why': '%s（%s）名义~%.0f' % (
                              isig.describe(sig['daily'], sig['exec'], sig_side),
                              '开空·半仓' if sig_side == 'short' else '趋势突破', notional),
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
        return set_sl(_ex, _sym, _qty, _e, sl_price=_sl,
                      side=pos_guard.registered_side(st, _sym))

    def _pg_close(_ex, _sym, _amt, _llm, _why=None):
        return close_pos(_ex, _sym, pos_guard.registered_side(st, _sym),
                         amt=_amt, llm=_llm, why=_why)

    PG_HOOKS = {'cancel_sl': cancel_sl, 'set_sl': _pg_set_sl, 'close': _pg_close}

    # 3a. 保本上移（纯降险，直接执行，不占 LLM 额度）
    for sym, sl, pside in sl_tasks:
        pos = st['real_pos'].get(sym) or {}
        try:
            cancel_sl(ex, sym)
            set_sl(ex, sym, float(pos.get('amt') or 0),
                   float(pos.get('entry') or 0), sl_price=sl, side=pside)
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
            amt = close_pos(ex, sym, c.get('side') or pos_guard.registered_side(st, sym),
                            llm=c['llm'])
            if amt:
                log.info('%s %s %f 张%s', '买回平空' if c.get('side') == 'short' else '平多',
                         cfg['name'], amt,
                         '｜LLM:%s' % c['llm'].get('note') if c['llm'].get('note') else '')
                st['real_pos'].pop(sym, None)
                save_pool(st)  # ds4.1 审阅：平仓后立刻落盘
        else:
            if c['llm'].get('verdict') == 'avoid':
                continue
            cside = c.get('side') or 'long'
            if dry:
                log.info('[dry] %s %s 名义~%.0f',
                         '开空' if cside == 'short' else '开多', cfg['name'], c['notional'])
                continue
            amt = (sell_open(ex, sym, c['notional'], llm=c['llm'], st=st) if cside == 'short'
                   else buy_open(ex, sym, c['notional'], llm=c['llm'], st=st))
            log.info('%s %s %.6f 张 (名义~%.0f)%s',
                     '开空' if cside == 'short' else '开多', cfg['name'], amt, c['notional'],
                     '｜LLM:%s' % c['llm'].get('note') if c['llm'].get('note') else '')
            # 记账已由 buy_open 在成交瞬间用真实成交均价落盘，不再用快照价二次覆盖
    save_pool(st)


def main():
    if not SHORT_ENABLED:
        log.warning('短线池只平不开（SHORT_ENABLED=0）：已有仓位仍会离场；'
                    '新日内混合信号已回测为正期望，建议删掉该开关')
    ex = make_fex()
    ex.load_markets()
    st = load_pool()
    mode = sys.argv[1] if len(sys.argv) > 1 else 'loop'
    if mode == 'once':
        decide(ex, st)
        return
    log.info('短线bot启动: %s刀池 %d标 %gx, 日线%d根定方向 + %s%d根择时, 每%.0fmin, 止损-%.1f%%',
             POOL_START, len(SYMBOLS), LEV, DAILY_N, TIMEFRAME, isig.EXEC_N,
             LOOP_MIN, SL_PCT * 100)
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
