#!/usr/bin/env python3
"""100 刀日内混合趋势 bot (Demo 合约, 真实下单) — 股票 TRADIFI 永续版

池子: 独立追踪 100 刀 (state_pool.json 记账池净值, 下单用池内限额)
标的: TSLA/COIN/PLTR/MSTR/HOOD 各 20%
杠杆: 5x（可配） | 周期: 日线定方向 + 1h 择时 | 每 15min 巡检 (7x24)
信号(2026-11-10 短线化, 详见 backtest/SHORTTERM.md):
       日线 20 根动量 > +2% 定多头方向、< -2% 定空头方向（半仓），中间观望；
       方向确定后，1h 12 根动量 > +1% 且价在 1h SMA200 上方才开多，
       < -1% 且在 SMA200 下方才开空；1h 动量衰减到 0 或日线方向翻转即离场。
       为什么不纯 1h：backtest/shortterm.py 扫 216 组，样本外 -2%~-9%（双边 0.04% 费
       吃光噪声收益）；v3 混合组 backtest/shortterm_hybrid.py 全期 +22.0% / 回撤
       11.5% / Sharpe 1.42 / 样本外 +2.9%，双向做空是样本外转正的关键。
约束: 每标名义 ≥ 5U(TRADIFI 门槛)；止损 2%（多空同口径）；池回撤 20% 暂停开新仓
T+0 利润保护(profit_guard 'intraday'): 峰值浮盈到 1% 止损上移保本; 到 2% 先平一半;
                            从峰值回撤 0.6% 或赚够 3.5% 破 SMA20 → 落袋
前置: 需已签 TradFi-Perps 协议 (POST /fapi/v1/stock/contract)
"""
import os, sys, json, time, logging
from datetime import datetime
import ccxt
from dotenv import load_dotenv

# 必须先加载 .env 再 import llm_gate：其模块级开关在导入时就求值，
# 放到后面会导致 .env 里的 BN_LLM_GATE_* / AGENT_LLM_ALLOW_CLOUD 读不到。
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

import algo_tools  # 显式 Algo 条件单 API (STOP 单 2025-12 起走 algo 端点)
import llm_gate     # 成交前 LLM 风控复核（买入可否决；趋势离场硬规则）
import profit_guard  # T+0 利润保护：保本上移 / 分批止盈 / 移动止盈
import pos_guard     # 方向/规模守卫：反向仓与超仓自动市价纠正
import intraday_signal as isig  # 日线定方向 + 1h 择时（必须在 load_dotenv 之后导入）

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
for _n in ('httpx', 'httpx2', 'httpcore', 'openai'):   # 屏蔽 SDK 每次请求的 INFO 噪音
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger('t100')

BASE = os.path.dirname(os.path.abspath(__file__))
POOL_FILE = os.path.join(BASE, 'state_pool.json')
POOL_START = float(os.environ.get('POOL_START', '100'))
LEV = float(os.environ.get('LEV', '5'))
FEE = 0.0004
PROXY = os.environ.get('PROXY', '' if sys.platform == 'darwin' else 'socks5h://172.25.16.1:10808')
# TRADIFI 永续 7x24 连续交易，美股 23h 交易后 1h 信号更连续；
# 巡检从 1h 缩到 15min，止损 2%，杠杆把日内小波动放大成有意义的弹性。
LOOP_MIN = float(os.environ.get('LOOP_MIN', os.environ.get('SHORT_LOOP_MIN', '15')))
DD_STOP = float(os.environ.get('DD_STOP', '0.20'))
SIGNAL = os.environ.get('DAILY_SIGNAL', 'mom').lower()   # mom | sma（日线定方向）
MOM_N = int(os.environ.get('DAILY_MOM_N', '20'))
MOM_TH = float(os.environ.get('DAILY_MOM_TH', '0.02'))
# 双向开关：默认关（纯多）。v3 回测显示双向做空是样本外转正的关键，建议在 .env 开。
ALLOW_SHORT = os.environ.get('BN_ALLOW_SHORT', '0').lower() not in ('0', 'false', 'off', 'no')
SHORT_MOM_TH = float(os.environ.get('BN_SHORT_MOM_TH', '0.02'))   # 日线跌超 2% 定空头方向
SHORT_RISK = float(os.environ.get('BN_SHORT_RISK', '0.5'))        # 空头名义 = 多头名义 × 0.5
SHORT_SL = float(os.environ.get('BN_SHORT_SL', '0.02'))            # 空头止损 +2%

# 股票 TRADIFI 永续 (测试网可成交, 需已签协议)
SYMBOLS = {
    'TSLA/USDT:USDT': {'alloc': 0.20, 'sma': int(os.environ.get('SMA_TSLA', '50')), 'name': 'TSLA', 'min_notional': 5},
    'COIN/USDT:USDT': {'alloc': 0.20, 'sma': int(os.environ.get('SMA_COIN', '50')), 'name': 'COIN', 'min_notional': 5},
    'PLTR/USDT:USDT': {'alloc': 0.20, 'sma': int(os.environ.get('SMA_PLTR', '50')), 'name': 'PLTR', 'min_notional': 5},
    'MSTR/USDT:USDT': {'alloc': 0.20, 'sma': int(os.environ.get('SMA_MSTR', '50')), 'name': 'MSTR', 'min_notional': 5},
    'HOOD/USDT:USDT': {'alloc': 0.20, 'sma': int(os.environ.get('SMA_HOOD', '50')), 'name': 'HOOD', 'min_notional': 5},
}


def make_fex():
    ex = ccxt.binance({
        'apiKey': os.environ.get('BN_API_KEY', ''),
        'secret': os.environ.get('BN_SECRET', ''),
        'enableRateLimit': True,
        'proxies': {'http': PROXY, 'https': PROXY},
        'options': {'defaultType': 'future', 'adjustForTimeDifference': True},
    })
    # 禁 keep-alive: 长间隔冷连接会被回收, 每次新建连接更稳
    ex.session.headers['Connection'] = 'close'
    ex.enable_demo_trading(True)
    return ex


def load_pool():
    if os.path.exists(POOL_FILE):
        try:
            st = json.load(open(POOL_FILE))
            return st
        except Exception:
            pass
    return {
        'pool_equity': POOL_START, 'cash': POOL_START,
        'peak': POOL_START, 'cooldown': False, 'real_pos': {},
        'log': [],
    }


def save_pool(st):
    json.dump(st, open(POOL_FILE, 'w'), indent=2)


def account_value(ex):
    """实际合约账户权益 (要扣掉池外部分? demo 5000 全是我们的)"""
    bal = ex.fetch_balance()
    return float(bal['info']['totalWalletBalance'])


def sync_from_real(ex, st):
    """真实持仓 -> 池记账 (只跟踪池内标的)

    注意：要保留利润保护写入的 peak/trimmed/sl/ts 字段；
    入场价变了说明是新仓（或加仓），peak 等状态必须重置。
    """
    algo_tools.set_symbol_map(ex.markets)
    positions = ex.fetch_positions(list(SYMBOLS.keys()))
    max_notional = POOL_START * LEV * 3      # 单仓名义上限：池子满杠杆 3 倍，超出即异常
    for p in positions:
        sym = p['symbol']
        # 方向守卫：反向仓/超仓/方向不符先市价清掉，不当成「持有中」参与决策。
        # 双向模式下只有「状态文件登记 short + 实际 short」才算策略自己开的空头。
        expect = pos_guard.registered_side(st, sym)
        if pos_guard.flatten_unexpected(
                ex, sym, p, st, max_notional=max_notional,
                name=(SYMBOLS.get(sym) or {}).get('name') or (SYMBOLS.get(sym) or {}).get('n'),
                why='日线sync', allow_short=ALLOW_SHORT, expected_side=expect):
            continue
        amt = abs(float(p['contracts']))
        if amt != 0:
            old = st['real_pos'].get(sym) or {}
            entry = float(p['entryPrice'])
            rec = {'amt': amt, 'entry': entry, 'side': pos_guard.side_of(p)}
            for k in ('peak', 'trimmed', 'sl', 'ts'):
                if k in old:
                    rec[k] = old[k]
            if old.get('entry') and abs(float(old['entry']) - entry) > 1e-9:
                for k in ('peak', 'trimmed', 'sl'):   # 新仓：利润保护状态清零
                    rec.pop(k, None)
                rec['ts'] = time.time()
            # ds4.1 审阅：方向翻转（多↔空）时多头语义的 peak/trimmed/sl 不得带进空头仓
            if old.get('side') and old['side'] != rec['side']:
                for k in ('peak', 'trimmed', 'sl'):
                    rec.pop(k, None)
                rec['ts'] = time.time()
            st['real_pos'][sym] = rec
        else:
            st['real_pos'].pop(sym, None)


TF_MS = {'1m': 60000, '5m': 300000, '15m': 900000,
         '1h': 3600000, '4h': 14400000, '1d': 86400000}


def closed_ohlcv(ex, sym, tf, need):
    """取已收盘 K 线的收盘价（未收那根不算，避免用半根 K 线触发信号）。"""
    ohlcv = ex.fetch_ohlcv(sym, tf, limit=need + 30)
    now = ex.milliseconds()
    bar = TF_MS.get(tf, 86400000)
    return [c[4] for c in ohlcv if c[0] + bar <= now]


def closed_daily(ex, sym, need):
    return closed_ohlcv(ex, sym, '1d', need)


def sma(closes, w):
    return sum(closes[-w:]) / w if len(closes) >= w else None


SL_PCT = float(os.environ.get('SL_PCT', '0.02'))   # 入场价下方 2% 止损（v3 回测口径）


def set_sl(ex, sym, qty, entry, sl_price=None, side='long'):
    """挂 reduceOnly 止损单 via Algo API (服务器端实时, 返回 algoId)

    多头：入场价下方 SL_PCT，用 SELL STOP；空头：入场价上方 SHORT_SL，用 BUY STOP。
    sl_price 给定时按给定价挂（利润保护保本上移用）。
    """
    is_short = (side == 'short')
    if sl_price:
        sl_price = round(sl_price, 2)
    elif is_short:
        sl_price = round(entry * (1 + SHORT_SL), 2)
    else:
        sl_price = round(entry * (1 - SL_PCT), 2)
    pct = (sl_price / entry - 1) * 100 if entry else 0.0
    try:
        r = algo_tools.place_stop_market(sym, 'buy' if is_short else 'sell', qty, sl_price)
        log.info('挂%s止损 %s @ %.2f (%+.1f%%) algoId=%s',
                 '空头' if is_short else '多头', sym, sl_price, pct, r.get('algoId'))
        return r.get('algoId')
    except Exception as e:
        log.warning('挂止损失败 %s: %s', sym, str(e)[:150])
        return None


def cancel_sl(ex, sym):
    """撤该标的所有 algo 条件单 (平仓前必调, 防僵尸单)"""
    try:
        algo_tools.cancel_all_algo(sym)
    except Exception as e:
        log.warning('撤 algo 单失败 %s: %s', sym, str(e)[:100])


def buy_open(ex, sym, notional_usd, llm=None, st=None):
    """市价开多, 名义 ~notional_usd。先设杠杆+挂止损。返回实际张数"""
    # ds4.1 审阅：先核对仓位再撤止损，避免「中止开仓却把已有仓止损撤掉」的裸仓窗口。
    for _p in ex.fetch_positions([sym]):
        if float(_p.get('contracts') or 0) and pos_guard.side_of(_p) != 'long':
            log.error('开多前发现 %s 非多头仓位(%s)，已中止本次开仓', sym, pos_guard.side_of(_p))
            return 0.0
    ex.set_leverage(int(LEV), sym)  # 必须 int, float 会 Binance 拒(-1102)
    cancel_sl(ex, sym)  # 清可能残留的旧止损单
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
           detail=f'名义~{notional_usd:.0f}' + (f'｜LLM:{_n}' if _n else '｜LLM:未复核'),
           pool='daily', lev=LEV, notional=notional_usd)
    return filled


def sell_open(ex, sym, notional_usd, llm=None, st=None):
    """市价开空（TRADIFI 永续做空）。与 buy_open 对称，但名义按 SHORT_RISK 缩过。

    开空前发现任何非 flat 仓位一律中止：绝不在已有仓位上反向叠加。
    """
    # ds4.1 审阅：先查仓再撤止损；成交后先落盘登记再挂止损（防竞态）。
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
    aid = set_sl(ex, sym, filled, avg, side='short')
    from trade_log import record
    _n = (llm or {}).get('note')
    record('open', sym, 'SELL_SHORT', filled, avg,
           detail=f'空头 名义~{notional_usd:.0f} 止损+{SHORT_SL*100:.0f}%' +
                  (f'｜LLM:{_n}' if _n else '｜LLM:未复核'),
           pool='daily', lev=LEV, notional=notional_usd)
    return filled


def buy_cover(ex, sym, amt=None, llm=None, why=None):
    """买入平空（reduceOnly）。空头仓绝不能用 sell_close，那是 9-30 事故根因。"""
    cancel_sl(ex, sym)
    try:
        ex.cancel_all_orders(sym)
    except Exception:
        pass
    real_short, entry_px, side, other_amt = 0.0, 0.0, 'flat', 0.0
    try:
        for p in ex.fetch_positions([sym]):
            if not float(p.get('contracts') or 0):
                continue
            side = pos_guard.side_of(p)
            if side == 'short':
                real_short = abs(float(p['contracts']))
                entry_px = float(p['entryPrice'])
            else:
                other_amt = float(p['contracts'])
    except Exception as e:
        log.warning('查持仓失败 %s: %s', sym, str(e)[:100])
    if side == 'long':
        log.error('%s 是多头仓(%.4f)，拒绝买入平空（会继续加多）→ 交方向守卫处理', sym, other_amt)
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
               detail=(why or '空头趋势离场') + (f'｜LLM:{_n}' if _n else ''),
               pool='daily', lev=LEV)
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
    """撤 algo 单 + 市价平仓, 记录盈亏 (why 用于区分趋势离场/利润保护)"""
    cancel_sl(ex, sym)  # 先撤止损单 (防平仓后触发反向)
    try:
        ex.cancel_all_orders(sym)
    except Exception:
        pass
    # 只平多头：拿真实多头数量（空头仓位反向卖出会变成加空，事故根因）
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
        avg = float(o.get('average') or (o.get('info') or {}).get('avgPrice') or 0)
        if not avg:
            try:
                avg = float(ex.fetch_ticker(sym).get('last') or 0)
            except Exception:
                avg = 0.0
        # 盈亏优先用交易所口径 realizedPnl（含手续费，最准）
        pnl = float((o.get('info') or {}).get('realizedPnl') or 0)
        if not pnl:
            pnl = (avg - entry_px) * qty if (avg and entry_px) else 0
        from trade_log import record
        _n = (llm or {}).get('note')
        record('close', sym, 'SELL', qty, avg, pnl,
               detail=(why or 'SMA离场') + (f'｜LLM:{_n}' if _n else ''), pool='daily', lev=LEV)
        return qty
    return 0


def pool_equity_real(ex, st):
    """池净值 = 初始100 + 各标的总未实现盈亏 (池只跟踪池内标的)"""
    pnl = 0.0
    for sym, cfg in SYMBOLS.items():
        for pp in ex.fetch_positions([sym]):
            amt = float(pp['contracts'])
            if amt:
                pnl += float(pp['unrealizedPnl'])
    return POOL_START + pnl


def calc_signal(closes, cfg):
    """按 DAILY_SIGNAL 计算单标信号（纯函数，便于回测/单测）。"""
    ma = sma(closes, cfg['sma'])
    mom = None
    if SIGNAL == 'mom':
        mom = closes[-1] / closes[-1 - MOM_N] - 1
        above = mom > MOM_TH
        desc = '日线%d根动量 %+.2f%% / 阈值 %.0f%%' % (MOM_N, mom * 100, MOM_TH * 100)
    else:
        above = closes[-1] > ma
        desc = '日线收盘 %.2f / SMA%d %.2f' % (closes[-1], cfg['sma'], ma)
    # side: 'long' / 'short' / None（观望）。above 保留给旧逻辑与单测。
    side = 'long' if above else None
    if ALLOW_SHORT and SIGNAL == 'mom' and mom is not None and mom < -SHORT_MOM_TH:
        side = 'short'
        desc += ' | 跌超%.0f%% → 开空(半仓)' % (SHORT_MOM_TH * 100)
    return {'above': above, 'price': closes[-1], 'ma': ma, 'mom': mom, 'desc': desc, 'side': side}


def hybrid_signal(ex, sym, cfg):
    """日内混合信号：日线定方向 + 1h 择时。返回 (daily, exec, side)。

    calc_signal 保留为旧日线信号的纯函数（单测/回退用），实盘走这里。
    """
    d_need = max(cfg.get('sma', 50) if SIGNAL == 'sma' else 0, MOM_N + 1) + 5
    daily = isig.daily_dir(closed_daily(ex, sym, d_need), mode=SIGNAL, cfg=cfg,
                           n=MOM_N, th=MOM_TH, short_th=SHORT_MOM_TH,
                           short_ok=ALLOW_SHORT)
    h_need = max(isig.TREND_N, isig.EXEC_N + 1) + 5
    exec_sig = isig.exec_signal(closed_ohlcv(ex, sym, isig.EXEC_TF, h_need))
    return daily, exec_sig, isig.target(daily, exec_sig)


def decide(ex, st, dry=False):
    """一轮决策：信号 → LLM 复核 → 下单。

    两阶段：先把买卖候选收集齐（不下单），合并成 1 次 LLM 请求复核，再执行。
    买入可被 LLM 否决（避免在局部高点/动能转弱时追高）；
    趋势离场（动量跌破阈值 / 收盘跌破 SMA）属策略核心，标 hard=True，LLM 只能确认。
    """
    today = datetime.utcnow().date().isoformat()
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
    log.info('=== %s ===', today)

    # 各标信号：日线定方向，1h 择时（两段时间框架分别只取已收 K）
    sigs = {}
    for sym, cfg in SYMBOLS.items():
        try:
            daily, exec_sig, side = hybrid_signal(ex, sym, cfg)
        except Exception as e:
            log.warning('%s 行情获取失败: %s', cfg['name'], str(e)[:120]); continue
        if daily['mom'] is None or exec_sig['mom'] is None:
            log.warning('%s 日线/1h 历史不足', cfg['name']); continue
        sigs[sym] = {'daily': daily, 'exec': exec_sig, 'side': side}
        log.info('%s %s', cfg['name'], isig.describe(daily, exec_sig, side))

    # 回撤风控: 深度回撤时暂停开新仓 (不清现有仓), 净值回升解除
    eq = pool_equity_real(ex, st)
    if eq > st['peak']: st['peak'] = eq
    dd = 1 - eq / st['peak'] if st['peak'] else 0
    log.info('池净值 %.2f (峰%.2f, 回撤%.1f%%)', eq, st['peak'], dd * 100)
    if dd > DD_STOP:
        st['cooldown'] = True
        log.warning('回撤%.0f%% 暂停开新仓 (持仓保留)', dd * 100)
    if eq >= st['peak']:
        st['cooldown'] = False  # 创新高解除

    # ---- 阶段一：收集候选（不下单）----
    ctx_cache = {}
    cands = []
    sl_tasks = []        # 保本上移任务 [(sym, sl_price)]
    for sym, cfg in SYMBOLS.items():
        if sym not in sigs:
            continue
        sig = sigs[sym]
        holding = sym in st['real_pos']
        ms = ctx_cache.get(sym)
        if ms is None:
            ms = ctx_cache[sym] = llm_gate.market_stats(ex, sym, sma_n=cfg['sma'],
                                                          tf=isig.EXEC_TF)
        base = {'symbol': sym, 'name': cfg['name'], 'lev': LEV,
                'price': ms.get('price') or sig['exec']['price'],
                'sma50': ms.get('sma50') or sig['exec']['trend'], 'sma20': ms.get('sma20'),
                'dev50': ms.get('dev50'), 'vs_ma20': ms.get('vs_ma20'),
                'd7': ms.get('d7'), 'd30': ms.get('d30')}
        pos_state = st['real_pos'].get(sym) or {}
        pos_side = pos_guard.registered_side(st, sym) if holding else 'long'
        # 持仓时用滞回规则判断「继续持有 or 离场」；空仓时才判断要不要开仓。
        keep = (isig.target(sig['daily'], sig['exec'], holding_side=pos_side)
                if holding else None)
        if holding and keep is None:
            # 信号不再支持当前方向（含观望）→ 按方向平仓；多头卖、空头买回。
            # 反手不做到同一轮：先平，下一轮 sync 干净后再开，避免「卖出平多」打到空头上。
            entry = float(pos_state.get('entry') or 0)
            sign = 1 if pos_side == 'long' else -1
            cands.append({**base, 'id': 't100|%s|sell' % sym, 'action': 'sell',
                          'side': pos_side, 'entry': entry,
                          'pnl_pct': round((base['price'] / entry - 1) * 100 * sign, 2) if entry else 0,
                          'why': '%s（%s趋势离场）' % (
                              isig.describe(sig['daily'], sig['exec'], None),
                              '空头' if pos_side == 'short' else '多头'),
                          'hard': True})
        elif holding:
            # T+0：趋势还在，但账面利润要保护 —— 保本上移 / 分批止盈 / 移动止盈
            try:
                pg = profit_guard.scan(ex, st, sym, profile='intraday', ms=ms,
                                       min_notional=cfg['min_notional'], side=pos_side)
            except Exception as e:
                log.warning('[利润保护] %s 扫描失败: %s', cfg['name'], str(e)[:120])
                pg = None
            if pg:
                log.info('[%s] %s（现价%.2f 浮盈%+.2f%% 峰值%+.2f%%）',
                         cfg['name'], pg['why'], pg['price'], pg['pnl_pct'], pg['peak_pct'])
                if pg['act'] == 'be_lock':
                    sl_tasks.append((sym, pg['sl'], pos_side))
                elif pg['act'] in ('trim', 'close'):
                    cands.append({**base, 'price': pg['price'],
                                  'id': 't100|%s|pg' % sym, 'action': 'sell', 'side': pos_side,
                                  'entry': pg['entry'], 'pnl_pct': pg['pnl_pct'],
                                  'peak_pct': pg['peak_pct'], 'dd_peak': pg['dd_peak'],
                                  'why': '利润保护：' + pg['why'],
                                  'hard': False, 'kind': 'protect', 'pg': pg})
        elif not holding and not st['cooldown'] and sig['side']:
            sig_side = sig['side']
            margin = eq * cfg['alloc']
            notional = margin * LEV * (SHORT_RISK if sig_side == 'short' else 1.0)
            if notional < cfg['min_notional']:
                log.info('%s 名义%.0f < 门槛%d 跳过', cfg['name'], notional, cfg['min_notional'])
                continue
            cands.append({**base, 'id': 't100|%s|buy' % sym, 'action': 'buy',
                          'side': sig_side,
                          'why': '%s（%s）名义~%.0f' % (
                              isig.describe(sig['daily'], sig['exec'], sig_side),
                              '开空·半仓' if sig_side == 'short' else '趋势突破', notional),
                          'hard': False, 'notional': notional})

    # ---- 阶段二：LLM 成交前复核（本轮候选合并 1 次请求）----
    if cands:      # dry 模式也走 LLM 全链路（只是不下单），便于演练
        dec = llm_gate.gate(st, cands, ctx_note='池净值 %.2f（峰值 %.2f，回撤 %.1f%%），持仓 %d 只' % (
            eq, st['peak'], dd * 100, len(st['real_pos'])))
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
                    'ts': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                    'msg': 'LLM 否决买入 %s：%s（%s）' % (
                        c['name'], d.get('note') or '', c['why'])})
    else:
        for c in cands:
            c['llm'] = {}

    # ---- 阶段三：执行 ----
    def _pg_set_sl(_ex, _sym, _qty, _sl):
        _p = st['real_pos'].get(_sym) or {}
        _e = float(_p.get('entry') or 0)
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
            # 硬规则（趋势离场）强制执行；利润保护类 LLM 说继续持有就听它的
            if not c.get('hard') and c['llm'].get('verdict') == 'avoid':
                log.warning('🧠 LLM建议继续持有 %s：%s', cfg['name'], c['llm'].get('note') or '')
                st.setdefault('log', []).append({
                    'ts': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                    'msg': 'LLM 建议继续持有 %s：%s（%s）' % (
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
                save_pool(st)  # ds4.1 审阅：平仓后立刻落盘，别依赖函数末尾单次保存
        else:
            if c['llm'].get('verdict') == 'avoid':
                continue
            cside = c.get('side') or 'long'
            if dry:
                log.info('[dry] %s %s 名义 ~%.0f',
                         '开空' if cside == 'short' else '开多', cfg['name'], c['notional'])
                continue
            amt = (sell_open(ex, sym, c['notional'], llm=c['llm'], st=st) if cside == 'short'
                   else buy_open(ex, sym, c['notional'], llm=c['llm'], st=st))
            log.info('%s %s %.6f 张 (名义~%.0f)%s',
                     '开空' if cside == 'short' else '开多', cfg['name'], amt, c['notional'],
                     '｜LLM:%s' % c['llm'].get('note') if c['llm'].get('note') else '')
            # 记账已由 buy_open/sell_open 在成交瞬间用真实成交均价落盘，
            # 不再用候选价快照二次覆盖（ds4.1 审阅 P0）。
    save_pool(st)


def main():
    ex = make_fex()
    ex.load_markets()
    st = load_pool()
    mode = sys.argv[1] if len(sys.argv) > 1 else 'loop'
    if mode == 'once':
        decide(ex, st)
        return
    log.info('trader100 启动: %s刀池 %d标 %gx, 日线%d根定方向 + %s%d根择时, 每%.0fmin',
             POOL_START, len(SYMBOLS), LEV, MOM_N, isig.EXEC_TF, isig.EXEC_N, LOOP_MIN)
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
            # 冷连接被代理/服务器回收(RemoteDisconnected) 偶发, 等2s重试一次避免漏掉本轮决策
            try:
                time.sleep(2)
                decide(ex, st)
                log.info('重试成功(上一轮连接被对端关闭, 已自动恢复)')
            except Exception as e2:
                cause2 = repr(e2.__cause__) if e2.__cause__ else ''
                log.error('重试也失败: %s | cause=%s', e2, cause2, exc_info=True)
        time.sleep(LOOP_MIN * 60)


if __name__ == '__main__':
    main()
