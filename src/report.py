"""
复盘报告数据生成模块：把分析/选股/回测结果汇总为前端可消费的 JSON。

输出：
- data/review_data.json    复盘页数据（信号表 + 标的详情 + 大盘温度计）
- data/recommend_data.json 推荐页数据（TopN 推荐 + 理由）
- data/backtest_data.json  回测页数据（汇总指标 + 逐条结果 + 净值）
"""
import json
import os
from datetime import datetime
from typing import List, Dict, Optional

from . import analyzer as az
from . import screener as scr
from . import backtest as bt

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)


def _market_temperature(results: List[az.AnalysisResult]) -> Dict:
    """大盘温度计（迁移自 market_analyzer 的 light score 思路，适配股票池）。"""
    if not results:
        return {"score": 50, "label": "数据不足"}
    up = sum(1 for r in results if r.change_pct > 0)
    breadth = up / len(results) * 100
    avg_chg = sum(r.change_pct for r in results) / len(results)
    index_score = max(0, min(100, 50 + avg_chg * 12))
    bull = sum(1 for r in results if r.signal_key in ("strong_buy", "buy"))
    bear = sum(1 for r in results if r.signal_key in ("sell", "reduce"))
    sig = bull / max(bull + bear, 1) * 100
    score = round(breadth * 0.45 + index_score * 0.35 + sig * 0.20)
    if score >= 70:
        label = "强势"
    elif score >= 55:
        label = "偏暖"
    elif score >= 40:
        label = "震荡"
    else:
        label = "偏弱"
    return {"score": score, "label": label,
            "breadth": round(breadth, 1), "avg_change": round(avg_chg, 2),
            "bull": bull, "bear": bear, "total": len(results)}


def build_indices(index_rows: List[Dict]) -> List[Dict]:
    """大盘指数行数据。index_rows 元素: {name, close, change_pct, trend_status}"""
    return index_rows


def build_review(market_name: str, analyses: List[az.AnalysisResult],
                 review_type: str = "post",
                 indices: Optional[List[Dict]] = None,
                 market_analyses: Optional[List[az.AnalysisResult]] = None) -> Dict:
    """生成复盘页数据。

    - items: 自选模块分析结果
    - market_items: 大盘模块（从大盘池中选出的标的）
    - indices: 大盘指数表现
    """
    rows = [a.to_dict() for a in analyses]
    temp = _market_temperature(analyses)
    up = sum(1 for a in analyses if a.change_pct > 0)
    down = sum(1 for a in analyses if a.change_pct < 0)
    by_score = sorted(analyses, key=lambda x: x.score, reverse=True)
    market_rows = [a.to_dict() for a in (market_analyses or [])]
    market_temp = _market_temperature(market_analyses) if market_analyses else None
    return {
        "market": market_name,
        "type": review_type,
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "temperature": temp,
        "indices": indices or [],
        "stats": {"up": up, "down": down, "total": len(analyses),
                  "strongest": by_score[0].name if by_score else "",
                  "weakest": by_score[-1].name if by_score else ""},
        "items": rows,
        "market_items": market_rows,
        "market_temperature": market_temp,
    }


def build_recommend(items: List[scr.ScreenItem], market_items: Optional[List[scr.ScreenItem]] = None,
                    indices: Optional[List[Dict]] = None, sectors: Optional[List[Dict]] = None,
                    sector_picks: Optional[List[scr.ScreenItem]] = None,
                    grid_signals: Optional[List[Dict]] = None) -> Dict:
    return {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(items),
        "picks": [it.to_dict() for it in items],
        "market_picks": [it.to_dict() for it in (market_items or [])],
        "sectors": sectors or [],
        "sector_picks": [it.to_dict() for it in (sector_picks or [])],
        "indices": indices or [],
        "grid_signals": grid_signals or [],
    }


def build_backtest(per_stock: Dict[str, Dict]) -> Dict:
    """per_stock: {name: {results:[...], summary:{...}, equity:{...}}}"""
    overall_results = []
    for v in per_stock.values():
        overall_results.extend(v.get("results", []))
    overall_summary = bt.compute_summary(overall_results)
    return {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overall": overall_summary,
        "stocks": {name: {"summary": v.get("summary", {}),
                          "results": [r.to_dict() for r in v.get("results", [])]}
                   for name, v in per_stock.items()},
    }


def save(name: str, data: Dict) -> str:
    path = os.path.join(DATA_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path
