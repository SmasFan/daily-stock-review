# -*- coding: utf-8 -*-
"""市场环境自适应（v4.0）单测：判据、平滑确认、state 落盘。"""
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "src"))

import sim_live as sl


def sh(mom20, dev20):
    return {"mom20": mom20, "dev20": dev20, "atr20": 1.5, "price": 3000.0}


def cyb(mom20, atr20):
    return {"mom20": mom20, "dev20": -1.0, "atr20": atr20, "price": 2000.0}


def review(breadth=50.0):
    return {"temperature": {"breadth": breadth}}


class TestRegimeJudge(unittest.TestCase):
    def test_panic_high_vol_and_down(self):
        r = sl.market_regime(review(), sh=sh(-2.5, -1.0), cyb=cyb(-10.0, 4.4))
        self.assertEqual(r["regime"], "panic")
        self.assertEqual(r["params"]["pos"], 0.0)
        self.assertFalse(r["params"]["new"])

    def test_bear_trend_down(self):
        r = sl.market_regime(review(), sh=sh(-3.0, -1.0), cyb=cyb(-5.0, 2.0))
        self.assertEqual(r["regime"], "bear")
        self.assertAlmostEqual(r["params"]["pos"], 0.30)

    def test_bear_by_chinext_without_index_bear(self):
        # 上证 20 日只 -1%，但创业板 -8% 且上证跌破 MA20 → 仍是 bear
        r = sl.market_regime(review(), sh=sh(-1.0, -1.2), cyb=cyb(-8.0, 2.5))
        self.assertEqual(r["regime"], "bear")

    def test_bull(self):
        r = sl.market_regime(review(), sh=sh(3.0, 1.2), cyb=cyb(2.0, 2.0))
        self.assertEqual(r["regime"], "bull")
        self.assertAlmostEqual(r["params"]["pos"], 1.0)

    def test_bull_blocked_by_high_vol(self):
        r = sl.market_regime(review(), sh=sh(3.0, 1.2), cyb=cyb(2.0, 3.8))
        self.assertNotEqual(r["regime"], "bull")

    def test_overheat(self):
        r = sl.market_regime(review(72), sh=sh(4.5, 1.0), cyb=cyb(3.0, 2.0))
        self.assertEqual(r["regime"], "overheat")

    def test_chop_default(self):
        r = sl.market_regime(review(), sh=sh(0.3, 0.2), cyb=cyb(0.5, 2.0))
        self.assertEqual(r["regime"], "chop")

    def test_missing_index_falls_back_chop(self):
        r = sl.market_regime(review(), sh={}, cyb={})
        self.assertEqual(r["regime"], "chop")
        self.assertIn("缺失", r["why"][0])


class TestRegimeSmoothing(unittest.TestCase):
    def test_first_call_adopts_immediately(self):
        st = {}
        r = sl.market_regime(review(), state=st, date="2026-11-14",
                             sh=sh(-3.0, -1.0), cyb=cyb(-9.0, 2.0))
        self.assertEqual(r["regime"], "bear")
        self.assertEqual(st["meta"]["regime_now"]["regime"], "bear")
        self.assertEqual(len(st["meta"]["regime_history"]), 1)

    def test_switch_needs_confirm(self):
        st = {"meta": {"regime_now": {"regime": "chop", "days": 5, "since": "2026-11-01"}}}
        # 第 1 天：bear 未确认 → 沿用 chop，pending 记 1 天
        r1 = sl.market_regime(review(), state=st, date="2026-11-14",
                              sh=sh(-3.0, -1.0), cyb=cyb(-9.0, 2.0))
        self.assertEqual(r1["regime"], "chop")
        self.assertEqual(r1["raw"], "bear")
        self.assertEqual(st["meta"]["regime_now"]["pending"]["days"], 1)
        # 第 2 天：确认 → 切到 bear
        r2 = sl.market_regime(review(), state=st, date="2026-11-15",
                              sh=sh(-3.0, -1.0), cyb=cyb(-9.0, 2.0))
        self.assertEqual(r2["regime"], "bear")
        self.assertEqual(r2["pending"], {})

    def test_history_appends_on_change(self):
        st = {"meta": {"regime_now": {"regime": "chop", "days": 3,
                                      "pending": {"regime": "bear", "days": 1}}}}
        sl.market_regime(review(), state=st, date="2026-11-15",
                         sh=sh(-3.0, -1.0), cyb=cyb(-9.0, 2.0))
        hist = st["meta"]["regime_history"]
        self.assertEqual(hist[-1]["regime"], "bear")
        self.assertEqual(hist[-1]["date"], "2026-11-15")

    def test_breadth_from_market_totals(self):
        rv = {"temperature": {"market_up": 734, "market_total": 5378}}
        r = sl.market_regime(rv, sh=sh(0.0, 0.0), cyb=cyb(0.0, 2.0))
        self.assertAlmostEqual(r["inputs"]["breadth"], 13.6, places=1)


if __name__ == "__main__":
    unittest.main()
