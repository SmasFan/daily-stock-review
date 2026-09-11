#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""profit_guard 单测（纯逻辑 + 假交易所，不需要 ccxt / 网络）

跑法：python -m unittest discover -s binance-llm-bot/tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import profit_guard as pg  # noqa: E402

P_SHORT = pg.cfg('short')
P_DAILY = pg.cfg('daily')


class FakeEx:
    """只需要 ticker / ohlcv / amount_to_precision 三个接口"""

    def __init__(self, px=100.0, high=None, nd=3):
        self.px = px
        self.high = px if high is None else high
        self.nd = nd

    def fetch_ticker(self, sym):
        return {'last': self.px}

    def fetch_ohlcv(self, sym, tf, since=None, limit=None):
        return [[0, 0, self.high, self.px, self.px, 0]]

    def amount_to_precision(self, sym, amt):
        return round(amt, self.nd)


class TestPlan(unittest.TestCase):
    def test_hold_when_tiny_gain(self):
        d = pg.plan(100, 100.5, 100.5, P_SHORT)
        self.assertEqual(d['act'], 'hold')

    def test_be_lock_after_peak(self):
        d = pg.plan(100, 100.5, 102.5, P_SHORT)      # 峰值 +2.5% ≥ lock 2%
        self.assertEqual(d['act'], 'be_lock')
        self.assertGreater(d['sl'], 100)             # 保本价必须高于入场价（覆盖手续费）

    def test_be_price_covers_fee(self):
        self.assertGreater(pg.be_price(100), 100 * (1 + 2 * pg.FEE))

    def test_trim_at_target(self):
        d = pg.plan(100, 103.2, 103.2, P_SHORT)      # +3.2% ≥ tp_scale 3%
        self.assertEqual(d['act'], 'trim')
        self.assertEqual(d['ratio'], P_SHORT['scale_ratio'])

    def test_trim_only_once(self):
        d = pg.plan(100, 103.2, 103.2, P_SHORT, trimmed=True)
        self.assertNotEqual(d['act'], 'trim')

    def test_trailing_close(self):
        # 峰值 +4% → 现 +2%，回吐 2 个点 = 峰值的一半，触发
        d = pg.plan(100, 102.0, 104.0, P_SHORT)
        self.assertEqual(d['act'], 'close')
        self.assertIn('移动止盈', d['why'])

    def test_trailing_ignored_below_min(self):
        # 峰值只有 2%（< trail_min 2.5%），即使回吐够也不该触发
        d = pg.plan(100, 98.0, 102.0, P_SHORT)
        self.assertNotEqual(d['act'], 'close')

    def test_lets_winner_run(self):
        # 峰值 +10% 只回吐 4 个点（不到一半）→ 不该被扫出
        d = pg.plan(100, 106.0, 110.0, P_SHORT, trimmed=True)
        self.assertNotEqual(d['act'], 'close')
        # 但回吐到 +4%（吐掉 6 个点）就该走了
        d2 = pg.plan(100, 104.0, 110.0, P_SHORT, trimmed=True)
        self.assertEqual(d2['act'], 'close')

    def test_momentum_close(self):
        # 已减过仓，+6% 但跌破 SMA20
        d = pg.plan(100, 106.0, 106.0, P_SHORT, ma20=106.5, trimmed=True)
        self.assertEqual(d['act'], 'close')
        self.assertIn('动能止盈', d['why'])

    def test_trailing_beats_trim(self):
        # 既满足分批（+3.5%）又在回吐（峰值 +12% 吐掉 8.5 点）→ 优先落袋
        d = pg.plan(100, 103.5, 112.0, P_SHORT)
        self.assertEqual(d['act'], 'close')

    def test_daily_profile_looser(self):
        # 短线会触发的回撤，日线参数下不该动
        self.assertEqual(pg.plan(100, 100.5, 102.5, P_SHORT)['act'], 'be_lock')
        self.assertEqual(pg.plan(100, 100.5, 102.5, P_DAILY)['act'], 'hold')


class TestScan(unittest.TestCase):
    def _st(self, entry=100.0, amt=1.0, **kw):
        pos = {'amt': amt, 'entry': entry}
        pos.update(kw)
        return {'real_pos': {'X/USDT:USDT': pos}}

    def test_scan_updates_peak(self):
        st = self._st()
        d = pg.scan(FakeEx(px=101.0, high=103.0), st, 'X/USDT:USDT', profile='short')
        self.assertEqual(st['real_pos']['X/USDT:USDT']['peak'], 103.0)
        self.assertEqual(d['peak_pct'], 3.0)
        # 峰值 +3% 已回吐 2 点（超过一半）→ 落袋
        self.assertEqual(d['act'], 'close')

    def test_scan_full_close_when_remaining_below_min(self):
        st = self._st(entry=100.0, amt=0.1)          # 名义 10U，减半后 5U < 门槛 6U
        d = pg.scan(FakeEx(px=104.0, high=104.0), st, 'X/USDT:USDT',
                    profile='short', min_notional=6)
        self.assertTrue(d['full'])

    def test_scan_none_without_position(self):
        self.assertIsNone(pg.scan(FakeEx(), {'real_pos': {}}, 'X/USDT:USDT'))

    def test_env_override(self):
        os.environ['PG_TP_SCALE'] = '1.0'
        try:
            p = pg.cfg('short')
            self.assertEqual(p['tp_scale'], 1.0)
            d = pg.plan(100, 101.2, 101.2, p)
            self.assertEqual(d['act'], 'trim')
        finally:
            os.environ.pop('PG_TP_SCALE', None)


class TestApply(unittest.TestCase):
    def _hooks(self, log):
        return {
            'cancel_sl': lambda ex, sym: log.append(('cancel', sym)),
            'set_sl': lambda ex, sym, qty, sl: log.append(('sl', sym, qty, sl)),
            'close': lambda ex, sym, amt, llm, why: log.append(
                ('close', sym, amt, why)),
        }

    def test_apply_be_lock(self):
        st = {'real_pos': {'X/USDT:USDT': {'amt': 1.0, 'entry': 100.0}}}
        log = []
        d = pg.plan(100, 100.5, 102.5, P_SHORT)
        d.update({'symbol': 'X/USDT:USDT', 'amt': 1.0})
        act, _ = pg.apply(None, st, 'X/USDT:USDT', d, self._hooks(log))
        self.assertEqual(act, 'be_lock')
        self.assertIn(('cancel', 'X/USDT:USDT'), log)
        self.assertEqual(log[-1][0], 'sl')
        self.assertEqual(st['real_pos']['X/USDT:USDT']['sl'], d['sl'])

    def test_apply_trim_restops_remaining(self):
        st = {'real_pos': {'X/USDT:USDT': {'amt': 1.0, 'entry': 100.0}}}
        log = []
        d = pg.plan(100, 103.2, 103.2, P_SHORT)
        d.update({'symbol': 'X/USDT:USDT', 'amt': 1.0, 'trim_qty': 0.5, 'rem_qty': 0.5})
        act, _ = pg.apply(None, st, 'X/USDT:USDT', d, self._hooks(log))
        self.assertEqual(act, 'trim')
        self.assertEqual(st['real_pos']['X/USDT:USDT']['amt'], 0.5)
        self.assertTrue(st['real_pos']['X/USDT:USDT']['trimmed'])
        kinds = [x[0] for x in log]
        self.assertEqual(kinds, ['close', 'sl'])     # 平一半 → 剩余重新保本止损
        self.assertEqual(log[0][2], 0.5)

    def test_apply_close_pops_position(self):
        st = {'real_pos': {'X/USDT:USDT': {'amt': 1.0, 'entry': 100.0}}}
        log = []
        d = pg.plan(100, 102.0, 104.0, P_SHORT)
        d.update({'symbol': 'X/USDT:USDT', 'amt': 1.0})
        act, _ = pg.apply(None, st, 'X/USDT:USDT', d, self._hooks(log))
        self.assertEqual(act, 'close')
        self.assertNotIn('X/USDT:USDT', st['real_pos'])
        self.assertIsNone(log[0][2])                 # amt=None = 全平


if __name__ == '__main__':
    unittest.main()
