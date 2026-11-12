# -*- coding: utf-8 -*-
"""全池红利账户（2026-11-12）：5 万口径派生，只读、不参与评分下单。"""
import json
import os
import unittest

import sim_live as S


class TestDividendAccount(unittest.TestCase):
    """从真实红利池账本派生出与 sim_live 账户同构的 dividend 账本。"""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(S.DATA_DIR, "sim_dividend.json")
        if not os.path.exists(path):
            raise unittest.SkipTest("data/sim_dividend.json 不存在")
        with open(path, "r", encoding="utf-8") as f:
            cls.src = json.load(f)

    def test_5w_capital_and_scale(self):
        a = S.dividend_account()
        self.assertIsNotNone(a)
        self.assertEqual(a["key"], "dividend")
        self.assertEqual(a["label"], "红利")
        self.assertEqual(a["start_cash"], 50000.0)
        scale = a["meta"]["scale"]
        self.assertAlmostEqual(scale, 50000.0 / self.src["meta"]["start_cash"], places=6)
        # 现金、净值按同一比例缩放
        self.assertAlmostEqual(a["cash"], self.src["cash"] * scale, places=1)
        self.assertAlmostEqual(a["equity"], (self.src["cash"] + sum(
            p["shares"] * p["last"] for p in self.src["positions"])) * scale, places=1)

    def test_positions_have_frontend_fields(self):
        a = S.dividend_account()
        for p in a["positions"]:
            self.assertTrue(p["code"])
            self.assertTrue(p["name"])
            self.assertGreater(p["shares"], 0)
            self.assertGreater(p["cost"], 0)
            # sim_live 的 equity_of 用 last_close / cost，卡片用 last
            self.assertGreater(p["last"], 0)
            self.assertEqual(p["last_close"], p["last"])
            self.assertEqual(p["kind"], "dividend")
        self.assertEqual(len(a["positions"]), len(self.src["positions"]))
        self.assertEqual(len(a["trades"]), len(self.src["trades"]))
        self.assertEqual(len(a["plan"]), len(self.src["plan"]))

    def test_plan_contains_current_pool(self):
        a = S.dividend_account()
        codes = [r["code"] for r in a["plan"]]
        self.assertIn("601985", codes)       # 2026-11-12 换入中国核电
        self.assertNotIn("601000", codes)    # 唐山港已剔除
        self.assertIn("601988", codes)
        self.assertIn("601398", codes)
        self.assertIn("601225", codes)

    def test_normalize_state_injects_without_touching_real_accounts(self):
        st = S.load_state()
        acc = st["pools"]["all"]["accounts"]
        self.assertIn("dividend", acc)
        self.assertEqual(acc["dividend"]["start_cash"], 50000.0)
        # 派生账户不能混进评分/共识下单循环
        keys = [k for k, _, _ in S.all_books(st, "all")]
        self.assertNotIn("dividend", keys)
        for k in ("aggressive", "balanced", "disciplined"):
            self.assertIn(k, keys)


if __name__ == "__main__":
    unittest.main()
