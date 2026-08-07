"""技术指标单元测试（零三方依赖，unittest）。"""
import unittest

from src import indicators as ind


class TestIndicators(unittest.TestCase):

    def test_sma(self):
        v = [1, 2, 3, 4, 5, 6]
        out = ind.sma(v, 3)
        self.assertEqual(out[:2], [None, None])
        self.assertAlmostEqual(out[2], 2.0)
        self.assertAlmostEqual(out[3], 3.0)
        self.assertAlmostEqual(out[5], 5.0)

    def test_ema_adjust_false(self):
        v = [1, 2, 3]
        out = ind.ema(v, 2)  # alpha = 2/3
        self.assertAlmostEqual(out[0], 1.0)
        self.assertAlmostEqual(out[1], 2.0 / 3 * 2 + 1.0 / 3 * 1)
        self.assertAlmostEqual(out[2], 2.0 / 3 * 3 + 1.0 / 3 * out[1])

    def test_macd_len_and_bar(self):
        import random
        random.seed(7)
        v = [100 + random.uniform(-2, 2) for _ in range(60)]
        dif, dea, bar = ind.macd(v)
        self.assertEqual(len(dif), len(v))
        self.assertAlmostEqual(bar[10], (dif[10] - dea[10]) * 2)

    def test_rsi_uptrend_high(self):
        v = list(range(1, 60))  # 单调上涨 → RSI≈100
        r = ind.rsi(v, 14)
        self.assertGreater(r[-1], 95)

    def test_rsi_downtrend_low(self):
        v = list(range(60, 1, -1))
        r = ind.rsi(v, 14)
        self.assertLess(r[-1], 5)

    def test_rsi_flat_50(self):
        v = [100.0] * 40
        self.assertEqual(ind.rsi(v, 14)[-1], 50.0)

    def test_bollinger_bounds(self):
        v = [10.0 + i * 0.1 for i in range(40)]  # 带方差的序列
        mid, up, low = ind.bollinger(v, 20)
        self.assertIsNone(mid[18])
        self.assertAlmostEqual(mid[19], 10.95)
        self.assertGreater(up[19], mid[19])
        self.assertLess(low[19], mid[19])

    def test_bias(self):
        self.assertAlmostEqual(ind.bias(110, 100), 10.0)
        self.assertIsNone(ind.bias(10, None))
        self.assertIsNone(ind.bias(10, 0))

    def test_volume_ratio(self):
        vols = [100.0] * 6
        self.assertAlmostEqual(ind.volume_ratio(vols, 5), 1.0)
        vols2 = [100.0] * 5 + [300.0]
        self.assertAlmostEqual(ind.volume_ratio(vols2, 5), 3.0)
        self.assertIsNone(ind.volume_ratio(vols, 1))

    def test_atr_constant_prices_zero(self):
        highs = [10.0] * 30
        lows = [9.0] * 30
        closes = [9.5] * 30
        a = ind.atr(highs, lows, closes, 14)
        self.assertIsNotNone(a[-1])
        self.assertAlmostEqual(a[-1], 1.0)  # 高-低 = 1 恒定

    def test_adx_length_and_limits(self):
        import random
        random.seed(3)
        n = 100
        closes, highs, lows = [], [], []
        px = 100.0
        for i in range(n):
            px *= 1 + random.uniform(-0.01, 0.01)
            closes.append(px)
            highs.append(px * 1.005)
            lows.append(px * 0.995)
        a = ind.adx(highs, lows, closes, 14)
        self.assertEqual(len(a), n)
        self.assertIsNone(a[20])  # 预热期
        for v in a[50:]:
            if v is not None:
                self.assertTrue(0 <= v <= 100)

    def test_adx_trend_strong(self):
        # 单调上行 → ADX 应显著 > 25（趋势市）
        n = 200
        closes = [100 + i for i in range(n)]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        a = ind.adx(highs, lows, closes, 14)
        self.assertGreater(a[-1], 25)

    def test_pct_change(self):
        closes = [100.0, 110.0, 99.0]
        self.assertAlmostEqual(ind.pct_change(closes, 1, 1), 10.0)
        self.assertAlmostEqual(ind.pct_change(closes, 2, 2), -1.0)
        self.assertIsNone(ind.pct_change(closes, 0, 1))

    def test_max_drawdown(self):
        closes = [100, 90, 80, 85, 95]
        self.assertAlmostEqual(ind.max_drawdown(closes, 5), -20.0)
        closes2 = [80, 85, 90]
        self.assertAlmostEqual(ind.max_drawdown(closes2, 3), 0.0)


if __name__ == "__main__":
    unittest.main()
