#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T+0 利润保护模块（币安 / TRADIFI 股票永续共用）

为什么需要它
------------
币安是 T+0：随时可平、可反手，没有「今天买明天才能卖」的约束，而且是 7x24
连续交易。原来两个 bot（日线 trader100 / 1h trader_short）只有
「收盘跌破 SMA50 平仓 + 硬止损」，完全没有止盈 —— 结果就是账面利润原样还回去：
短线池净值从峰值 106.28 回吐到 100，日线池从 104.49 回吐到 100。
A 股 T+1 那套「让利润奔跑、收盘再决定」的惯性，在这里是负期望。

原则：**先保证不亏，再谈赚多少** —— 已经到手的浮盈回吐，比少赚一段趋势代价更高。

四条规则（优先级从高到低）
--------------------------
1) 移动止盈 close  ：峰值浮盈到过 trail_min%，且已回吐 ≥ max(峰值浮盈*give_back, trail)
                     → 全平落袋。用「回吐比例」而不是固定回撤，赚得多就容忍更大回撤，
                       不会因为 2% 的正常噪音被扫出大趋势（至少保住一半利润）
2) 分批止盈 trim   ：浮盈 ≥ tp_scale% 且未减过仓 → 平掉 scale_ratio，剩余止损上移保本
3) 动能止盈 close  ：浮盈 ≥ tp_fast% 但价格跌破 SMA20 → 全平（趋势末端常见形态）
4) 保本上移 be_lock：峰值浮盈到过 lock% → 止损上移到 入场价*(1+BE_BUF)，盈利单绝不变亏损单

1/2/3 交 LLM 复核（hard=False，prompt 里已明确 T+0 优先保利润、默认放行）；
4 是纯风控降险，直接执行不占 LLM 额度。

开关：BN_PROFIT_GUARD=0 关闭整个模块（退化成原来的纯趋势离场）。
参数：PG_LOCK / PG_TRAIL / PG_TRAIL_MIN / PG_GIVE_BACK / PG_TP_SCALE /
     PG_SCALE_RATIO / PG_TP_FAST
     （不带前缀的 PG_* 对两套 profile 同时生效，也可调用时用 kwargs 覆盖）
"""
import logging
import os
import time

log = logging.getLogger('pg')

FEE = float(os.environ.get('BN_FEE', '0.0004'))        # taker 单边
BE_BUF = float(os.environ.get('BE_BUF', '0.0015'))     # 保本价上浮：覆盖双边手续费+滑点

# 单位都是「价格涨跌幅 %」，不是杠杆后的权益涨跌幅
PROFILE_DEFAULTS = {
    'short': dict(lock=2.0, trail=1.5, trail_min=2.5, give_back=0.5,
                  tp_scale=3.0, scale_ratio=0.5, tp_fast=5.0),
    'daily': dict(lock=4.0, trail=3.0, trail_min=4.0, give_back=0.5,
                  tp_scale=7.0, scale_ratio=0.5, tp_fast=12.0),
}

ON = os.environ.get('BN_PROFIT_GUARD', '1').lower() not in ('0', 'false', 'off', 'no')

_ENV = {
    'lock': 'PG_LOCK', 'trail': 'PG_TRAIL', 'trail_min': 'PG_TRAIL_MIN',
    'give_back': 'PG_GIVE_BACK', 'tp_scale': 'PG_TP_SCALE',
    'scale_ratio': 'PG_SCALE_RATIO', 'tp_fast': 'PG_TP_FAST',
}


def cfg(profile='daily', **over):
    """取参数：profile 默认值 → PG_* 环境变量 → 调用方 kwargs。"""
    p = dict(PROFILE_DEFAULTS.get(profile) or PROFILE_DEFAULTS['daily'])
    for k, ev in _ENV.items():
        v = os.environ.get(ev)
        if v not in (None, ''):
            try:
                p[k] = float(v)
            except ValueError:
                pass
    p.update({k: v for k, v in over.items() if v is not None})
    return p


def be_price(entry):
    """保本价：入场价上浮 BE_BUF，保证平仓不亏（含手续费滑点）。"""
    return round(float(entry) * (1 + BE_BUF), 6)


def _pct(px, base):
    return (float(px) / float(base) - 1) * 100 if base else 0.0


def _intrabar_high(ex, sym, tf, since_ms, cap=1000):
    """自入场以来的最高价（含未收 K 线），失败返回 None。"""
    try:
        o = ex.fetch_ohlcv(sym, tf, since=int(since_ms), limit=cap)
        if o:
            return max(c[2] for c in o)
    except Exception as e:
        log.debug('[%s] 取 %s 高点失败: %s', sym, tf, str(e)[:80])
    return None


def update_peak(st, sym, price, high=None):
    """维护 real_pos[sym]['peak']（持仓期间最高价）。返回最新 peak。"""
    pos = (st.get('real_pos') or {}).get(sym)
    if not pos:
        return None
    best = max(float(price or 0), float(high or 0))
    cur = float(pos.get('peak') or 0)
    if best > cur:
        pos['peak'] = round(best, 6)
        return pos['peak']
    return cur or (round(best, 6) if best else None)


def plan(entry, price, peak, p, ma20=None, trimmed=False):
    """纯函数决策（不碰交易所），返回 {'act','why','sl','ratio'}。

    act: close（全平） / trim（减仓） / be_lock（只上移止损） / hold
    """
    if not entry or not price:
        return {'act': 'hold', 'why': '无价格数据', 'sl': None, 'ratio': 0}
    entry, price = float(entry), float(price)
    peak = float(peak or price)
    gain = _pct(price, entry)
    peak_gain = _pct(peak, entry)
    dd = (1 - price / peak) * 100 if peak else 0.0
    be = be_price(entry)

    # 1) 移动止盈：利润正在回吐，优先落袋。
    #    阈值 = max(峰值浮盈 * give_back, trail)：赚得多容忍更大回撤，但不低于 trail 过滤噪音
    gave = peak_gain - gain
    if peak_gain >= p['trail_min'] and gave >= max(peak_gain * p['give_back'], p['trail']):
        return {'act': 'close', 'sl': None, 'ratio': 0,
                'why': '移动止盈 峰值%+.1f%%→现%+.1f%%(回吐%.1f%%，保住%.0f%%)' % (
                    peak_gain, gain, gave,
                    (gain / peak_gain * 100) if peak_gain else 0)}
    # 2) 分批止盈：先锁一半，剩下的用保本价跑
    if (not trimmed) and p['scale_ratio'] > 0 and gain >= p['tp_scale']:
        return {'act': 'trim', 'sl': be, 'ratio': p['scale_ratio'],
                'why': '分批止盈 浮盈%+.1f%%≥%.1f%%，平%d%%锁利' % (
                    gain, p['tp_scale'], round(p['scale_ratio'] * 100))}
    # 3) 动能止盈：赚够了但短线结构破
    if gain >= p['tp_fast'] and ma20 and price < float(ma20):
        return {'act': 'close', 'sl': None, 'ratio': 0,
                'why': '动能止盈 浮盈%+.1f%%但跌破SMA20 %.2f' % (gain, float(ma20))}
    # 4) 保本上移：不再让盈利单变亏损单
    if peak_gain >= p['lock']:
        return {'act': 'be_lock', 'sl': be, 'ratio': 0,
                'why': '保本上移 峰值%+.1f%%，止损→%.2f' % (peak_gain, be)}
    return {'act': 'hold', 'sl': None, 'ratio': 0,
            'why': '浮盈%+.1f%% 峰值%+.1f%% 未触发' % (gain, peak_gain)}


def scan(ex, st, sym, profile='daily', price=None, ms=None, min_notional=0,
         peak_tf='1h', p=None, now=None):
    """扫描单个持仓的利润保护信号（**不下单**）。

    会就地维护 real_pos[sym] 的 peak / ts / sl 字段。返回 None（无需动作）
    或 dict（act=be_lock/trim/close，附带执行所需信息）。
    """
    if not ON:
        return None
    pos = (st.get('real_pos') or {}).get(sym)
    if not pos:
        return None
    p = p or cfg(profile)
    entry = float(pos.get('entry') or 0)
    if not entry:
        return None
    amt = float(pos.get('amt') or 0)
    if amt <= 0:
        return None
    if not pos.get('ts'):
        pos['ts'] = now or time.time()

    px = float(price or (ms or {}).get('price') or 0)
    if not px:
        try:
            px = float(ex.fetch_ticker(sym).get('last') or 0)
        except Exception:
            return None
    if not px:
        return None

    hi = _intrabar_high(ex, sym, peak_tf, float(pos['ts']) * 1000)
    peak = update_peak(st, sym, px, hi)
    if not peak:
        peak = px

    d = plan(entry, px, peak, p,
             ma20=(ms or {}).get('sma20'),
             trimmed=bool(pos.get('trimmed')))
    d.update({'symbol': sym, 'price': round(px, 6), 'peak': round(peak, 6),
              'entry': entry, 'amt': amt,
              'pnl_pct': round(_pct(px, entry), 2),
              'peak_pct': round(_pct(peak, entry), 2),
              'dd_peak': round((1 - px / peak) * 100, 2) if peak else 0.0,
              'full': False})

    if d['act'] == 'trim':
        qty = amt * float(d['ratio'])
        try:
            qty = float(ex.amount_to_precision(sym, qty))
        except Exception:
            pass
        rem = amt - qty
        # 减仓后剩余名义低于门槛 → 干脆全平（留迷你仓没意义，还占一个止损单）
        if rem <= 0 or px * rem < float(min_notional or 0):
            d['full'] = True
            d['why'] = d['why'].replace('平%d%%锁利' % round(p['scale_ratio'] * 100),
                                        '剩余名义<门槛直接全平')
        d['trim_qty'] = qty
        d['rem_qty'] = rem
    return d


def apply(ex, st, sym, d, hooks, llm=None):
    """执行利润保护动作。hooks 需含 cancel_sl / set_sl / close 三个回调。

    close(ex, sym, amt, llm, why)      —— 平仓（内部会撤 algo 单），amt=None 表示全平
    set_sl(ex, sym, qty, sl_price)     —— 挂/改止损
    cancel_sl(ex, sym)                 —— 撤 algo 单
    返回 (动作标签, 说明)；失败抛给调用方。
    """
    pos = (st.get('real_pos') or {}).get(sym) or {}
    act = d.get('act')

    if act == 'be_lock':
        try:
            hooks['cancel_sl'](ex, sym)
        except Exception:
            pass
        hooks['set_sl'](ex, sym, float(pos.get('amt') or d['amt']), d['sl'])
        pos['sl'] = d['sl']
        log.info('[利润保护] %s 保本上移 → 止损 %.2f（%s）', sym, d['sl'], d['why'])
        return 'be_lock', d['why']

    if act == 'trim' and not d.get('full'):
        qty = d['trim_qty']
        hooks['close'](ex, sym, qty, llm, '利润保护：' + d['why'])
        rem = round(float(pos.get('amt') or d['amt']) - qty, 8)
        pos['amt'] = rem
        pos['trimmed'] = True
        note = '减仓%.6f，剩余%.6f' % (qty, rem)
        if rem > 0 and d.get('sl'):
            try:
                hooks['set_sl'](ex, sym, rem, d['sl'])
                pos['sl'] = d['sl']
                note += '，止损上移%.2f' % d['sl']
            except Exception as e:
                log.warning('[利润保护] %s 减仓后改止损失败: %s', sym, str(e)[:120])
        log.info('[利润保护] %s %s（%s）', sym, note, d['why'])
        return 'trim', d['why']

    if act in ('trim', 'close'):      # trim 但 full=True → 整仓平掉
        hooks['close'](ex, sym, None, llm, '利润保护：' + d['why'])
        st.get('real_pos', {}).pop(sym, None)
        log.info('[利润保护] %s 全平（%s）', sym, d['why'])
        return 'close', d['why']

    return 'hold', d.get('why') or ''
