#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日内混合信号（日线定方向 + 1h 择时），trader100 / trader_short 共用。

为什么不是「纯 1h 动量」
------------------------
2026-11-10 把 1h K 线全池扫了一遍（backtest/shortterm.py v1/v2）：
  - v1 直接把「|动量|<阈值就平仓」搬到 1h → 每 20~30 根换手，双边 0.04% 费吃光收益，
    样本外 -12%、MDD 63%；
  - v2 加滞回平仓 + 1h SMA200 过滤 → MDD 降到 15%，但样本外仍 -2%~-9%，收益归零；
  - v3（backtest/shortterm_hybrid.py）用「日线 20 根动量定方向，1h 只做同向触发」
    → 全期 +22.0% / MDD 11.5% / Sharpe 1.42 / 样本外 +2.9%（13 标含双边费、5x）。
纯 1h 噪声里没有稳定 edge，方向必须来自更高时间框架；1h 负责「什么时候进、什么时候走」。

最终参数（v3 稳健平台，详见 backtest/SHORTTERM.md）
--------------------------------------------------
日线 20 根动量 > +2% 定多头方向、< -2% 定空头方向（半仓）；
1h 12 根动量 > +1% 且价在 1h SMA200 上方才开多，< -1% 且在 SMA200 下方才开空；
持仓中日线方向翻转 / 1h 动量衰减到 0（滞回 exit_ratio=0）即离场；
止损 2%（多空同口径），利润保护走 profit_guard 的 intraday profile。

美股 23h 交易后 1h 信号更连续；巡检 15min 而不是 1h，杠杆 5x 把日内小波动
放大成有意义的弹性（BN_LEV / LEV 可调，杠杆只缩放收益与回撤，不改变信号质量）。
"""
import os

# 参数全部可被 .env 覆盖；回测用的就是这里的默认值。
DAILY_N = int(os.environ.get('DAILY_MOM_N', '20'))
DAILY_TH = float(os.environ.get('DAILY_MOM_TH', '0.02'))
SHORT_DAILY_TH = float(os.environ.get('BN_SHORT_MOM_TH', '0.02'))
EXEC_TF = os.environ.get('BN_EXEC_TF', '1h')
EXEC_N = int(os.environ.get('BN_EXEC_N', '12'))
EXEC_TH = float(os.environ.get('BN_EXEC_TH', '0.01'))
TREND_N = int(os.environ.get('BN_TREND_N', '200'))
EXIT_RATIO = float(os.environ.get('BN_EXIT_RATIO', '0.0'))
SHORT_RISK = float(os.environ.get('BN_SHORT_RISK', '0.5'))


def sma(closes, w):
    return sum(closes[-w:]) / w if len(closes) >= w else None


def daily_dir(closes, mode='mom', cfg=None, n=DAILY_N, th=DAILY_TH,
              short_th=SHORT_DAILY_TH, short_ok=True):
    """日线定方向。返回 {'dir': 'long'|'short'|None, 'mom', 'desc'}。"""
    if len(closes) < n + 1:
        return {'dir': None, 'mom': None, 'desc': '日线历史不足'}
    mom = closes[-1] / closes[-1 - n] - 1
    if mode == 'sma':
        w = (cfg or {}).get('sma', 50)
        ma = sma(closes, w)
        d = 'long' if (ma is not None and closes[-1] > ma) else None
        desc = '日线收盘 %.2f / SMA%d %s' % (closes[-1], w, '%.2f' % ma if ma else 'n/a')
        return {'dir': d, 'mom': mom, 'desc': desc}
    d = 'long' if mom > th else ('short' if (short_ok and mom < -short_th) else None)
    desc = '日线%d根动量 %+.2f%%（阈值 ±%.0f%%/%.0f%%）' % (
        n, mom * 100, th * 100, short_th * 100)
    return {'dir': d, 'mom': mom, 'desc': desc}


def exec_signal(closes, n=EXEC_N, th=EXEC_TH, trend_n=TREND_N, tf=EXEC_TF):
    """1h（或配置周期）执行层。返回 {'mom','price','trend','above_trend','desc'}。"""
    if len(closes) < n + 1:
        return {'mom': None, 'price': closes[-1] if closes else 0,
                'trend': None, 'above_trend': None, 'desc': '%s 历史不足' % tf}
    mom = closes[-1] / closes[-1 - n] - 1
    trend = sma(closes, trend_n) if trend_n else None
    above = (closes[-1] > trend) if trend is not None else None
    desc = '%s %d根动量 %+.2f%%（阈值 %.0f%%）' % (tf, n, mom * 100, th * 100)
    if trend is not None:
        desc += ' / SMA%d %.2f（%s）' % (trend_n, trend, '上方' if above else '下方')
    return {'mom': mom, 'price': closes[-1], 'trend': trend,
            'above_trend': above, 'desc': desc}


def target(daily, ex, holding_side=None, th=EXEC_TH, exit_ratio=EXIT_RATIO):
    """把日线方向 + 1h 执行层合成目标方向。

    holding_side=None → 决定要不要开仓；否则决定现有仓要不要继续持有。
    返回 'long' / 'short' / None（None = 观望或平仓）。

    与 v3 回测逐行对齐：
      sig>0 需 mom>th 且 px>SMA200；sig<0 需 mom<-th 且 px<SMA200；
      持仓中 dir != side 立即离场；同向动量衰减到 exit_ratio*th 才平（滞回）。
    """
    d = daily.get('dir')
    mom = ex.get('mom')
    if mom is None or d is None:
        return None
    px, trend, above = ex.get('price'), ex.get('trend'), ex.get('above_trend')
    sig = None
    if d == 'long' and mom > th and (trend is None or px > trend):
        sig = 'long'
    elif d == 'short' and mom < -th and (trend is None or px < trend):
        sig = 'short'
    if holding_side is None:
        return sig
    if d != holding_side:                       # 日线方向翻转或转观望
        return None
    if sig is not None and sig != holding_side:
        return None
    if holding_side == 'long' and mom < exit_ratio * th:
        return None
    if holding_side == 'short' and mom > -exit_ratio * th:
        return None
    return holding_side


def describe(daily, ex, side):
    """日志 / LLM 触发原因用的可读文本。"""
    tag = {'long': '✓多', 'short': '✓空', None: '✗观望'}.get(side, '✗观望')
    return '%s ｜ %s → %s' % (daily['desc'], ex['desc'], tag)
