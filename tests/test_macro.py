"""宏观政策与新闻情绪模块单元测试（离线：词典情感 + 主题映射 + 提醒判定）。"""
import unittest

from src import macro as mc


class TestSentiment(unittest.TestCase):

    def test_positive_news(self):
        s = mc.sentiment_of("央行宣布降准降息，市场流动性宽松，A股三大指数全线上涨")
        self.assertGreater(s["score"], 0)
        self.assertGreater(s["pos"], 0)

    def test_negative_news(self):
        s = mc.sentiment_of("上市公司业绩亏损扩大，股价大跌，资金流出")
        self.assertLess(s["score"], 0)
        self.assertGreater(s["neg"], 0)

    def test_policy_source_weight(self):
        self.assertEqual(mc.policy_source_of("国务院常务会议研究促进资本市场发展"), 1.6)
        self.assertEqual(mc.policy_source_of("国家统计局发布CPI数据"), 1.2)
        self.assertIsNone(mc.policy_source_of("某公司发布年度报告"))


class TestTheme(unittest.TestCase):

    def test_theme_hit(self):
        self.assertIn("半导体/芯片", mc.theme_of("国产芯片取得突破，半导体产业景气度回升"))
        self.assertIn("人工智能/算力", mc.theme_of("数据中心算力需求大增，大模型加速落地"))
        self.assertIn("机器人/具身智能", mc.theme_of("人形机器人量产加速，减速器需求提升"))
        self.assertIn("金融/货币政策", mc.theme_of("央行降准释放长期流动性，利好银行保险"))
        self.assertIn("房地产/基建", mc.theme_of("住建部推进城中村改造，专项债发力基建投资"))
        self.assertIn("医药/医疗", mc.theme_of("创新药获批上市，医保谈判落地"))

    def test_no_theme(self):
        self.assertEqual(mc.theme_of("今日天气晴朗适合出行"), [])

    def test_sectors_of(self):
        secs = mc._sectors_of("光伏组件价格回升，储能装机超预期")
        self.assertIn("新能源电力", secs)


class TestRiskWords(unittest.TestCase):

    def test_risk_hit(self):
        self.assertTrue(mc.risk_words_hit("公司收到监管警示函，涉嫌违规减持"))
        self.assertFalse(mc.risk_words_hit("公司业绩稳步增长，股价创年内新高"))

    def test_survey_guard(self):
        # 统计口径"调查显示"不判风险
        a = mc.analyze_news({"title": "央行调查显示企业信心回升", "summary": ""})
        self.assertNotIn("调查", a["risks"])

    def test_tariff_rebate_guard(self):
        a = mc.analyze_news({"title": "多家上市公司宣布收到美国关税退税", "summary": ""})
        self.assertNotIn("关税", a["risks"])


class TestAnalyzeNews(unittest.TestCase):

    def test_policy_positive_bull(self):
        a = mc.analyze_news({"title": "证监会：加大中长期资金入市力度，市场信心提振",
                             "summary": "政策支持资本市场发展"})
        self.assertEqual(a["kind"], "bull")
        self.assertGreater(a["score"], 0)
        self.assertGreater(a["source_w"], 1)

    def test_risk_event(self):
        a = mc.analyze_news({"title": "XX股份被证监会立案调查，存在退市风险",
                             "summary": "公司公告收到立案告知书"})
        self.assertEqual(a["kind"], "risk")
        self.assertLess(a["score"], 0)

    def test_neutral_info(self):
        a = mc.analyze_news({"title": "某公司发布季度例行经营数据公告", "summary": ""})
        self.assertEqual(a["kind"], "info")

    def test_negative_risk(self):
        a = mc.analyze_news({"title": "行业需求下滑，产品价格持续走低", "summary": ""})
        self.assertEqual(a["kind"], "risk")


class TestAggregation(unittest.TestCase):

    def setUp(self):
        self.analyzed = [
            mc.analyze_news({"title": "证监会：加大中长期资金入市力度，市场信心提振", "summary": ""}),
            mc.analyze_news({"title": "XX股份被证监会立案调查，存在退市风险", "summary": ""}),
            mc.analyze_news({"title": "光伏组件价格回升，储能装机超预期", "summary": ""}),
            mc.analyze_news({"title": "某公司发布季度例行公告", "summary": ""}),
        ]

    def test_build_alerts(self):
        alerts = mc.build_alerts(self.analyzed)
        self.assertTrue(alerts["bulls"])
        self.assertTrue(alerts["risks"])
        for b in alerts["bulls"]:
            self.assertEqual(b["kind"], "bull")
        for r in alerts["risks"]:
            self.assertEqual(r["kind"], "risk")

    def test_theme_scores(self):
        themes = mc.build_theme_scores(self.analyzed)
        names = {t["name"] for t in themes}
        self.assertIn("金融/货币政策", names)
        self.assertIn("新能源", names)

    def test_sector_impact(self):
        alerts = mc.build_alerts(self.analyzed)
        secs = mc.build_sector_impact(alerts, self.analyzed)
        by_name = {s["sector"]: s for s in secs}
        self.assertIn("红利金融", by_name)
        self.assertGreater(by_name["红利金融"]["net"], 0)
        self.assertIn("新能源电力", by_name)


if __name__ == "__main__":
    unittest.main()
