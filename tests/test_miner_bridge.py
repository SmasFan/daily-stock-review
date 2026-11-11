# -*- coding: utf-8 -*-
"""miner_bridge 的数据格式化与收益估算单测（不依赖网络/矿机）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import miner_bridge as m  # noqa: E402


def fake_live():
    return {
        'ok': True,
        'worker': 'smas.rig5090',
        'miner_running': True,
        'stale_seconds': 2.0,
        'market': {
            'price': 2.0, 'change24h': -3.5, 'net_hashrate': 1e20,
            'mcap': 5e8, 'height': 1000.0, 'block_time': 200.0,
            'difficulty': 1e7, 'block_reward': 2000.0, 'ok': True,
            'pool_fee': 0.01, 'source': 'pearltrack.io', 'stale': False,
        },
        'gpu': {'temp': 70.0, 'power_draw': 400.0, 'power_limit': 450.0,
                'util_gpu': 99.0, 'fan': 55.0, 'sm_mhz': 2100.0,
                'mem_used': 3000.0, 'mem_total': 32000.0, 'eco': True},
        'cpu': {'util': 10.0, 'power': 60.0},
        'raw': {
            'version': '1.8.1', 'uptime_str': '1 days 00:00:00',
            'algos': [{
                'name': 'pearlhash', 'pool_status': 'Connected',
                'total_hashrate': 3.5e14,
                'total_accepted_shares': 100, 'total_rejected_shares': 5,
                'total_invalid_shares': 20,
            }],
        },
        'sample': {'hr': 3.5e14, 'acc': 100, 'rej': 5, 'inv': 20,
                   'status': 'Connected', 'algo': 'pearlhash'},
    }


class TestHashrateText(unittest.TestCase):
    def test_units(self):
        self.assertEqual(m.hr_text(3.5e14), '350.00 TH/s')
        self.assertEqual(m.hr_text(9.223e19), '92.23 EH/s')
        self.assertEqual(m.hr_text(1e9), '1.00 GH/s')

    def test_bad_values(self):
        self.assertEqual(m.hr_text(None), '—')
        self.assertEqual(m.hr_text(0), '—')
        self.assertEqual(m.hr_text('abc'), '—')


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.d = m.build(fake_live(), {'samples': []})

    def test_revenue_formula(self):
        # share=3.5e14/1e20=3.5e-6; gross=share*(86400/200)*2000=3.024 PRL
        # prl=3.024*0.99=2.99376; usd=2.99376*2=5.98752
        self.assertAlmostEqual(self.d['sharePct'], 0.00035, places=8)
        self.assertAlmostEqual(self.d['dailyPrl'], 2.99376, places=4)
        self.assertAlmostEqual(self.d['dailyUsd'], 5.98752, places=4)
        self.assertEqual(self.d['poolFeePct'], 1.0)

    def test_core_fields(self):
        self.assertEqual(self.d['hashrateText'], '350.00 TH/s')
        self.assertEqual(self.d['netHashrateText'], '100.00 EH/s')
        self.assertEqual(self.d['price'], 2.0)
        self.assertEqual(self.d['change24h'], -3.5)
        self.assertEqual(self.d['worker'], 'smas.rig5090')
        self.assertEqual(self.d['version'], '1.8.1')
        self.assertEqual(self.d['algo'], 'pearlhash')
        self.assertEqual(self.d['poolStatus'], 'Connected')
        self.assertTrue(self.d['running'])
        self.assertTrue(self.d['ok'])
        self.assertTrue(self.d['marketOk'])

    def test_gpu_and_shares(self):
        self.assertEqual(self.d['gpu']['temp'], 70.0)
        self.assertEqual(self.d['gpu']['power'], 400.0)
        self.assertTrue(self.d['gpu']['eco'])
        self.assertEqual(self.d['shares']['accepted'], 100)
        self.assertEqual(self.d['shares']['rejected'], 5)
        self.assertEqual(self.d['shares']['invalid'], 20)
        self.assertAlmostEqual(self.d['shares']['acceptRate'], 80.0, places=4)

    def test_missing_market_is_safe(self):
        live = fake_live()
        live['market'] = {}
        d = m.build(live, {'samples': []})
        self.assertEqual(d['dailyPrl'], 0.0)
        self.assertEqual(d['dailyUsd'], 0.0)
        self.assertEqual(d['sharePct'], 0.0)
        self.assertFalse(d['marketOk'])


class TestSpark(unittest.TestCase):
    def test_downsample(self):
        hist = {'samples': [{'hr': float(i + 1)} for i in range(100)]}
        vals = m.spark_of(hist, 100.0)
        self.assertEqual(len(vals), 48)
        self.assertTrue(all(v > 0 for v in vals))

    def test_fallback_when_empty(self):
        self.assertEqual(m.spark_of({'samples': []}, 3.5e14), [350000000000000, 350000000000000])
        self.assertEqual(m.spark_of(None, 0), [])

    def test_ignores_zero_and_bad(self):
        hist = {'samples': [{'hr': 0}, {'hr': 'x'}, {'hr': 10}, {'hr': 20}]}
        self.assertEqual(m.spark_of(hist, 10), [10, 20])


if __name__ == '__main__':
    unittest.main()
