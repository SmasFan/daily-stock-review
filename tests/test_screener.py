"""选股器单元测试：横截面打分 / 硬过滤 / 板块内价值分位 / 理由生成。"""
import unittest

from src import screener as scr


def make_item(name, code, price, amount=1e8, pe=None, pb=None, turnover=2.0,
              volume_ratio=1.0, change_pct=1.0, change_60d=10.0,
              tech_score=60.0, signal_key="buy", signal="买入",
              trend_status="多头排列", sector="银行",
              value_score=50.0, stability_score=50.0,
              ideal_buy=None, stop_loss=None):
    it = scr.ScreenItem(
        name=name, code=code, sector=sector, price=price, amount=amount,
        turnover=turnover, pe=pe, pb=pb, volume_ratio=volume_ratio,
        change_pct=change_pct, change_60d=change_60d,
        tech_score=tech_score, signal_key=signal_key, signal=signal,
        trend_status=trend_status,
    )
    it.value_score = value_score
    it.stability_score = stability_score
    it.ideal_buy = ideal_buy
    it.stop_loss = stop_loss
    return it


class TestScreener(unittest.TestCase):

    def test_rank_pct_basic(self):
        vals = [100, 200, 300]
        r = scr.rank_pct(vals, higher_is_better=True)
        self.assertEqual(r[0], 0.0)   # 最小 → 0
        self.assertEqual(r[2], 100.0)  # 最大 → 100
        self.assertEqual(r[1], 50.0)

    def test_rank_pct_none(self):
        vals = [None, 100, 300]
        r = scr.rank_pct(vals, higher_is_better=True)
        self.assertEqual(r[0], 25.0)   # None 给 25
        self.assertEqual(r[2], 100.0)

    def test_rank_pct_empty(self):
        self.assertEqual(scr.rank_pct([]), [])

    def test_hard_filter_st_price(self):
        items = [
            make_item("ST危险", "000001", 5.0),
            make_item("退市股", "000002", 3.0),
            make_item("正常", "000003", 0.0),
            make_item("好股", "000004", 10.0),
        ]
        out = scr.screen(items, top_n=10)
        names = [it.name for it in out]
        self.assertEqual(names, ["好股"])

    def test_value_rank_sector_inside(self):
        # 银行板块内：低PE 得高分；板块样本≥5 时按板块内分位
        items = [make_item(f"银{i}", f"60{i:04d}", 10.0, pe=pe, sector="银行")
                 for i, pe in enumerate([4, 5, 6, 7, 8, 9])]
        items[0].pb = 0.5
        items[1].pb = 0.6
        items[2].pb = 0.7
        items[3].pb = 0.8
        items[4].pb = 0.9
        items[5].pb = 1.0
        for i in range(6):
            items[i].turnover = 2.0
            items[i].amount = 1e8 + i * 1e6
        out = scr.screen(items, top_n=10)
        by_name = {it.name: it for it in out}
        # 最低PE 银行（银0）应排最高价值分
        v0 = by_name["银0"].value_score
        v5 = by_name["银5"].value_score
        self.assertGreater(v0, v5)

    def test_total_score_blend(self):
        a = make_item("A", "1", 10.0, tech_score=80)
        b = make_item("B", "2", 10.0, tech_score=40)
        out = scr.screen([a, b], tech_weight=0.5, top_n=10)
        self.assertGreater(out[0].total_score, out[1].total_score)
        # 权重校验：50%技术 + 50%横截面
        self.assertAlmostEqual(a.total_score, round(a.tech_score * 0.5 + a.cross_score * 0.5, 1))

    def test_build_buy_reason(self):
        it = make_item("A", "1", 10.0, signal_key="strong_buy", signal="强烈买入",
                       tech_score=82, change_60d=8.0, value_score=75, stability_score=75,
                       ideal_buy=9.9, stop_loss=9.5)
        r = scr.build_buy_reason(it)
        self.assertIn("强烈买入", r)
        self.assertIn("止损位9.5", r)
        self.assertIn("估值处于池内低位", r)

    def test_rating_of(self):
        self.assertEqual(scr.rating_of(85), "A")
        self.assertEqual(scr.rating_of(60), "B")
        self.assertEqual(scr.rating_of(45), "C")
        self.assertEqual(scr.rating_of(25), "D")
        self.assertEqual(scr.rating_of(5), "E")

    def test_signal_sort_key(self):
        items = [
            make_item("卖", "1", 10.0, signal_key="sell"),
            make_item("买", "2", 10.0, signal_key="buy"),
            make_item("强买", "3", 10.0, signal_key="strong_buy"),
        ]
        items.sort(key=scr.strength_key)
        self.assertEqual([it.name for it in items], ["强买", "买", "卖"])

    def test_market_regime_normal_day(self):
        items = [
            make_item("A", "1", 10.0, change_pct=1.0, signal_key="buy"),
            make_item("B", "2", 10.0, change_pct=-0.5, signal_key="buy"),
        ]
        r = scr.apply_market_regime(items, 0.50, 0.5)  # 广度50%，非过热
        self.assertFalse(r["overheat"])
        self.assertEqual(items[0].signal_key, "buy")
        self.assertEqual(items[1].signal_key, "buy")
        self.assertEqual(r["downgraded"], [])

    def test_market_regime_overheat_downgrades_underperformer(self):
        items = [
            make_item("跑赢", "1", 10.0, change_pct=2.5, signal_key="strong_buy"),
            make_item("跑平", "2", 10.0, change_pct=1.5, signal_key="buy"),
            make_item("落后", "3", 10.0, change_pct=0.5, signal_key="buy"),
            make_item("观望者", "4", 10.0, change_pct=3.0, signal_key="watch"),
        ]
        r = scr.apply_market_regime(items, 0.80, 1.5)  # 广度80% 过热，指数+1.5%
        self.assertTrue(r["overheat"])
        self.assertEqual(items[0].signal_key, "strong_buy")  # 跑赢 → 保留
        self.assertEqual(items[1].signal_key, "buy")         # 跑平 → 保留
        self.assertEqual(items[2].signal_key, "watch")       # 落后 → 降为观望
        self.assertTrue(items[2].overheat_downgraded)
        self.assertFalse(items[0].overheat_downgraded)
        self.assertEqual(items[3].signal_key, "watch")       # 本来观望 → 不动
        self.assertEqual(r["downgraded"], ["落后"])

    def test_market_regime_missing_index(self):
        items = [make_item("A", "1", 10.0, change_pct=1.0, signal_key="buy")]
        r = scr.apply_market_regime(items, 0.80, None)  # 指数缺失 → 不干预
        self.assertFalse(r["overheat"])
        self.assertEqual(items[0].signal_key, "buy")

    def test_market_regime_cold_downgrades_buy(self):
        items = [
            make_item("强买", "1", 10.0, change_pct=-1.0, signal_key="strong_buy"),
            make_item("买", "2", 10.0, change_pct=-0.5, signal_key="buy"),
            make_item("观望者", "3", 10.0, change_pct=1.0, signal_key="watch"),
        ]
        r = scr.apply_market_regime(items, 0.20, -1.0, cold_temp=15)  # 温度15<20 急跌低温
        self.assertTrue(r["cold"])
        self.assertEqual(items[0].signal_key, "strong_buy")  # 强买保留
        self.assertEqual(items[1].signal_key, "watch")       # buy 降观望
        self.assertTrue(items[1].overheat_downgraded)
        self.assertEqual(items[2].signal_key, "watch")
        self.assertEqual(r["downgraded"], ["买"])

    def test_market_regime_warm_day_no_downgrade(self):
        items = [make_item("A", "1", 10.0, change_pct=1.0, signal_key="buy")]
        r = scr.apply_market_regime(items, 0.50, 0.5, cold_temp=55)  # 温度正常 → 不干预
        self.assertFalse(r["cold"])
        self.assertEqual(items[0].signal_key, "buy")

    def test_buy_reason_overheat_suffix(self):
        it = make_item("A", "1", 10.0, signal_key="buy", signal="买入",
                       tech_score=70, trend_status="多头排列")
        it.overheat_downgraded = True
        it.signal_key = "watch"
        r = scr.build_buy_reason(it)
        self.assertIn("降为观望", r)
        self.assertIn("暂缓追高", r)


if __name__ == "__main__":
    unittest.main()
