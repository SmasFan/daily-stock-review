#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trader100 信号函数测试：动量默认逻辑 + SMA 回退。"""
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import trader100  # noqa: E402


class TestCalcSignal(unittest.TestCase):
    def setUp(self):
        self._sig, self._n, self._th = trader100.SIGNAL, trader100.MOM_N, trader100.MOM_TH
        trader100.SIGNAL, trader100.MOM_N, trader100.MOM_TH = 'mom', 20, 0.02
        self.cfg = {'sma': 50}

    def tearDown(self):
        trader100.SIGNAL, trader100.MOM_N, trader100.MOM_TH = self._sig, self._n, self._th

    def test_momentum_above_threshold(self):
        closes = [100.0] * 20 + [103.0]
        sig = trader100.calc_signal(closes, self.cfg)
        self.assertTrue(sig['above'])
        self.assertAlmostEqual(sig['mom'], 0.03)
        self.assertIn('+3.00%', sig['desc'])

    def test_momentum_below_threshold(self):
        closes = [100.0] * 20 + [101.0]
        sig = trader100.calc_signal(closes, self.cfg)
        self.assertFalse(sig['above'])
        self.assertIn('+1.00%', sig['desc'])

    def test_sma_fallback(self):
        trader100.SIGNAL = 'sma'
        closes = [1, 2, 3, 4, 5, 6, 7]
        sig = trader100.calc_signal(closes, {'sma': 5})
        self.assertTrue(sig['above'])   # 7 > mean(3..7)=5
        self.assertIsNone(sig['mom'])
        self.assertIn('SMA5', sig['desc'])


if __name__ == '__main__':
    unittest.main()
