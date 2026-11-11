#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""美股模拟盘网页快照的离线回归测试（不访问行情接口）。"""
import json
import os
import tempfile
import unittest
from unittest import mock

from usim import broker, config, web


def make_bars(count=90):
    bars = []
    for i in range(count):
        close = 100.0 + i * 0.5
        bars.append({"date": "2026-%02d-%02d" % (i // 28 + 1, i % 28 + 1),
                     "open": close - 0.2, "close": close,
                     "high": close + 1, "low": close - 1, "volume": 1000})
    return bars


class TestUsSimWeb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ussim_test_")
        root = self.tmp.name
        self.paths = {
            "DATA_DIR": root,
            "STATE_FILE": os.path.join(root, "state.json"),
            "WEB_JSON": os.path.join(root, "web.json"),
            "BACKTEST_FILE": os.path.join(root, "missing-backtest.json"),
            "QUOTE_CACHE": os.path.join(root, "quotes.json"),
            "KLINE_CACHE": os.path.join(root, "kline.json"),
            "LOG_FILE": os.path.join(root, "usim.log"),
        }
        self.patchers = [mock.patch.object(config, key, value) for key, value in self.paths.items()]
        for p in self.patchers:
            p.start()
        broker.save(broker.new_state())
        self.bars = {s: make_bars() for s in config.UNIVERSE}
        self.quotes = {s: {"price": 150.0, "chg_pct": 1.2, "name": s, "ts": 1}
                       for s in config.UNIVERSE + [config.BENCH]}

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    def test_empty_account_snapshot_is_complete(self):
        data = web.export_snapshot(quotes=self.quotes, bars_map=self.bars,
                                   bench_bars=make_bars())
        self.assertEqual(data["account"]["equity"], 100.0)
        self.assertEqual(data["account"]["position_count"], 0)
        self.assertEqual(len(data["pool"]), len(config.UNIVERSE))
        self.assertTrue(all(p["close"] for p in data["pool"]))
        self.assertEqual(data["errors"], [])
        self.assertTrue(data["market"]["regime_ok"])
        with open(config.WEB_JSON, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["meta"]["mode"], "paper")
        self.assertEqual(saved["meta"]["currency"], "USD")

    def test_position_is_marked_with_latest_quote(self):
        st = broker.load()
        st["cash"] = 50.0
        st["positions"]["AAPL"] = {
            "qty": 0.5, "avg": 100.0, "stop": 90.0, "stop0": 90.0,
            "peak": 105.0, "atr": 3.0, "opened": "2026-11-06 09:30:00",
        }
        broker.save(st)
        quotes = dict(self.quotes)
        quotes["AAPL"] = {"price": 110.0, "chg_pct": 2.0, "name": "苹果", "ts": 1}
        data = web.build_snapshot(quotes=quotes, bars_map=self.bars,
                                  bench_bars=make_bars())
        self.assertEqual(data["account"]["equity"], 105.0)
        self.assertEqual(data["account"]["market_value"], 55.0)
        self.assertEqual(data["account"]["unrealized"], 5.0)
        self.assertAlmostEqual(data["account"]["ret_pct"], 5.0)
        self.assertEqual(data["positions"][0]["last"], 110.0)
        self.assertEqual(data["positions"][0]["upnl"], 5.0)

    def test_missing_quote_falls_back_to_position_price(self):
        st = broker.load()
        st["cash"] = 80.0
        st["positions"]["AAPL"] = {
            "qty": 0.2, "avg": 100.0, "last": 105.0,
            "stop": 90.0, "stop0": 90.0, "opened": "2026-11-06 09:30:00",
        }
        broker.save(st)
        data = web.build_snapshot(quotes={}, bars_map={}, bench_bars=[])
        self.assertEqual(data["positions"][0]["last"], 105.0)
        self.assertEqual(data["account"]["equity"], 101.0)
        self.assertEqual(data["candidates"], [])


if __name__ == "__main__":
    unittest.main()
