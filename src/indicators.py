"""
技术指标计算模块（纯 Python，无三方依赖）。

指标口径对齐股票分析项目（daily_stock_analysis/src/stock_analyzer.py）：
- MA: 简单滚动均值（MA5/10/20/60）
- MACD: EMA(12,26,9)，A股口径 BAR = (DIF - DEA) * 2
- RSI: Wilder/SMMA 口径（alpha=1/period），周期 6/12/24
- BOLL: MA20 ± 2 倍标准差（总体标准差）
- 乖离率: bias_maN = (price - maN) / maN * 100
- 量比: 当日成交量 / 前 N 日均量
"""
from typing import List, Optional


def sma(values: List[float], period: int) -> List[Optional[float]]:
    """简单移动平均，前 period-1 个为 None。"""
    out: List[Optional[float]] = []
    acc = 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= period:
            acc -= values[i - period]
        if i < period - 1:
            out.append(None)
        else:
            out.append(acc / period)
    return out


def ema(values: List[float], period: int) -> List[float]:
    """指数移动平均（adjust=False 口径，与 pandas ewm(span, adjust=False) 一致）。"""
    alpha = 2.0 / (period + 1.0)
    out: List[float] = []
    prev = values[0] if values else 0.0
    for v in values:
        prev = alpha * v + (1 - alpha) * prev
        out.append(prev)
    return out


def macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """返回 (dif, dea, bar) 三条序列，bar = (dif - dea) * 2（A股口径）。"""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = ema(dif, signal)
    bar = [(d - e) * 2 for d, e in zip(dif, dea)]
    return dif, dea, bar


def rsi(closes: List[float], period: int) -> List[float]:
    """Wilder RSI。起始段不足时返回 50（中性）。"""
    n = len(closes)
    out = [50.0] * n
    if n < 2:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        gains[i] = max(diff, 0.0)
        losses[i] = max(-diff, 0.0)
    alpha = 1.0 / period
    avg_gain = gains[1]
    avg_loss = losses[1]
    for i in range(1, n):
        avg_gain = alpha * gains[i] + (1 - alpha) * avg_gain
        avg_loss = alpha * losses[i] + (1 - alpha) * avg_loss
        if i < period:
            continue
        if avg_loss == 0:
            out[i] = 100.0 if avg_gain > 0 else 50.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100 - 100 / (1 + rs)
    return out


def bollinger(closes: List[float], period: int = 20, k: float = 2.0):
    """布林带，返回 (mid, upper, lower)。标准差为总体口径（除以 period）。"""
    mid = sma(closes, period)
    upper: List[Optional[float]] = []
    lower: List[Optional[float]] = []
    for i in range(len(closes)):
        if mid[i] is None:
            upper.append(None)
            lower.append(None)
            continue
        m = mid[i]
        var = sum((closes[i - j] - m) ** 2 for j in range(period)) / period
        std = var ** 0.5
        upper.append(m + k * std)
        lower.append(m - k * std)
    return mid, upper, lower


def bias(price: float, ma: Optional[float]) -> Optional[float]:
    """乖离率（%）。"""
    if ma is None or ma == 0:
        return None
    return (price - ma) / ma * 100


def volume_ratio(volumes: List[float], idx: int, lookback: int = 5) -> Optional[float]:
    """量比 = 当日量 / 前 lookback 日均量。"""
    if idx < 1 or idx - lookback < 0:
        return None
    window = volumes[idx - lookback:idx]
    if not window:
        return None
    avg = sum(window) / len(window)
    if avg == 0:
        return None
    return volumes[idx] / avg


def rolling_std(values: List[float], period: int) -> List[Optional[float]]:
    """滚动标准差（总体口径），用于波动率/ATR 近似。"""
    out: List[Optional[float]] = []
    for i in range(len(values)):
        if i < period - 1:
            out.append(None)
            continue
        window = values[i - period + 1:i + 1]
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / period
        out.append(var ** 0.5)
    return out


def pct_change(closes: List[float], idx: int, lookback: int) -> Optional[float]:
    """区间涨跌幅（%）。"""
    if idx - lookback < 0 or closes[idx - lookback] == 0:
        return None
    return (closes[idx] / closes[idx - lookback] - 1) * 100


def max_drawdown(closes: List[float], lookback: int) -> Optional[float]:
    """近 lookback 日内最大回撤（%，负值）。"""
    if len(closes) < 2:
        return None
    window = closes[-lookback:] if lookback < len(closes) else closes[:]
    peak = window[0]
    mdd = 0.0
    for v in window:
        if v > peak:
            peak = v
        dd = (v / peak - 1) * 100
        if dd < mdd:
            mdd = dd
    return mdd
