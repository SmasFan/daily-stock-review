#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓方向 / 规模守卫（默认只做多；显式开启双向后允许**已登记**的空头仓）

背景（2026-09-30 事故）
----------------------
trader_short 的平仓路径只看 contracts>0，不看方向：
9/24 META 多头平掉后触发了一次「卖出 0.08」，实际把仓位做成了 **空头 0.08**；
下一轮 sync 把空头当成「持有中」，跌破 SMA50 再次卖出 → 0.16、0.32 … 每轮翻倍，
两天滚到 **18.56 张空单（名义 1.37 万 U，浮亏 -160 U）**，短线池净值显示 -60.58U。

本模块提供三件事：
1) side_of(p)         —— 从 ccxt 持仓对象判方向（long/short/flat），不看符号猜
2) registered_side(st, sym) —— 池记账里登记的方向（旧状态无 side 字段一律按 long 处理）
3) flatten_unexpected —— 反向仓 / 超仓自动市价纠正（reduceOnly），并清掉池记账

2026-11-09 起支持双向：调用方传 allow_short=True 且 expected_side='short' 时，
空头仓才算「策略自己开的」，否则一律按事故残留买回平掉。
即：**方向以记账为准，交易所实际方向与记账不符 = 异常**。
平仓永远 reduceOnly，绝不允许「卖出平多」打到空头上继续加空。
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


def registered_side(st, sym):
    """池记账里登记的方向；旧状态没有 side 字段时按 long（向后兼容）。"""
    rec = ((st or {}).get('real_pos') or {}).get(sym) or {}
    return rec.get('side') if rec.get('side') in ('long', 'short') else 'long'


def flatten_unexpected(ex, sym, p, st=None, max_notional=None, name=None, why='方向守卫',
                       allow_short=False, expected_side=None):
    """反向仓 / 超仓 / 方向不符 → 市价平掉。

    allow_short + expected_side='short' 时，登记过的空头是合法持仓，只做超仓检查。
    返回 True 表示「该仓位已被判定异常并已处理（调用方应 continue，不要记账）」，
    False 表示仓位正常，调用方照常处理。
    """
    s = side_of(p)
    if s == 'flat':
        return False
    if expected_side is None:
        expected_side = registered_side(st, sym)
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

    # 交易所实际方向与记账不符 → 一定是事故残留或手工干预，双向模式下也要纠正
    if s != expected_side:
        log.warning('[%s] %s 方向不符: 实际%s / 记账%s %s张 → 市价平掉', why, sym, s, expected_side, c)
        try:
            _market_reduce(ex, sym, 'sell' if s == 'long' else 'buy', c, reduce_only=True)
        except Exception as e:
            log.error('[%s] %s 方向纠正失败: %s', why, sym, str(e)[:150])
            _drop_state(st, sym, '方向守卫：%s 方向不符(实际%s/记账%s) 平仓失败 %s（下轮重试）'
                        % (tag, s, expected_side, str(e)[:80]))
            return True
        _drop_state(st, sym, '方向守卫：%s 方向不符(实际%s/记账%s) 已市价清除' % (tag, s, expected_side))
        return True

    # 超仓检查（多空同口径，上限不变）
    if max_notional and notional > float(max_notional):
        log.warning('[%s] %s 超仓: %s 张 名义~%.0f > 上限%.0f → 市价减回', why, sym, c, notional, float(max_notional))
        try:
            _market_reduce(ex, sym, 'sell' if s == 'long' else 'buy', c, reduce_only=True)
        except Exception as e:
            log.error('[%s] %s 超仓纠正失败: %s', why, sym, str(e)[:150])
            # ds4.1 审阅：平仓失败时与「方向不符」分支保持一致，跳过记账，
            # 不能把异常仓当成正常持仓继续参与决策。
            return True
        _drop_state(st, sym, '方向守卫：%s 超仓 %.4f 张(名义~%.0f) 已市价清除' % (tag, c, notional))
        return True

    if s == 'long':
        return False
    # ds4.1 审阅 P0：只有「记账明确登记 short」的空头才允许存在。
    # 旧状态无 side 字段 / 状态文件缺登记 → expected_side='long' 或 None，
    # 一律按事故残留买回；否则未经登记的幽灵空头会绕过守卫进入决策。
    if allow_short and s == 'short' and expected_side == 'short':
        return False       # 已登记的空头，正常参与决策

    # 空头 → 策略只做多 / 双向未开启，一定是 bug 残留，立即买回平掉
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
