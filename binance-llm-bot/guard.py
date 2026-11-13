#!/usr/bin/env python3
"""本地止损守护 (竞技场版：读 state_arena.json 的每笔止损口径)
每 30s 查持仓价, 偏离入场价 2%（多空同口径）立即市价平仓
独立常驻, 与 trader100 / trader_short 的日内混合策略互补

2026-11-10 短线化：日线池原 -8% / 短线池 -3% / 空头 +6% 全部收紧到 2%，
与 v3 回测（backtest/shortterm_hybrid.py）的 1h 止损口径一致；巡检周期从 1h
缩到 15min 后，本地守护也要更快才能兜住服务器端 algo 单未覆盖的窗口。

2026-11-09 起支持双向：空头仓只有在 BN_ALLOW_SHORT=1 **且**池状态文件登记
side='short' 才算合法，按「涨破入场价 +2%」止损买回；其余一律按事故残留买回平掉。
"""
import os, sys, time, json, logging
import ccxt
from dotenv import load_dotenv
import pos_guard
import algo_tools  # 条件单/撤单 + symbol 映射（模块级导入：方向守卫分支也要用）

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('guard')

BASE = os.path.dirname(os.path.abspath(__file__))
# 股票 TRADIFI 永续 (日线 5 标 + 短线 8 标)
# 竞技场 23 标（与 arena_engine.SYMBOLS 保持一致）
SYMBOLS = [n + '/USDT:USDT' for n in (
    'AMZN', 'COIN', 'CRCL', 'EWJ', 'GOOGL', 'HOOD', 'INTC', 'META', 'MSTR', 'MU',
    'NVDA', 'PAYP', 'PLTR', 'QQQ', 'SAMSUNG', 'SKHYNIX', 'SOXL', 'SOXS', 'SPY',
    'TSLA', 'XAG', 'XAU', 'XPT')]
SL_PCT = float(os.environ.get('SL_PCT', '0.02'))          # 日线池止损 -2%（v3 回测口径）
SHORT_SL_PCT = float(os.environ.get('SHORT_SL_PCT', '0.02'))  # 短线池止损 -2%
SHORT_STOP_PCT = float(os.environ.get('BN_SHORT_SL', '0.02'))  # 空头止损 +2%（多空同口径）
ALLOW_SHORT = os.environ.get('BN_ALLOW_SHORT', '0').lower() not in ('0', 'false', 'off', 'no')
# 池归属：guard 同时看两套池，止损记录要落到对应池才能算进该池已实现盈亏
DAILY_SYMBOLS = {'TSLA/USDT:USDT', 'COIN/USDT:USDT', 'PLTR/USDT:USDT', 'MSTR/USDT:USDT', 'HOOD/USDT:USDT'}
STATE_FILES = ('state_arena.json',)
CHECK_SEC = int(os.environ.get('GUARD_SEC', '30'))
PROXY = os.environ.get('PROXY', '' if sys.platform == 'darwin' else 'socks5h://172.25.16.1:10808')
LEV = float(os.environ.get('LEV', '5'))
SHORT_LEV = float(os.environ.get('SHORT_LEV', '5'))


def make_fex():
    ex = ccxt.binance({
        'apiKey': os.environ.get('BN_API_KEY', ''),
        'secret': os.environ.get('BN_SECRET', ''),
        'enableRateLimit': True,
        'proxies': {'http': PROXY, 'https': PROXY},
        'options': {'defaultType': 'future', 'adjustForTimeDifference': True,
                    # demo-api/demo-dapi 的 exchangeInfo 经常超时；只拉 demo-fapi 的线性合约
                    'fetchMarkets': ['linear'], 'timeout': 60000},
    })
    # 禁 keep-alive: 低频轮询(30s-6h)下复用连接会被代理/服务器回收导致 RemoteDisconnected, 每次新建连接更稳
    ex.session.headers['Connection'] = 'close'
    ex.enable_demo_trading(True)
    return ex


def registered():
    """从竞技场状态文件读登记的 方向 / 止损百分比 / 归属策略。"""
    out = {}
    for fn in STATE_FILES:
        try:
            with open(os.path.join(BASE, fn)) as f:
                d = json.load(f)
            for s, rec in (d.get('real_pos') or {}).items():
                if not isinstance(rec, dict):
                    continue
                side = rec.get('side') if rec.get('side') in ('long', 'short') else 'long'
                out[s] = {'side': side, 'sl_pct': rec.get('sl_pct'),
                          'strategy': rec.get('strategy')}
        except Exception:
            continue
    return out


def check_once(ex):
    reg = registered()
    for sym in SYMBOLS:
        for p in ex.fetch_positions([sym]):
            amt = abs(float(p['contracts']))
            if not amt:
                continue
            side = pos_guard.side_of(p)
            expect = (reg.get(sym) or {}).get('side') or 'long'
            # 方向守卫：未登记 / 方向不符 / 双向未开启时的空头 = 事故残留，立即买回平掉。
            # 否则「跌破止损线 → 卖出」打在空头仓上会持续加空（2026-09-30 META 事故）。
            if pos_guard.flatten_unexpected(
                    ex, sym, p, None, name=sym.split('/')[0], why='止损守护',
                    allow_short=True, expected_side=expect):
                continue
            entry = float(p['entryPrice'])
            mark = float(p.get('markPrice') or entry)
            liq = float(p.get('liquidationPrice') or 0)
            lev = LEV
            is_short = (side == 'short')
            _reg = reg.get(sym) or {}
            sl_pct = float(_reg.get('sl_pct') or (SHORT_STOP_PCT if is_short else SL_PCT))
            sl_price = entry * (1 + sl_pct) if is_short else entry * (1 - sl_pct)
            close_side = 'buy' if is_short else 'sell'
            triggered = mark >= sl_price if is_short else mark <= sl_price
            # 止损触发
            if triggered:
                log.warning('!!! %s %s触发止损: mark %.2f %s SL %.2f (入场%.2f), 市价平仓',
                            sym, '空头' if is_short else '多头', mark,
                            '>=' if is_short else '<=', sl_price, entry)
                try:
                    algo_tools.set_symbol_map(ex.markets)
                    try:
                        algo_tools.cancel_all_algo(sym)  # 先撤 algo 止损单, 防平仓后残留反向触发
                    except Exception:
                        pass
                    _o = ex.create_order(sym, 'market', close_side,
                                         ex.amount_to_precision(sym, amt), None, {'reduceOnly': True})
                    from trade_log import record
                    # 用真实成交均价（缺失时退回 mark）保证已实现盈亏不为 0
                    avg = float(_o.get('average') or (_o.get('info') or {}).get('avgPrice') or 0) or mark
                    pnl = (entry - avg) * amt if is_short else (avg - entry) * amt
                    record('sl', sym, 'STOP_LOSS_SHORT' if is_short else 'STOP_LOSS', amt, avg, pnl,
                           detail=f'{"空" if is_short else "多"} 入场{entry:.2f} SL@{sl_price:.2f}',
                           pool=(_reg.get('strategy') or 'arena'), lev=lev)
                    log.warning('已平 %s %f 张', sym, amt)
                except Exception as e:
                    log.error('平仓失败 %s: %s', sym, str(e)[:150])
            else:
                pnl_pct = (1 - mark / entry if is_short else mark / entry - 1) * 100
                log.info('%s %s 入场%.2f mark%.2f (%+.1f%%) SL@%.2f 距SL %.1f%%',
                         sym, '空' if is_short else '多', entry, mark, pnl_pct, sl_price,
                         abs(mark - sl_price) / mark * 100)


def main():
    ex = make_fex()
    # demo-fapi 的 exchangeInfo 偶发 SSLError/超时：重试 3 次，仍失败也不退出（
    # guard 是止损守护，进程活着比启动即崩更重要，循环里会继续重试）
    for i in range(3):
        try:
            ex.load_markets()
            break
        except Exception as e:
            log.warning('load_markets 失败(%d/3): %s', i + 1, e)
            time.sleep(3)
    log.info('止损守护启动: 每%ds检查, 日线 -%.1f%% / 短线 -%.1f%% / 空头 +%.1f%%',
             CHECK_SEC, SL_PCT * 100, SHORT_SL_PCT * 100, SHORT_STOP_PCT * 100)
    while True:
        if not getattr(ex, 'markets', None):
            try:
                ex.load_markets()
                log.info('市场字典已重新加载')
            except Exception as e:
                log.warning('市场字典加载失败，下轮再试: %s', e)
                time.sleep(CHECK_SEC)
                continue
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
            check_once(ex)
        except Exception as e:
            cause = repr(e.__cause__) if e.__cause__ else ''
            log.error('守护轮询失败: %s | cause=%s', e, cause)
            # 空闲 keep-alive 连接被代理/服务器回收(RemoteDisconnected), 重试(新连接) 保证止损检查不丢轮
            try:
                time.sleep(2)
                check_once(ex)
                log.info('重试成功(上一轮连接被对端关闭, 已自动恢复)')
            except Exception as e2:
                cause2 = repr(e2.__cause__) if e2.__cause__ else ''
                log.error('重试也失败: %s | cause=%s', e2, cause2, exc_info=True)
        time.sleep(CHECK_SEC)


if __name__ == '__main__':
    main()
