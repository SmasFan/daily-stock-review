#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pos_guard 单测：反向仓/超仓必须被纠正，且永远不会「越卖越空」

事故背景（2026-09-30 META）：
  trader_short 平仓路径只看 contracts>0，不看 side。多头平完后又卖了一次，
  仓位变空头 0.08；之后每轮「跌破 SMA50 → 卖出」把空头翻倍，两天滚到 -18.56 张
  （名义 1.37 万 U，浮亏 -160U），短线池净值被拖到 -59U。

跑法：python3 -m unittest discover -s binance-llm-bot/tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pos_guard  # noqa: E402
import trader_short  # noqa: E402
import guard  # noqa: E402

SYM = 'META/USDT:USDT'


def mkpos(side, qty, entry=730.85, mark=739.42):
    """构造 ccxt 风格的持仓对象（关键：contracts 恒为正，方向看 side / positionAmt）"""
    signed = qty if side == 'long' else -qty
    return {
        'symbol': SYM, 'side': side, 'contracts': qty,
        'entryPrice': entry, 'markPrice': mark,
        'info': {'positionAmt': str(signed), 'notional': str(signed * mark)},
    }


class FakeEx:
    """记录下单调用；不联网"""

    def __init__(self, positions=None, fail_order=False):
        self.positions = positions or []
        self.orders = []
        self.fail_order = fail_order
        self.markets = {}      # algo_tools.set_symbol_map(ex.markets) 需要

    def amount_to_precision(self, sym, qty):
        return round(float(qty), 8)

    def fetch_positions(self, syms=None):
        if not syms:
            return list(self.positions)
        return [p for p in self.positions if p['symbol'] in syms]

    def cancel_all_orders(self, sym):
        return None

    def fetch_ticker(self, sym):
        return {'last': 739.42}

    def create_order(self, sym, type_, side, qty, price=None, params=None):
        if self.fail_order:
            raise RuntimeError('binance {"code":-2019,"msg":"Margin is insufficient."}')
        self.orders.append({'side': side, 'qty': qty, 'params': params or {}})
        return {'id': '1', 'average': 739.42, 'filled': qty, 'info': {'realizedPnl': '-159.0978'}}


class TestSideOf(unittest.TestCase):
    def test_directions(self):
        self.assertEqual(pos_guard.side_of(mkpos('long', 1)), 'long')
        self.assertEqual(pos_guard.side_of(mkpos('short', 1)), 'short')
        self.assertEqual(pos_guard.side_of({'contracts': 0, 'info': {'positionAmt': '0'}}), 'flat')

    def test_side_from_position_amt_when_side_missing(self):
        p = {'contracts': 2.0, 'info': {'positionAmt': '-2.0'}}
        self.assertEqual(pos_guard.side_of(p), 'short')
        self.assertEqual(pos_guard.signed_amt(p), -2.0)


class TestFlattenUnexpected(unittest.TestCase):
    def test_normal_long_untouched(self):
        ex, st = FakeEx(), {'real_pos': {SYM: {'amt': 0.08, 'entry': 730.85}}}
        self.assertFalse(pos_guard.flatten_unexpected(ex, SYM, mkpos('long', 0.08), st, max_notional=1500))
        self.assertEqual(ex.orders, [])
        self.assertIn(SYM, st['real_pos'])

    def test_short_is_bought_back_and_dropped(self):
        """核心回归：反向仓必须是「买入平掉」，而不是继续卖（卖就是加空）"""
        ex = FakeEx()
        st = {'real_pos': {SYM: {'amt': 18.56, 'entry': 730.85}}, 'log': []}
        p = mkpos('short', 18.56)
        self.assertTrue(pos_guard.flatten_unexpected(ex, SYM, p, st, max_notional=1500))
        self.assertEqual(len(ex.orders), 1)
        self.assertEqual(ex.orders[0]['side'], 'buy')            # 买入 = 平空
        self.assertEqual(ex.orders[0]['qty'], 18.56)
        self.assertTrue(ex.orders[0]['params'].get('reduceOnly'))  # 绝不允许反手
        self.assertNotIn(SYM, st['real_pos'])                     # 记账同步清掉
        self.assertTrue(st['log'])

    def test_oversized_long_is_trimmed(self):
        ex = FakeEx()
        st = {'real_pos': {SYM: {'amt': 18.56, 'entry': 730.85}}, 'log': []}
        p = mkpos('long', 18.56)                                  # 名义 1.37 万 U，远超 1500 上限
        self.assertTrue(pos_guard.flatten_unexpected(ex, SYM, p, st, max_notional=1500))
        self.assertEqual(ex.orders[0]['side'], 'sell')
        self.assertTrue(ex.orders[0]['params'].get('reduceOnly'))
        self.assertNotIn(SYM, st['real_pos'])

    def test_close_failure_still_blocks_accounting(self):
        ex = FakeEx(fail_order=True)
        st = {'real_pos': {SYM: {'amt': 1.0, 'entry': 730.85}}, 'log': []}
        self.assertTrue(pos_guard.flatten_unexpected(ex, SYM, mkpos('short', 1.0), st, max_notional=1500))
        self.assertNotIn(SYM, st['real_pos'])      # 下轮重试，但绝不当成正常持仓参与决策


class TestSellCloseRefusesShort(unittest.TestCase):
    def setUp(self):
        self._orig_cancel = trader_short.cancel_sl
        trader_short.cancel_sl = lambda ex, sym: None    # 不碰 algo 网络接口

    def tearDown(self):
        trader_short.cancel_sl = self._orig_cancel

    def test_short_position_is_not_sold(self):
        ex = FakeEx(positions=[mkpos('short', 18.56)])
        self.assertEqual(trader_short.sell_close(ex, SYM), 0)
        self.assertEqual(ex.orders, [])                   # 没卖出 → 不会加空

    def test_long_position_is_closed_with_reduce_only(self):
        ex = FakeEx(positions=[mkpos('long', 0.08)])
        qty = trader_short.sell_close(ex, SYM)
        self.assertEqual(qty, 0.08)
        self.assertEqual(ex.orders[0]['side'], 'sell')
        self.assertTrue(ex.orders[0]['params'].get('reduceOnly'))

    def test_partial_qty_is_capped_by_real_long(self):
        """state 里记 0.08，实际只剩 0.04 → 只能卖 0.04"""
        ex = FakeEx(positions=[mkpos('long', 0.04)])
        self.assertEqual(trader_short.sell_close(ex, SYM, amt=0.08), 0.04)


class TestGuardDirection(unittest.TestCase):
    def test_guard_flattens_short_instead_of_stopping_out(self):
        ex = FakeEx(positions=[mkpos('short', 18.56)])
        guard.check_once(ex)
        self.assertEqual(len(ex.orders), 1)
        self.assertEqual(ex.orders[0]['side'], 'buy')     # 空头 → 买入平掉
        self.assertTrue(ex.orders[0]['params'].get('reduceOnly'))

    def test_guard_keeps_long_above_stop(self):
        ex = FakeEx(positions=[mkpos('long', 0.08, entry=100.0, mark=99.0)])
        guard.check_once(ex)
        self.assertEqual(ex.orders, [])                   # 未触及 -12% 止损，不动手


if __name__ == '__main__':
    unittest.main()
