#!/usr/bin/env python3
"""全天候双向合约盘（影子模拟，不下真实单）
=================================================
背景：币安 TRADIFI_PERPETUAL 是 7x24 连续交易的永续合约，可杠杆、可做空，
不存在「美股开盘才能买卖」的限制。但 demo-fapi 只开放 30 个 TRADIFI 标的，
中国合约（CXMT 长鑫 / UNITREE 宇树 / 港股中国科技）在 demo 全部不存在或
PENDING_TRADING，因此这一池用「主网公开行情 + 本地虚拟记账」运行：

  * 行情：https://fapi.binance.com/fapi/v1/klines （公开接口，无需 API Key）
  * 交易：纯虚拟记账，不调用任何下单接口，不碰真实资金
  * 时段：7x24，只标注「盘前 / 盘中 / 盘后 / 夜盘 / 周末」，不做任何闸门
  * 方向：long / short 双向，1h 未收 K 线的最新价即可触发（夜盘、盘前照常）
  * 杠杆：PERP_LEV 默认 5x，风控 8% 止损 / 12% 止盈 / 峰值回吐 50%

输出：data/binance_perp.json（前端读）；状态：state_perp.json
用法：python3 perp_sim.py --once | --loop [--interval 900]
"""
import os
import sys
import json
import time
import math
import random
import argparse
import datetime as dt
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
load_dotenv(os.path.join(BASE, '.env'))          # 必须最先加载（模块级常量依赖 .env）
STATE = os.path.join(BASE, 'state_perp.json')
OUT = os.path.join(ROOT, 'data', 'binance_perp.json')
PROXY = os.environ.get('PROXY', 'http://127.0.0.1:7897')
API = 'https://fapi.binance.com/fapi/v1'

START_CASH = float(os.environ.get('PERP_CASH', '100'))
LEV = float(os.environ.get('PERP_LEV', '5'))
RISK = float(os.environ.get('PERP_RISK', '0.5'))     # 总名义 = 净值 * LEV * RISK
MOM_N = int(os.environ.get('PERP_MOM_N', '12'))       # 1h K 线动量回看根数
MOM_TH = float(os.environ.get('PERP_MOM_TH', '0.015'))  # ±1.5% 开仓阈值
EXIT_TH = float(os.environ.get('PERP_EXIT_TH', '0.004'))  # 动量归零平仓
SL_PCT = float(os.environ.get('PERP_SL', '0.08'))
TP_PCT = float(os.environ.get('PERP_TP', '0.12'))
GIVE_BACK = float(os.environ.get('PERP_GIVE_BACK', '0.5'))
FEE = float(os.environ.get('PERP_FEE', '0.0004'))
TIMEFRAME = os.environ.get('PERP_TF', '1h')

# ---------------------------------------------------------------- 标的宇宙
# group: us=美股 / cn=中国大陆(CN_EQUITY) / hk=港股中国资产 / proxy=中国科技篮子
CONTRACTS = [
    # 现有 demo 盘的 13 个美股（这里同样用主网行情跑双向影子）
    ('TSLAUSDT',      '特斯拉',        'us'),
    ('COINUSDT',      'Coinbase',     'us'),
    ('PLTRUSDT',      'Palantir',     'us'),
    ('MSTRUSDT',      'MicroStrategy','us'),
    ('HOODUSDT',      'Robinhood',    'us'),
    ('NVDAUSDT',      '英伟达',        'us'),
    ('METAUSDT',      'Meta',         'us'),
    ('AMZNUSDT',      '亚马逊',        'us'),
    ('QQQUSDT',       '纳指100ETF',   'us'),
    ('SPYUSDT',       '标普500ETF',   'us'),
    ('GOOGLUSDT',     '谷歌',          'us'),
    ('INTCUSDT',      '英特尔',        'us'),
    ('CRCLUSDT',      'Circle',       'us'),
    # 中国大陆 CN_EQUITY（币安主网 2026-11 新增）
    ('CXMTUSDT',      '长鑫存储',      'cn'),
    ('UNITREEUSDT',   '宇树科技',      'cn'),
    # 港股 / 中概 中国资产（HK_EQUITY / EQUITY）
    ('HK0700USDT',    '腾讯控股',      'hk'),
    ('HK1810USDT',    '小米集团',      'hk'),
    ('TENCENTUSDT',   '腾讯(ADR)',    'hk'),
    ('GIGADEVUSDT',   '兆易创新',      'hk'),
    ('ZHONGJIUSDT',   '中际旭创',      'hk'),
    ('BYDUSDT',       '比亚迪',        'hk'),
    ('MEITUANUSDT',   '美团',          'hk'),
    ('KUAISHOUUSDT',  '快手',          'hk'),
    ('POPMARTUSDT',   '泡泡玛特',      'hk'),
    ('MINIMAXUSDT',   'MiniMax',       'hk'),
    ('ZHIPUUSDT',     '智谱AI',        'hk'),
    ('BABAUSDT',      '阿里巴巴',      'hk'),
    ('PDDUSDT',       '拼多多',        'hk'),
    ('HK0992USDT',    '联想集团',      'hk'),
    ('HK0625USDT',    '布鲁可',        'hk'),
]
# 「科创50」在币安没有原生合约（STARUSDT 是 Alpha/Crypto 币，不是指数）。
# 用可交易的中国硬科技合约等权合成一个篮子，只作方向参考、不单独下单。
STAR50_PROXY = ['CXMTUSDT', 'UNITREEUSDT', 'GIGADEVUSDT', 'ZHONGJIUSDT',
                'HK0700USDT', 'HK1810USDT', 'BYDUSDT', 'MINIMAXUSDT']
GROUP_LABEL = {'us': '美股', 'cn': '中国大陆', 'hk': '港股/中概', 'proxy': '科创50代理'}

_opener = None


def opener():
    global _opener
    if _opener is None:
        handlers = []
        if PROXY:
            handlers.append(urllib.request.ProxyHandler({'http': PROXY, 'https': PROXY}))
        _opener = urllib.request.build_opener(*handlers)
    return _opener


def fetch_klines(symbol, interval=TIMEFRAME, limit=90, retry=4):
    """主网公开 K 线。返回 [{t,o,h,l,c,v,closed}]，含最新一根未收 K。

    币安公开 REST 有 IP 权重限流，30 个标的并发时偶发 HTTP 429；
    指数退避 + 错峰启动，且不因个别标的失败拖垮整轮（由 run_once 记 errors）。
    """
    url = '%s/klines?symbol=%s&interval=%s&limit=%d' % (API, symbol, interval, limit)
    for i in range(retry + 1):
        try:
            with opener().open(url, timeout=20) as r:
                raw = json.load(r)
            out = []
            for k in raw:
                out.append({'t': int(k[0]), 'o': float(k[1]), 'h': float(k[2]),
                            'l': float(k[3]), 'c': float(k[4]), 'v': float(k[5]),
                            'closed': int(k[6]) < int(time.time() * 1000)})
            return out
        except Exception as e:
            if i == retry:
                raise RuntimeError('%s klines 失败: %s' % (symbol, str(e)[:120]))
            is429 = '429' in str(e)
            time.sleep((2.0 * (2 ** i) if is429 else 0.6 * (i + 1)) + random.random() * 0.4)


def session_label(now=None):
    """只标注时段，不做闸门。ET 固定 UTC-5（11 月为 EST）。"""
    now = now or dt.datetime.now(dt.timezone.utc)
    et = now.astimezone(dt.timezone(dt.timedelta(hours=-5)))
    wd, hm = et.weekday(), et.hour * 60 + et.minute
    if wd >= 5:
        return '周末 · 合约连续交易', 'weekend'
    if 4 * 60 <= hm < 9 * 60 + 30:
        return '盘前 · 合约可交易', 'pre'
    if 9 * 60 + 30 <= hm < 16 * 60:
        return '盘中 · 合约可交易', 'regular'
    if 16 * 60 <= hm < 20 * 60:
        return '盘后 · 合约可交易', 'post'
    return '夜盘 · 合约可交易', 'night'


def calc_signal(closes):
    """双向动量：>+th 做多，<-th 做空，动量归零平仓。"""
    if len(closes) < MOM_N + 2:
        return 'flat', 0.0, '数据不足'
    mom = closes[-1] / closes[-1 - MOM_N] - 1
    if mom > MOM_TH:
        return 'long', mom, '1h×%d 动量 %+.2f%% > +%.1f%% → 做多' % (MOM_N, mom * 100, MOM_TH * 100)
    if mom < -MOM_TH:
        return 'short', mom, '1h×%d 动量 %+.2f%% < -%.1f%% → 做空' % (MOM_N, mom * 100, MOM_TH * 100)
    return 'flat', mom, '1h×%d 动量 %+.2f%% 中性' % (MOM_N, mom * 100)


def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE) as f:
                s = json.load(f)
            s.setdefault('realized', 0.0)
            s.setdefault('positions', {})
            s.setdefault('trades', [])
            s.setdefault('equity_curve', [])
            s.setdefault('start_cash', START_CASH)
            return s
        except Exception:
            pass
    return {'start_cash': START_CASH, 'realized': 0.0, 'positions': {},
            'trades': [], 'equity_curve': [], 'created': dt.datetime.now().isoformat(timespec='seconds')}


def save_state(st):
    tmp = STATE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def _pnl_of(p, price):
    """方向化浮盈（名义金额口径，已含杠杆）。"""
    sign = 1 if p['side'] == 'long' else -1
    return (price / p['entry'] - 1) * sign * p['notional']


def run_once(st):
    syms = [c[0] for c in CONTRACTS]
    bars = {}
    errs = {}
    # 4 路并发 + 错峰提交，避开公开接口权重限流（8 路会触发 429）
    def _fetch(s, idx):
        time.sleep(idx * 0.08)
        return fetch_klines(s)

    with ThreadPoolExecutor(max_workers=4) as pool:
        fut = {pool.submit(_fetch, s, i): s for i, s in enumerate(syms)}
        for f in as_completed(fut):
            s = fut[f]
            try:
                bars[s] = f.result()
            except Exception as e:
                errs[s] = str(e)[:100]
    # 上一轮持仓里本轮缺行情的标的：沿用 state 里的入场价估值，不按 0 处理
    for k, q in st['positions'].items():
        if k not in bars:
            bars[k] = [{'t': 0, 'o': q['entry'], 'h': q['entry'], 'l': q['entry'],
                        'c': q['entry'], 'v': 0.0, 'closed': True}]

    now = dt.datetime.now(dt.timezone.utc)
    sess, sess_key = session_label(now)
    ts = now.isoformat(timespec='seconds')
    today = now.astimezone(dt.timezone(dt.timedelta(hours=-5))).date().isoformat()
    price, chg, sigs = {}, {}, {}
    for sym in syms:
        b = bars.get(sym)
        if not b or len(b) < 3:
            continue
        price[sym] = b[-1]['c']
        # 24h 涨跌：1h K 线回看 24 根
        ref = b[-25]['c'] if len(b) >= 25 else b[0]['c']
        chg[sym] = (b[-1]['c'] / ref - 1) * 100 if ref else 0
        sigs[sym] = calc_signal([x['c'] for x in b])

    equity = st['start_cash'] + st['realized']
    for sym, p in list(st['positions'].items()):
        if sym in price:
            equity += _pnl_of(p, price[sym])

    # ---- 先平仓：止损 / 止盈 / 峰值回吐 / 信号反转或归零
    for sym, p in list(st['positions'].items()):
        px = price.get(sym)
        if not px:
            continue
        sign = 1 if p['side'] == 'long' else -1
        gain = (px / p['entry'] - 1) * sign
        peak = p.get('peak', 0.0)
        # 峰值：多头用最高盈利，空头同样用最大浮盈
        peak = max(peak, gain)
        p['peak'] = peak
        reason = ''
        if gain <= -SL_PCT:
            reason = '止损 %.1f%%' % (gain * 100)
        elif gain >= TP_PCT:
            reason = '止盈 +%.1f%%' % (gain * 100)
        elif peak >= TP_PCT * 0.6 and gain <= peak * (1 - GIVE_BACK):
            reason = '峰值回吐 %.1f%%→%.1f%%' % (peak * 100, gain * 100)
        else:
            s = sigs.get(sym, ('flat', 0, ''))[0]
            if (s == 'flat' and abs(sigs.get(sym, ('', 0, ''))[1]) < EXIT_TH) or \
               (s != 'flat' and s != p['side']):
                reason = '信号反转/动量归零'
        if reason:
            gross = _pnl_of(p, px)
            fee = p['notional'] * FEE * 2
            net = gross - fee
            st['realized'] += net
            st['trades'].append({'ts': ts, 'sym': sym, 'cn': dict((c[0], c[1]) for c in CONTRACTS).get(sym, sym),
                                 'action': 'close', 'side': p['side'], 'entry': p['entry'], 'price': px,
                                 'notional': p['notional'], 'pnl': round(net, 4), 'reason': reason,
                                 'session': sess_key})
            del st['positions'][sym]
            equity = st['start_cash'] + st['realized'] + sum(
                _pnl_of(q, price.get(k, q['entry'])) for k, q in st['positions'].items())

    # ---- 再开仓：按信号数等权分配，最多 MAX_POS 个
    max_pos = int(os.environ.get('PERP_MAX_POS', '12'))
    cands = [s for s in syms if s in sigs and sigs[s][0] != 'flat' and s not in st['positions']]
    # 已有持仓优先，剩下的按 |动量| 降序
    cands.sort(key=lambda s: abs(sigs[s][1]), reverse=True)
    slots = max(0, max_pos - len(st['positions']))
    for sym in cands[:slots]:
        side = sigs[sym][0]
        px = price[sym]
        notional = equity * LEV * RISK / max(max_pos, 1)
        if notional < 1:
            continue
        st['positions'][sym] = {'side': side, 'entry': px, 'notional': round(notional, 4),
                                'qty': notional / px, 'peak': 0.0, 'opened': ts,
                                'session': sess_key, 'desc': sigs[sym][2]}
        st['trades'].append({'ts': ts, 'sym': sym, 'cn': dict((c[0], c[1]) for c in CONTRACTS).get(sym, sym),
                             'action': 'open', 'side': side, 'entry': px, 'price': px,
                             'notional': round(notional, 4), 'pnl': 0, 'reason': sigs[sym][2],
                             'session': sess_key})

    st['trades'] = st['trades'][-400:]
    eq_final = st['start_cash'] + st['realized'] + sum(
        _pnl_of(q, price.get(k, q['entry'])) for k, q in st['positions'].items())
    # 当日收益：以今日首个快照为基准（保留首点，末点持续更新；否则 day_open 会漂成上一轮净值）
    curve = [x for x in st['equity_curve'] if x.get('date') == today]
    day_open = curve[0]['equity'] if curve else None
    others = [x for x in st['equity_curve'] if x.get('date') != today]
    if day_open is None:
        today_pts = [{'date': today, 'ts': ts, 'equity': round(eq_final, 4)}]
        day_ret = 0.0
    else:
        day_ret = (eq_final / day_open - 1) * 100
        today_pts = [curve[0], {'date': today, 'ts': ts, 'equity': round(eq_final, 4)}]
    st['equity_curve'] = (others + today_pts)[-40:]

    name_of = dict((c[0], c[1]) for c in CONTRACTS)
    group_of = dict((c[0], c[2]) for c in CONTRACTS)
    rows = []
    for sym, cn, grp in CONTRACTS:
        b = bars.get(sym)
        s = sigs.get(sym, ('flat', 0, '无行情'))
        pos = st['positions'].get(sym)
        rows.append({'sym': sym, 'cn': cn, 'group': grp, 'group_label': GROUP_LABEL[grp],
                     'price': price.get(sym), 'chg': round(chg.get(sym, 0), 2),
                     'signal': s[0], 'mom': round(s[1] * 100, 2), 'desc': s[2],
                     'side': pos['side'] if pos else '', 'entry': pos['entry'] if pos else None,
                     'pnl': round(_pnl_of(pos, price.get(sym, pos['entry'])), 4) if pos else 0,
                     'pnl_pct': round(pos['peak'] * 100, 2) if pos else 0,
                     'bars': len(b) if b else 0, 'err': errs.get(sym, '')})
    positions = []
    for sym, p in st['positions'].items():
        px = price.get(sym, p['entry'])
        sign = 1 if p['side'] == 'long' else -1
        positions.append({'sym': sym, 'cn': name_of.get(sym, sym), 'group': group_of.get(sym, ''),
                          'side': p['side'], 'entry': p['entry'], 'price': px,
                          'notional': p['notional'], 'lev': LEV,
                          'pnl': round(_pnl_of(p, px), 4),
                          'pnl_pct': round((px / p['entry'] - 1) * sign * 100, 2),
                          'opened': p['opened'], 'session': p.get('session', ''), 'desc': p.get('desc', '')})
    positions.sort(key=lambda x: -abs(x['pnl']))

    # 科创50代理（中国科技等权篮子）：只作方向参考
    proxy_vals = [chg.get(s) for s in STAR50_PROXY if s in chg]
    proxy_moms = [sigs[s][1] for s in STAR50_PROXY if s in sigs]
    proxy = {'label': '科创50代理 · 中国科技等权篮子', 'members': STAR50_PROXY,
             'chg': round(sum(proxy_vals) / len(proxy_vals), 2) if proxy_vals else None,
             'mom': round(sum(proxy_moms) / len(proxy_moms) * 100, 2) if proxy_moms else None,
             'note': '币安无「科创50」原生合约（STARUSDT 是 Alpha/Crypto 币，非指数）；此篮子仅作方向参考，不单独下单'}

    out = {'generatedAt': ts, 'session': sess, 'session_key': sess_key,
           'mode': '影子模拟 · 主网公开行情 · 不接真实下单',
           'note': '币安 TRADIFI 永续为 7x24 连续交易，可杠杆、可做空；本盘无时段闸门，夜盘/盘前/周末照常巡检。'
                   'Demo 测试网不开放中国合约，故用主网行情虚拟记账；要真实下单需另配主网 API（真实资金）。',
           'account': {'start_cash': st['start_cash'], 'equity': round(eq_final, 4),
                       'realized': round(st['realized'], 4), 'today_ret': round(day_ret, 2),
                       'lev': LEV, 'risk': RISK, 'max_pos': max_pos,
                       'sl_pct': SL_PCT, 'tp_pct': TP_PCT, 'mom_n': MOM_N, 'mom_th': MOM_TH,
                       'tf': TIMEFRAME, 'positions': len(st['positions'])},
           'positions': positions, 'contracts': rows, 'star50_proxy': proxy,
           'trades': st['trades'][-40:][::-1], 'equity_curve': st['equity_curve'][-40:],
           'errors': errs}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    save_state(st)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--loop', action='store_true')
    ap.add_argument('--interval', type=int, default=900)
    a = ap.parse_args()
    if not a.loop:
        o = run_once(load_state())
        ac = o['account']
        print('%s | %s | 净值 %.2fU 今日 %+.2f%% | 持仓 %d | 行情 %d/%d | 错误 %d'
              % (o['generatedAt'], o['session'], ac['equity'], ac['today_ret'],
                 ac['positions'], sum(1 for r in o['contracts'] if r['price']),
                 len(o['contracts']), len(o['errors'])))
        for p in o['positions'][:8]:
            print('  %-14s %-6s %-5s 入%.4f 现%.4f %+.2f%% %+.3fU'
                  % (p['cn'], p['side'], p['group'], p['entry'], p['price'], p['pnl_pct'], p['pnl']))
        return
    while True:
        try:
            o = run_once(load_state())
            print('[progress] %s 净值 %.2fU 今日 %+.2f%% 持仓 %d %s'
                  % (o['generatedAt'], o['account']['equity'], o['account']['today_ret'],
                     o['account']['positions'], o['session']), flush=True)
        except Exception as e:
            print('[perp] 本轮失败: %s' % str(e)[:200], flush=True)
        time.sleep(a.interval)


if __name__ == '__main__':
    main()
