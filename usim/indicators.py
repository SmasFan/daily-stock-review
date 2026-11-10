# -*- coding: utf-8 -*-
"""K 线小工具：SMA / ATR / RSI，纯标准库，输入为 bar 列表。"""
BAR_FIELDS = ("date", "open", "close", "high", "low", "volume")


def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / float(n)


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(-n, 0):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100.0 - 100.0 / (1.0 + rs)


def atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(-n, 0):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / float(n)


def ret_n(closes, n):
    if len(closes) < n + 1:
        return None
    base = closes[-n - 1]
    if not base:
        return None
    return closes[-1] / base - 1.0


def closes_of(bars):
    return [b["close"] for b in bars]
