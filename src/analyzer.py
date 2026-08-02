"""
个股趋势分析与信号评分模块。

逻辑迁移自股票分析项目 daily_stock_analysis/src/stock_analyzer.py：
- 趋势七档判断（强势多头/多头/弱势多头/盘整/弱势空头/空头/强势空头）
- 百分制综合评分：趋势30 + 乖离率20 + 量能15 + 支撑10 + MACD15 + RSI10
- 五档信号映射：强烈买入(80+)/买入(60+)/观望(40+)/减仓(20+)/卖出(<20)
- 买卖点位：ideal_buy=MA5、secondary_buy=MA10、stop_loss=MA20、take_profit=前高
- 交易纪律：乖离>5% 严禁追高；只做 MA5>MA10>MA20 多头
"""
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from . import indicators as ind

# 量能阈值
VOLUME_HEAVY_RATIO = 1.5
VOLUME_SHRINK_RATIO = 0.7
# 乖离率阈值（%）
BIAS_THRESHOLD = 5.0
# 均线支撑容忍度
MA_SUPPORT_TOLERANCE = 0.02

# 五档信号口径（canonical decision scale）
DECISION_SCALE = [
    (80, "strong_buy", "强烈买入"),
    (60, "buy", "买入"),
    (40, "watch", "观望"),
    (20, "reduce", "减仓"),
    (0, "sell", "卖出"),
]


def signal_key_for_score(score: float) -> str:
    for threshold, key, _ in DECISION_SCALE:
        if score >= threshold:
            return key
    return "sell"


def signal_label_for_key(key: str) -> str:
    for _, k, label in DECISION_SCALE:
        if k == key:
            return label
    return "卖出"


@dataclass
class AnalysisResult:
    name: str
    code: str = ""
    date: str = ""
    close: float = 0.0
    change_pct: float = 0.0
    sector: str = ""
    # 趋势
    trend_status: str = ""
    trend_strength: int = 50
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None
    ma60: Optional[float] = None
    # 乖离率
    bias_ma5: Optional[float] = None
    bias_ma10: Optional[float] = None
    bias_ma20: Optional[float] = None
    # 量能
    volume_ratio: Optional[float] = None
    volume_status: str = ""
    # 支撑压力
    support: Optional[float] = None
    resistance: Optional[float] = None
    support_ma5: bool = False
    support_ma10: bool = False
    # MACD / RSI
    macd_status: str = ""
    macd_dif: Optional[float] = None
    macd_dea: Optional[float] = None
    macd_bar: Optional[float] = None
    rsi6: Optional[float] = None
    rsi12: Optional[float] = None
    rsi24: Optional[float] = None
    rsi_status: str = ""
    # 布林带
    boll_pos: Optional[float] = None
    # 评分与信号
    score: int = 50
    signal_key: str = "watch"
    signal: str = "观望"
    # 买卖点位
    ideal_buy: Optional[float] = None
    secondary_buy: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    # 高低点
    high20: Optional[float] = None
    low20: Optional[float] = None
    change_60d: Optional[float] = None

    def to_dict(self):
        return asdict(self)


def _judge_trend(ma5, ma10, ma20, prev_ma5, prev_ma10, prev_ma20):
    """趋势七档判断。返回 (状态, 强度)。"""
    if None in (ma5, ma10, ma20):
        return "数据不足", 50
    spread_now = (ma5 - ma20) / ma20 * 100 if ma20 else 0
    spread_prev = None
    if None not in (prev_ma5, prev_ma20) and prev_ma20:
        spread_prev = (prev_ma5 - prev_ma20) / prev_ma20 * 100
    expanding = spread_prev is not None and abs(spread_now) > abs(spread_prev)

    if ma5 > ma10 > ma20:
        if expanding and spread_now > 5:
            return "强势多头", 90
        return "多头排列", 75
    if ma5 > ma10 and ma10 <= ma20:
        return "弱势多头", 55
    if ma5 < ma10 and ma10 >= ma20:
        return "弱势空头", 40
    if ma5 < ma10 < ma20:
        if expanding and spread_now < -5:
            return "强势空头", 10
        return "空头排列", 25
    return "盘整", 50


def _judge_macd(dif, dea, idx):
    """MACD 状态机。"""
    if idx < 1:
        return "数据不足"
    d, e = dif[idx], dea[idx]
    pd, pe = dif[idx - 1], dea[idx - 1]
    prev_diff = pd - pe
    curr_diff = d - e
    if prev_diff <= 0 < curr_diff:
        return "零上金叉" if d > 0 else "金叉"
    if prev_diff >= 0 > curr_diff:
        return "死叉"
    if pd <= 0 < d:
        return "上穿零轴"
    if pd >= 0 > d:
        return "下穿零轴"
    if d > 0 and e > 0:
        return "多头"
    return "空头"


def _judge_rsi(rsi12_val):
    if rsi12_val is None:
        return "数据不足"
    if rsi12_val > 70:
        return "超买"
    if rsi12_val >= 60:
        return "强势"
    if rsi12_val >= 40:
        return "中性"
    if rsi12_val >= 30:
        return "弱势"
    return "超卖"


def _judge_volume(vratio, change_pct):
    if vratio is None:
        return "量能正常"
    if vratio >= VOLUME_HEAVY_RATIO:
        return "放量上涨" if change_pct >= 0 else "放量下跌"
    if vratio <= VOLUME_SHRINK_RATIO:
        return "缩量上涨" if change_pct >= 0 else "缩量回调"
    return "量能正常"


def _score_bias(bias_ma5, trend_status, trend_strength):
    """乖离率评分（0-20）。"""
    if bias_ma5 is None:
        return 10
    b = bias_ma5
    threshold = BIAS_THRESHOLD
    # 强势多头放宽 1.5 倍
    if trend_status == "强势多头" and trend_strength >= 70:
        threshold *= 1.5
    if -3 < b < 0:
        return 20
    if -5 < b <= -3:
        return 16
    if b <= -5:
        return 8
    if 0 <= b < 2:
        return 18
    if 2 <= b < threshold:
        return 14
    if trend_status == "强势多头" and b >= threshold:
        return 10  # 强趋势补偿，轻仓追踪
    return 4  # 严禁追高


def analyze_stock(name: str, dates: List[str], opens: List[float], closes: List[float],
                  highs: List[float], lows: List[float], volumes: List[float],
                  code: str = "") -> Optional[AnalysisResult]:
    """对单只股票做完整趋势分析与评分。"""
    n = len(closes)
    if n < 30:
        return None
    idx = n - 1

    ma5s = ind.sma(closes, 5)
    ma10s = ind.sma(closes, 10)
    ma20s = ind.sma(closes, 20)
    ma60s = ind.sma(closes, 60) if n >= 60 else ma20s
    dif, dea, bar = ind.macd(closes)
    rsi6 = ind.rsi(closes, 6)
    rsi12 = ind.rsi(closes, 12)
    rsi24 = ind.rsi(closes, 24)
    _, boll_u, boll_l = ind.bollinger(closes, 20)

    close = closes[idx]
    prev_close = closes[idx - 1]
    change_pct = (close / prev_close - 1) * 100 if prev_close else 0.0

    ma5, ma10, ma20, ma60 = ma5s[idx], ma10s[idx], ma20s[idx], ma60s[idx]
    p_ma5, p_ma10, p_ma20 = ma5s[idx - 5], ma10s[idx - 5], ma20s[idx - 5]

    trend_status, trend_strength = _judge_trend(ma5, ma10, ma20, p_ma5, p_ma10, p_ma20)
    bias5 = ind.bias(close, ma5)
    bias10 = ind.bias(close, ma10)
    bias20 = ind.bias(close, ma20)
    vratio = ind.volume_ratio(volumes, idx)
    volume_status = _judge_volume(vratio, change_pct)
    macd_status = _judge_macd(dif, dea, idx)
    rsi12_val = rsi12[idx]
    rsi_status = _judge_rsi(rsi12_val)

    # 支撑压力
    high20 = max(highs[-20:])
    low20 = min(lows[-20:])
    resistance = high20
    support = ma20 if ma20 else low20
    support_ma5 = ma5 is not None and close >= ma5 and abs(close / ma5 - 1) <= MA_SUPPORT_TOLERANCE
    support_ma10 = ma10 is not None and close >= ma10 and abs(close / ma10 - 1) <= MA_SUPPORT_TOLERANCE

    # 布林带位置 0-1
    boll_pos = None
    if boll_u[idx] and boll_l[idx] and boll_u[idx] != boll_l[idx]:
        boll_pos = (close - boll_l[idx]) / (boll_u[idx] - boll_l[idx])

    # ============ 百分制评分 ============
    trend_scores = {"强势多头": 30, "多头排列": 26, "弱势多头": 18, "盘整": 12,
                    "弱势空头": 8, "空头排列": 4, "强势空头": 0}
    volume_scores = {"缩量回调": 15, "放量上涨": 12, "量能正常": 10, "缩量上涨": 6, "放量下跌": 0}
    macd_scores = {"零上金叉": 15, "金叉": 12, "上穿零轴": 10, "多头": 8,
                   "空头": 2, "下穿零轴": 0, "死叉": 0}
    rsi_scores = {"超卖": 10, "强势": 8, "中性": 5, "弱势": 3, "超买": 0}

    score = 0
    score += trend_scores.get(trend_status, 12)
    score += _score_bias(bias5, trend_status, trend_strength)
    score += volume_scores.get(volume_status, 10)
    s_sup = (5 if support_ma5 else 0) + (5 if support_ma10 else 0)
    score += s_sup
    score += macd_scores.get(macd_status, 2)
    score += rsi_scores.get(rsi_status, 5)
    score = max(0, min(100, int(round(score))))

    # ============ 信号映射（含趋势过滤） ============
    score_signal = signal_key_for_score(score)
    bull_trends = {"强势多头", "多头排列", "弱势多头"}
    bear_trends = {"空头排列", "强势空头"}
    if score_signal == "strong_buy" and trend_status in {"强势多头", "多头排列"}:
        final = "strong_buy"
    elif score_signal in ("strong_buy", "buy") and trend_status in bull_trends:
        final = "buy"
    elif score_signal in ("strong_buy", "buy") and trend_status in {"盘整", "弱势空头"}:
        final = "watch"
    elif score_signal == "watch":
        final = "watch"
    elif score_signal == "sell" or trend_status in bear_trends:
        final = "sell"
    else:
        final = "reduce" if score_signal == "reduce" else "sell"
    signal_label = signal_label_for_key(final)

    # ============ 买卖点位 ============
    ideal_buy = ma5
    secondary_buy = ma10
    stop_loss = ma20
    take_profit = resistance

    return AnalysisResult(
        name=name, code=code, date=dates[idx], close=round(close, 3),
        change_pct=round(change_pct, 2),
        trend_status=trend_status, trend_strength=trend_strength,
        ma5=_r(ma5), ma10=_r(ma10), ma20=_r(ma20), ma60=_r(ma60),
        bias_ma5=_r(bias5), bias_ma10=_r(bias10), bias_ma20=_r(bias20),
        volume_ratio=_r(vratio), volume_status=volume_status,
        support=_r(support), resistance=_r(resistance),
        support_ma5=support_ma5, support_ma10=support_ma10,
        macd_status=macd_status, macd_dif=_r(dif[idx]), macd_dea=_r(dea[idx]), macd_bar=_r(bar[idx]),
        rsi6=_r(rsi6[idx], 1), rsi12=_r(rsi12[idx], 1), rsi24=_r(rsi24[idx], 1),
        rsi_status=rsi_status,
        boll_pos=_r(boll_pos, 3),
        score=score, signal_key=final, signal=signal_label,
        ideal_buy=_r(ideal_buy), secondary_buy=_r(secondary_buy),
        stop_loss=_r(stop_loss), take_profit=_r(take_profit),
        high20=_r(high20), low20=_r(low20),
        change_60d=_r(ind.pct_change(closes, idx, 60)),
    )


def _r(v, nd=2):
    return round(v, nd) if v is not None else None
