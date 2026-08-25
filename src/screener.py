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


SIG_STRENGTH = {"strong_buy": 0, "buy": 1, "watch": 2, "reduce": 3, "sell": 4}


def strength_key(it) -> int:
    """推荐强度排序键：strong_buy(0) > buy(1) > watch(2) > reduce(3) > sell(4)。"""
    return SIG_STRENGTH.get(it.signal_key, 5)


def rank_pct(values: List[float], higher_is_better: bool = True) -> List[float]:
    """横截面百分位排名（0-100，0=最差 100=最好）。相同值取平均名次。无效值(None)给 25。

    2026-08-07 修正：旧版把"名次位置"直接当分数，方向与参数相反——
    高PE股拿到 100 的"估值分"（PE 4621 的股票反而进估值低位榜）。
    现按 higher_is_better 语义归一：数值最优 → 100。
    """
    n = len(values)
    if n == 0:
        return []
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    scores = [25.0] * n
    if not valid:
        return scores
    sorted_vals = sorted(v for _, v in valid)
    rank_map: Dict[float, List[int]] = {}
    for i, v in enumerate(sorted_vals):
        rank_map.setdefault(v, []).append(i)
    m = len(sorted_vals)
    for i, v in valid:
        ranks = rank_map[v]
        p = sum(ranks) / len(ranks) / max(m - 1, 1)
        scores[i] = p * 100 if higher_is_better else (1 - p) * 100
    return scores


@dataclass
class ScreenItem:
    name: str
    code: str
    sector: str = ""
    price: float = 0.0
    open: float = 0.0
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
    # 持仓标记（在推荐列表中置顶并展示徽章）
    is_holding: bool = False
    # 普涨过热日被降档标记（2026-08-07 新增：未跑赢大盘的买入信号降为观望）
    overheat_downgraded: bool = False
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
    # 价值因子（2026-08 修正）：板块内分位优先（同行业才可比：银行 PE~6 与
    # 科技 PE~50 混排会把"低PE行业"系统性顶高）；板块样本 <5 时回退全池分位
    pool_pe_rank = rank_pct(pes, higher_is_better=False)
    pool_pb_rank = rank_pct(pbs, higher_is_better=False)
    pe_rank, pb_rank = list(pool_pe_rank), list(pool_pb_rank)
    sect_groups: Dict[str, List[int]] = {}
    for i, it in enumerate(pool):
        sect_groups.setdefault((it.sector or "").strip() or "其他", []).append(i)
    for sec, idxs in sect_groups.items():
        if len(idxs) < 5:
            continue
        sec_pe = [pes[i] for i in idxs]
        sec_pb = [pbs[i] for i in idxs]
        sec_pe_rank = rank_pct(sec_pe, higher_is_better=False)
        sec_pb_rank = rank_pct(sec_pb, higher_is_better=False)
        for j, i in enumerate(idxs):
            pe_rank[i] = sec_pe_rank[j]
            pb_rank[i] = sec_pb_rank[j]

    for i, it in enumerate(pool):
        # 动量（2026-08 降权）：当日涨跌只占 30%（单日动量是噪音），
        # 60 日趋势占 70%；且横截面动量权重下调（技术分已含趋势，避免双重计费）
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
        it.momentum_score = max(0, min(100, day * 0.3 + trend * 0.7))

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

        # 横截面综合分（2026-08 权重修正：价值升权+板块内分位、动量降权避免
        # 与技术分重复计费、活跃度降权减少对日内热度的追逐）
        it.cross_score = max(0, min(100,
            it.value_score * 0.30
            + it.liquidity_score * 0.15
            + it.stability_score * 0.20
            + it.momentum_score * 0.20
            + it.activity_score * 0.15))

        # 综合总分：技术分 + 横截面分
        it.total_score = round(it.tech_score * tech_weight + it.cross_score * (1 - tech_weight), 1)
        it.rating = rating_of(it.total_score)

        # 推荐理由（2026-08-11 修正：卖出/减仓信号只展示卖出依据，不再罗列利好因素）
        if it.signal_key in ("sell", "reduce"):
            it.reasons = f"{it.signal}：{it.trend_status or '趋势走弱'}"
        else:
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


def apply_market_regime(items: List[ScreenItem], breadth_up_ratio: Optional[float],
                        index_change: Optional[float],
                        overheat_threshold: float = 0.65,
                        cold_temp: Optional[float] = None,
                        cold_threshold: float = 20) -> Dict:
    """普涨过热日闸门 + 急跌低温闸门 + 相对强度过滤。

    2026-08-07 新增：普涨日技术分整体抬升，绝对阈值(评分≥60)让全市场都变成"买入"。
    对策（配合全市场涨跌家数）：
    - 当 breadth_up_ratio（全市场上涨家数占比）≥ overheat_threshold 视为过热日
    - 过热日：只有当日跑赢大盘（change_pct ≥ index_change）的 strong_buy/buy
      保留买入信号，其余降为观望（watch），避免普涨日追高
    2026-08 新增：急跌低温闸门——市场温度 < cold_threshold（默认 20）时，
    趋势/买点信号整体失真，buy 信号全部降为观望，仅保留 strong_buy 且跑赢大盘者。
    依据：跟踪回测显示 8/10、8/14、8/18 等急跌日（温度<20）推荐胜率仅 10-20%。
    - 指数数据缺失时用池内平均涨幅作代理基准
    - 非过热/非低温日不干预，信号维持原样

    返回 {overheat, cold, threshold, breadth_up_ratio, benchmark, downgraded: [names]}。
    """
    downgraded = []
    overheat = False
    cold = False
    if breadth_up_ratio is not None and index_change is not None:
        overheat = breadth_up_ratio >= overheat_threshold
        if overheat:
            for it in items:
                if it.signal_key not in ("strong_buy", "buy"):
                    continue
                if it.change_pct < index_change:
                    it.signal_key = "watch"
                    it.signal = "观望"
                    it.overheat_downgraded = True
                    downgraded.append(it.name)
    if cold_temp is not None and cold_temp < cold_threshold:
        cold = True
        for it in items:
            if it.signal_key == "buy":
                it.signal_key = "watch"
                it.signal = "观望"
                it.overheat_downgraded = True
                downgraded.append(it.name)
    return {"overheat": overheat, "cold": cold,
            "threshold": overheat_threshold, "cold_threshold": cold_threshold,
            "breadth_up_ratio": round(breadth_up_ratio, 4) if breadth_up_ratio is not None else None,
            "benchmark": index_change,
            "downgraded": downgraded}


def build_buy_reason(it: "ScreenItem") -> str:
    """生成"当日购买原因"（规则化，基于技术信号/趋势/动量/买卖点位）。

    对齐股票分析项目的交易纪律：
    - 严进：乖离>5% 不追高
    - 趋势：只做多头排列
    - 买点：MA5/MA10 附近最佳，MA20 止损
    - 动量/估值/波动
    """
    # 2026-08-11 新增：卖出/减仓信号只展示卖出依据，不再罗列利好
    if it.signal_key in ("sell", "reduce"):
        return f"{it.signal}：{it.trend_status or '趋势走弱'}，建议逢高减仓/离场"
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
    if it.overheat_downgraded:
        parts.append("普涨过热日未跑赢大盘，降为观望，暂缓追高")
    if not parts:
        parts.append("综合评分靠前，可小仓跟踪")
    return "；".join(parts)
