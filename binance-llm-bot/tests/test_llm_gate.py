#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llm_gate 的 LLM 通道测试：Agent 队列优先 + 等不到就放行。

重点验证「免费接管」这层接线本身是否正确，不依赖网络与付费 key：
  prompt 构造 → Agent 答案解析 → 闸门写缓存；Agent 无答案 → 全部放行。
"""
import os
import sys
import types
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import llm_gate  # noqa: E402


BUY = {'id': 'buy:TSLA/USDT:USDT', 'action': 'buy', 'symbol': 'TSLA/USDT:USDT',
       'name': 'TSLA', 'price': 100.0, 'sma50': 90.0, 'sma20': 95.0, 'dev50': 11.1,
       'vs_ma20': '上方', 'd7': 2.0, 'd30': 8.0, 'lev': 2, 'why': '站上SMA50'}
PROTECT = {'id': 'sell:COIN/USDT:USDT', 'action': 'sell', 'symbol': 'COIN/USDT:USDT',
           'name': 'COIN', 'price': 106.0, 'entry': 100.0, 'pnl_pct': 6.0,
           'sma50': 95.0, 'dev50': 11.6, 'd7': 3.0, 'peak_pct': 6.4, 'dd_peak': 0.4,
           'kind': 'protect', 'why': '移动止盈'}


def _fake_agent(answer=None):
    """塞一个假的 llm_agent 模块进 sys.modules，模拟「我回答了/没回答」。"""
    mod = types.ModuleType('llm_agent')
    seen = {}

    def ask(kind, system, user, expect_json=True, wait=None, meta=None, **kw):
        seen.update({'kind': kind, 'system': system, 'user': user, 'wait': wait,
                     'meta': meta})
        return answer

    mod.ask = ask
    mod.seen = seen
    sys.modules['llm_agent'] = mod
    return mod


class TestBuildPrompt(unittest.TestCase):
    def test_returns_system_user_short(self):
        system, user, short = llm_gate._build_prompt([BUY, PROTECT], ctx_note='净值 98')
        self.assertIn('T+0', system)
        self.assertIn('利润保护', system)
        self.assertIn('净值 98', user)
        self.assertEqual(sorted(short), ['t1', 't2'])
        self.assertIn('t1 | 买 TSLA', user)
        # 利润保护单要带上峰值/回撤，并标注类型
        self.assertIn('峰值+6.4% 自峰值回撤0.4%', user)
        self.assertIn('利润保护(T+0默认放行)', user)

    def test_hard_sell_marked(self):
        hard = dict(PROTECT, kind=None, hard=True, peak_pct=None, dd_peak=None)
        _, user, _ = llm_gate._build_prompt([hard])
        self.assertIn('硬规则(不可否决)', user)
        self.assertNotIn('峰值', user)     # 非利润保护单不带峰值/回撤


class TestParse(unittest.TestCase):
    def test_by_id(self):
        _, _, short = llm_gate._build_prompt([BUY])
        out = llm_gate._parse(
            '{"decisions":[{"id":"t1","verdict":"avoid","note":"追高"}]}', short)
        self.assertEqual(out[BUY['id']]['verdict'], 'avoid')
        self.assertEqual(out[BUY['id']]['note'], '追高')

    def test_fallback_match_symbol(self):
        _, _, short = llm_gate._build_prompt([BUY])
        out = llm_gate._parse(
            '```json\n{"decisions":[{"id":"TSLA/USDT:USDT","verdict":"allow"}]}\n```',
            short)
        self.assertEqual(out[BUY['id']]['verdict'], 'allow')

    def test_empty_returns_none(self):
        _, _, short = llm_gate._build_prompt([BUY])
        self.assertIsNone(llm_gate._parse('{"decisions":[]}', short))


class TestAgentChannel(unittest.TestCase):
    def setUp(self):
        self._saved = (llm_gate.AGENT_WAIT, llm_gate.ALLOW_CLOUD)
        llm_gate.ALLOW_CLOUD = False        # 测试里绝不允许真的去付费云端
        self._mods = sys.modules.get('llm_agent')

    def tearDown(self):
        llm_gate.AGENT_WAIT, llm_gate.ALLOW_CLOUD = self._saved
        if self._mods is not None:
            sys.modules['llm_agent'] = self._mods
        else:
            sys.modules.pop('llm_agent', None)

    def test_agent_answer_used(self):
        mod = _fake_agent({'decisions': [{'id': 't1', 'verdict': 'avoid',
                                          'note': '跌破SMA20'}]})
        dec = llm_gate._call([BUY], ctx_note='x')
        self.assertEqual(mod.seen['kind'], 'bn_gate')
        self.assertEqual(dec[BUY['id']]['verdict'], 'avoid')
        # meta 里带上任务摘要，方便我在队列里一眼看懂问的是什么
        self.assertEqual(mod.seen['meta']['n'], 1)
        self.assertEqual(mod.seen['meta']['tasks'][0]['symbol'], 'TSLA/USDT:USDT')

    def test_no_answer_returns_none(self):
        _fake_agent(None)
        self.assertIsNone(llm_gate._call([BUY]))

    def test_gate_falls_back_to_allow(self):
        _fake_agent(None)
        st = {}
        dec = llm_gate.gate(st, [BUY])
        self.assertEqual(dec[BUY['id']]['verdict'], 'allow')
        self.assertEqual(dec[BUY['id']]['src'], 'fallback')

    def test_gate_caches_agent_verdict(self):
        _fake_agent({'decisions': [{'id': 't1', 'verdict': 'avoid', 'note': '弱'}]})
        st = {}
        d1 = llm_gate.gate(st, [BUY])
        self.assertEqual(d1[BUY['id']]['src'], 'llm')
        # 第二次不再问队列（把 ask 换成爆炸版，被调用就失败）
        def boom(*a, **k):
            raise AssertionError('不应重复提问')
        sys.modules['llm_agent'].ask = boom
        d2 = llm_gate.gate(st, [BUY])
        self.assertEqual(d2[BUY['id']]['verdict'], 'avoid')
        self.assertEqual(d2[BUY['id']]['src'], 'cache')


if __name__ == '__main__':
    unittest.main()
