#!/usr/bin/env python3
"""本地止损守护 (补 demo 无服务器端止损)
每 60s 查持仓价, 跌破入场价 -12% 立即市价平仓
独立常驻, 与 trader100.py 的 6h 策略互补
"""
import os, sys, time, logging
import ccxt
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('guard')

BASE = os.path.dirname(os.path.abspath(__file__))
# 股票 TRADIFI 永续 (日线 5 标 + 短线 8 标)
SYMBOLS = ['TSLA/USDT:USDT', 'COIN/USDT:USDT', 'PLTR/USDT:USDT', 'MSTR/USDT:USDT', 'HOOD/USDT:USDT',
           'NVDA/USDT:USDT', 'META/USDT:USDT', 'AMZN/USDT:USDT', 'QQQ/USDT:USDT',
           'SPY/USDT:USDT', 'GOOGL/USDT:USDT', 'INTC/USDT:USDT', 'CRCL/USDT:USDT']
SL_PCT = float(os.environ.get('SL_PCT', '0.12'))
CHECK_SEC = int(os.environ.get('GUARD_SEC', '60'))
PROXY = os.environ.get('PROXY', 'socks5h://172.25.16.1:10808')
LEV = 5.0


def make_fex():
    ex = ccxt.binance({
        'apiKey': os.environ.get('BN_API_KEY', ''),
        'secret': os.environ.get('BN_SECRET', ''),
        'enableRateLimit': True,
        'proxies': {'http': PROXY, 'https': PROXY},
        'options': {'defaultType': 'future', 'adjustForTimeDifference': True},
    })
    # 禁 keep-alive: 低频轮询(30s-6h)下复用连接会被代理/服务器回收导致 RemoteDisconnected, 每次新建连接更稳
    ex.session.headers['Connection'] = 'close'
    ex.enable_demo_trading(True)
    return ex


def check_once(ex):
    for sym in SYMBOLS:
        for p in ex.fetch_positions([sym]):
            amt = float(p['contracts'])
            if not amt:
                continue
            entry = float(p['entryPrice'])
            mark = float(p.get('markPrice') or entry)
            liq = float(p.get('liquidationPrice') or 0)
            sl_price = entry * (1 - SL_PCT)
            # 止损触发
            if mark <= sl_price:
                log.warning('!!! %s 触发止损: mark %.2f <= SL %.2f (入场%.2f), 市价平仓', sym, mark, sl_price, entry)
                try:
                    import algo_tools
                    algo_tools.set_symbol_map(ex.markets)
                    try:
                        algo_tools.cancel_all_algo(sym)  # 先撤 algo 止损单, 防平仓后残留反向触发
                    except Exception:
                        pass
                    ex.create_order(sym, 'market', 'sell', ex.amount_to_precision(sym, amt))
                    from trade_log import record
                    record('sl', sym, 'STOP_LOSS', amt, mark, (mark-entry)*amt,
                           detail=f'入场{entry:.2f} SL@{sl_price:.2f}', pool=None, lev=LEV)
                    log.warning('已平 %s %f 张', sym, amt)
                except Exception as e:
                    log.error('平仓失败 %s: %s', sym, str(e)[:150])
            else:
                pnl_pct = (mark / entry - 1) * 100
                log.info('%s 入场%.2f mark%.2f (%+.1f%%) SL@%.2f 距SL %.1f%%',
                         sym, entry, mark, pnl_pct, sl_price, (mark - sl_price) / mark * 100)


def main():
    ex = make_fex()
    ex.load_markets()
    log.info('止损守护启动: 每%ds检查, 止损线 -%.0f%%', CHECK_SEC, SL_PCT * 100)
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
