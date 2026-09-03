"""
网格策略操作提醒：基于均值线偏离度对自选股生成买卖提示。

口径（对齐 src/grid_backtest.py）：
- 均值线：750 日滚动分位估值锚（PE/PB/ROE），无估值时退化为价格均线
- 仓位规则：pos = base_pos - slope*dev，每涨 0.5% 抛 1%，每跌 0.5% 买 1.04%
- 半永久锁仓：dev<=-5% 后加仓锁仓；dev>=+5% 超涨清仓
- 操作提醒：以「目标仓位 vs 当前策略仓位」差值给出方向，dev 越偏越强
"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from typing import Dict, List, Optional, Tuple

from . import grid_backtest as gbt
from . import data_provider as dp
from . import stock_pool as sp

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKTEST_INDEX_PATH = os.path.join(BASE_DIR, "data", "backtest_index.json")
BACKTEST_SPLIT_DIR = os.path.join(BASE_DIR, "data", "backtest")


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
    # 与回测口径一致：解析 ATR 动态步长
    gparams = gbt.resolve_grid_params(a, gparams)
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
    """从已有回测结果生成信号，避免重复拉长K线/估值。

    stock 可为完整单只文件（含 close/dates/dev/position/anchor）或精简索引条目
    （仅 summary）。完整数据给出现价；仅摘要时现价置 None，由调用方用行情快照补齐。
    """
    gparams = gparams or gbt.GridParams()
    # 与回测口径一致：复用回测实际使用的 ATR 动态步长
    step = (stock.get("summary") or {}).get("grid_step")
    if step:
        gparams = replace(gparams, grid_step=step)
    devs = stock.get("dev") or []
    positions = stock.get("position") or []
    dates = stock.get("dates") or []
    anchors = stock.get("anchor") or []
    if not devs or devs[-1] is None or not dates:
        return None
    dev = devs[-1]
    pos = positions[-1] if positions else None
    sig = grid_action(dev, pos, gparams)
    last_close = stock.get("close")
    if isinstance(last_close, (list, tuple)):
        last_close = last_close[-1] if last_close else None
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


def _bt_index_members() -> Tuple[Dict, Dict]:
    """读取 backtest 索引（含拆分文件路径）。返回 (members, index)。

    members: {code: {name, stock}}，代码优先匹配。无索引/读取失败返回空。
    """
    index = {}
    if os.path.exists(BACKTEST_INDEX_PATH):
        try:
            with open(BACKTEST_INDEX_PATH, "r", encoding="utf-8") as fp:
                index = json.load(fp) or {}
        except Exception:
            index = {}
    bt_stocks = index.get("stocks") or {}
    try:
        backtest_meta = sp.BACKTEST_CODES
    except Exception:
        backtest_meta = []
    name_to_code = {meta["name"]: meta["code"] for meta in backtest_meta}
    code_to_name = {meta["code"]: meta["name"] for meta in backtest_meta}
    # 先补索引里没有 code 映射的（索引按 name 键存储）
    members = {}
    for name, entry in bt_stocks.items():
        code = name_to_code.get(name, "")
        if not code:
            continue
        members.setdefault(code, {"name": name, "stock": entry})
    return members, index


def _load_split_stock(code: str) -> Optional[Dict]:
    """加载单只拆分文件（data/backtest/{name}.json），失败返回 None。"""
    name = ""
    try:
        for meta in sp.BACKTEST_CODES:
            if meta["code"] == code:
                name = meta["name"]
                break
    except Exception:
        return None
    if not name:
        return None
    path = os.path.join(BACKTEST_SPLIT_DIR, name + ".json")
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return None


def build_grid_signals(codes: List[Tuple[str, str]],
                       gparams: Optional[gbt.GridParams] = None,
                       ap: Optional[gbt.AnchorParams] = None,
                       backtest_data: Optional[Dict] = None,
                       holding_codes: Optional[set] = None) -> List[Dict]:
    """批量计算网格信号。codes: [(name, code), ...]。返回按操作优先级排序列表。

    复用顺序（避免重复拉长K线/估值）：
    1. 完整拆分文件 data/backtest/{name}.json（含最新 dev/position/close，当日盘后已刷新）
    2. 索引/传入 backtest_data 的精简摘要（dev/position 序列缺失时跳过）
    其余标的现场计算。
    backtest_data: 兼容旧参数，传 backtest_index.json 结构即可；缺省自动读索引。
    holding_codes: 持仓代码集合，命中时标记 is_holding=True。
    """
    holding_codes = holding_codes or set()
    gparams = gparams or gbt.GridParams()
    order = {"clear": 0, "buy": 1, "reduce": 2, "hold": 3, "wait": 4}
    members, index = _bt_index_members()
    if backtest_data:
        index = backtest_data if isinstance(backtest_data, dict) else {}
        members = {}
        bt_stocks = index.get("stocks") or {}
        try:
            bt_meta = sp.BACKTEST_CODES
        except Exception:
            bt_meta = []
        name_to_code = {m["name"]: m["code"] for m in bt_meta}
        for nm, entry in bt_stocks.items():
            c = name_to_code.get(nm, "")
            if c:
                members.setdefault(c, {"name": nm, "stock": entry})

    out = []
    done = set()
    for name, code in codes:
        member = members.get(code)
        if not member:
            continue
        bt_name = member["name"]
        s = _load_split_stock(code) or member["stock"]
        if not s:
            continue
        if not (s.get("dev") or s.get("position")):
            continue  # 精简摘要无序列，走现场计算
        sig = signal_from_backtest(bt_name, code, s, gparams)
        if sig:
            sig["is_holding"] = code in holding_codes
            sig["_prio"] = (0 if sig["is_holding"] else 1, order.get(sig["action_key"], 5))
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
            s["is_holding"] = code in holding_codes
            s["_prio"] = (0 if s["is_holding"] else 1, order.get(s["action_key"], 5))
            out.append(s)
    out.sort(key=lambda x: x["_prio"])
    for x in out:
        x.pop("_prio", None)
    return out
