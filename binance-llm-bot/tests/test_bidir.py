#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双向做空改造的回归测试（2026-11-09）

核心防线：默认 BN_ALLOW_SHORT=0 时行为与旧版一致；显式开启且状态文件登记
short 时才允许空头存在；平仓永远 reduceOnly，多头卖 / 空头买回。
"""
import os
import sys
import unittest
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import pos_guard
import profit_guard
import trader100
import llm_gate


class FakeEx:
    def __init__(self, mark=100.0, fail=False):
        self.mark = mark
        self.fail = fail
        self.orders = []
        self.markets = {}

    def amount_to_precision(self, sym, qty):
        return qty

    def fetch_ticker(self, sym):
        return {'last': self.mark}

    def fetch_positions(self, syms=None):
        return []

    def cancel_all_orders(self, sym):
        pass

    def create_order(self, sym, typ, side, qty, price=None, params=None):
        if self.fail:
            raise RuntimeError('order rejected')
        self.orders.append((sym, typ, side, qty, params))
        return {'filled': qty, 'average': self.mark, 'info': {}}


def mkpos(side, qty=1.0, entry=100.0, mark=100.0, notional=100.0):
    amt = qty if side == 'long' else -qty
    return {'symbol': 'X/USDT:USDT', 'contracts': qty, 'entryPrice': entry,
            'markPrice': mark, 'side': side,
            'info': {'positionAmt': str(amt), 'notional': str(notional)}}


class TestPosGuardBidir(unittest.TestCase):
    """allow_short=False（默认）把空头当事故买回；显式开启且登记 short 才放过。"""

    def test_default_short_is_bought_back(self):
        ex, st = FakeEx(), {'real_pos': {'X/USDT:USDT': {'amt': 1.0, 'entry': 100.0}}}
        with patch('pos_guard._drop_state') as drop:
            self.assertTrue(pos_guard.flatten_unexpected(
                ex, 'X/USDT:USDT', mkpos('short'), st, max_notional=1000,
                allow_short=False, expected_side='short'))
        self.assertEqual(ex.orders[0][2], 'buy')
        self.assertTrue(ex.orders[0][4].get('reduceOnly'))
        drop.assert_called_once()

    def test_registered_short_is_kept(self):
        ex = FakeEx()
        st = {'real_pos': {'X/USDT:USDT': {'amt': 1.0, 'entry': 100.0, 'side': 'short'}}}
        self.assertFalse(pos_guard.flatten_unexpected(
            ex, 'X/USDT:USDT', mkpos('short'), st, max_notional=1000,
            allow_short=True))
        self.assertEqual(ex.orders, [])

    def test_registered_short_uses_accounting_side(self):
        st = {'real_pos': {'X/USDT:USDT': {'amt': 1.0, 'entry': 100.0, 'side': 'short'}}}
        self.assertEqual(pos_guard.registered_side(st, 'X/USDT:USDT'), 'short')
        self.assertEqual(pos_guard.registered_side({}, 'X/USDT:USDT'), 'long')

    def test_direction_mismatch_is_corrected_even_in_bidir(self):
        """记账 short 但实际 long（事故残留/手工干预）→ 卖出平多，不是买入加多。"""
        ex = FakeEx()
        self.assertTrue(pos_guard.flatten_unexpected(
            ex, 'X/USDT:USDT', mkpos('long'), None, max_notional=1000,
            allow_short=True, expected_side='short'))
        self.assertEqual(ex.orders[0][2], 'sell')
        self.assertTrue(ex.orders[0][4].get('reduceOnly'))

    def test_oversized_short_is_bought_back(self):
        ex = FakeEx()
        self.assertTrue(pos_guard.flatten_unexpected(
            ex, 'X/USDT:USDT', mkpos('short', 18.56, notional=13724), None,
            max_notional=1500, allow_short=True, expected_side='short'))
        self.assertEqual(ex.orders[0][2], 'buy')


class TestProfitGuardShort(unittest.TestCase):
    """空头止盈/峰值/保本全部镜像。"""

    def test_be_price_mirrors(self):
        self.assertGreater(profit_guard.be_price(100, 'long'), 100)
        self.assertLess(profit_guard.be_price(100, 'short'), 100)
        self.assertLess(profit_guard.be_price(100, 'short'),
                        100 * (1 - 2 * profit_guard.FEE))

    def test_pct_mirrors(self):
        self.assertAlmostEqual(profit_guard._pct(105, 100, 'long'), 5.0)
        self.assertAlmostEqual(profit_guard._pct(95, 100, 'short'), 5.0)

    def test_update_peak_uses_low_for_short(self):
        st = {'real_pos': {'X': {'peak': 95.0}}}
        self.assertEqual(profit_guard.update_peak(st, 'X', 92.0, high=90.0, side='short'), 90.0)
        self.assertEqual(profit_guard.update_peak(st, 'X', 98.0, side='short'), 90.0)

    def test_plan_short_trailing_close(self):
        # peak 浮盈 11%、现浮盈 5% → 回吐 6% > max(11*0.5, 1.5)，触发移动止盈
        p = profit_guard.cfg('short')
        d = profit_guard.plan(100, 95, 89, p, side='short')
        self.assertEqual(d['act'], 'close')
        self.assertIn('移动止盈', d['why'])

    def test_plan_short_momentum_close_when_price_above_ma20(self):
        # trimmed=True 跳过分批止盈，才能走到动能止盈分支（tp_fast > tp_scale）
        p = profit_guard.cfg('short')
        d = profit_guard.plan(100, 88, 90, p, ma20=85.0, trimmed=True, side='short')
        self.assertEqual(d['act'], 'close')
        self.assertIn('涨破', d['why'])
        # 同样价格但空头未涨破 SMA20 → 不应动能止盈
        d2 = profit_guard.plan(100, 88, 90, p, ma20=95.0, trimmed=True, side='short')
        self.assertNotIn('动能止盈', d2['why'])

    def test_plan_long_unchanged(self):
        p = profit_guard.cfg('daily')
        self.assertEqual(profit_guard.plan(100, 101, 101, p)['act'], 'hold')
        self.assertGreater(profit_guard.be_price(100), 100)


class TestCalcSignalShort(unittest.TestCase):
    """trader100.calc_signal 的双向分支：保留 above/mom/desc，新增 side。"""

    def _closes(self, last, n=25):
        return [100.0] * (n - 1) + [last]

    def test_default_no_short(self):
        with patch.object(trader100, 'ALLOW_SHORT', False), \
             patch.object(trader100, 'MOM_N', 20), \
             patch.object(trader100, 'MOM_TH', 0.02), \
             patch.object(trader100, 'SHORT_MOM_TH', 0.08), \
             patch.object(trader100, 'SIGNAL', 'mom'):
            sig = trader100.calc_signal(self._closes(90.0), {'sma': 50})
        self.assertFalse(sig['above'])
        self.assertIsNone(sig['side'])
        self.assertIn('-10.00%', sig['desc'])

    def test_short_when_drop_over_8pct(self):
        with patch.object(trader100, 'ALLOW_SHORT', True), \
             patch.object(trader100, 'MOM_N', 20), \
             patch.object(trader100, 'MOM_TH', 0.02), \
             patch.object(trader100, 'SHORT_MOM_TH', 0.08), \
             patch.object(trader100, 'SHORT_RISK', 0.5), \
             patch.object(trader100, 'SIGNAL', 'mom'):
            sig = trader100.calc_signal(self._closes(90.0), {'sma': 50})
        self.assertFalse(sig['above'])
        self.assertEqual(sig['side'], 'short')
        self.assertIn('开空', sig['desc'])

    def test_long_unchanged(self):
        with patch.object(trader100, 'ALLOW_SHORT', True), \
             patch.object(trader100, 'MOM_N', 20), \
             patch.object(trader100, 'MOM_TH', 0.02), \
             patch.object(trader100, 'SHORT_MOM_TH', 0.08), \
             patch.object(trader100, 'SIGNAL', 'mom'):
            sig = trader100.calc_signal(self._closes(103.0), {'sma': 50})
        self.assertTrue(sig['above'])
        self.assertEqual(sig['side'], 'long')
        self.assertIn('+3.00%', sig['desc'])


class TestLlmPromptShort(unittest.TestCase):
    """双向候选在 prompt 里语义清晰，且不破坏旧的多头文案。"""

    BUY = {'action': 'buy', 'name': 'TSLA', 'symbol': 'TSLA/USDT:USDT',
           'price': 100.0, 'sma50': 95.0, 'dev50': 5.0, 'sma20': 98.0,
           'vs_ma20': '上方', 'd7': 1.0, 'd30': 3.0, 'lev': 5, 'why': '动量+3%'}

    SELL = {'action': 'sell', 'name': 'TSLA', 'symbol': 'TSLA/USDT:USDT',
            'price': 100.0, 'entry': 95.0, 'pnl_pct': 5.3, 'sma50': 95.0,
            'dev50': 5.0, 'd7': 1.0, 'why': '趋势离场', 'hard': True,
            'peak_pct': 6.4, 'dd_peak': 0.4, 'kind': 'protect'}

    def test_default_long_wording_unchanged(self):
        sys_p, user, short = llm_gate._build_prompt([self.BUY, self.SELL])
        self.assertIn('t1 | 买 TSLA', user)
        self.assertIn('t2 | 卖 TSLA', user)
        self.assertIn('硬规则(不可否决)', user)
        self.assertIn('峰值+6.4% 自峰值回撤0.4%', user)

    def test_short_wording(self):
        _, user, _ = llm_gate._build_prompt([
            dict(self.BUY, side='short', why='跌超8%'),
            dict(self.SELL, side='short', hard=True)])
        self.assertIn('t1 | 开空 TSLA', user)
        self.assertIn('t2 | 买回平空 TSLA', user)

    def test_system_prompt_explains_bidir(self):
        # 2026-11-10 日内混合改造：空头止损从 +6% 收紧到 2%，上下文改 1h 口径
        sys_p, _, _ = llm_gate._build_prompt([self.BUY])
        for frag in ('开空', '买入平空', '半仓', '止损 2%', '近 7 小时',
                     '别否决统计信号', 'SMA200'):
            self.assertIn(frag, sys_p)

    def test_shadow_skips_short(self):
        st = {'shadow': {'open': [], 'stats': {'n': 0, 'correct': 0, 'wrong': 0, 'sum_ret': 0.0}}}
        with patch('llm_gate.market_stats', return_value={'price': 100.0}), \
             patch('llm_gate.time.time', return_value=0):
            n = llm_gate.shadow_record(st, [dict(self.BUY, side='short')],
                                       {'t1': {'verdict': 'avoid', 'note': 'x'}})
        self.assertEqual(n, 0)
        self.assertEqual(st['shadow']['open'], [])


class TestClosePos(unittest.TestCase):
    """close_pos 按方向分派，绝不对空头下卖单。"""

    def test_short_uses_buy_cover(self):
        with patch.object(trader100, 'buy_cover', return_value=1.0) as cover, \
             patch.object(trader100, 'sell_close', return_value=1.0) as sell:
            self.assertEqual(trader100.close_pos(FakeEx(), 'X', 'short'), 1.0)
        cover.assert_called_once()
        sell.assert_not_called()

    def test_long_uses_sell_close(self):
        with patch.object(trader100, 'buy_cover', return_value=1.0) as cover, \
             patch.object(trader100, 'sell_close', return_value=2.0) as sell:
            self.assertEqual(trader100.close_pos(FakeEx(), 'X', 'long'), 2.0)
        sell.assert_called_once()
        cover.assert_not_called()


if __name__ == '__main__':
    unittest.main()
