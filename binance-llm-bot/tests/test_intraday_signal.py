#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日内混合信号单测：口径与 backtest/shortterm_hybrid.py (v3) 逐行对齐。

v3 最优组：日线 MOM20 ±2% 定方向、1h MOM12 ±1% + SMA200 过滤择时、
exit_ratio=0 滞回离场、2% 止损、双向半仓做空。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import intraday_signal as isig


def flat_then(n, last, base=100.0):
    """前 n 根横盘，最后一根跳到 last（动量 = last/base - 1）。"""
    return [base] * n + [last]


class TestDailyDir(unittest.TestCase):
    def test_long_when_mom_above_2pct(self):
        r = isig.daily_dir(flat_then(25, 105.0), n=20, th=0.02,
                           short_th=0.02, short_ok=True)
        self.assertEqual(r['dir'], 'long')
        self.assertAlmostEqual(r['mom'], 0.05)
        self.assertIn('动量', r['desc'])

    def test_short_when_mom_below_minus_2pct(self):
        r = isig.daily_dir(flat_then(25, 97.0), n=20, th=0.02,
                           short_th=0.02, short_ok=True)
        self.assertEqual(r['dir'], 'short')
        self.assertAlmostEqual(r['mom'], -0.03)

    def test_short_disabled_falls_to_none(self):
        r = isig.daily_dir(flat_then(25, 97.0), n=20, th=0.02,
                           short_th=0.02, short_ok=False)
        self.assertIsNone(r['dir'])

    def test_middle_band_is_neutral(self):
        r = isig.daily_dir(flat_then(25, 99.0), n=20, th=0.02,
                           short_th=0.02, short_ok=True)
        self.assertIsNone(r['dir'])

    def test_history_too_short(self):
        r = isig.daily_dir([100.0] * 5, n=20, th=0.02)
        self.assertIsNone(r['dir'])
        self.assertIsNone(r['mom'])

    def test_sma_fallback_only_long(self):
        up = isig.daily_dir(flat_then(60, 110.0), mode='sma', cfg={'sma': 50})
        down = isig.daily_dir(flat_then(60, 90.0), mode='sma', cfg={'sma': 50})
        self.assertEqual(up['dir'], 'long')
        self.assertIsNone(down['dir'])  # 旧 SMA 回退不做空


class TestExecSignal(unittest.TestCase):
    def test_up_above_sma200(self):
        r = isig.exec_signal(flat_then(225, 105.0), n=12, th=0.01, trend_n=200)
        self.assertAlmostEqual(r['mom'], 0.05)
        self.assertTrue(r['above_trend'])
        self.assertIn('SMA200', r['desc'])

    def test_down_below_sma200(self):
        r = isig.exec_signal(flat_then(225, 95.0), n=12, th=0.01, trend_n=200)
        self.assertAlmostEqual(r['mom'], -0.05)
        self.assertFalse(r['above_trend'])

    def test_trend_disabled(self):
        r = isig.exec_signal(flat_then(20, 105.0), n=12, th=0.01, trend_n=0)
        self.assertIsNone(r['trend'])
        self.assertIsNone(r['above_trend'])

    def test_history_too_short(self):
        r = isig.exec_signal([100.0] * 5, n=12, th=0.01, trend_n=200)
        self.assertIsNone(r['mom'])


class TestTarget(unittest.TestCase):
    """合成目标方向；这是 v3 回测里 sig/持仓判定的同一套规则。"""

    def setUp(self):
        self.long = {'dir': 'long', 'mom': 0.05, 'desc': 'd'}
        self.short = {'dir': 'short', 'mom': -0.05, 'desc': 'd'}
        self.neutral = {'dir': None, 'mom': 0.0, 'desc': 'd'}
        self.ex_up = {'mom': 0.02, 'price': 105.0, 'trend': 100.0,
                      'above_trend': True, 'desc': 'h'}
        self.ex_down = {'mom': -0.02, 'price': 95.0, 'trend': 100.0,
                        'above_trend': False, 'desc': 'h'}

    def test_open_long_needs_both_frames(self):
        self.assertEqual(isig.target(self.long, self.ex_up), 'long')
        self.assertIsNone(isig.target(self.neutral, self.ex_up))       # 无日线方向
        self.assertIsNone(isig.target(self.long, self.ex_down))         # 1h 反向
        # 动量够但价在 SMA200 下方 → 不开多
        below = dict(self.ex_up, price=95.0, above_trend=False)
        self.assertIsNone(isig.target(self.long, below))

    def test_open_short_needs_both_frames(self):
        self.assertEqual(isig.target(self.short, self.ex_down), 'short')
        self.assertIsNone(isig.target(self.long, self.ex_down))
        above = dict(self.ex_down, price=105.0, above_trend=True)
        self.assertIsNone(isig.target(self.short, above))

    def test_trend_filter_disabled_when_no_sma(self):
        no_trend = dict(self.ex_up, trend=None, above_trend=None)
        self.assertEqual(isig.target(self.long, no_trend), 'long')

    def test_hysteresis_keeps_position_below_entry_threshold(self):
        # exit_ratio=0：只要动量还没衰减到 0 就继续持有，避免 1h 噪声反复换手
        weak = dict(self.ex_up, mom=0.005)
        self.assertEqual(isig.target(self.long, weak, holding_side='long',
                                     th=0.01, exit_ratio=0.0), 'long')

    def test_exit_on_momentum_decay(self):
        weak = dict(self.ex_up, mom=-0.001)
        self.assertIsNone(isig.target(self.long, weak, holding_side='long',
                                      th=0.01, exit_ratio=0.0))

    def test_exit_on_daily_flip(self):
        self.assertIsNone(isig.target(self.short, dict(self.ex_up, mom=0.02),
                                      holding_side='long'))
        self.assertIsNone(isig.target(self.neutral, self.ex_up,
                                      holding_side='long'))

    def test_short_side_hysteresis(self):
        weak = dict(self.ex_down, mom=-0.005)
        self.assertEqual(isig.target(self.short, weak, holding_side='short',
                                     th=0.01, exit_ratio=0.0), 'short')
        reverse = dict(self.ex_down, mom=0.001)
        self.assertIsNone(isig.target(self.short, reverse, holding_side='short',
                                      th=0.01, exit_ratio=0.0))

    def test_describe_contains_both_frames(self):
        s = isig.describe(self.long, self.ex_up, 'long')
        self.assertIn('d', s)
        self.assertIn('h', s)
        self.assertIn('✓多', s)


class TestHybridSignalIntegration(unittest.TestCase):
    """用假交易所验证两个 bot 的 hybrid_signal 真的取了两种时间框架。"""

    @staticmethod
    def fake_ex(daily, hourly):
        now = 1_800_000_000_000
        ms = {'1d': 86400000, '1h': 3600000}

        class Ex:
            def fetch_ohlcv(self, sym, tf, limit=None):
                closes = daily if tf == '1d' else hourly
                bar = ms.get(tf, 3600000)
                return [[now - (len(closes) - i) * bar * 2, c, c, c, c, 1]
                        for i, c in enumerate(closes)]

            def milliseconds(self):
                return now
        return Ex()

    def test_trader100_hybrid_signal(self):
        import trader100
        ex = self.fake_ex(flat_then(25, 105.0), flat_then(225, 105.0))
        daily, exec_sig, side = trader100.hybrid_signal(
            ex, 'TSLA/USDT:USDT', {'sma': 50})
        self.assertEqual(daily['dir'], 'long')
        self.assertTrue(exec_sig['above_trend'])
        self.assertEqual(side, 'long')
        self.assertEqual(trader100.LOOP_MIN, 15.0)
        self.assertEqual(trader100.SL_PCT, 0.02)

    def test_trader_short_hybrid_signal(self):
        import trader_short
        ex = self.fake_ex(flat_then(25, 97.0), flat_then(225, 95.0))
        daily, exec_sig, side = trader_short.hybrid_signal(ex, 'NVDA/USDT:USDT')
        self.assertEqual(daily['dir'], 'short')
        self.assertFalse(exec_sig['above_trend'])
        self.assertEqual(side, 'short')
        self.assertTrue(trader_short.SHORT_ENABLED)
        self.assertEqual(trader_short.SL_PCT, 0.02)
        self.assertTrue(callable(trader_short.sell_open))


if __name__ == '__main__':
    unittest.main()
