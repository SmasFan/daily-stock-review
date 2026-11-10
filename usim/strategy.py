# -*- coding: utf-8 -*-
"""策略：45 日新高突破 + 大盘环境过滤 + ATR 移动止损。

入场（全部硬规则）：
  close > SMA50 且 close > 前 BREAKOUT_N 日最高收盘 且 RSI14 < RSI_MAX
  → 次日开盘买入，初始止损 = 入场价 - STOP_ATR × ATR
离场：
  浮盈达 TRAIL_R 倍 R 后启用吊灯止损（峰值 - TRAIL_ATR × ATR，只上移）
  跌破止损即卖；TAKE_R>0 时额外半仓止盈；BREAKEVEN_R>0 时保本
定仓：权益×RISK_PCT / 止损距离，再被单票上限与现金夹住（支持碎股）
"""
from . import config
from .indicators import atr, closes_of, ret_n, rsi, sma


def regime_ok(bench_bars):
    """SPY 在 50 日均线之上才允许开新仓。"""
    if not config.REGIME_FILTER or not bench_bars:
        return True
    closes = closes_of(bench_bars)
    if len(closes) < config.SMA_SLOW + 1:
        return True
    m = sma(closes, config.SMA_SLOW)
    return bool(m) and closes[-1] > m


def evaluate(symbol, bars):
    """返回 None 或信号 dict。"""
    if len(bars) < max(config.BREAKOUT_N, config.SMA_SLOW, config.ATR_N) + 5:
        return None
    closes = closes_of(bars)
    px = closes[-1]
    hi = max(closes[-config.BREAKOUT_N - 1:-1])
    m = sma(closes, config.SMA_SLOW)
    a = atr(bars, config.ATR_N)
    r = rsi(closes, config.RSI_N)
    if not (m and a and r is not None):
        return None
    if px <= hi:
        return None                    # 没突破
    if px <= m:
        return None                    # 中期趋势不成立
    if r >= config.RSI_MAX:
        return None                    # 超买不追
    stop = px - config.STOP_ATR * a
    return {
        "symbol": symbol,
        "price": px,
        "breakout": round(hi, 2),
        "sma_slow": round(m, 2),
        "atr": round(a, 3),
        "rsi": round(r, 1),
        "mom20": round((ret_n(closes, config.MOM_N) or 0) * 100, 2),
        "score": round((px / hi - 1) * 100, 3),
        "stop": stop,
    }


def candidates(symbols, bars_map):
    out = []
    for sym in symbols:
        bars = bars_map.get(sym)
        if not bars:
            continue
        sig = evaluate(sym, bars)
        if sig:
            out.append(sig)
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def size(equity, cash, sig):
    stop_dist = max(sig["price"] - sig["stop"], sig["price"] * 0.01)
    qty = equity * config.RISK_PCT / stop_dist
    qty = min(qty,
              equity * config.MAX_WEIGHT / sig["price"],
              max(cash * (1 - config.CASH_BUFFER), 0) / sig["price"])
    qty = round(qty, config.QTY_DECIMALS)
    return 0.0 if qty * sig["price"] < config.MIN_NOTIONAL else qty


def manage(pos, px):
    """更新移动止损/保本，返回 (离场原因 或 None, 卖出比例)。"""
    pos["peak"] = max(pos.get("peak") or px, px)
    r0 = pos["avg"] - pos.get("stop0", pos["stop"])
    a = pos.get("atr") or 0
    if config.BREAKEVEN_R > 0 and r0 > 0 and px >= pos["avg"] + config.BREAKEVEN_R * r0:
        pos["stop"] = max(pos["stop"], pos["avg"])
    if a and r0 > 0 and px >= pos["avg"] + config.TRAIL_R * r0:
        pos["stop"] = max(pos["stop"], pos["peak"] - config.TRAIL_ATR * a)
    if px <= pos["stop"]:
        return "止损 %.2f" % pos["stop"], 1.0
    if config.TAKE_R > 0 and not pos.get("took_half") and r0 > 0 \
            and px >= pos["avg"] + config.TAKE_R * r0:
        return "止盈半仓", 0.5
    return None, 0.0
