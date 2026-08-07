"""网格信号模块单元测试：操作提醒推导。"""
import unittest

from src import grid_backtest as gbt
from src import grid_signal as gs


class TestGridAction(unittest.TestCase):

    def test_clear_on_overheat(self):
        p = gbt.GridParams(dev_clear=0.05)
        s = gs.grid_action(0.10, 0.5, p)
        self.assertEqual(s["action_key"], "clear")
        self.assertEqual(s["action"], "超涨清仓")

    def test_buy_when_dev_below_target(self):
        p = gbt.GridParams(base_pos=0.70)
        # dev=-10% → 目标仓位远高于当前 0.5
        s = gs.grid_action(-0.10, 0.5, p)
        self.assertEqual(s["action_key"], "buy")
        self.assertIn("逢低加仓", s["action"])

    def test_reduce_when_dev_above_target(self):
        p = gbt.GridParams(base_pos=0.70)
        s = gs.grid_action(0.04, 0.9, p)
        self.assertEqual(s["action_key"], "reduce")

    def test_hold_near_target(self):
        p = gbt.GridParams(base_pos=0.70)
        s = gs.grid_action(0.0, 0.7, p)
        self.assertEqual(s["action_key"], "hold")
        self.assertEqual(s["action"], "持有观察")

    def test_wait_at_min_position(self):
        p = gbt.GridParams(base_pos=0.10, min_pos=0.10)
        # dev=+4% 时目标仓位 = 0.10（下限），当前已在下限 → 空仓等待
        s = gs.grid_action(0.04, 0.10, p)
        self.assertEqual(s["action_key"], "wait")

    def test_hold_at_max_position(self):
        p = gbt.GridParams(max_pos=1.0)
        s = gs.grid_action(-0.30, 1.0, p)
        self.assertEqual(s["action_key"], "hold")
        self.assertIn("满仓", s["action"])

    def test_no_data(self):
        p = gbt.GridParams()
        s = gs.grid_action(None, None, p)
        self.assertEqual(s["action_key"], "hold")
        self.assertIn("数据不足", s["action"])

    def test_priority_order(self):
        order = {"clear": 0, "buy": 1, "reduce": 2, "hold": 3, "wait": 4}
        p = gbt.GridParams()
        for key in order:
            s = gs.grid_action(0.0, 0.7, p)
            s["action_key"] = key
        # 构造一个含全部操作类型的信号列表排序
        sigs = [
            {"action_key": "hold", "is_holding": False},
            {"action_key": "clear", "is_holding": False},
            {"action_key": "buy", "is_holding": False},
            {"action_key": "clear", "is_holding": True},
        ]
        sigs.sort(key=lambda x: (0 if x["is_holding"] else 1, order[x["action_key"]]))
        self.assertEqual(sigs[0]["action_key"], "clear")  # 持仓优先
        self.assertEqual(sigs[1]["action_key"], "clear")
        self.assertEqual(sigs[2]["action_key"], "buy")


if __name__ == "__main__":
    unittest.main()
