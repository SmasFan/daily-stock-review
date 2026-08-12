# -*- coding: utf-8 -*-
"""上升趋势扫描共享逻辑（单一真源）。

build_uptrend.py（页面数据）与 uptrend-screener skill（对话查询）共用，
避免两份扫描代码漂移。
"""
from typing import Dict, List, Optional

from . import analyzer, data_provider as dp, stock_pool

UPTREND = ("强势多头", "多头排列")


def build_name_map(codes: List[str]) -> Dict[str, str]:
    """自选池名称映射 + 腾讯快照补全缺失代码的名称。"""
    name_map = stock_pool.get_code_name()
    missing = [c for c in codes if c not in name_map]
    if missing:
        try:
            for sym, q in dp.fetch_quotes(missing).items():
                nm = q.get("name")
                if nm:
                    name_map = {**name_map, q.get("code", sym): nm}
        except Exception:
            pass
    return name_map


def scan_uptrend(codes: Optional[List[str]] = None, use_cache: bool = True) -> List:
    """扫描股票池，返回处于上升趋势（强势多头/多头排列）的分析结果，评分降序。

    - codes: 默认全部自选股
    - use_cache: False 时强制实时拉取 K 线
    """
    codes = codes or stock_pool.WATCHLIST_CODES
    name_map = build_name_map(codes)
    rows = []
    for code in codes:
        k = dp.fetch_daily_kline(code, count=120, use_cache=use_cache)
        if not k or len(k["closes"]) < 30:
            continue
        name = name_map.get(code, code)
        r = analyzer.analyze_stock(name, k["dates"], k["opens"], k["closes"],
                                   k["highs"], k["lows"], k["volumes"], code=code)
        if not r or r.trend_status not in UPTREND:
            continue
        rows.append(r)
    rows.sort(key=lambda r: -r.score)
    return rows
