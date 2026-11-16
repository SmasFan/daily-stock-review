#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一打分器 direction_score 单测（不联网）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import direction_score as D  # noqa: E402


def make_ctx(daily, hourly, price=None):
    """daily/hourly 为收盘价序列，ma 由模块自身口径算。"""
    ma20 = sum(hourly[-20:]) / 20 if len(hourly) >= 20 else None
    ma200 = sum(hourly[-200:]) / 200 if len(hourly) >= 200 else None
    return {'c': list(hourly), 'd': list(daily),
            'ma20': ma20, 'ma200': ma200,
            'price': price if price is not None else hourly[-1]}


def trend_up_daily():
    # 日线 60 根稳涨，20 日动量 > +2%，站上 SMA50
    return [100 + i for i in range(60)]


def trend_down_daily():
    # 日线 60 根稳跌，20 日动量 < -2%，跌破 SMA50
    return [200 - i for i in range(60)]


def flat_daily():
    return [100.0] * 60


def hourly_up(ma200_base=100.0):
    # 1h 250 根，前 200 根走平（SMA200≈base），后 50 根拉升 → 站上 SMA200、动量>1%
    return [ma200_base] * 200 + [ma200_base * (1 + 0.002 * i) for i in range(1, 51)]


def hourly_down(ma200_base=200.0):
    return [ma200_base] * 200 + [ma200_base * (1 - 0.002 * i) for i in range(1, 51)]


class TestDirectionScore(unittest.TestCase):
    def test_uptrend_gives_positive(self):
        c = make_ctx(trend_up_daily(), hourly_up())
        sc = D.direction_score(c, (1, 1, 1, 1, 0))
        self.assertGreater(sc, 0.5)

    def test_downtrend_gives_negative(self):
        c = make_ctx(trend_down_daily(), hourly_down())
        sc = D.direction_score(c, (1, 1, 1, 1, 0))
        self.assertLess(sc, -0.5)

    def test_weak_signal_is_small(self):
        # 全平数据：f1/f2 动量为 0，f3/f4 按「站上/跌破」二元规则把相等判为跌破 → -0.5。
        # 与 backtest/unified_sweep.py 的 score() 严格同口径（相等算跌破）。
        c = make_ctx(flat_daily(), [100.0] * 250)
        self.assertLessEqual(abs(D.direction_score(c, (1, 1, 1, 1, 0))), 0.5)

    def test_short_data_gives_zero(self):
        c = make_ctx([100.0] * 10, [100.0] * 12)
        self.assertEqual(D.direction_score(c, (1, 1, 1, 1, 0)), 0.0)

    def test_zero_weights_gives_zero(self):
        c = make_ctx(trend_up_daily(), hourly_up())
        self.assertEqual(D.direction_score(c, (0, 0, 0, 0, 0)), 0.0)

    def test_f5_sign_flips(self):
        # 现价远高于 SMA20（超买）：f5sign=+1 → f5=-1 拉低分；f5sign=-1 → f5=+1 抬高
        h = [100.0] * 200 + [100 + i for i in range(1, 51)]
        c = make_ctx(flat_daily(), h, price=200.0)   # price 远超 SMA20
        pos = D.direction_score(c, (0, 0, 0, 0, 1), f5sign=1)
        neg = D.direction_score(c, (0, 0, 0, 0, 1), f5sign=-1)
        self.assertLess(pos, 0)
        self.assertGreater(neg, 0)


class TestScoreSignal(unittest.TestCase):
    def test_each_strategy_has_config(self):
        for sid in ('S2', 'S4', 'S6'):
            fn, sl = D.make_score_signal(sid)
            self.assertTrue(callable(fn))
            self.assertGreater(sl, 0)

    def test_uptrend_opens_long(self):
        c = make_ctx(trend_up_daily(), hourly_up())
        for sid in ('S2', 'S6'):
            fn, _ = D.make_score_signal(sid)
            self.assertEqual(fn(c, None), 'long', sid)

    def test_downtrend_opens_short(self):
        c = make_ctx(trend_down_daily(), hourly_down())
        for sid in ('S2', 'S6'):
            fn, _ = D.make_score_signal(sid)
            self.assertEqual(fn(c, None), 'short', sid)

    def test_holding_kept_while_score_strong(self):
        c = make_ctx(trend_up_daily(), hourly_up())
        fn, _ = D.make_score_signal('S2')
        self.assertEqual(fn(c, 'long'), 'long')
        fn4, _ = D.make_score_signal('S4')
        # S4 反转权重只看 f5；uptrend 序列 price 远高 SMA20 → f5sign=-1 时 f5=+1，
        # sc 会 > eth 才保持
        self.assertIn(fn4(c, 'long'), ('long', None))

    def test_holding_released_when_score_fades(self):
        # 构造净分≈0 的衰减态：日线价微高于 SMA50(f3=+1)、1h 价低于 SMA200(f4=-1)、
        # 动量不足阈值(f1=0)，S2/S6/S4 都应落进死区 → 多单空单都离场。
        daily = [100 + 0.01 * i for i in range(60)]
        hourly = [100.0] * 200 + [99.0] * 50
        c = make_ctx(daily, hourly)
        for sid in ('S2', 'S4', 'S6'):
            fn, _ = D.make_score_signal(sid)
            self.assertIsNone(fn(c, 'long'), sid)
            self.assertIsNone(fn(c, 'short'), sid)

    def test_unknown_strategy_raises(self):
        with self.assertRaises(KeyError):
            D.make_score_signal('S9')


class TestArenaWiring(unittest.TestCase):
    def test_s2_s4_s6_use_score_signal(self):
        import arena_engine as A
        by = {s['id']: s for s in A.STRATS}
        # fn 应来自 direction_score，不再是旧的纯多 sig_daily_trend/sig_grid/sig_rebound
        self.assertIsNot(by['S2']['fn'], A.sig_daily_trend)
        self.assertIsNot(by['S6']['fn'], A.sig_grid)
        self.assertIsNot(by['S4']['fn'], A.sig_rebound)
        self.assertAlmostEqual(by['S2']['sl'], 0.03)
        self.assertAlmostEqual(by['S6']['sl'], 0.02)
        self.assertAlmostEqual(by['S4']['sl'], 0.025)

    def test_s1_s5_unchanged(self):
        import arena_engine as A
        by = {s['id']: s for s in A.STRATS}
        self.assertIs(by['S1']['fn'], A.sig_hybrid)
        self.assertIs(by['S5']['fn'], A.sig_breakout)
        self.assertAlmostEqual(by['S1']['weight'], 0.60)
        self.assertAlmostEqual(by['S5']['weight'], 0.20)


if __name__ == '__main__':
    unittest.main()
