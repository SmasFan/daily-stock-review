#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓方向 / 规模守卫（策略只做多，绝不允许反向仓与超仓）

背景（2026-09-30 事故）
----------------------
trader_short 的平仓路径只看 contracts>0，不看方向：
9/24 META 多头平掉后触发了一次「卖出 0.08」，实际把仓位做成了 **空头 0.08**；
下一轮 sync 把空头当成「持有中」，跌破 SMA50 再次卖出 → 0.16、0.32 … 每轮翻倍，
两天滚到 **18.56 张空单（名义 1.37 万 U，浮亏 -160 U）**，短线池净值显示 -60.58U。

本模块提供两件事：
1) side_of(p)         —— 从 ccxt 持仓对象判方向（long/short/flat），不看符号猜
2) flatten_unexpected —— 反向仓 / 超仓自动市价纠正（reduceOnly），并清掉池记账

调用方（trader100 / trader_short / guard）在拿到 fetch_positions 结果后立刻调用，
保证「记账」和「下单决策」都不会再把反向仓当成正常多头。
"""
import logging

log = logging.getLogger('posguard') if not logging.getLogger().handlers else logging.getLogger('posguard')


def side_of(p):
    """ccxt 持仓对象 → 'long' / 'short' / 'flat'（优先用原始 positionAmt）"""
    info = (p or {}).get('info') or {}
    try:
        pamt = float(info.get('positionAmt')) if info.get('positionAmt') not in (None, '') else None
    except (TypeError, ValueError):
        pamt = None
    if pamt is None:
        try:
            pamt = float((p or {}).get('contracts') or 0)
            if (p or {}).get('side') == 'short':
                pamt = -abs(pamt)
        except (TypeError, ValueError):
            pamt = 0.0
    if pamt > 0:
        return 'long'
    if pamt < 0:
        return 'short'
    return 'flat'


def signed_amt(p):
    """带符号的持仓量（>0 多头 / <0 空头）"""
    try:
        c = abs(float((p or {}).get('contracts') or 0))
    except (TypeError, ValueError):
        return 0.0
    return -c if side_of(p) == 'short' else c


def _drop_state(st, sym, msg):
    """清掉池记账 + 追加日志（状态页可见）"""
    if not st:
        return
    (st.get('real_pos') or {}).pop(sym, None)
    try:
        (st.get('close_fail') or {}).pop(sym, None)
    except Exception:
        pass
    st.setdefault('log', []).append(__import__('time').strftime('%Y-%m-%d %H:%M:%S') + ' · ' + msg)


def flatten_unexpected(ex, sym, p, st=None, max_notional=None, name=None, why='方向守卫'):
    """反向仓（策略只做多时的空头）/ 超仓 → 市价平掉。

    返回 True 表示「该仓位已被判定异常并已处理（调用方应 continue，不要记账）」，
    False 表示仓位正常，调用方照常处理。
    """
    s = side_of(p)
    if s == 'flat':
        return False
    tag = name or sym.split('/')[0]
    try:
        c = abs(float(p.get('contracts') or 0))
    except (TypeError, ValueError):
        c = 0.0
    if c <= 0:
        return False
    info = p.get('info') or {}
    try:
        notional = abs(float(info.get('notional') or 0)) or c * float(p.get('markPrice') or p.get('entryPrice') or 0)
    except (TypeError, ValueError):
        notional = 0.0

    if s == 'long':
        if max_notional and notional > float(max_notional):
            log.warning('[%s] %s 超仓: %s 张 名义~%.0f > 上限%.0f → 市价减回', why, sym, c, notional, float(max_notional))
            try:
                _market_reduce(ex, sym, 'sell', c, reduce_only=True)
            except Exception as e:
                log.error('[%s] %s 超仓纠正失败: %s', why, sym, str(e)[:150])
                return False
            _drop_state(st, sym, '方向守卫：%s 超仓 %.4f 张(名义~%.0f) 已市价清除' % (tag, c, notional))
            return True
        return False

    # 空头 → 策略只做多，一定是 bug 残留，立即平掉
    log.warning('[%s] %s 反向仓(side=%s %s张 名义~%.0f, 入场%.2f) → 市价平掉', why, sym, s, c, notional,
                float(p.get('entryPrice') or 0))
    try:
        _market_reduce(ex, sym, 'buy', c, reduce_only=True)
    except Exception as e:
        log.error('[%s] %s 反向仓平仓失败: %s', why, sym, str(e)[:150])
        _drop_state(st, sym, '方向守卫：%s 反向仓 %.4f 张平仓失败 %s（下轮重试）' % (tag, c, str(e)[:80]))
        return True
    _drop_state(st, sym, '方向守卫：%s 反向仓 %.4f 张(名义~%.0f) 已市价平掉' % (tag, c, notional))
    log.info('[%s] %s 反向仓已清除', why, sym)
    return True


def _market_reduce(ex, sym, side, qty, reduce_only=True):
    """市价减仓；reduceOnly 保证不会反手开新仓（这是本次事故的直接止血点）"""
    q = ex.amount_to_precision(sym, qty)
    params = {'reduceOnly': True} if reduce_only else {}
    return ex.create_order(sym, 'market', side, q, None, params)
