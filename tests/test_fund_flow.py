"""资金流与机构数据模块单元测试（离线：分类逻辑 + 解析逻辑）。"""
import unittest
from unittest import mock

from src import fund_flow as ff


class TestClassifyHolder(unittest.TestCase):

    def test_national_team(self):
        self.assertEqual(ff.classify_holder("中央汇金资产管理有限责任公司"), "central_hj")
        self.assertEqual(ff.classify_holder("中国证券金融股份有限公司"), "central_zj")
        self.assertEqual(ff.classify_holder("全国社保基金一零二组合"), "social_security")
        self.assertEqual(ff.classify_holder("基本养老保险基金一六零二二组合"), "pension")

    def test_other_institutions(self):
        self.assertEqual(ff.classify_holder("香港中央结算有限公司"), "qfii")
        self.assertEqual(ff.classify_holder("中国人寿保险股份有限公司"), "insurance")
        self.assertEqual(ff.classify_holder("易方达蓝筹精选混合型基金"), "fund")
        self.assertIsNone(ff.classify_holder("张三"))


class TestFflowParse(unittest.TestCase):

    @mock.patch.object(ff, "_em_get")
    def test_full_row(self, mock_get):
        mock_get.return_value = (
            '{"data":{"klines":["2026-08-07,-45229592.0,-18458356.0,63687948.0,-10177956.0,'
            '-35051636.0,-8.69,1,2,3,4,9.21,-0.86",'
            '"2026-08-06,-1000000.0,-2000000.0,3000000.0,-4000000.0,-5000000.0,-1,1,1,1,1,9.0,-1.0"]}}'
        )
        h = ff.fetch_stock_fflow_history("600000")
        self.assertIsNotNone(h)
        t = h["today"]
        self.assertEqual(t["main_net"], -45229592.0)
        self.assertEqual(t["super_net"], -35051636.0)
        self.assertEqual(t["big_net"], -10177956.0)
        self.assertEqual(t["mid_net"], 63687948.0)
        self.assertEqual(t["small_net"], -18458356.0)
        self.assertEqual(t["close"], 9.21)
        # 两日均值累计：主力 = -45229592 + (-1000000)
        self.assertEqual(h["main_net_5d"], -46229592.0)
        self.assertEqual(h["cum_source"], "eastmoney")

    @mock.patch.object(ff, "_em_get")
    def test_short_row_fallback_sina(self, mock_get):
        # 当日只有主力的短行 → 5/10日累计降级为新浪超大单口径
        mock_get.return_value = '{"data":{"klines":["2026-08-07,-1000000.0"]}}'
        with mock.patch.object(ff, "_sina_fflow_cumulative", return_value={"main_net_5d": 5.0, "main_net_10d": 10.0}):
            h = ff.fetch_stock_fflow_history("600000")
        self.assertIsNotNone(h)
        self.assertEqual(h["today"]["main_net"], -1000000.0)
        self.assertEqual(h["main_net_5d"], 5.0)
        self.assertEqual(h["cum_source"], "sina_super")

    @mock.patch.object(ff, "_em_get")
    def test_failure_returns_none(self, mock_get):
        mock_get.side_effect = RuntimeError("blocked")
        self.assertIsNone(ff.fetch_stock_fflow_history("600000"))

    @mock.patch.object(ff, "_get")
    def test_sina_cumulative(self, mock_get):
        # sina 累计解析：r0_net(超大单) 前5/10日求和
        rows = [{"opendate": f"2026-08-0{i}", "r0_net": f"{i * 1000}.0"} for i in range(1, 11)]
        import json
        mock_get.return_value = json.dumps(rows)
        s = ff._sina_fflow_cumulative("600000")
        self.assertEqual(s["main_net_5d"], 15000.0)   # 1000+2000+3000+4000+5000
        self.assertEqual(s["main_net_10d"], 55000.0)  # 1..10 求和


if __name__ == "__main__":
    unittest.main()
