#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""红利低波池（sim_dividend）单元测试：买点、整手、防飞刀、减档、清仓、一日一档。"""
import json
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "src"))

import sim_dividend as S  # noqa: E402


def closes(n=80, v=100.0):
    return [float(v)] * n


def lv(ma20=100.0, ma60=None):
    if ma60 is None:
        ma60 = ma20          # 默认平缓趋势，方便单测只调 MA20
    return {
        "ma20": ma20, "ma60": ma60,
        "dip_price": round(ma20 * (1 - S.RULE["dip"]), 4),
        "floor_price": round(ma60 * (1 - S.RULE["floor"]), 4),
        "exit_price": round(ma60 * (1 - S.RULE["ex"]), 4),
    }


class TestMA(unittest.TestCase):
    def test_ma_insufficient(self):
        self.assertIsNone(S.ma(closes(19), 20))

    def test_ma_value(self):
        self.assertAlmostEqual(S.ma([1, 2, 3, 4], 2), 3.5)

    def test_levels(self):
        r = lv()
        self.assertAlmostEqual(r["dip_price"], 96.0, places=3)
        self.assertAlmostEqual(r["floor_price"], 85.0, places=3)
        self.assertAlmostEqual(r["exit_price"], 88.0, places=3)

    def test_levels_short_history(self):
        self.assertIsNone(S.levels(closes(30)))


class TestLotShares(unittest.TestCase):
    def test_normal_budget_buys_whole_lots(self):
        # 预算 5555、价 1.5 → 37 手 → 3600 股（向下取整到百）
        self.assertEqual(S.lot_shares(1.5, 5555.0, cash=100000.0) % 100, 0)
        self.assertGreater(S.lot_shares(1.5, 5555.0, cash=100000.0), 0)

    def test_high_price_buys_one_lot_when_cash_enough(self):
        # 美的 89.6 元：每档预算 5555 买不到 1 手，但现金够 → 买 100 股
        self.assertEqual(S.lot_shares(89.6, 5555.0, cash=100000.0), 100)

    def test_high_price_no_cash(self):
        self.assertEqual(S.lot_shares(89.6, 5555.0, cash=1000.0), 0)


class TestStep(unittest.TestCase):
    def setUp(self):
        self.st = S.new_state()

    def test_buy_on_dip(self):
        acts = S.step(self.st, "515080", 1.50, lv(ma20=1.60), "2026-11-09", "10:00")
        self.assertIn("buy", acts)
        self.assertEqual(len(self.st["positions"]), 1)
        self.assertEqual(self.st["positions"][0]["lots"], 1)
        self.assertEqual(self.st["positions"][0]["shares"] % 100, 0)

    def test_no_buy_above_dip(self):
        acts = S.step(self.st, "515080", 1.58, lv(ma20=1.60), "2026-11-09", "10:00")
        self.assertEqual(acts, [])
        self.assertEqual(self.st["positions"], [])

    def test_no_buy_below_floor(self):
        # 跌破 MA60×(1-15%) 是飞刀，不接
        acts = S.step(self.st, "515080", 0.80, lv(ma20=1.00, ma60=1.00),
                      "2026-11-09", "10:00")
        self.assertEqual(acts, [])
        self.assertEqual(self.st["positions"], [])

    def test_one_lot_per_day(self):
        r = lv(ma20=1.60, ma60=1.60)
        S.step(self.st, "515080", 1.50, r, "2026-11-09", "10:00")
        S.step(self.st, "515080", 1.50, r, "2026-11-09", "10:02")
        S.step(self.st, "515080", 1.50, r, "2026-11-09", "10:04")
        self.assertEqual(self.st["positions"][0]["lots"], 1)
        S.step(self.st, "515080", 1.50, r, "2026-11-10", "10:00")
        self.assertEqual(self.st["positions"][0]["lots"], 2)

    def test_max_lots(self):
        r = lv(ma20=1.60, ma60=1.60)
        for d in range(1, 7):
            S.step(self.st, "515080", 1.50, r, "2026-11-%02d" % d, "10:00")
        self.assertEqual(self.st["positions"][0]["lots"], S.RULE["maxl"])

    def test_trim_at_tp(self):
        r = lv(ma20=1.60, ma60=1.60)
        for d in (1, 2):
            S.step(self.st, "515080", 1.50, r, "2026-11-%02d" % d, "10:00")
        before = self.st["positions"][0]["shares"]
        # 成本 1.5，+15% = 1.725；MA60 清仓线 = 1.6*0.88 = 1.408，价 1.75 不触发清仓
        acts = S.step(self.st, "515080", 1.75, r, "2026-11-03", "10:00")
        self.assertIn("trim", acts)
        self.assertLess(self.st["positions"][0]["shares"], before)
        self.assertEqual(self.st["positions"][0]["lots"], 1)

    def test_clear_on_stop_loss(self):
        r = lv(ma20=1.60, ma60=1.60)
        S.step(self.st, "515080", 1.50, r, "2026-11-01", "10:00")
        # 亏 10%：1.35；但 MA60 清仓线 1.408 更高，两个条件都触发 → 清仓
        acts = S.step(self.st, "515080", 1.35, r, "2026-11-02", "10:00")
        self.assertIn("clear", acts)
        self.assertEqual(self.st["positions"], [])

    def test_clear_below_ma60_exit(self):
        r = lv(ma20=1.60, ma60=1.60)
        S.step(self.st, "515080", 1.50, r, "2026-11-01", "10:00")
        acts = S.step(self.st, "515080", 1.40, r, "2026-11-02", "10:00")
        self.assertIn("clear", acts)
        self.assertEqual(self.st["positions"], [])

    def test_no_buy_when_allow_buy_false(self):
        acts = S.step(self.st, "515080", 1.50, lv(ma20=1.60), "2026-11-09", "10:00",
                      allow_buy=False)
        self.assertEqual(acts, [])

    def test_cash_never_negative(self):
        r = lv(ma20=1.60, ma60=1.60)
        for code in S.CODES:
            for d in range(1, 5):
                S.step(self.st, code, 1.50, r, "2026-11-%02d" % d, "10:00")
        self.assertGreaterEqual(self.st["cash"], 0)


class TestEquity(unittest.TestCase):
    def test_equity_and_curve(self):
        st = S.new_state()
        S.step(st, "515080", 1.50, lv(ma20=1.60), "2026-11-09", "10:00")
        eq = S.equity(st)
        self.assertAlmostEqual(eq, S.RULE["start_cash"], delta=20)
        p1 = S.snapshot_curve(st, "2026-11-09")
        p2 = S.snapshot_curve(st, "2026-11-09")
        self.assertEqual(len(st["equity_curve"]), 1)
        self.assertEqual(p1["equity"], p2["equity"])
        S.snapshot_curve(st, "2026-11-10")
        self.assertEqual(len(st["equity_curve"]), 2)

    def test_fee_drag(self):
        st = S.new_state()
        S.step(st, "515080", 1.50, lv(ma20=1.60), "2026-11-09", "10:00")
        # 买入当天按现价计净值，应略低于初始（费用）
        self.assertLess(S.equity(st), S.RULE["start_cash"])


class TestBacktest(unittest.TestCase):
    """买法结论必须可复现：长持退出优于早止盈与买入持有（Sharpe/回撤口径）。"""

    @classmethod
    def setUpClass(cls):
        cls.path = "/tmp/divpool/kline.json"
        if not os.path.exists(cls.path):
            raise unittest.SkipTest("缺历史K线 /tmp/divpool/kline.json，跳过回测回归")
        sys.path.insert(0, os.path.join(BASE, "backtest"))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dividend_buy", os.path.join(BASE, "backtest", "dividend_buy.py"))
        cls.B = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.B)
        with open(cls.path, encoding="utf-8") as f:
            cls.D = json.load(f)

    def test_chosen_beats_early_take_profit(self):
        c = self.B.run(self.D)
        e = self.B.run(self.D, mode="early", tp=0.06, sl=0.06, ex=0.05)
        self.assertGreater(c["total"], e["total"])
        self.assertGreater(c["oos"], e["oos"])

    def test_chosen_lower_mdd_than_hold(self):
        c = self.B.run(self.D)
        h = self.B.run(self.D, mode="bh")
        self.assertLess(c["mdd"], h["mdd"])
        self.assertGreater(c["sharpe"], h["sharpe"])

    def test_chosen_oos_positive(self):
        c = self.B.run(self.D)
        self.assertGreater(c["train"], 0)
        self.assertGreater(c["oos"], 0)
        self.assertGreater(c["oos_sh"], 1.5)

    def test_neighborhood_stable(self):
        # dip 3.5~5% 都在同一平台，不是单点过拟合
        for dip in (0.035, 0.04, 0.045, 0.05):
            r = self.B.run(self.D, dip=dip)
            self.assertGreater(r["total"], 0.30)
            self.assertGreater(r["oos_sh"], 1.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestIntradaySession(unittest.TestCase):
    """当日涨幅刷新 + 非交易时段不交易（2026-11-11）。"""
    def test_weekday_boundaries(self):
        from datetime import datetime
        # 2026-11-11 是周三
        for h, m, want in [(9, 14, False), (9, 15, True), (11, 35, True),
                           (11, 36, False), (12, 54, False), (12, 55, True),
                           (15, 5, True), (15, 6, False)]:
            with self.subTest(hm="%02d:%02d" % (h, m)):
                self.assertEqual(S.in_session(datetime(2026, 11, 11, h, m)), want)

    def test_weekend_closed(self):
        from datetime import datetime
        self.assertFalse(S.in_session(datetime(2026, 11, 14, 10, 0)))  # 周六

    def test_refresh_plan_updates_day_change(self):
        st = S.new_state()
        st["positions"] = [{
            "code": "600177", "name": "雅戈尔", "shares": 800, "avg": 6.69,
            "lots": 1, "last": 8.0, "last_chg": None,
            "buy_date": "2024-07-23", "ts": "", "levels": {},
        }]
        orig = S.closed_closes
        S.closed_closes = lambda code, use_cache=True: (None, [100.0] * 80)
        try:
            S.refresh_plan(st, {"600177": {"price": 8.47, "change": 0.12}}, use_cache=True)
        finally:
            S.closed_closes = orig
        self.assertEqual(st["positions"][0]["last"], 8.47)
        self.assertEqual(st["positions"][0]["last_chg"], 0.12)
        row = next(r for r in st["plan"] if r["code"] == "600177")
        self.assertEqual(row["chg"], 0.12)


class TestExpandWeights(unittest.TestCase):
    """2026-11-12 扩池：核心 6 只全份、新增 4 只半份，且老账本自动迁移。"""

    def test_pool_and_weight_sum(self):
        self.assertEqual(len(S.POOL), 10)
        self.assertEqual(len(S.CORE_CODES), 6)
        self.assertEqual(len(S.WEIGHTS), 4)
        self.assertAlmostEqual(S.WSUM, 8.0)

    def test_unit_budget(self):
        core = S.unit_budget("600900")
        ext = S.unit_budget("601988")
        self.assertAlmostEqual(core, S.RULE["start_cash"] / 6, places=2)
        self.assertAlmostEqual(ext, S.RULE["start_cash"] * 0.5 / 8, places=2)
        self.assertLess(ext, core / 2)

    def test_buy_lot_ext_uses_half_budget(self):
        a, b = S.new_state(), S.new_state()
        S.buy_lot(a, "600900", 25.0, "2026-11-12", "10:00")
        S.buy_lot(b, "601988", 7.0, "2026-11-12", "10:00")
        pa = a["positions"][0]
        pb = b["positions"][0]
        ca, cb = pa["shares"] * 25.0, pb["shares"] * 7.0
        # 整手取整后不超过每档预算，且最多差一手
        self.assertLessEqual(ca, S.unit_budget("600900") / 3)
        self.assertGreater(ca, S.unit_budget("600900") / 3 - 25.0 * 100)
        self.assertLessEqual(cb, S.unit_budget("601988") / 3)
        self.assertGreater(cb, S.unit_budget("601988") / 3 - 7.0 * 100)
        self.assertEqual(pa["shares"] % 100, 0)
        self.assertEqual(pb["shares"] % 100, 0)
        self.assertLess(cb, ca)
        self.assertEqual((pa["lots"], pb["lots"]), (1, 1))

    def test_new_state_meta_carries_weights(self):
        st = S.new_state()
        pool = {p["code"]: p for p in st["meta"]["pool"]}
        self.assertEqual(pool["600900"]["weight"], 1.0)
        self.assertEqual(pool["601988"]["weight"], 0.5)
        self.assertEqual(pool["601988"]["budget"], 6250)
        self.assertIn("半份", st["meta"]["pool_rule"])

    def test_refresh_plan_migrates_old_meta(self):
        st = S.new_state()
        st["meta"]["pool"] = [{"code": c, "name": n} for c, n in S.POOL]
        st["meta"].pop("pool_rule", None)
        orig = S.closed_closes
        S.closed_closes = lambda code, use_cache=True: (None, [100.0] * 80)
        try:
            S.refresh_plan(st, use_cache=True)
        finally:
            S.closed_closes = orig
        self.assertEqual(len(st["plan"]), 10)
        self.assertEqual(st["meta"]["pool"][-1]["weight"], 0.5)
        self.assertIn("半份", st["meta"]["pool_rule"])
        self.assertIn("expand", st["meta"]["backtest"])

    def test_expand_improves_risk_adjusted_return(self):
        ex = S.BACKTEST["expand"]
        self.assertGreater(ex["added4_half"]["sharpe"], ex["base6"]["sharpe"])
        self.assertLess(ex["added4_half"]["mdd"], ex["base6"]["mdd"])
        self.assertLess(ex["equal_weight_all25"]["sharpe"], ex["base6"]["sharpe"])
