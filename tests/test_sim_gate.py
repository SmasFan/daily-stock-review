#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大盘闸门分档（v3.12）单元测试。

背景：2026-09-11~18 连续 6 日 block，原因逐日退化为单靠一条 LLM「防御」，
期间主线涨 5%~14%、模拟盘 93.8% 现金闲置。v3.12 把「开/关」改成「仓位旋钮」：
软因素（LLM 防御 / 广度过热）只降档到 caution（半仓），硬崩盘才 block（空仓）。
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import sim_live as S  # noqa: E402


def _review(breadth=50.0, sh_signal="买入", sh_score=70, bench=0.5):
    return {
        "indices": [
            {"code": "sh000001", "change_pct": 0.5,
             "factors": {"signal": sh_signal, "score": sh_score}},
            {"code": "sh000300", "change_pct": bench},
        ],
        "temperature": {"breadth": breadth, "score": 55},
    }


def _llm(sentiment, score, age_h=1.0):
    gen = time.strftime("%Y-%m-%d %H:%M:%S",
                        time.localtime(time.time() - age_h * 3600))
    return {"generatedAt": gen, "llm": {"sentiment": sentiment, "score": score}}


class FakeJson:
    """替身：按文件名返回预设内容（未给的返回 None）。"""

    def __init__(self, macro=None, inst=None):
        self.macro = macro
        self.inst = inst

    def __call__(self, name):
        if name == "macro_llm_data.json":
            return self.macro
        if name == "institution_data.json":
            return self.inst
        return None


def _gate(review, macro=None, inst=None):
    old = S.load_json
    S.load_json = FakeJson(macro, inst)
    try:
        return S.market_gate(review, state=None, date="2026-09-18")
    finally:
        S.load_json = old


class TestGatePos(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(S.gate_pos_of("attack"), 1.0)
        self.assertEqual(S.gate_pos_of("open"), 1.0)
        self.assertEqual(S.gate_pos_of("caution"), S.CAUTION_POS)
        self.assertEqual(S.gate_pos_of("reduce"), 0.6)
        self.assertEqual(S.gate_pos_of("block"), 0.0)

    def test_unknown_is_half(self):
        self.assertEqual(S.gate_pos_of("nonsense"), 0.5)


class TestMarketGate(unittest.TestCase):
    def test_healthy_market_is_open(self):
        gate, why = _gate(_review(breadth=55), _llm("中性", 52),
                          {"overview": {"main_net": 0}})
        self.assertEqual(gate, "open")
        self.assertEqual(S.gate_pos_of(gate), 1.0)

    def test_overheat_is_caution_not_block(self):
        """普涨过热（资金涌入）只降半仓，不再一单不开 —— 本次改造的核心。"""
        gate, why = _gate(_review(breadth=82.6), _llm("中性", 52),
                          {"overview": {"main_net": 683e8}})
        self.assertEqual(gate, "caution")
        self.assertEqual(S.gate_pos_of(gate), 0.5)
        self.assertIn("普涨过热", why)

    def test_llm_defensive_is_caution(self):
        gate, why = _gate(_review(breadth=50), _llm("防御", 45),
                          {"overview": {"main_net": 0}})
        self.assertEqual(gate, "caution")
        self.assertIn("LLM宏观防御", why)

    def test_stale_llm_defensive_ignored(self):
        """对称新鲜度：过期的「防御」与过期的「多头」一样不可信。"""
        gate, why = _gate(_review(breadth=55), _llm("防御", 45, age_h=90),
                          {"overview": {"main_net": 0}})
        self.assertEqual(gate, "open")
        self.assertIn("过期", why)

    def test_attack_when_flow_and_breadth_healthy(self):
        gate, why = _gate(_review(breadth=60), _llm("中性", 52),
                          {"overview": {"main_net": 150e8}})
        self.assertEqual(gate, "attack")
        self.assertEqual(S.gate_pos_of(gate), 1.0)
        self.assertIn("进攻档", why)

    def test_hard_flat_only_when_bear_and_collapse(self):
        gate, why = _gate(_review(breadth=12, sh_signal="卖出", sh_score=30),
                          _llm("空头", 30), {"overview": {"main_net": -200e8}})
        self.assertEqual(gate, "block")
        self.assertEqual(S.gate_pos_of(gate), 0.0)

    def test_bear_without_collapse_is_caution(self):
        """上证转空但个股未崩 → 半仓，不再直接空仓。"""
        gate, why = _gate(_review(breadth=45, sh_signal="减仓", sh_score=40),
                          _llm("中性", 52), {"overview": {"main_net": 0}})
        self.assertEqual(gate, "caution")
        self.assertNotEqual(S.gate_pos_of(gate), 0.0)


class TestBreakoutPlan(unittest.TestCase):
    def test_breakout_allowed_in_open_attack_caution(self):
        it = {"code": "600584", "name": "长电科技", "close": 71.0, "ma20": 70.8,
              "resistance": 78.5, "trend_status": "强势多头",
              "score": 80, "signal": "买入"}

        def _above_of(gate):
            if (gate in S.BREAKOUT_GATES and it["close"] >= it["ma20"]
                    and it["trend_status"] in ("强势多头", "多头排列")):
                _r = it["resistance"]
                if _r and _r <= it["close"] * 1.03:
                    return round(max(it["close"] * (1 + S.BREAKOUT_PREM), _r * 1.001), 3)
                return round(it["close"] * (1 + S.BREAKOUT_PREM), 3)
            return None

        # open/attack/caution 允许突破（过热日往往没有回踩）；reduce/block 不追
        for gate, expect in (("open", True), ("attack", True),
                             ("caution", True), ("reduce", False), ("block", False)):
            self.assertEqual(_above_of(gate) is not None, expect, msg=gate)
        self.assertGreater(_above_of("open"), it["close"])
        # caution 的追高幅度更紧
        self.assertLess(S.breakout_band_of("caution"), S.breakout_band_of("open"))

    def test_breakout_uses_near_resistance_or_premium(self):
        # 前高在 3% 内 → 以突破前高为触发价
        near = {"close": 100.0, "resistance": 101.5}
        self.assertAlmostEqual(round(near["resistance"] * 1.001, 3), 101.601, places=3)
        self.assertLessEqual(near["resistance"], near["close"] * 1.03)
        # 前高太远（>3%）→ 退化为现价 +0.5%
        far = {"close": 100.0, "resistance": 120.0}
        self.assertGreater(far["resistance"], far["close"] * 1.03)
        self.assertAlmostEqual(round(far["close"] * (1 + S.BREAKOUT_PREM), 3), 100.5, places=3)

    def test_breakout_band_limits_chase(self):
        above = 74.8
        cap_open = above * (1 + S.breakout_band_of("open"))
        cap_caution = above * (1 + S.breakout_band_of("caution"))
        self.assertTrue(above + 0.1 <= cap_open)      # 刚突破 → 可买
        self.assertTrue(cap_caution < cap_open)       # caution 追高空间更小


if __name__ == "__main__":
    unittest.main()


class TestProbeBreakout(unittest.TestCase):
    """_probe_account 的买入触发：回踩线 + 突破线双通道（v3.12）。"""

    def _cfg(self):
        return S.ACCOUNTS["aggressive"]

    def _acct(self, plan):
        return {"key": "aggressive", "label": "激进", "cash": 50000.0,
                "positions": [], "plan": [plan], "trades": []}

    def _plan(self, **kw):
        base = {"code": "600584", "name": "长电科技", "status": "wait",
                "close": 100.0, "buy_below": 99.0, "buy_above": 100.5,
                "breakout_band": 0.015, "gate": "caution",
                "budget": 10000.0, "stop_atr": 95.0, "tp": 120.0}
        base.update(kw)
        return base

    def _quote(self, price, prev=98.0):
        return {"price": price, "prevClose": prev, "high": price, "low": price}

    def _run(self, plan, price, prev=98.0, live_gate=None):
        acct = self._acct(plan)
        quotes = {"600584": self._quote(price, prev)}
        _sells, buys, _notes = S._probe_account(
            "all", acct, self._cfg(), quotes, "2026-09-18", "10:00:00", {},
            live_gate=live_gate)
        return buys

    def test_breakout_triggers_above_line(self):
        buys = self._run(self._plan(), 100.6)
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["entry_type"], "突破")

    def test_breakout_band_rejects_chase(self):
        # 100.5×1.015 = 102.01 → 102.1 超出 band，不追
        self.assertEqual(self._run(self._plan(), 102.1), [])

    def test_pullback_still_works(self):
        buys = self._run(self._plan(), 98.9)
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["entry_type"], "回踩")

    def test_block_gate_blocks_all(self):
        self.assertEqual(self._run(self._plan(gate="block"), 98.9), [])

    def test_max_change_cap(self):
        # 相对昨收 98×1.08 = 105.84 → 106 不买
        self.assertEqual(self._run(self._plan(), 106.0, prev=98.0), [])

    def test_caution_band_tightens_via_live_gate(self):
        # 计划 band 3%，实时降到 caution → 用 1.5%
        p = self._plan(breakout_band=0.03)
        self.assertEqual(len(self._run(p, 100.6)), 1)
        self.assertEqual(self._run(p, 102.5, live_gate="caution"), [])


class TestBreakoutBandOf(unittest.TestCase):
    def test_caution_tighter(self):
        self.assertLess(S.breakout_band_of("caution"), S.breakout_band_of("open"))
        self.assertEqual(S.breakout_band_of("open"), S.BREAKOUT_BAND)
