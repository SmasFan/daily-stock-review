"""数据提供与数据映射单元测试：腾讯行情解析 / 温度计 / 跟踪快照。"""
import unittest

from src import data_provider as dp
from src import report as rp
from src import analyzer as az
from build_tracking import top10_buys, track_return


class TestParseQuote(unittest.TestCase):

    @staticmethod
    def build_raw():
        """构造腾讯行情字符串，字段位与 parse_quote 对齐：
        p[3]=现价 p[4]=昨收 p[32]=涨跌% p[33]=高 p[34]=低 p[36]=量
        p[37]=成交额(万) p[38]=换手 p[39]=PE p[46]=PB p[51]=量比"""
        p = [""] * 60
        p[0], p[1], p[2] = "1", "浦发银行", "600000"
        p[3], p[4], p[5] = "10.50", "10.40", "10.45"
        p[32], p[33], p[34] = "0.96", "10.59", "10.38"
        p[36], p[37] = "12345", "67890.00"
        p[38], p[39], p[46] = "1.50", "28.61", "6.05"
        p[51] = "2.30"
        return 'v_sh600000="' + "~".join(p) + '";'

    def test_parse_basic(self):
        q = dp.parse_quote(self.build_raw(), "600000")
        self.assertIsNotNone(q)
        self.assertEqual(q["name"], "浦发银行")
        self.assertAlmostEqual(q["price"], 10.50)
        self.assertAlmostEqual(q["change"], 0.96)
        self.assertAlmostEqual(q["pe"], 28.61)
        self.assertAlmostEqual(q["pb"], 6.05)
        self.assertAlmostEqual(q["turnover"], 1.50)
        self.assertAlmostEqual(q["volumeRatio"], 2.30)
        self.assertEqual(q["amount"], 67890.00 * 10000)

    def test_tencent_symbol(self):
        self.assertEqual(dp.tencent_symbol("600000"), "sh600000")
        self.assertEqual(dp.tencent_symbol("000001"), "sz000001")
        self.assertEqual(dp.tencent_symbol("510300"), "sh510300")
        self.assertEqual(dp.tencent_symbol("159915"), "sz159915")
        self.assertEqual(dp.tencent_symbol("920725"), "bj920725")

    def test_parse_missing(self):
        self.assertIsNone(dp.parse_quote("v_sh000000=", "000000"))


def make_results(pcts, keys):
    out = []
    for i, (p, k) in enumerate(zip(pcts, keys)):
        r = az.AnalysisResult(name=f"S{i}", change_pct=p, signal_key=k, score=50)
        out.append(r)
    return out


class TestMarketTemperature(unittest.TestCase):

    def test_fallback_watchlist(self):
        results = make_results([1.0, -1.0, 2.0], ["buy", "sell", "buy"])
        t = rp._market_temperature(results)
        self.assertEqual(t["source"], "watchlist")
        self.assertAlmostEqual(t["breadth"], 66.7, places=1)
        self.assertTrue(0 <= t["score"] <= 100)

    def test_market_breadth_source(self):
        results = make_results([1.0, -1.0, 2.0], ["buy", "sell", "buy"])
        breadth = {"up": 3000, "down": 2000, "flat": 100, "total": 5100, "source": "market"}
        t = rp._market_temperature(results, breadth)
        self.assertEqual(t["source"], "market")
        self.assertAlmostEqual(t["breadth"], 3000 / 5100 * 100, places=1)
        self.assertEqual(t["market_up"], 3000)
        self.assertEqual(t["market_total"], 5100)

    def test_stale_market(self):
        results = make_results([1.0], ["buy"])
        breadth = {"up": 100, "down": 100, "flat": 0, "total": 200,
                   "source": "market", "stale": True}
        t = rp._market_temperature(results, breadth)
        self.assertEqual(t["source"], "market-stale")

    def test_empty(self):
        t = rp._market_temperature([])
        self.assertEqual(t["score"], 50)
        self.assertEqual(t["label"], "数据不足")


class TestBuildReview(unittest.TestCase):

    def test_market_regime_passthrough(self):
        results = make_results([2.0, -1.0], ["buy", "watch"])
        regime = {"overheat": True, "threshold": 0.65,
                  "breadth_up_ratio": 0.8, "breadth_source": "全市场",
                  "benchmark_change": 1.5, "downgraded_count": 1,
                  "downgraded": ["S0"], "note": "普涨过热日"}
        rv = rp.build_review("A股", results, "post", market_regime=regime)
        self.assertEqual(rv["market_regime"], regime)
        self.assertTrue(rv["market_regime"]["overheat"])

    def test_market_regime_none(self):
        results = make_results([1.0], ["buy"])
        rv = rp.build_review("A股", results, "post")
        self.assertIsNone(rv["market_regime"])

    def test_review_structure(self):
        results = make_results([2.0, -1.0], ["buy", "watch"])
        rv = rp.build_review("A股", results, "post")
        self.assertEqual(rv["stats"]["total"], 2)
        self.assertEqual(len(rv["items"]), 2)
        self.assertEqual(rv["stats"]["strongest"], "S0")


class TestTracking(unittest.TestCase):

    def test_top10_buys(self):
        picks = [
            {"signal_key": "sell", "name": "s"},
            {"signal_key": "buy", "name": "b1"},
            {"signal_key": "strong_buy", "name": "b2"},
            {"signal_key": "watch", "name": "w"},
        ] * 5
        out = top10_buys(picks)
        self.assertLessEqual(len(out), 10)
        for it in out:
            self.assertIn(it["signal_key"], ("buy", "strong_buy"))

    def test_top10_buys_fallback(self):
        picks = [{"signal_key": "watch", "name": f"w{i}"} for i in range(12)]
        out = top10_buys(picks)
        self.assertEqual(len(out), 10)

    def test_track_return(self):
        kline = {"dates": ["2026-01-01", "2026-01-02", "2026-01-03"],
                 "closes": [100.0, 110.0, 121.0]}
        self.assertAlmostEqual(track_return(kline, "2026-01-01"), 21.0)
        self.assertAlmostEqual(track_return(kline, "2026-01-03"), 0.0)
        self.assertIsNone(track_return({}, "2026-01-01"))


if __name__ == "__main__":
    unittest.main()
