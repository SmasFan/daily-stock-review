#!/usr/bin/env python3
"""arena_engine 单元测试：策略信号纯函数 + 记账/仲裁逻辑（不联网）。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arena_engine as A  # noqa: E402


def ctx(c, d=None, o=None, h=None, l=None, entry=None):
    c = list(c)
    return {'c': c, 'o': list(o or c), 'h': list(h or c), 'l': list(l or c),
            'd': list(d if d is not None else c),
            'ma20': A.sma(c, 20), 'ma50': A.sma(c, 50), 'ma200': A.sma(c, 200),
            'entry': entry, 'price': c[-1]}


class TestSignals(unittest.TestCase):
    def test_s1_flat_when_daily_and_intraday_disagree(self):
        # 日线大涨、1h 下跌 → 方向多但择时空仓 → 观望
        daily = [100 + i for i in range(30)]
        hourly = [200 - i for i in range(60)]
        self.assertIsNone(A.sig_hybrid(ctx(hourly, daily), None))

    def test_s1_holds_long_while_daily_bull_and_intraday_bull(self):
        daily = [100 + i * 2 for i in range(30)]
        hourly = [100 + i * 0.5 for i in range(60)]
        self.assertEqual(A.sig_hybrid(ctx(hourly, daily), 'long'), 'long')

    def test_s1_exits_long_when_daily_turns_flat(self):
        daily = [100 + i for i in range(20)] + [120] * 20   # 最近 20 根动量归零
        hourly = [100 + i * 0.5 for i in range(60)]
        self.assertIsNone(A.sig_hybrid(ctx(hourly, daily), 'long'))

    def test_s2_long_only_above_momentum_and_sma50(self):
        up = [100 + i for i in range(60)]          # +50% 动量、站上 SMA50
        self.assertEqual(A.sig_daily_trend(ctx(up), None), 'long')
        down = [200 - i for i in range(60)]
        self.assertIsNone(A.sig_daily_trend(ctx(down), None))

    def test_s3_both_directions(self):
        rising = [100 + i for i in range(60)]
        falling = [200 - i for i in range(60)]
        self.assertEqual(A.sig_trend_ma(ctx(rising), None), 'long')
        self.assertEqual(A.sig_trend_ma(ctx(falling), None), 'short')

    def test_s4_rebound_needs_4pct_below_ma20_and_green_bar(self):
        c = [100] * 60 + [93.0]            # 93/99.65-1 = -6.7%，低于 SMA20 4% 以上
        o = list(c)
        o[-1] = 94.0                       # 收阳
        self.assertEqual(A.sig_rebound(ctx(c, o=o), None), 'long')
        self.assertIsNone(A.sig_rebound(ctx([100] * 60), None))

    def test_s4_exits_at_ma20(self):
        c = [100] * 60
        self.assertIsNone(A.sig_rebound(ctx(c, entry=90), 'long'))

    def test_s5_breakout_up_and_down(self):
        base = [100] * 30
        up = base + [101.5]
        h = list(up); l = list(up)
        for i in range(30):
            h[i] = 100.5; l[i] = 99.5
        self.assertEqual(A.sig_breakout(ctx(up, h=h, l=l), None), 'long')
        dn = base + [98.0]
        h2 = list(dn); l2 = list(dn)
        for i in range(30):
            h2[i] = 100.5; l2[i] = 99.5
        self.assertEqual(A.sig_breakout(ctx(dn, h=h2, l=l2), None), 'short')

    def test_s5_exits_on_sma50_loss(self):
        c = [100] * 60
        c[-1] = 80.0
        self.assertIsNone(A.sig_breakout(ctx(c), 'long'))

    def test_s6_grid_buys_lower_band(self):
        c = [100] * 300
        c[-1] = 94.0                       # SMA200=100 → -6% 落在 [-7%,-5%]
        self.assertEqual(A.sig_grid(ctx(c), None), 'long')
        self.assertIsNone(A.sig_grid(ctx([100] * 300), None))

    def test_s6_exits_at_upper_band(self):
        c = [100] * 300
        c[-1] = 97.5
        self.assertIsNone(A.sig_grid(ctx(c), 'long'))


class TestBookkeeping(unittest.TestCase):
    def test_notional_scales_with_weight_and_risk(self):
        st = {}
        s1 = A.STRAT_BY_ID['S1']
        s4 = A.STRAT_BY_ID['S4']
        # 100 * 0.60 / 6 * 5 = 50U ; 100 * 0.05 / 1 * 5 * 0.5 = 12.5U
        self.assertAlmostEqual(A.strat_notional(st, s1), 50.0)
        self.assertAlmostEqual(A.strat_notional(st, s4), 12.5)

    def test_every_strategy_notional_above_exchange_minimum(self):
        for s in A.STRATS:
            self.assertGreaterEqual(A.strat_notional({}, s), A.MIN_NOTIONAL,
                                    '%s 单笔名义低于 5U 门槛' % s['id'])

    def test_weights_sum_to_one_and_s3_absent(self):
        self.assertAlmostEqual(sum(s['weight'] for s in A.STRATS), 1.0)
        self.assertNotIn('S3', A.STRAT_BY_ID)

    def test_pos_count_and_stats(self):
        st = {'real_pos': {'A/USDT:USDT': {'strategy': 'S1'},
                           'B/USDT:USDT': {'strategy': 'S1'},
                           'C/USDT:USDT': {'strategy': 'S5'}},
              'trades': [
                  {'strategy': 'S1', 'pnl_usd': 5.0},
                  {'strategy': 'S1', 'pnl_usd': -2.0},
                  {'strategy': 'S5', 'pnl_usd': 1.0}]}
        self.assertEqual(A.pos_count(st, 'S1'), 2)
        self.assertEqual(A.pos_count(st, 'S5'), 1)
        s = A.stats_of(st)
        self.assertEqual(s['S1']['trades'], 2)
        self.assertEqual(s['S1']['wins'], 1)
        self.assertAlmostEqual(s['S1']['win_rate'], 50.0)
        self.assertAlmostEqual(s['S1']['pnl_usd'], 3.0)
        self.assertEqual(s['S5']['win_rate'], 100.0)


class FakeEx:
    def __init__(self, positions=None, px=100.0):
        self._positions = positions or []
        self._px = px
        self.orders = []

    def fetch_positions(self, syms=None):
        return self._positions

    def fetch_ticker(self, sym):
        return {'last': self._px}

    def set_leverage(self, n, sym):
        pass

    def amount_to_precision(self, sym, q):
        return round(float(q), 3)

    def create_order(self, sym, typ, side, qty, price=None, params=None):
        self.orders.append((sym, side, qty, params))
        return {'filled': qty, 'average': self._px, 'info': {'realizedPnl': 0}}


class TestArbitration(unittest.TestCase):
    """同一只票只能被一个策略持有；平仓只由开仓策略决定。"""

    def test_open_skips_symbol_already_held(self):
        ex = FakeEx(positions=[{'symbol': 'NVDA/USDT:USDT', 'contracts': 1.0,
                                'entryPrice': 100.0, 'side': 'long',
                                'info': {'positionAmt': '1.0'}}])
        st = {'real_pos': {'NVDA/USDT:USDT': {'strategy': 'S1', 'side': 'long',
                                              'amt': 1.0, 'entry': 100.0}},
              'trades': [], 'log': []}
        with mock.patch.object(A, 'fetch_ctx', lambda ex, n: ctx([100] * 60)), \
             mock.patch.object(A, 'cancel_sl', lambda *a: None), \
             mock.patch.object(A, 'set_sl', lambda *a, **k: (None, 0)), \
             mock.patch.object(A, 'save_state', lambda st: None):
            A.decide(ex, st, dry=True)
        # 已持仓的 NVDA 不会被任何策略再开一次（只允许出现 reduceOnly 平仓单）
        self.assertFalse([o for o in ex.orders if not (o[3] or {}).get('reduceOnly')])

    def test_close_rejects_direction_mismatch(self):
        ex = FakeEx(positions=[{'symbol': 'NVDA/USDT:USDT', 'contracts': 1.0,
                                'entryPrice': 100.0, 'side': 'short',
                                'info': {'positionAmt': '-1.0'}}])
        st = {'real_pos': {'NVDA/USDT:USDT': {'strategy': 'S1', 'side': 'long'}},
              'trades': [], 'log': []}
        with mock.patch.object(A, 'cancel_sl', lambda *a: None), \
             mock.patch.object(A, 'save_state', lambda st: None):
            n = A.close_position(ex, st, 'NVDA', why='test')
        self.assertEqual(n, 0)
        self.assertEqual(ex.orders, [])


class TestS2GuardAndCooldown(unittest.TestCase):
    """S2 持仓时实时价跌破日线 SMA50 就离场 + 平仓后冷却（2026-11-12 SOXS 教训）。"""

    def _ctx(self, price):
        # 前 61 根 100、后 20 根 200：日线 20 根动量 +100%，SMA50≈140，日线收盘 200 在上方
        d = [100.0] * 61 + [200.0] * 20
        return {'d': d, 'price': price, 'ma20': None, 'ma50': None, 'ma200': None}

    def test_s2_holds_above_sma50(self):
        self.assertEqual(A.sig_daily_trend(self._ctx(201.0), 'long'), 'long')

    def test_s2_exits_when_price_breaks_sma50(self):
        self.assertIsNone(A.sig_daily_trend(self._ctx(139.0), 'long'))

    def test_s2_open_signal_ignores_realtime_price(self):
        # 空仓时只看日线收盘，不受盘中价影响
        self.assertEqual(A.sig_daily_trend(self._ctx(100.0), None), 'long')

    def test_cooldown_blocks_reentry(self):
        now = 1_700_000_000.0
        st = {'last_exit': {'SOXS/USDT:USDT': now}}
        self.assertTrue(A.in_cooldown(st, 'SOXS/USDT:USDT', now=now + 60))
        self.assertFalse(A.in_cooldown(st, 'SOXS/USDT:USDT',
                                       now=now + A.COOLDOWN_MIN * 60 + 1))

    def test_cooldown_without_record(self):
        self.assertFalse(A.in_cooldown({}, 'SOXS/USDT:USDT'))


class TestFamilyExclusivity(unittest.TestCase):
    """SOXL（半导体3x正向）与 SOXS（半导体3x反向）同族二选一（2026-11-16）。
    两者经济方向可同向（多SOXS=空SOXL净值），同持=重复押注半导体主题。"""

    def _st(self):
        return {'real_pos': {
            'SOXL/USDT:USDT': {'strategy': 'S1', 'side': 'short', 'amt': 1.0,
                               'entry': 50.0, 'ts': 1000},
            'SOXS/USDT:USDT': {'strategy': 'S5', 'side': 'long', 'amt': 1.0,
                               'entry': 56.0, 'ts': 2000},
        }, 'trades': [], 'log': [], 'last_exit': {}}

    def _ex(self):
        return FakeEx(positions=[
            {'symbol': 'SOXL/USDT:USDT', 'contracts': 1.0, 'entryPrice': 50.0,
             'side': 'short', 'info': {'positionAmt': '-1.0'}},
            {'symbol': 'SOXS/USDT:USDT', 'contracts': 1.0, 'entryPrice': 56.0,
             'side': 'long', 'info': {'positionAmt': '1.0'}},
        ])

    def test_family_of(self):
        self.assertEqual(A.family_of('SOXL'), 'SOX')
        self.assertEqual(A.family_of('SOXS'), 'SOX')
        self.assertIsNone(A.family_of('NVDA'))

    def test_family_conflicts_detects_pair(self):
        st = self._st()
        self.assertEqual([n for n, _ in A.family_conflicts(st, 'SOXL')], ['SOXS'])
        self.assertEqual([n for n, _ in A.family_conflicts(st, 'SOXS')], ['SOXL'])
        self.assertEqual(A.family_conflicts(st, 'NVDA'), [])

    def test_converge_keeps_higher_priority(self):
        st = self._st()
        ex = self._ex()
        with mock.patch.object(A, 'cancel_sl', lambda *a: None), \
             mock.patch.object(A, 'save_state', lambda st: None):
            closed = A.converge_families(ex, st, dry=False)
        # S1(序0) 优先于 S5(序1) → 平 SOXS 留 SOXL
        self.assertEqual(closed, ['SOXS'])
        self.assertIn('SOXL/USDT:USDT', st['real_pos'])
        self.assertNotIn('SOXS/USDT:USDT', st['real_pos'])

    def test_converge_noop_single_member(self):
        st = {'real_pos': {'SOXL/USDT:USDT': {'strategy': 'S1', 'side': 'short',
                                             'amt': 1.0, 'entry': 50.0, 'ts': 1000}},
              'trades': [], 'log': [], 'last_exit': {}}
        self.assertEqual(A.converge_families(self._ex(), st, dry=False), [])
        self.assertIn('SOXL/USDT:USDT', st['real_pos'])

    def test_open_blocked_by_family_member(self):
        st = self._st()
        self.assertTrue(A.family_conflicts(st, 'SOXS'))   # SOXL 在手 → SOXS 不再开
        self.assertFalse(A.family_conflicts(self._st() | {'real_pos': {}}, 'SOXS')
                         if False else A.family_conflicts({'real_pos': {}}, 'SOXS'))


class TestCircuitBreaker(unittest.TestCase):
    """行情接口整体挂掉时熔断本轮，避免 23 标逐个 60s 超时把心跳拖到几十分钟
    （2026-11-16 实测 826 次重启 / 心跳间隔 34min）。"""

    def test_decide_breaks_after_consecutive_fetch_failures(self):
        calls = {'n': 0}

        def boom(ex, name):
            calls['n'] += 1
            raise RuntimeError('network down')

        ex = FakeEx()
        st = {'real_pos': {}, 'trades': [], 'log': [], 'last_exit': {}}
        with mock.patch.object(A, 'fetch_ctx', boom), \
             mock.patch.object(A, 'save_state', lambda st: None), \
             mock.patch.object(A.time, 'sleep', lambda *a, **k: None):
            A.decide(ex, st, dry=True, echo_progress=False)
        # 5 个连续失败即熔断；每个标的重试 2 次 → 调用 10 次，远小于 23 标 × 2
        self.assertEqual(calls['n'], 10)

    def test_decide_processes_all_when_fetches_ok(self):
        calls = {'n': 0}

        def ok(ex, name):
            calls['n'] += 1
            return ctx([100] * 60)

        ex = FakeEx()
        st = {'real_pos': {}, 'trades': [], 'log': [], 'last_exit': {}}
        with mock.patch.object(A, 'fetch_ctx', ok), \
             mock.patch.object(A, 'cancel_sl', lambda *a: None), \
             mock.patch.object(A, 'set_sl', lambda *a, **k: (None, 0)), \
             mock.patch.object(A, 'save_state', lambda st: None), \
             mock.patch.object(A.time, 'sleep', lambda *a, **k: None):
            A.decide(ex, st, dry=True, echo_progress=False)
        self.assertEqual(calls['n'], len(A.SYMBOLS))


if __name__ == '__main__':
    unittest.main()
