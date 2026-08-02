"""
信号回测引擎。

逻辑迁移自股票分析项目 daily_stock_analysis/src/core/backtest_engine.py：
将历史信号（买入/卖出/观望等）对照之后 N 个交易日的真实行情，评估信号是否兑现。
- long-only、日线级别
- 方向推断：买入类=up，卖出类=down，持有类=not_down，观望=flat
- 胜负判定：中性带宽 ±2%，|收益|在带内为 neutral
- 模拟交易：按起始价入场，逐 bar 检查止损/止盈触发，否则窗口末出场
- 汇总：胜率、方向准确率、平均收益、止损止盈触发率、最大回撤
"""
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict

NEUTRAL_BAND_PCT = 2.0


@dataclass
class Bar:
    date: str
    high: float
    low: float
    close: float


@dataclass
class SignalRecord:
    """一条历史信号。"""
    date: str            # 信号产生日
    signal_key: str      # strong_buy/buy/watch/reduce/sell
    score: int = 50
    start_price: float = 0.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


@dataclass
class EvalResult:
    date: str
    signal_key: str
    score: int
    direction: str           # up/down/not_down/flat
    start_price: float
    end_close: float
    stock_return_pct: float
    outcome: str             # win/loss/neutral
    direction_correct: Optional[bool]
    simulated_return_pct: Optional[float]
    exit_reason: str = ""
    first_hit_days: Optional[int] = None
    max_high: Optional[float] = None
    min_low: Optional[float] = None

    def to_dict(self):
        return asdict(self)


def infer_direction(signal_key: str) -> str:
    if signal_key in ("strong_buy", "buy"):
        return "up"
    if signal_key in ("sell", "reduce"):
        return "down"
    return "flat"  # watch


def evaluate_single(rec: SignalRecord, forward_bars: List[Bar],
                    eval_window: int = 10, band: float = NEUTRAL_BAND_PCT) -> Optional[EvalResult]:
    """评估单条信号。forward_bars 为信号日之后的交易日序列。"""
    if not forward_bars or rec.start_price <= 0:
        return None
    bars = forward_bars[:eval_window]
    end_close = bars[-1].close
    ret = (end_close - rec.start_price) / rec.start_price * 100
    direction = infer_direction(rec.signal_key)

    # 胜负判定
    if direction == "up":
        outcome = "win" if ret >= band else ("loss" if ret <= -band else "neutral")
        direction_correct = ret > 0
    elif direction == "down":
        outcome = "win" if ret <= -band else ("loss" if ret >= band else "neutral")
        direction_correct = ret < 0
    else:  # flat
        outcome = "win" if abs(ret) <= band else "loss"
        direction_correct = abs(ret) <= band

    # 模拟交易（仅做多信号模拟入场）
    sim_ret = None
    exit_reason = "cash"
    first_hit_days = None
    if direction == "up":
        exit_price = bars[-1].close
        exit_reason = "window_end"
        for i, b in enumerate(bars):
            hit_sl = rec.stop_loss is not None and b.low <= rec.stop_loss
            hit_tp = rec.take_profit is not None and b.high >= rec.take_profit
            if hit_sl and hit_tp:
                exit_price = rec.stop_loss  # 保守按止损
                exit_reason = "ambiguous_stop_loss"
                first_hit_days = i + 1
                break
            if hit_sl:
                exit_price = rec.stop_loss
                exit_reason = "stop_loss"
                first_hit_days = i + 1
                break
            if hit_tp:
                exit_price = rec.take_profit
                exit_reason = "take_profit"
                first_hit_days = i + 1
                break
        sim_ret = (exit_price - rec.start_price) / rec.start_price * 100

    return EvalResult(
        date=rec.date, signal_key=rec.signal_key, score=rec.score,
        direction=direction, start_price=round(rec.start_price, 3),
        end_close=round(end_close, 3), stock_return_pct=round(ret, 2),
        outcome=outcome, direction_correct=direction_correct,
        simulated_return_pct=round(sim_ret, 2) if sim_ret is not None else None,
        exit_reason=exit_reason, first_hit_days=first_hit_days,
        max_high=round(max(b.high for b in bars), 3),
        min_low=round(min(b.low for b in bars), 3),
    )


def compute_summary(results: List[EvalResult]) -> Dict:
    """汇总指标。"""
    completed = [r for r in results if r is not None]
    if not completed:
        return {"total": 0}
    wins = sum(1 for r in completed if r.outcome == "win")
    losses = sum(1 for r in completed if r.outcome == "loss")
    neutrals = sum(1 for r in completed if r.outcome == "neutral")
    decisive = wins + losses
    win_rate = wins / decisive * 100 if decisive else None
    dirs = [r for r in completed if r.direction_correct is not None]
    dir_acc = sum(1 for r in dirs if r.direction_correct) / len(dirs) * 100 if dirs else None
    sims = [r.simulated_return_pct for r in completed if r.simulated_return_pct is not None]
    sl = [r for r in completed if r.exit_reason in ("stop_loss", "ambiguous_stop_loss")]
    tp = [r for r in completed if r.exit_reason == "take_profit"]
    longs = [r for r in completed if r.direction == "up"]

    # 信号分布
    breakdown: Dict[str, Dict] = {}
    for r in completed:
        b = breakdown.setdefault(r.signal_key, {"total": 0, "win": 0, "loss": 0, "neutral": 0})
        b["total"] += 1
        b[r.outcome] = b.get(r.outcome, 0) + 1
    for k, b in breakdown.items():
        d = b["win"] + b["loss"]
        b["win_rate_pct"] = round(b["win"] / d * 100, 1) if d else None

    return {
        "total": len(completed),
        "win": wins, "loss": losses, "neutral": neutrals,
        "win_rate_pct": round(win_rate, 1) if win_rate is not None else None,
        "direction_accuracy_pct": round(dir_acc, 1) if dir_acc is not None else None,
        "avg_stock_return_pct": round(sum(r.stock_return_pct for r in completed) / len(completed), 2),
        "avg_simulated_return_pct": round(sum(sims) / len(sims), 2) if sims else None,
        "stop_loss_triggers": len(sl),
        "take_profit_triggers": len(tp),
        "long_signals": len(longs),
        "signal_breakdown": breakdown,
    }


def equity_max_drawdown(closes: List[float]) -> Optional[float]:
    """净值序列最大回撤（%，负值）。"""
    if len(closes) < 2:
        return None
    peak = closes[0]
    mdd = 0.0
    for v in closes:
        if v > peak:
            peak = v
        dd = (v / peak - 1) * 100
        if dd < mdd:
            mdd = dd
    return round(mdd, 2)


def backfill_signals(dates, closes, highs, lows, volumes, opens, eval_window=10,
                     step=1, min_bars=70, analyzer_fn=None):
    """
    对一段K线历史，逐日（step间隔）回放生成信号，再用后续 eval_window 根K线验证。
    返回 (results, summary)。analyzer_fn 为 analyze_stock 兼容函数。
    """
    from . import analyzer as _az
    fn = analyzer_fn or _az.analyze_stock
    results: List[EvalResult] = []
    n = len(closes)
    for i in range(min_bars, n - eval_window, step):
        sub_dates = dates[:i + 1]
        sub_o = opens[:i + 1]
        sub_c = closes[:i + 1]
        sub_h = highs[:i + 1]
        sub_l = lows[:i + 1]
        sub_v = volumes[:i + 1]
        ar = fn("_", sub_dates, sub_o, sub_c, sub_h, sub_l, sub_v)
        if ar is None:
            continue
        fwd = [Bar(dates[j], highs[j], lows[j], closes[j])
               for j in range(i + 1, min(i + 1 + eval_window, n))]
        rec = SignalRecord(date=dates[i], signal_key=ar.signal_key, score=ar.score,
                           start_price=closes[i], stop_loss=ar.stop_loss,
                           take_profit=ar.take_profit)
        r = evaluate_single(rec, fwd, eval_window)
        if r:
            results.append(r)
    return results, compute_summary(results)
