"""
网格策略操作提醒：基于均值线偏离度对自选股生成买卖提示。

口径（对齐 src/grid_backtest.py）：
- 均值线：750 日滚动分位估值锚（PE/PB/ROE），无估值时退化为价格均线
- 仓位规则：pos = base_pos - slope*dev，每涨 0.5% 抛 1%，每跌 0.5% 买 1.04%
- 半永久锁仓：dev<=-5% 后加仓锁仓；dev>=+5% 超涨清仓
- 操作提醒：以「目标仓位 vs 当前策略仓位」差值给出方向，dev 越偏越强
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from . import grid_backtest as gbt
from . import data_provider as dp


def grid_action(dev: Optional[float], pos: Optional[float],
                p: gbt.GridParams) -> Dict:
    """由最新偏离度与当前仓位，推导操作提醒。

    返回 {action, action_key, target_pos, dev, grid_idx, reason}。
    action_key 取 buy / reduce / hold / clear / wait。
    """
    if dev is None or pos is None:
        return {"action": "数据不足", "action_key": "hold", "target_pos": None,
                "dev": dev, "grid_idx": None, "reason": "均值线样本不足，无法计算偏离度"}

    target = gbt.position_target(dev, p)
    grid = gbt.grid_index(dev, p)
    diff = target - pos

    if dev >= p.dev_clear:
        action, key = "超涨清仓", "clear"
        hint = f"偏离均值线 {dev:+.2%}（≥+{p.dev_clear:.0%}），策略超涨清仓，建议分批卖出离场"
    elif diff > 0.05:
        action, key = "逢低加仓", "buy"
        hint = f"偏离均值线 {dev:+.2%}，目标仓位 {target:.0%} 高于当前 {pos:.0%}，越跌越买"
    elif diff < -0.05:
        action, key = "逢高减仓", "reduce"
        hint = f"偏离均值线 {dev:+.2%}，目标仓位 {target:.0%} 低于当前 {pos:.0%}，越涨越抛"
    elif pos <= 0.12:
        action, key = "空仓等待", "wait"
        hint = f"偏离均值线 {dev:+.2%}，策略仓位已降至下限 {p.min_pos:.0%}，暂持币观望等回踩"
    elif pos >= 0.98:
        action, key = "满仓锁仓", "hold"
        hint = f"偏离均值线 {dev:+.2%}，策略仓位已到上限 100%，回踩均值前持有不动"
    else:
        action, key = "持有观察", "hold"
        hint = f"偏离均值线 {dev:+.2%}，目标仓位 {target:.0%} 与当前 {pos:.0%} 接近，维持现有仓位"

    return {"action": action, "action_key": key, "target_pos": round(target, 4),
            "dev": round(dev, 4), "grid_idx": grid, "reason": hint}


def compute_grid_signal(name: str, code: str,
                        gparams: Optional[gbt.GridParams] = None,
                        ap: Optional[gbt.AnchorParams] = None) -> Optional[Dict]:
    """对单个标的计算网格策略操作提醒。

    复用网格回测的均值线与仓位计算，取最新一日信号。
    """
    gparams = gparams or gbt.GridParams()
    ap = ap or gbt.AnchorParams(lookback_days=750, min_periods=500)
    try:
        k = dp.fetch_daily_kline_long(code, count=3200)
    except Exception:
        return None
    if not k or len(k["closes"]) < 300:
        return None
    try:
        panel = gbt.build_panel(name, code, k["dates"], k["opens"], k["closes"],
                                k["highs"], k["lows"], k["volumes"])
    except Exception:
        return None
    if not panel:
        return None
    # 数据不足时自适应缩短均值线窗口（对齐 run_grid_backtest）
    n_all = len(panel["close"])
    eff_ap = ap
    if n_all < ap.lookback_days:
        eff_lb = max(ap.min_periods, n_all - 250)
        eff_lb = min(eff_lb, max(200, n_all // 2))
        eff_ap = gbt.AnchorParams(
            lookback_days=eff_lb,
            fair_pct=ap.fair_pct,
            min_periods=max(120, min(ap.min_periods, n_all // 3)),
            weights=ap.weights,
        )
    try:
        a = gbt.compute_anchor(panel, eff_ap)
    except Exception:
        return None
    n = len(a["close"])
    if n < 2:
        return None
    dev = a["dev"][-1]
    pos = gbt.position_target(dev, gparams) if dev is not None else None
    sig = grid_action(dev, pos, gparams)
    last_close = a["close"][-1]
    anchor = a["anchor"][-1]
    return {
        "name": name,
        "code": code,
        "date": a["date"][-1],
        "close": round(last_close, 4) if last_close is not None else None,
        "anchor": round(anchor, 4) if anchor is not None else None,
        "price_only": a.get("price_only", False),
        "position": round(pos, 4) if pos is not None else None,
        "action": sig["action"],
        "action_key": sig["action_key"],
        "target_pos": sig["target_pos"],
        "dev": sig["dev"],
        "grid_idx": sig["grid_idx"],
        "reason": sig["reason"],
    }


def signal_from_backtest(name: str, code: str, stock: Dict,
                         gparams: Optional[gbt.GridParams] = None) -> Optional[Dict]:
    """从已有的回测结果（backtest_data.json 的 stocks[name]）生成信号，避免重复计算。

    stock 结构对齐 build_backtest_data：{summary, equity, benchmark, dates, position, dev, trades, price_only}
    """
    gparams = gparams or gbt.GridParams()
    devs = stock.get("dev") or []
    positions = stock.get("position") or []
    dates = stock.get("dates") or []
    anchors = stock.get("anchor") or []
    if not devs or devs[-1] is None or not dates:
        return None
    dev = devs[-1]
    pos = positions[-1] if positions else None
    sig = grid_action(dev, pos, gparams)
    last_close = None
    anchor = anchors[-1] if anchors else None
    return {
        "name": name,
        "code": code,
        "date": dates[-1],
        "close": last_close,
        "anchor": round(anchor, 4) if anchor is not None else None,
        "price_only": bool(stock.get("price_only", False)),
        "position": round(pos, 4) if pos is not None else None,
        "action": sig["action"],
        "action_key": sig["action_key"],
        "target_pos": sig["target_pos"],
        "dev": sig["dev"],
        "grid_idx": sig["grid_idx"],
        "reason": sig["reason"],
    }


def build_grid_signals(codes: List[Tuple[str, str]],
                       gparams: Optional[gbt.GridParams] = None,
                       ap: Optional[gbt.AnchorParams] = None,
                       backtest_data: Optional[Dict] = None) -> List[Dict]:
    """批量计算网格信号。codes: [(name, code), ...]。返回按操作优先级排序列表。

    backtest_data: 可选，若提供则优先复用其中已有的回测结果（按名称匹配，
    并自动用回测标的代码兜底），其余标的再现场计算，避免重复拉取长K线/估值数据。
    """
    out = []
    order = {"clear": 0, "buy": 1, "reduce": 2, "hold": 3, "wait": 4}
    done = set()
    if backtest_data:
        bt_stocks = backtest_data.get("stocks") or {}
        # 名称 -> code 映射（BACKTEST_CODES 顺序提供真实代码，用于按 code 兜底匹配）
        name_to_code = {}
        try:
            from .stock_pool import BACKTEST_CODES
            name_to_code = {meta["name"]: meta["code"] for meta in BACKTEST_CODES}
        except Exception:
            name_to_code = {}
        for name, code in codes:
            s = bt_stocks.get(name)
            bt_name = name
            if not s:
                # 名称可能带空格/变形，用 code 反查真实名称
                for k, v in name_to_code.items():
                    if v == code and k in bt_stocks:
                        bt_name = k
                        break
                s = bt_stocks.get(bt_name)
            if not s:
                continue
            sig = signal_from_backtest(bt_name, code, s, gparams)
            if sig:
                sig["_prio"] = order.get(sig["action_key"], 5)
                out.append(sig)
                done.add(code)
    for name, code in codes:
        if code in done:
            continue
        try:
            s = compute_grid_signal(name, code, gparams, ap)
        except Exception:
            s = None
        if s:
            s["_prio"] = order.get(s["action_key"], 5)
            out.append(s)
    out.sort(key=lambda x: x["_prio"])
    for x in out:
        x.pop("_prio", None)
    return out
