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


def _market_temperature(results: List[az.AnalysisResult],
                        breadth: Optional[Dict] = None) -> Dict:
    """大盘温度计（迁移自 market_analyzer 的 light score 思路）。

    2026-08-07 优化（P1）：广度优先用全市场涨跌家数（src/market_breadth.py），
    替代旧版"250 只自选池广度"（有偏：自选池高配科技/红利，不代表全市场）。
    全市场数据不可用时自动回退自选池广度，并在返回中标明 source。
    """
    if not results:
        return {"score": 50, "label": "数据不足"}
    m_up = (breadth or {}).get("up") if breadth else None
    m_total = (breadth or {}).get("total") if breadth else None
    pool_up = sum(1 for r in results if r.change_pct > 0)
    if m_total:
        breadth_pct = m_up / m_total * 100
        source = (breadth or {}).get("source", "market")
        if (breadth or {}).get("stale"):
            source += "-stale"
    else:
        breadth_pct = pool_up / len(results) * 100
        source = "watchlist"
    avg_chg = sum(r.change_pct for r in results) / len(results)
    index_score = max(0, min(100, 50 + avg_chg * 12))
    bull = sum(1 for r in results if r.signal_key in ("strong_buy", "buy"))
    bear = sum(1 for r in results if r.signal_key in ("sell", "reduce"))
    sig = bull / max(bull + bear, 1) * 100
    score = round(breadth_pct * 0.45 + index_score * 0.35 + sig * 0.20)
    if score >= 70:
        label = "强势"
    elif score >= 55:
        label = "偏暖"
    elif score >= 40:
        label = "震荡"
    else:
        label = "偏弱"
    return {"score": score, "label": label,
            "breadth": round(breadth_pct, 1), "avg_change": round(avg_chg, 2),
            "bull": bull, "bear": bear, "total": len(results),
            "source": source,
            "market_up": m_up,
            "market_down": (breadth or {}).get("down"),
            "market_flat": (breadth or {}).get("flat"),
            "market_total": m_total,
            "watchlist_up": pool_up,
            "watchlist_total": len(results)}


def build_indices(index_rows: List[Dict]) -> List[Dict]:
    """大盘指数行数据。index_rows 元素: {name, close, change_pct, trend_status}"""
    return index_rows


def build_review(market_name: str, analyses: List[az.AnalysisResult],
                 review_type: str = "post",
                 indices: Optional[List[Dict]] = None,
                 market_analyses: Optional[List[az.AnalysisResult]] = None,
                 breadth: Optional[Dict] = None,
                 market_regime: Optional[Dict] = None) -> Dict:
    """生成复盘页数据。

    - items: 自选模块分析结果
    - market_items: 大盘模块（从大盘池中选出的标的）
    - indices: 大盘指数表现
    - breadth: 全市场涨跌家数（P1 温度计去偏，None 时回退自选池广度）
    - market_regime: 普涨过热日闸门结果（与推荐页口径一致，None 表示未触发）
    """
    rows = [a.to_dict() for a in analyses]
    temp = _market_temperature(analyses, breadth)
    up = sum(1 for a in analyses if a.change_pct > 0)
    down = sum(1 for a in analyses if a.change_pct < 0)
    by_score = sorted(analyses, key=lambda x: x.score, reverse=True)
    market_rows = [a.to_dict() for a in (market_analyses or [])]
    market_temp = _market_temperature(market_analyses, breadth) if market_analyses else None
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
        "market_regime": market_regime,
    }


def build_recommend(items: List[scr.ScreenItem], market_items: Optional[List[scr.ScreenItem]] = None,
                    indices: Optional[List[Dict]] = None, sectors: Optional[List[Dict]] = None,
                    sector_stocks: Optional[Dict[str, List[scr.ScreenItem]]] = None,
                    grid_signals: Optional[List[Dict]] = None,
                    temperature: Optional[Dict] = None) -> Dict:
    temp = temperature or _market_temperature([])
    return {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(items),
        "picks": [it.to_dict() for it in items],
        "market_picks": [it.to_dict() for it in (market_items or [])],
        "sectors": sectors or [],
        "sector_stocks": {k: [it.to_dict() for it in v] for k, v in (sector_stocks or {}).items()},
        "indices": indices or [],
        "temperature": temp,
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
    # 紧凑序列化（indent=None）：线上 GitHub Pages 加载大文件更快更稳
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return path
