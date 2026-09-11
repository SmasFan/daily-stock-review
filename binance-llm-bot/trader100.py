#!/usr/bin/env python3
"""100 刀混合趋势 bot (Demo 合约, 真实下单) — 股票 TRADIFI 永续版

池子: 独立追踪 100 刀 (state_pool.json 记账池净值, 下单用池内限额)
标的: TSLA/COIN/PLTR/MSTR/HOOD 各 20% (各自独立 SMA 趋势)
杠杆: 2x | 周期: 日线, 每 6h 检查
约束: 每标名义 ≥ 5U(TRADIFI 门槛), 2x 下保证金够
离场: 收盘 < SMA 平仓; 池回撤 20% 全清冷却
T+0 利润保护(profit_guard): 峰值浮盈到 4% 止损上移保本; 浮盈到 7% 先平一半;
                            从峰值回撤 3% 或赚够 12% 破 SMA20 → 落袋（交 LLM 复核, 默认放行）
前置: 需已签 TradFi-Perps 协议 (POST /fapi/v1/stock/contract)
"""
import os, sys, json, time, logging
from datetime import datetime
import ccxt
from dotenv import load_dotenv

import algo_tools  # 显式 Algo 条件单 API (STOP 单 2025-12 起走 algo 端点)
import llm_gate     # 成交前 LLM 风控复核（买入可否决；趋势离场硬规则）
import profit_guard  # T+0 利润保护：保本上移 / 分批止盈 / 移动止盈

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
for _n in ('httpx', 'httpx2', 'httpcore', 'openai'):   # 屏蔽 SDK 每次请求的 INFO 噪音
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger('t100')

BASE = os.path.dirname(os.path.abspath(__file__))
POOL_FILE = os.path.join(BASE, 'state_pool.json')
POOL_START = float(os.environ.get('POOL_START', '100'))
LEV = float(os.environ.get('LEV', '3'))
FEE = 0.0004
PROXY = os.environ.get('PROXY', 'socks5h://172.25.16.1:10808')
LOOP_HOURS = float(os.environ.get('LOOP_HOURS', '6'))
DD_STOP = float(os.environ.get('DD_STOP', '0.20'))

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
    # 禁 keep-alive: 6h 长间隔冷连接必被回收, 每次新建连接更稳
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
                for k in ('peak', 'trimmed', 'sl'):   # 新仓：利润保护状态清零
                    rec.pop(k, None)
                rec['ts'] = time.time()
            st['real_pos'][sym] = rec
        else:
            st['real_pos'].pop(sym, None)


def closed_daily(ex, sym, need):
    ohlcv = ex.fetch_ohlcv(sym, '1d', limit=need + 30)
    now = ex.milliseconds()
    return [c[4] for c in ohlcv if c[0] + 86400000 <= now]


def sma(closes, w):
    return sum(closes[-w:]) / w if len(closes) >= w else None


SL_PCT = float(os.environ.get('SL_PCT', '0.12'))   # 入场价下方 12% 止损


def set_sl(ex, sym, qty, entry, sl_price=None):
    """挂 reduceOnly 止损单 via Algo API (服务器端实时, 返回 algoId)

    sl_price 给定时按给定价挂（利润保护上移保本用），否则按入场价下方 SL_PCT 挂。
    """
    sl_price = round(sl_price if sl_price else entry * (1 - SL_PCT), 2)
    pct = (1 - sl_price / entry) * 100 if entry else SL_PCT * 100
    try:
        r = algo_tools.place_stop_market(sym, 'sell', qty, sl_price)
        log.info('挂止损 %s @ %.2f (%+.1f%%) algoId=%s', sym, sl_price, -pct, r.get('algoId'))
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


def buy_open(ex, sym, notional_usd, llm=None):
    """市价开多, 名义 ~notional_usd。先设杠杆+挂止损。返回实际张数"""
    ex.set_leverage(int(LEV), sym)  # 必须 int, float 会 Binance 拒(-1102)
    cancel_sl(ex, sym)  # 清可能残留的旧止损单
    t = ex.fetch_ticker(sym)
    qty = ex.amount_to_precision(sym, notional_usd / t['last'])
    o = ex.create_order(sym, 'market', 'buy', qty)
    filled = float(o.get('filled', qty))
    avg = float(o.get('average') or t['last'])
    aid = set_sl(ex, sym, filled, avg)
    from trade_log import record
    _n = (llm or {}).get('note')
    record('open', sym, 'BUY', filled, avg,
           detail=f'名义~{notional_usd:.0f}' + (f'｜LLM:{_n}' if _n else '｜LLM:未复核'),
           pool=None, lev=LEV, notional=notional_usd)
    return filled


def sell_close(ex, sym, amt=None, llm=None, why=None):
    """撤 algo 单 + 市价平仓, 记录盈亏 (why 用于区分趋势离场/利润保护)"""
    cancel_sl(ex, sym)  # 先撤止损单 (防平仓后触发反向)
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
        # 有指定量: 从 state 或持仓取入场价
        try:
            for p in ex.fetch_positions([sym]):
                if float(p['contracts']) > 0:
                    entry_px = float(p['entryPrice'])
        except Exception:
            pass
    if amt:
        o = ex.create_order(sym, 'market', 'sell', ex.amount_to_precision(sym, amt))
        avg = float(o.get('average') or 0)
        # 平仓前抓最后浮盈 (均价优先, 退路用持仓) —— 修正: 平仓前取 entry, 用成交均价算
        pnl = (avg - entry_px) * amt if (avg and entry_px) else 0
        from trade_log import record
        _n = (llm or {}).get('note')
        record('close', sym, 'SELL', amt, avg, pnl,
               detail=(why or 'SMA离场') + (f'｜LLM:{_n}' if _n else ''), lev=LEV)
        return amt
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


def decide(ex, st, dry=False):
    """一轮决策：信号 → LLM 复核 → 下单。

    两阶段：先把买卖候选收集齐（不下单），合并成 1 次 LLM 请求复核，再执行。
    买入可被 LLM 否决（避免在局部高点/动能转弱时追高）；
    趋势离场（收盘跌破 SMA50）属策略核心，标 hard=True，LLM 只能确认。
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

    # 各标信号
    sigs = {}
    for sym, cfg in SYMBOLS.items():
        closes = closed_daily(ex, sym, cfg['sma'] + 5)
        if len(closes) < cfg['sma']:
            log.warning('%s 历史不足', cfg['name']); continue
        ma = sma(closes, cfg['sma'])
        sigs[sym] = {'above': closes[-1] > ma, 'price': closes[-1], 'ma': ma}
        log.info('%s 收%.2f > SMA%d %.2f? %s', cfg['name'], closes[-1], cfg['sma'], ma,
                 '✓多' if sigs[sym]['above'] else '✗空')

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
            ms = ctx_cache[sym] = llm_gate.market_stats(ex, sym, sma_n=cfg['sma'])
        base = {'symbol': sym, 'name': cfg['name'], 'lev': LEV,
                'price': ms.get('price') or sig['price'],
                'sma50': ms.get('sma50') or sig['ma'], 'sma20': ms.get('sma20'),
                'dev50': ms.get('dev50'), 'vs_ma20': ms.get('vs_ma20'),
                'd7': ms.get('d7'), 'd30': ms.get('d30')}
        if holding and not sig['above']:
            pos = st['real_pos'].get(sym) or {}
            entry = float(pos.get('entry') or 0)
            cands.append({**base, 'id': 't100|%s|sell' % sym, 'action': 'sell',
                          'entry': entry,
                          'pnl_pct': round((base['price'] / entry - 1) * 100, 2) if entry else 0,
                          'why': '日线收盘跌破SMA%d %.2f（趋势离场）' % (cfg['sma'], sig['ma']),
                          'hard': True})
        elif holding:
            # T+0：趋势还在，但账面利润要保护 —— 保本上移 / 分批止盈 / 移动止盈
            try:
                pg = profit_guard.scan(ex, st, sym, profile='daily', ms=ms,
                                       min_notional=cfg['min_notional'])
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
                                  'id': 't100|%s|pg' % sym, 'action': 'sell',
                                  'entry': pg['entry'], 'pnl_pct': pg['pnl_pct'],
                                  'peak_pct': pg['peak_pct'], 'dd_peak': pg['dd_peak'],
                                  'why': '利润保护：' + pg['why'],
                                  'hard': False, 'kind': 'protect', 'pg': pg})
        elif not holding and not st['cooldown'] and sig['above']:
            margin = eq * cfg['alloc']
            notional = margin * LEV
            if notional < cfg['min_notional']:
                log.info('%s 名义%.0f < 门槛%d 跳过', cfg['name'], notional, cfg['min_notional'])
                continue
            cands.append({**base, 'id': 't100|%s|buy' % sym, 'action': 'buy',
                          'why': '日线收盘站上SMA%d %.2f（趋势突破）名义~%.0f' % (
                              cfg['sma'], sig['ma'], notional),
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
            amt = sell_close(ex, sym, llm=c['llm'])
            if amt:
                log.info('平仓 %s %f 张%s', cfg['name'], amt,
                         '｜LLM:%s' % c['llm'].get('note') if c['llm'].get('note') else '')
                st['real_pos'].pop(sym, None)
        else:
            if c['llm'].get('verdict') == 'avoid':
                continue
            if dry:
                log.info('[dry] 开多 %s 名义 ~%.0f', cfg['name'], c['notional'])
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
    log.info('trader100 启动: %s刀池 %d标 %gx, 每%.0fh', POOL_START, len(SYMBOLS), LEV, LOOP_HOURS)
    while True:
        try:
            decide(ex, st)
        except Exception as e:
            cause = repr(e.__cause__) if e.__cause__ else ''
            log.error('本轮失败: %s | cause=%s', e, cause)
            # 冷连接被代理/服务器回收(RemoteDisconnected) 偶发, 等2s重试一次避免漏掉6h决策
            try:
                time.sleep(2)
                decide(ex, st)
                log.info('重试成功(上一轮连接被对端关闭, 已自动恢复)')
            except Exception as e2:
                cause2 = repr(e2.__cause__) if e2.__cause__ else ''
                log.error('重试也失败: %s | cause=%s', e2, cause2, exc_info=True)
        time.sleep(LOOP_HOURS * 3600)


if __name__ == '__main__':
    main()
