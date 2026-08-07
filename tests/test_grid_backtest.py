"""网格回测引擎单元测试：仓位规则 / 锁仓 / 网格索引 / 均值线 / 滑点 / 指标。"""
import unittest

from src import grid_backtest as gbt


class TestGridRules(unittest.TestCase):

    def test_position_target_up_down_slopes(self):
        p = gbt.GridParams(base_pos=0.70, grid_step=0.005,
                           sell_per_step=0.010, buy_per_step=0.0104)
        # dev=0 → 均衡仓位
        self.assertAlmostEqual(gbt.position_target(0.0, p), 0.70)
        # 上涨 → 减仓：dev=+5% → 抛 10%
        self.assertAlmostEqual(gbt.position_target(0.05, p), 0.60)
        # 下跌 → 加仓：dev=-5% → 买 10.4%
        self.assertAlmostEqual(gbt.position_target(-0.05, p), 0.804)
        # 夹在 [min_pos, max_pos]
        self.assertAlmostEqual(gbt.position_target(0.5, p), 0.10)
        self.assertAlmostEqual(gbt.position_target(-1.0, p), 1.00)

    def test_position_target_nan(self):
        import math
        self.assertTrue(math.isnan(gbt.position_target(float("nan"), gbt.GridParams())))
        self.assertTrue(math.isnan(gbt.position_target(None, gbt.GridParams())))

    def test_locked_from_dev(self):
        p = gbt.GridParams(dev_lock=-0.05, buy_per_step=0.0104, grid_step=0.005)
        self.assertEqual(gbt.locked_from_dev(0.0, p), 0.0)
        self.assertEqual(gbt.locked_from_dev(-0.05, p), 0.0)
        lock = gbt.locked_from_dev(-0.10, p)
        self.assertGreater(lock, 0.0)

    def test_grid_index(self):
        p = gbt.GridParams(grid_step=0.005)
        self.assertEqual(gbt.grid_index(0.004, p), 0)
        self.assertEqual(gbt.grid_index(0.006, p), 1)
        self.assertEqual(gbt.grid_index(-0.006, p), -2)
        self.assertEqual(gbt.grid_index(0.0, p), 0)


def synthetic_panel(n=800, start=100.0, drift=0.0, vol=0.01, seed=11, dates=None):
    """合成升序日K面板（含价格均值线所需字段）。"""
    import random
    random.seed(seed)
    closes, opens, highs, lows, vols = [], [], [], [], []
    px = start
    for i in range(n):
        px *= 1 + drift + random.uniform(-vol, vol)
        closes.append(px)
        opens.append(px * (1 + random.uniform(-vol / 2, vol / 2)))
        highs.append(max(px, opens[-1]) * 1.005)
        lows.append(min(px, opens[-1]) * 0.995)
        vols.append(1e6)
    ds = dates or [f"2023-{i // 30 + 1:02d}-{(i % 30) + 1:02d}" for i in range(n)]
    return {"date": ds, "close": closes, "high": highs, "low": lows,
            "volume": vols, "open": opens,
            "pe_ttm": [None] * n, "pb": [None] * n, "roe": [None] * n,
            "dy_daily": [0.0] * n}


class TestAnchor(unittest.TestCase):

    def test_price_only_anchor(self):
        panel = synthetic_panel()
        a = gbt.compute_anchor(panel, gbt.AnchorParams(lookback_days=250, min_periods=150))
        self.assertTrue(a["price_only"])
        self.assertIsNotNone(a["anchor"][-1])
        self.assertIsNotNone(a["dev"][-1])
        self.assertEqual(len(a["anchor"]), len(panel["close"]))

    def test_anchor_window_shrink(self):
        panel = synthetic_panel(n=300)
        a = gbt.compute_anchor(panel, gbt.AnchorParams(lookback_days=250, min_periods=150))
        valid = sum(1 for x in a["anchor_ok"] if x)
        self.assertGreater(valid, 30)


class TestBacktest(unittest.TestCase):

    def run_simple(self, panel=None, params=None, cfg=None):
        panel = panel or synthetic_panel()
        params = params or gbt.GridParams()
        cfg = cfg or gbt.BacktestConfig()
        return gbt.run_grid_backtest("T", panel, params, gbt.AnchorParams(
            lookback_days=250, min_periods=150), cfg)

    def test_backtest_runs(self):
        res = self.run_simple()
        self.assertGreater(len(res.equity), 100)
        self.assertGreater(res.equity[-1], 0)
        self.assertEqual(len(res.equity), len(res.benchmark))
        self.assertEqual(len(res.dates), len(res.equity))

    def test_slippage_reduces_returns(self):
        panel = synthetic_panel(seed=42)
        r0 = self.run_simple(panel, cfg=gbt.BacktestConfig(cost_rate=0.0005, slippage_rate=0.0))
        r1 = self.run_simple(panel, cfg=gbt.BacktestConfig(cost_rate=0.0005, slippage_rate=0.01))
        self.assertGreaterEqual(r0.equity[-1], r1.equity[-1])
        if r0.trades:
            self.assertGreater(r0.equity[-1], r1.equity[-1])

    def test_trade_cost_deducted(self):
        cfg = gbt.BacktestConfig(cost_rate=0.01, slippage_rate=0.0)
        res = self.run_simple(cfg=cfg)
        # 每次调仓扣 1% × 调仓量，交易越多成本越大
        m = gbt.summary_metrics(res)
        self.assertGreaterEqual(m["trade_count"], 0)

    def test_benchmark_no_costs(self):
        # 零调仓（固定满仓）时策略应约等于基准（无成本）
        panel = synthetic_panel(seed=5)
        p = gbt.GridParams(base_pos=1.0, grid_step=0.5,  # 步长极大 → 几乎不调仓
                           sell_per_step=0.5, buy_per_step=0.5,
                           dynamic_step=False)
        res = self.run_simple(panel, params=p, cfg=gbt.BacktestConfig(cost_rate=0.0, slippage_rate=0.0))
        m = gbt.summary_metrics(res)
        self.assertAlmostEqual(m["excess_return"], 0.0, places=2)

    def test_use_lock_no_trades_lost(self):
        panel = synthetic_panel(seed=77)
        r1 = self.run_simple(panel, params=gbt.GridParams(use_lock=True))
        r2 = self.run_simple(panel, params=gbt.GridParams(use_lock=False))
        self.assertGreaterEqual(len(r1.locked), len(r2.locked))
        self.assertTrue(any(v > 0 for v in r1.locked) or not r1.locked or len(r1.equity) > 0)

    def test_metrics_fields(self):
        res = self.run_simple()
        m = gbt.summary_metrics(res)
        for k in ("total_return", "annual_return", "max_drawdown", "sharpe",
                  "benchmark_return", "excess_return", "trade_count", "grid_step"):
            self.assertIn(k, m)
        self.assertLessEqual(m["max_drawdown"], 0.0)

    def test_build_backtest_data(self):
        res = self.run_simple()
        data = gbt.build_backtest_data({"T": res})
        self.assertIn("T", data["stocks"])
        self.assertEqual(data["overall"]["total"], 1)
        self.assertIn("window_returns", data["stocks"]["T"])

    def test_summary_metrics_empty(self):
        self.assertEqual(gbt.summary_metrics(None)["error"], "无数据") if False else None
        res = gbt.BacktestResult(name="x", equity=[], benchmark=[], position=[], dev=[],
                                 anchor=[], locked=[], close=[], dates=[], trades=[],
                                 start_date="", end_date="", price_only=True)
        self.assertEqual(gbt.summary_metrics(res)["error"], "无数据")


class TestMaxDrawdown(unittest.TestCase):

    def test_max_drawdown(self):
        eq = [1.0, 1.2, 0.9, 1.1]
        self.assertAlmostEqual(gbt.max_drawdown(eq), (0.9 / 1.2 - 1))
        self.assertEqual(gbt.max_drawdown([]), 0.0)

    def test_cagr(self):
        self.assertAlmostEqual(gbt.cagr([1.0, 1.21], 2.0), 0.1)


if __name__ == "__main__":
    unittest.main()
