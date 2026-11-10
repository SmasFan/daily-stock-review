#!/usr/bin/env python3
"""多策略竞技场引擎 (Demo 合约, 真实下单) — 6 个策略共用 100U / 23 只 TradFi 永续

为什么这样设计（2026-11-10，见 backtest/arena.py + arena_results.csv）:
  * 分池赛马是假的：S1 在 13 标上 +26.8%，换成 5 只就 -28.4%。
    比出来的是「谁分到的票好」，不是「谁策略好」。所以 6 个策略跑同一批 23 标。
  * 币安是单向持仓：同一只票两个策略一个多一个空会净额抵消、止损互踩。
    所以做 **symbol 级仲裁** —— 每只票同一时刻只归一个策略：
    空仓时按胜率优先级（S1 > S5 > S2 > S6 > S4）取第一个出信号的策略开仓；
    持仓期间平仓/离场只由「开仓那个策略」决定，其他策略不得移动这笔仓位。
  * 每个策略独立记账（trades 里打 strategy 标签），胜率和收益互不污染。

策略（全池 23 标回测，每策略独立 100U / 5x / 双边 0.04%）:
  S1 日内双向  权重 60%  +19.2% 胜率 57.4% 样本外 +17.3%   ← 唯一三把尺子都赢
  S5 区间突破  权重 20%  +1.5%  胜率 30.4% 样本外 +16.2%   ← 只最近两月好，小仓位试
  S2 日线趋势  权重  8%  -2.6%  胜率 29.2% 样本外 +1.4%
  S6 区间网格  权重  7%  -4.4%  胜率 38.5% 样本外 -2.4%
  S4 超跌反弹  权重  5%  -7.4%  胜率 45.8% 样本外 -11.3%
  S3 双均线    不上线（-25.3%，两次回测都翻负）

口径差异（重要）: 第一版**不接 profit_guard 分批止盈/移动止盈**，只用固定止损 +
信号离场。回测里 S1 用了 PG_SHORT（1% 保本 / 2% 平半 / 3.5% 破 SMA20 落袋），
上线版的行为会更「粗」，实盘胜率可能略低于回测；等跑够样本再决定是否接入。
"""
import os
import sys
import json
import time
import logging
from datetime import datetime

import ccxt
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
# 必须先加载 .env 再 import intraday_signal：其模块级阈值在导入时求值。
load_dotenv(os.path.join(BASE, '.env'))

import algo_tools          # noqa: E402  显式 Algo 条件单 API (STOP)
import pos_guard           # noqa: E402  方向/规模守卫
import intraday_signal as isig  # noqa: E402  日线定方向 + 1h 择时

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
for _n in ('httpx', 'httpx2', 'httpcore', 'openai', 'urllib3'):
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger('arena')

STATE_FILE = os.path.join(BASE, 'state_arena.json')
STATUS_JSON = os.path.join(BASE, '..', 'data', 'binance_arena.json')
START_EQUITY = float(os.environ.get('ARENA_START', '100'))
LEV = float(os.environ.get('LEV', '5'))
FEE = 0.0004
PROXY = os.environ.get('PROXY', '' if sys.platform == 'darwin' else 'socks5h://172.25.16.1:10808')
LOOP_MIN = float(os.environ.get('ARENA_LOOP_MIN', os.environ.get('LOOP_MIN', '15')))
DRY = os.environ.get('ARENA_DRY', '0').lower() not in ('0', 'false', 'off', 'no')
MIN_NOTIONAL = float(os.environ.get('MIN_NOTIONAL', '5'))

# 23 只（剔除了 1h 数据异常的 KORU 跳变 1840%、XYZ 跳变 153%）
SYMBOLS = ['AMZN', 'COIN', 'CRCL', 'EWJ', 'GOOGL', 'HOOD', 'INTC', 'META', 'MSTR',
           'MU', 'NVDA', 'PAYP', 'PLTR', 'QQQ', 'SAMSUNG', 'SKHYNIX', 'SOXL',
           'SOXS', 'SPY', 'TSLA', 'XAG', 'XAU', 'XPT']
CN_NAME = {'AMZN': '亚马逊', 'COIN': 'Coinbase', 'CRCL': 'Circle', 'EWJ': '日本ETF',
           'GOOGL': '谷歌', 'HOOD': 'Robinhood', 'INTC': '英特尔', 'META': 'Meta',
           'MSTR': '微策略', 'MU': '美光', 'NVDA': '英伟达', 'PAYP': 'PayPal',
           'PLTR': 'Palantir', 'QQQ': '纳指ETF', 'SAMSUNG': '三星', 'SKHYNIX': 'SK海力士',
           'SOXL': '半导体3倍多', 'SOXS': '半导体3倍空', 'SPY': '标普ETF',
           'TSLA': '特斯拉', 'XAG': '白银', 'XAU': '黄金', 'XPT': '铂金'}


def sym_of(name):
    return name + '/USDT:USDT'


def sma(closes, w):
    return sum(closes[-w:]) / w if closes is not None and len(closes) >= w else None


# ------------------------------------------------------------------ 信号纯函数
# 入参 ctx: {'o','h','l','c' 为 1h 序列, 'd' 为日线收盘, 'ma20','ma50','ma200',
#            'entry'(持仓时), 'price'}；holding: 'long'/'short'/None。
# 返回：持仓时返回同向表示继续持有、None 表示离场；空仓时返回开仓方向或 None。
# 全部与 backtest/arena.py 的 sim_* 一一对应，改一边必须同步另一边。

def sig_hybrid(ctx, holding):
    """S1 日线 20 根 ±2% 定方向 + 1h 12 根 ±1% 且过 SMA200。"""
    daily = isig.daily_dir(ctx['d'], short_ok=True)
    ex = isig.exec_signal(ctx['c'])
    if daily.get('dir') is None and holding is None:
        return None
    return isig.target(daily, ex, holding_side=holding)


def sig_daily_trend(ctx, holding):
    """S2 只做多：日线 20 根动量 > 2% 且收盘在日线 SMA50 上方。"""
    d = ctx['d']
    if len(d) < 21:
        return None
    mom = d[-1] / d[-21] - 1
    ma50d = sma(d, 50)
    ok = mom > 0.02 and (ma50d is None or d[-1] > ma50d)
    return 'long' if ok else None


def sig_trend_ma(ctx, holding):
    """S3 1h SMA20 vs SMA50 双向（回测垫底，默认不上线，保留以便对比）。"""
    fast, slow, px = ctx['ma20'], ctx['ma50'], ctx['price']
    if fast is None or slow is None:
        return None
    if holding == 'long':
        return 'long' if (fast > slow and px >= fast) else None
    if holding == 'short':
        return 'short' if (fast < slow and px <= fast) else None
    if fast > slow:
        return 'long'
    return 'short' if fast < slow else None


def sig_rebound(ctx, holding):
    """S4 1h 收盘低于 SMA20 达 4% 且收阳做多；回 SMA20 或 +3% 离场。"""
    ma20, c, o, px = ctx['ma20'], ctx['c'], ctx['o'], ctx['price']
    if ma20 is None or len(c) < 2:
        return None
    if holding == 'long':
        entry = ctx.get('entry') or 0
        gain = (px / entry - 1) * 100 if entry else 0.0
        if px >= ma20 or gain >= 3.0:
            return None
        return 'long'
    if c[-1] < o[-1] and px / ma20 - 1 <= -0.04:
        return 'long'
    return None


def sig_breakout(ctx, holding):
    """S5 1h 收盘破前 24 根高/低 0.2% 追单；回中轨（SMA50）或反向破位离场。"""
    c, h, l, px, ma50 = ctx['c'], ctx['h'], ctx['l'], ctx['price'], ctx['ma50']
    if len(c) < 26:
        return None
    hi, lo = max(h[-25:-1]), min(l[-25:-1])
    if holding == 'long':
        if px < lo * 0.998 or (ma50 is not None and px < ma50):
            return None
        return 'long'
    if holding == 'short':
        if px > hi * 1.002 or (ma50 is not None and px > ma50):
            return None
        return 'short'
    if px > hi * 1.002:
        return 'long'
    if px < lo * 0.998:
        return 'short'
    return None


def sig_grid(ctx, holding):
    """S6 只做多：跌到 SMA200 下沿 -5% 买入，回到 -3% 或冲出 ±7% 离场。"""
    ma200, px = ctx['ma200'], ctx['price']
    if ma200 is None:
        return None
    low, high, out_lo, out_hi = ma200 * 0.95, ma200 * 0.97, ma200 * 0.93, ma200 * 1.07
    if holding == 'long':
        if px >= high or px < out_lo or px > out_hi:
            return None
        return 'long'
    if out_lo <= px <= low:
        return 'long'
    return None


# 权重来自 23 标全池回测（60/20/8/7/5），S3 权重 0 = 不上线。
STRATS = [
    dict(id='S1', name='日内双向', weight=0.60, max_pos=6, sl=0.02, risk=1.0, fn=sig_hybrid,
         note='日线定方向 + 1h 择时，多空双向'),
    dict(id='S5', name='区间突破', weight=0.20, max_pos=2, sl=0.025, risk=0.5, fn=sig_breakout,
         note='破前 24 根高/低追单，半仓'),
    dict(id='S2', name='日线趋势', weight=0.08, max_pos=2, sl=0.08, risk=1.0, fn=sig_daily_trend,
         note='日线 20 根动量 >2% 且站上 SMA50，纯多'),
    dict(id='S6', name='区间网格', weight=0.07, max_pos=2, sl=0.03, risk=1.0, fn=sig_grid,
         note='SMA200 -5% 下沿买入，纯多'),
    dict(id='S4', name='超跌反弹', weight=0.05, max_pos=1, sl=0.025, risk=0.5, fn=sig_rebound,
         note='低于 SMA20 4% 且收阳反弹，半仓'),
]
STRAT_BY_ID = {s['id']: s for s in STRATS}


def strat_notional(st, strat):
    """单笔名义 = 该策略资金份额 / 并发上限 × 杠杆 × 仓位系数。"""
    return START_EQUITY * strat['weight'] / strat['max_pos'] * LEV * strat['risk']


# ------------------------------------------------------------------ 交易所
def make_fex():
    ex = ccxt.binance({
        'apiKey': os.environ.get('BN_API_KEY', ''),
        'secret': os.environ.get('BN_SECRET', ''),
        'enableRateLimit': True,
        'timeout': 60000,   # demo-api 的 exchangeInfo 实测 14.5s，默认超时会抛 NetworkError
        'proxies': {'http': PROXY, 'https': PROXY},
        'options': {'defaultType': 'future', 'adjustForTimeDifference': True,
                # 只拉 linear（demo-fapi）；demo-api / demo-dapi 常超时，会让 load_markets 整体失败
                'fetchMarkets': ['linear']},
    })
    ex.session.headers['Connection'] = 'close'
    ex.enable_demo_trading(True)
    return ex


def fetch_ctx(ex, name):
    """取已收 1h（240 根）+ 日线（80 根），组装信号上下文。"""
    sym = sym_of(name)
    now = ex.milliseconds()
    r1h = [r for r in ex.fetch_ohlcv(sym, '1h', limit=260) if r[0] + 3600000 <= now]
    r1d = [r for r in ex.fetch_ohlcv(sym, '1d', limit=90) if r[0] + 86400000 <= now]
    if len(r1h) < 30:
        raise RuntimeError('1h K 线不足 (%d)' % len(r1h))
    c = [r[4] for r in r1h]
    return {'o': [r[1] for r in r1h], 'h': [r[2] for r in r1h],
            'l': [r[3] for r in r1h], 'c': c, 'd': [r[4] for r in r1d],
            'ma20': sma(c, 20), 'ma50': sma(c, 50), 'ma200': sma(c, 200),
            'price': c[-1]}


def set_sl(ex, sym, qty, entry, sl_pct, side):
    """挂 reduceOnly 服务器端止损（多：-sl%，空：+sl%）。"""
    price = round(entry * (1 - sl_pct) if side == 'long' else entry * (1 + sl_pct), 6)
    try:
        r = algo_tools.place_stop_market(sym, 'sell' if side == 'long' else 'buy', qty, price)
        log.info('挂止损 %s %s @ %s (%.1f%%) algoId=%s', sym, side, price,
                 sl_pct * 100, r.get('algoId'))
        return r.get('algoId'), price
    except Exception as e:
        log.warning('挂止损失败 %s: %s', sym, str(e)[:150])
        return None, price


def cancel_sl(ex, sym):
    try:
        algo_tools.cancel_all_algo(sym)
    except Exception as e:
        log.warning('撤 algo 单失败 %s: %s', sym, str(e)[:100])


# ------------------------------------------------------------------ 状态
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            st = json.load(open(STATE_FILE))
            st.setdefault('real_pos', {})
            st.setdefault('trades', [])
            st.setdefault('log', [])
            return st
        except Exception:
            log.warning('state_arena.json 损坏，重建')
    return {'start': START_EQUITY, 'real_pos': {}, 'trades': [], 'log': [],
            'created': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}


def save_state(st):
    tmp = STATE_FILE + '.tmp'
    json.dump(st, open(tmp, 'w'), indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def note(st, msg):
    log.info(msg)
    st['log'].append(datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' · ' + msg)
    del st['log'][:-200]


def pos_count(st, sid):
    return sum(1 for p in st['real_pos'].values() if p.get('strategy') == sid)


def stats_of(st):
    """按策略聚合：笔数 / 胜率 / 累计盈亏 / 平均盈亏。"""
    out = {}
    for s in STRATS:
        ts = [t for t in st['trades'] if t.get('strategy') == s['id']]
        wins = [t for t in ts if t.get('pnl_usd', 0) > 0]
        pnl = sum(t.get('pnl_usd', 0) for t in ts)
        gw = sum(t['pnl_usd'] for t in wins)
        gl = -sum(t['pnl_usd'] for t in ts if t.get('pnl_usd', 0) <= 0)
        out[s['id']] = {
            'name': s['name'], 'weight': s['weight'], 'trades': len(ts),
            'wins': len(wins),
            'win_rate': round(len(wins) / len(ts) * 100, 1) if ts else None,
            'pnl_usd': round(pnl, 2),
            'pnl_pct': round(pnl / (START_EQUITY * s['weight']) * 100, 2) if s['weight'] else 0,
            'avg_win': round(gw / len(wins), 2) if wins else 0,
            'avg_loss': round(-gl / max(1, len(ts) - len(wins)), 2) if ts else 0,
            'pf': round(gw / gl, 2) if gl > 0 else None,
            'open': pos_count(st, s['id']), 'max_pos': s['max_pos'],
            'notional': round(strat_notional(st, s), 1),
        }
    return out


# ------------------------------------------------------------------ 下单
def open_position(ex, st, name, side, strat, dry=None):
    """按策略开仓并登记 strategy 标签；成交后立刻落盘再挂止损。"""
    sym = sym_of(name)
    notional = strat_notional(st, strat)
    if notional < MIN_NOTIONAL:
        note(st, '%s 名义 %.1fU 低于门槛 %.0fU，跳过' % (name, notional, MIN_NOTIONAL))
        return 0.0
    # 只在完全空仓时开：绝不与已有仓位反向叠加
    for p in ex.fetch_positions([sym]):
        if float(p.get('contracts') or 0):
            log.error('开仓前发现 %s 已有仓位(%s)，中止', name, pos_guard.side_of(p))
            return 0.0
    ex.set_leverage(int(LEV), sym)
    cancel_sl(ex, sym)
    px = float(ex.fetch_ticker(sym)['last'])
    qty = float(ex.amount_to_precision(sym, notional / px))
    if qty * px < MIN_NOTIONAL:
        note(st, '%s 精度后名义 %.1fU 低于门槛，跳过' % (name, qty * px))
        return 0.0
    if (DRY if dry is None else dry):
        log.info('[dry] 开%s %s 名义~%.0f 策略=%s', side, name, notional, strat['id'])
        return 0.0
    o = ex.create_order(sym, 'market', 'buy' if side == 'long' else 'sell', qty, None,
                        {'reduceOnly': False})
    filled = float(o.get('filled') or qty)
    avg = float(o.get('average') or px)
    st['real_pos'][sym] = {'strategy': strat['id'], 'side': side, 'amt': filled,
                           'entry': avg, 'ts': time.time(), 'notional': notional,
                           'margin': notional / LEV, 'name': name}
    save_state(st)
    aid, slp = set_sl(ex, sym, filled, avg, strat['sl'], side)
    st['real_pos'][sym]['sl'] = slp
    st['real_pos'][sym]['sl_pct'] = strat['sl']
    st['real_pos'][sym]['algo'] = aid
    save_state(st)
    note(st, '开%s %s %s %.6f 张 @%.2f 名义~%.0fU 策略=%s 止损%.1f%%'
         % ('多' if side == 'long' else '空', strat['id'], name, filled, avg,
            notional, strat['id'], strat['sl'] * 100))
    return filled


def close_position(ex, st, name, why='信号离场', dry=None):
    """按登记方向平仓（reduceOnly），记一笔 trade。"""
    sym = sym_of(name)
    pos = st['real_pos'].get(sym) or {}
    side = pos.get('side') or 'long'
    sid = pos.get('strategy') or 'S1'
    cancel_sl(ex, sym)
    try:
        ex.cancel_all_orders(sym)
    except Exception:
        pass
    real_amt, real_entry, real_side = 0.0, 0.0, 'flat'
    try:
        for p in ex.fetch_positions([sym]):
            if float(p.get('contracts') or 0):
                real_amt = abs(float(p['contracts']))
                real_entry = float(p['entryPrice'])
                real_side = pos_guard.side_of(p)
    except Exception as e:
        log.warning('查持仓失败 %s: %s', name, str(e)[:100])
    if real_side not in ('flat', side):
        log.error('%s 方向不符（登记 %s / 实际 %s），拒绝平仓，交方向守卫', name, side, real_side)
        return 0
    if (DRY if dry is None else dry) or real_amt <= 0:
        log.info('[dry] 平%s %s %s', side, name, why)
        st['real_pos'].pop(sym, None)
        save_state(st)
        return 0
    o = ex.create_order(sym, 'market', 'sell' if side == 'long' else 'buy',
                        ex.amount_to_precision(sym, real_amt), None, {'reduceOnly': True})
    avg = float(o.get('average') or (o.get('info') or {}).get('avgPrice') or 0) or \
        float(ex.fetch_ticker(sym).get('last') or 0)
    pnl = float((o.get('info') or {}).get('realizedPnl') or 0)
    if not pnl:
        pnl = (avg - real_entry) * real_amt * (1 if side == 'long' else -1)
    ret_pct = (avg / real_entry - 1) * 100 * (1 if side == 'long' else -1) if real_entry else 0
    st['trades'].append({'strategy': sid, 'name': name, 'side': side,
                         'entry': round(real_entry, 6), 'exit': round(avg, 6),
                         'amt': round(real_amt, 8), 'pnl_usd': round(pnl, 4),
                         'pnl_pct': round(ret_pct, 2),
                         'ts_in': int(pos.get('ts') or 0), 'ts_out': int(time.time()),
                         'why': why})
    st['real_pos'].pop(sym, None)
    save_state(st)
    note(st, '平%s %s %s @%.2f 盈亏 %+.2fU (%+.2f%%) ｜%s'
         % (side, sid, name, avg, pnl, ret_pct, why))
    return real_amt


def sync_from_real(ex, st):
    """以交易所为准对齐：消失的仓位清掉；交易所多出来的仓位登记为 S1 并告警。"""
    syms = [sym_of(n) for n in SYMBOLS]
    try:
        positions = ex.fetch_positions(syms)
    except Exception as e:
        log.warning('同步持仓失败: %s', str(e)[:120])
        return
    alive = {}
    for p in positions:
        if not float(p.get('contracts') or 0):
            continue
        sym = p['symbol']
        alive[sym] = {'amt': abs(float(p['contracts'])), 'entry': float(p['entryPrice']),
                      'side': pos_guard.side_of(p)}
    for sym in list(st['real_pos'].keys()):
        if sym not in alive:
            note(st, '交易所已无 %s 仓位，本地记录清理' % sym)
            st['real_pos'].pop(sym, None)
    for sym, real in alive.items():
        if sym not in st['real_pos']:
            # 接管旧仓位：名义/保证金按「张数 × 入场价」补算，止损口径取接管策略默认值，
            # 否则状态页会显示名义 0U（错把「有 5x 杠杆的小仓」看成「没上杠杆」）。
            _notional = real['amt'] * real['entry']
            st['real_pos'][sym] = {'strategy': 'S1', 'side': real['side'],
                                   'amt': real['amt'], 'entry': real['entry'],
                                   'ts': time.time(), 'name': sym.split('/')[0],
                                   'notional': round(_notional, 2),
                                   'margin': round(_notional / LEV, 2),
                                   'sl_pct': STRAT_BY_ID['S1']['sl'],
                                   'adopted': True}
            note(st, '发现未登记仓位 %s %s，登记为 S1' % (sym, real['side']))
        else:
            rec = st['real_pos'][sym]
            rec['amt'] = real['amt']
            rec['entry'] = real['entry']
            if not rec.get('notional'):      # 旧版本接管时没记名义，补算
                rec['notional'] = round(real['amt'] * real['entry'], 2)
                rec['margin'] = round(rec['notional'] / LEV, 2)
            if not rec.get('sl_pct'):
                rec['sl_pct'] = STRAT_BY_ID.get(rec.get('strategy') or 'S1',
                                                STRAT_BY_ID['S1'])['sl']
            if not rec.get('sl'):   # 接管来的旧仓没记本地止损价，从服务器端 algo 单补回
                try:
                    _stops = algo_tools.algo_stops_for(
                        sym, 'SELL' if rec.get('side') == 'long' else 'BUY')
                    if _stops:
                        rec['sl'] = float(_stops[0]['triggerPrice'])
                except Exception:
                    pass


# ------------------------------------------------------------------ 决策
def decide(ex, st, dry=DRY, echo_progress=True):
    sync_from_real(ex, st)
    ctxs = {}
    for i, name in enumerate(SYMBOLS, 1):
        # 23 标 × 2 个 K 线请求连发会撞 demo 限流（实测 CRCL 偶发 -1122/超时），
        # 每标之间留间隔，失败再重试一次。
        for attempt in (1, 2):
            try:
                _ctx = fetch_ctx(ex, name)
                ctxs[name] = _ctx
                # 当日涨幅 = 现价 / 上一根已收日线收盘 - 1（即相对昨收）
                try:
                    st.setdefault('day_change', {})[name] = round(
                        (_ctx['price'] / float(_ctx['d'][-1]) - 1) * 100, 2)
                except Exception:
                    pass
                break
            except Exception as e:
                if attempt == 2:
                    log.warning('取 %s 行情失败(已重试): %s', name, str(e)[:120])
                else:
                    time.sleep(1.2)
        time.sleep(0.25)
        if echo_progress and (i % 5 == 0 or i == len(SYMBOLS)):
            print('[progress] %d/%d' % (i, len(SYMBOLS)), flush=True)

    # 1) 已有仓位：只由开仓策略决定去留
    for name in list(ctxs.keys()):
        sym = sym_of(name)
        pos = st['real_pos'].get(sym)
        if not pos:
            continue
        sid = pos.get('strategy') or 'S1'
        strat = STRAT_BY_ID.get(sid) or STRAT_BY_ID['S1']
        side = pos.get('side') or 'long'
        ctx = dict(ctxs[name])
        ctx['entry'] = float(pos.get('entry') or 0)
        try:
            keep = strat['fn'](ctx, side)
        except Exception as e:
            log.error('%s 策略 %s 信号异常: %s', name, sid, str(e)[:120])
            continue
        if keep != side:
            close_position(ex, st, name, why='%s 离场' % sid, dry=dry)

    # 2) 空仓：按优先级仲裁，每只票只归一个策略
    opened = 0
    for name in ctxs:
        if sym_of(name) in st['real_pos']:
            continue
        ctx = ctxs[name]
        for strat in STRATS:
            if pos_count(st, strat['id']) >= strat['max_pos']:
                continue
            try:
                side = strat['fn'](ctx, None)
            except Exception as e:
                log.error('%s 策略 %s 开仓信号异常: %s', name, strat['id'], str(e)[:120])
                continue
            if not side:
                continue
            if open_position(ex, st, name, side, strat, dry=dry):
                opened += 1
            break
    save_state(st)
    return opened


def equity_report(ex, st):
    """总权益 = 起始 + 已平仓盈亏 + 当前持仓未实现盈亏。"""
    realized = sum(t.get('pnl_usd', 0) for t in st['trades'])
    unreal = 0.0
    try:
        for p in ex.fetch_positions([sym_of(n) for n in SYMBOLS]):
            if float(p.get('contracts') or 0):
                unreal += float(p.get('unrealizedPnl') or 0)
    except Exception as e:
        log.warning('取未实现盈亏失败: %s', str(e)[:100])
    return {'realized': round(realized, 2), 'unrealized': round(unreal, 2),
            'equity': round(START_EQUITY + realized + unreal, 2)}


def write_status(ex, st):
    """写 data/binance_arena.json 给状态页读。"""
    eq = equity_report(ex, st)
    stats = stats_of(st)
    ranked = sorted((dict(v, id=k) for k, v in stats.items()),
                    key=lambda x: (x['pnl_pct'], x['win_rate'] or 0), reverse=True)
    now = time.time()
    dchg = st.get('day_change') or {}

    def _htxt(h):
        h = max(0.0, float(h))
        return ('%dd%dh' % (int(h / 24), int(h % 24))) if h >= 24 else ('%dh%02dm' % (int(h), int(h % 1 * 60)))

    pos_list = []
    for k, v in st['real_pos'].items():
        p = dict(v, symbol=k.split('/')[0])
        p['holdHours'] = round((now - float(v.get('ts') or now)) / 3600, 2)
        p['holdText'] = _htxt(p['holdHours'])
        p['dayChange'] = dchg.get(p['symbol'])
        p['cn'] = CN_NAME.get(p['symbol'], '')
        pos_list.append(p)
    gross = sum(float(p.get('notional') or 0) for p in pos_list)
    margin_used = sum(float(p.get('margin') or 0) for p in pos_list)
    holds = [p['holdHours'] for p in pos_list]
    changes = [p['dayChange'] for p in pos_list if p['dayChange'] is not None]
    slots = sum(int(s['max_pos']) for s in STRATS)
    n_trades = sum(int(v.get('trades') or 0) for v in stats.values())
    n_wins = sum(int(v.get('wins') or 0) for v in stats.values())
    # 当日基线：上线首日以 100U 本金为基线；跨自然日则用首次巡检时的权益锚定，
    # 这样「当日收益」只反映当日发生的变化（含当日平仓与当日浮动）。
    today = datetime.now().strftime('%Y-%m-%d')
    anchor = st.get('day_anchor')
    if not anchor:
        anchor = {'date': today, 'equity': START_EQUITY}
    elif anchor.get('date') != today:
        anchor = {'date': today, 'equity': eq['equity']}
    st['day_anchor'] = anchor
    save_state(st)
    day_pnl = eq['equity'] - float(anchor['equity'])
    cum_pnl = eq['equity'] - START_EQUITY
    payload = {
        'generatedAt': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'start': START_EQUITY, 'lev': LEV, 'loopMin': LOOP_MIN, 'dry': DRY,
        'equity': eq['equity'], 'realized': eq['realized'], 'unrealized': eq['unrealized'],
        'returnPct': round((eq['equity'] / START_EQUITY - 1) * 100, 2),
        'cumPnl': round(cum_pnl, 4), 'cumPnlPct': round(cum_pnl / START_EQUITY * 100, 2),
        'dayDate': today, 'dayPnl': round(day_pnl, 4),
        'dayPnlPct': round(day_pnl / max(float(anchor['equity']), 1) * 100, 2),
        'holdMax': _htxt(max(holds)) if holds else '—',
        'holdAvg': _htxt(sum(holds) / len(holds)) if holds else '—',
        'dayChangeAvg': round(sum(changes) / len(changes), 2) if changes else None,
        'symbols': len(SYMBOLS), 'slots': slots,
        'grossNotional': round(gross, 2), 'marginUsed': round(margin_used, 2),
        'winRate': round(n_wins / n_trades * 100, 1) if n_trades else None,
        'stats': stats, 'ranked': ranked,
        'positions': pos_list,
        'trades': len(st['trades']),
        'log': st['log'][-30:],
    }
    try:
        os.makedirs(os.path.dirname(STATUS_JSON), exist_ok=True)
        tmp = STATUS_JSON + '.tmp'
        json.dump(payload, open(tmp, 'w'), indent=2, ensure_ascii=False)
        os.replace(tmp, STATUS_JSON)
    except Exception as e:
        log.warning('写状态 JSON 失败: %s', e)
    return payload


def main():
    ex = make_fex()
    for _i in range(3):
        try:
            ex.load_markets()
            break
        except Exception as _e:
            log.warning('load_markets 失败(重试 %d/3): %s', _i + 1, str(_e)[:120])
            time.sleep(5)
    if not getattr(ex, 'markets', None):
        log.error('load_markets 三次均失败，干净退出等 watchdog 下一分钟拉起')
        return
    algo_tools.set_symbol_map(ex.markets)
    st = load_state()
    mode = sys.argv[1] if len(sys.argv) > 1 else 'loop'
    if mode == 'once':
        decide(ex, st, dry=(DRY or '--dry' in sys.argv))
        p = write_status(ex, st)
        print(json.dumps({'equity': p['equity'], 'returnPct': p['returnPct'],
                          'stats': p['stats']}, indent=2, ensure_ascii=False))
        return
    log.info('竞技场启动: %.0fU / %d 标 / %gx / 每 %.0fmin；策略 %s',
             START_EQUITY, len(SYMBOLS), LEV, LOOP_MIN,
             ' / '.join('%s(%d%%)' % (s['id'], s['weight'] * 100) for s in STRATS))
    while True:
        try:
            ex.options['adjustForTimeDifference'] = True
            decide(ex, st)
            p = write_status(ex, st)
            log.info('权益 %.2fU (%+.2f%%) 持仓 %d 笔 成交 %d 笔',
                     p['equity'], p['returnPct'], len(p['positions']), p['trades'])
        except Exception as e:
            log.exception('本轮异常: %s', str(e)[:200])
            time.sleep(60)
        time.sleep(LOOP_MIN * 60)


if __name__ == '__main__':
    main()
