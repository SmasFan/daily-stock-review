"""
选股推荐模块。

结合两部分逻辑（迁移自股票分析项目）：
1. 个股技术信号分（src/analyzer.py 的百分制评分）——衡量单票技术形态
2. 横截面多因子打分（迁移自 screening/scorer.py）——在同一股票池内比较：
   - 动量 momentum：当日涨跌 + 60日趋势（过热/破位惩罚）
   - 价值 value：PE/PB 横截面分位（越低越好）
   - 流动性 liquidity：成交额分位
   - 活跃度 activity：量比/换手率（理想区间）
   - 稳定性 stability：低波动/低回撤加分

推荐流程（对齐 L1硬过滤 → 打分 → TopK → 输出）：
- 硬过滤：剔除 ST/退市、价格过低、成交额过低
- 综合分 = 技术分 * tech_weight + 横截面分 * (1 - tech_weight)
- 输出 TopN 推荐，含信号、买卖点位、评分理由
"""
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict


def rank_pct(values: List[float], higher_is_better: bool = True) -> List[float]:
    """横截面百分位排名（0-100）。相同值取平均名次。无效值(None)给 25。"""
    n = len(values)
    if n == 0:
        return []
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    scores = [25.0] * n
    if not valid:
        return scores
    sorted_vals = sorted((v for _, v in valid), reverse=higher_is_better)
    rank_map: Dict[float, List[int]] = {}
    for i, v in enumerate(sorted_vals):
        rank_map.setdefault(v, []).append(i)
    m = len(sorted_vals)
    for i, v in valid:
        ranks = rank_map[v]
        scores[i] = sum(ranks) / len(ranks) / max(m - 1, 1) * 100
    return scores


@dataclass
class ScreenItem:
    name: str
    code: str
    sector: str = ""
    price: float = 0.0
    change_pct: float = 0.0
    amount: float = 0.0
    turnover: float = 0.0
    pe: Optional[float] = None
    pb: Optional[float] = None
    volume_ratio: Optional[float] = None
    # 技术面
    tech_score: float = 50.0
    signal_key: str = "watch"
    signal: str = "观望"
    trend_status: str = ""
    change_60d: Optional[float] = None
    # 因子分
    momentum_score: float = 50.0
    value_score: float = 50.0
    liquidity_score: float = 50.0
    activity_score: float = 50.0
    stability_score: float = 50.0
    cross_score: float = 50.0
    # 综合
    total_score: float = 50.0
    rating: str = "C"
    # 买卖点位
    ideal_buy: Optional[float] = None
    secondary_buy: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reasons: str = ""

    def to_dict(self):
        return asdict(self)


def rating_of(total: float) -> str:
    if total >= 80:
        return "A"
    if total >= 60:
        return "B"
    if total >= 40:
        return "C"
    if total >= 20:
        return "D"
    return "E"


def screen(items: List[ScreenItem], tech_weight: float = 0.5, top_n: int = 10) -> List[ScreenItem]:
    """对股票池做横截面打分并输出排序。items 需已填充 tech_score/price/amount/pe/pb/turnover/volume_ratio/change_pct/change_60d。"""
    # ---- L1 硬过滤 ----
    pool = []
    for it in items:
        nm = it.name.upper()
        if "ST" in nm or "退" in it.name:
            continue
        if it.price <= 0:
            continue
        pool.append(it)
    if not pool:
        return []

    # ---- 横截面因子 ----
    amounts = [it.amount for it in pool]
    pes = [it.pe if (it.pe and it.pe > 0) else None for it in pool]
    pbs = [it.pb if (it.pb and it.pb > 0) else None for it in pool]
    liquidity = rank_pct(amounts, higher_is_better=True)
    pe_rank = rank_pct(pes, higher_is_better=False)
    pb_rank = rank_pct(pbs, higher_is_better=False)

    for i, it in enumerate(pool):
        # 动量：当日 60 + change*5（>5% 追高惩罚）；60日 55 + change60*0.9（>45% 过热、<-20% 破位）
        day = 60 + it.change_pct * 5
        if it.change_pct > 5:
            day -= (it.change_pct - 5) * 10
        elif it.change_pct < -2:
            day -= (-2 - it.change_pct) * 3
        if it.change_60d is not None:
            trend = 55 + it.change_60d * 0.9
            if it.change_60d > 45:
                trend -= (it.change_60d - 45) * 1.5
            elif it.change_60d < -20:
                trend -= (-20 - it.change_60d) * 1.0
        else:
            trend = 50
        it.momentum_score = max(0, min(100, day * 0.5 + trend * 0.5))

        it.value_score = max(0, min(100, (pe_rank[i] + pb_rank[i]) / 2))
        it.liquidity_score = liquidity[i]

        # 活跃度：量比理想 2.0、换手理想 4%
        vr = it.volume_ratio or 1.0
        vr_score = max(0, 100 - abs(vr - 2.0) * 15)
        if vr > 5:
            vr_score -= (vr - 5) * 8
        to = it.turnover
        to_score = max(0, 100 - abs(to - 4.0) * 10) if to > 0 else 50
        if to > 12:
            to_score -= (to - 12) * 5
        it.activity_score = max(0, min(100, vr_score * 0.5 + to_score * 0.5))

        # 稳定性：基准 78，按 |涨跌|、高换手、极端量比扣分
        stab = 78.0
        stab -= min(abs(it.change_pct) * 4, 20)
        if to > 8:
            stab -= (to - 8) * 2
        if vr > 4:
            stab -= (vr - 4) * 3
        it.stability_score = max(0, min(100, stab))

        # 横截面综合分（对齐原 scorer 权重结构）
        non_tech = (1 - tech_weight)
        it.cross_score = max(0, min(100,
            it.value_score * (non_tech * 0.5)
            + it.liquidity_score * (non_tech * 0.25)
            + it.stability_score * (non_tech * 0.25)
            + it.momentum_score * (tech_weight * 0.55)
            + it.activity_score * (tech_weight * 0.45)))

        # 综合总分：技术分 + 横截面分
        it.total_score = round(it.tech_score * tech_weight + it.cross_score * (1 - tech_weight), 1)
        it.rating = rating_of(it.total_score)

        # 推荐理由
        reasons = []
        if it.signal_key in ("strong_buy", "buy"):
            reasons.append(f"技术信号「{it.signal}」({it.tech_score:.0f}分)")
        if it.trend_status in ("强势多头", "多头排列"):
            reasons.append(it.trend_status)
        if it.momentum_score >= 70:
            reasons.append("动量强")
        if it.value_score >= 70:
            reasons.append("估值低")
        if it.stability_score >= 70:
            reasons.append("波动低")
        it.reasons = "、".join(reasons) if reasons else "综合排名靠前"

    pool.sort(key=lambda x: x.total_score, reverse=True)
    return pool[:top_n]


def build_buy_reason(it: "ScreenItem") -> str:
    """生成"当日购买原因"（规则化，基于技术信号/趋势/动量/买卖点位）。

    对齐股票分析项目的交易纪律：
    - 严进：乖离>5% 不追高
    - 趋势：只做多头排列
    - 买点：MA5/MA10 附近最佳，MA20 止损
    - 动量/估值/波动
    """
    parts = []
    if it.trend_status in ("强势多头", "多头排列"):
        parts.append(f"均线{it.trend_status}，趋势向上")
    elif it.trend_status == "弱势多头":
        parts.append("均线转多但未完全多头排列")
    if it.signal_key in ("strong_buy", "buy"):
        parts.append(f"技术信号「{it.signal}」(评分{it.tech_score:.0f})")
    if it.change_60d is not None:
        if 0 < it.change_60d <= 35:
            parts.append(f"60日涨幅{it.change_60d:.0f}%，处于健康上行区间")
        elif it.change_60d > 45:
            parts.append(f"60日已涨{it.change_60d:.0f}%，注意过热风险")
    if it.value_score >= 70:
        parts.append("估值处于池内低位")
    if it.momentum_score >= 70:
        parts.append("当日动量强")
    if it.stability_score >= 70:
        parts.append("波动小、回撤可控")
    if it.ideal_buy and it.price:
        dist = (it.price - it.ideal_buy) / it.ideal_buy * 100
        if dist <= 2:
            parts.append(f"现价距理想买点({it.ideal_buy})仅{dist:.1f}%")
    if it.stop_loss:
        parts.append(f"止损位{it.stop_loss}，破位离场")
    if not parts:
        parts.append("综合评分靠前，可小仓跟踪")
    return "；".join(parts)
