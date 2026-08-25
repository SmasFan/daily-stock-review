"""分析器单元测试：趋势判断 / 评分 / 信号映射 / 买卖点位。"""
import unittest

from src import analyzer as az


def make_series(mode: str, n: int = 120, start: float = 100.0):
    """生成合成K线：uptrend / downtrend / flat / volatile。"""
    closes = []
    if mode == "uptrend":
        for i in range(n):
            closes.append(start + i * 1.0)
    elif mode == "downtrend":
        for i in range(n):
            closes.append(start - i * 1.0)
    elif mode == "flat":
        closes = [start] * n
    else:  # volatile 震荡
        import math
        for i in range(n):
            closes.append(start + math.sin(i / 3.0) * 5)
    opens = [c for c in closes]
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    vols = [1000000.0] * n
    dates = [f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}" for i in range(n)]
    return dates, opens, closes, highs, lows, vols


class TestAnalyzer(unittest.TestCase):

    def test_uptrend_result(self):
        dates, o, c, h, l, v = make_series("uptrend")
        r = az.analyze_stock("测试", dates, o, c, h, l, v, code="600000")
        self.assertIsNotNone(r)
        self.assertIn(r.trend_status, ("强势多头", "多头排列"))
        self.assertGreater(r.score, 60)
        self.assertIn(r.signal_key, ("strong_buy", "buy"))
        self.assertTrue(r.ma5 > r.ma10 > r.ma20)  # 多头排列：短均线在上

    def test_downtrend_result(self):
        dates, o, c, h, l, v = make_series("downtrend")
        r = az.analyze_stock("测试", dates, o, c, h, l, v)
        self.assertIsNotNone(r)
        self.assertIn(r.trend_status, ("空头排列", "强势空头", "弱势空头"))
        self.assertIn(r.signal_key, ("sell", "reduce"))
        self.assertLess(r.score, 40)

    def test_short_data_none(self):
        dates, o, c, h, l, v = make_series("flat", n=20)
        self.assertIsNone(az.analyze_stock("测试", dates, o, c, h, l, v))

    def test_buy_points(self):
        dates, o, c, h, l, v = make_series("uptrend")
        r = az.analyze_stock("测试", dates, o, c, h, l, v)
        self.assertIsNotNone(r.ideal_buy)
        self.assertIsNotNone(r.secondary_buy)
        self.assertIsNotNone(r.stop_loss)
        self.assertGreater(r.take_profit, r.close)

    def test_atr_stop(self):
        dates, o, c, h, l, v = make_series("volatile")
        r = az.analyze_stock("测试", dates, o, c, h, l, v)
        self.assertIsNotNone(r.atr14)
        self.assertIsNotNone(r.atr_stop)
        self.assertLess(r.atr_stop, r.close)

    def test_signal_scale(self):
        self.assertEqual(az.signal_key_for_score(85), "strong_buy")
        self.assertEqual(az.signal_key_for_score(68), "buy")
        self.assertEqual(az.signal_key_for_score(60), "watch")
        self.assertEqual(az.signal_key_for_score(40), "watch")
        self.assertEqual(az.signal_key_for_score(25), "reduce")
        self.assertEqual(az.signal_key_for_score(10), "sell")
        self.assertEqual(az.signal_label_for_key("strong_buy"), "强烈买入")


if __name__ == "__main__":
    unittest.main()
