"""
持仓分析模块：盘中实时跟踪 + 盘后复盘 + 网格策略提醒。

- 配置: holdings.json（{name, code, cost, shares}）
- 实时行情: 腾讯快照（现价/涨跌/PE/PB）
- 网格信号: 复用 grid_signal 的均值线偏离度操作提醒
- 输出: data/holdings_data.json
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Dict, List, Optional

from . import data_provider as dp
from . import grid_signal as gs
from . import grid_backtest as gbt
from . import stock_pool as sp

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_PATH = os.path.join(BASE_DIR, "holdings.json")
os.makedirs(DATA_DIR, exist_ok=True)


def load_holdings() -> List[Dict]:
    """读取 holdings.json，返回持仓列表 [{name, code, cost, shares}]。"""
    if not os.path.exists(CONFIG_PATH):
        return []
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        holdings = d.get("holdings") or []
        # 代码统一补零为6位
        for h in holdings:
            h["code"] = str(h.get("code", "")).zfill(6)
        return holdings
    except Exception:
        return []


def is_market_open() -> bool:
    """判断当前是否为 A 股交易时间（工作日 9:30-11:30、13:00-15:00）。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (570 <= hm <= 690) or (780 <= hm <= 900)


def build_holdings_data(gparams: Optional[gbt.GridParams] = None,
                        ap: Optional[gbt.AnchorParams] = None,
                        bt_data: Optional[Dict] = None) -> Dict:
    """生成持仓数据：行情 + 网格信号 + 汇总。

    返回 {generatedAt, market_open, total_market, items: [...]}，items 按操作优先级排序。
    """
    holdings = load_holdings()
    if not holdings:
        return {"generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "market_open": is_market_open(), "holdings": [], "items": [],
                "total_market": 0}
    gparams = gparams or gbt.GridParams()
    ap = ap or gbt.AnchorParams(lookback_days=750, min_periods=500)

    codes = [h["code"] for h in holdings]
    quotes = dp.fetch_quotes(codes)
    sector_map = sp.get_code_sector()

    items = []
    for h in holdings:
        code = h["code"]
        q = quotes.get(code) or {}
        price = q.get("price") or 0
        change = q.get("change") or 0
        shares = float(h.get("shares") or 0)

        market = price * shares if price else 0

        # 网格信号：优先复用回测结果，否则现场计算
        grid = None
        if bt_data:
            bt_stocks = bt_data.get("stocks") or {}
            bt_name = None
            try:
                from .stock_pool import BACKTEST_CODES
                for meta in BACKTEST_CODES:
                    if meta["code"] == code:
                        bt_name = meta["name"]
                        break
            except Exception:
                pass
            sig = None
            if bt_name and bt_name in bt_stocks:
                sig = gs.signal_from_backtest(h["name"], code, bt_stocks[bt_name], gparams)
            if sig:
                grid = sig
        if not grid:
            grid = gs.compute_grid_signal(h["name"], code, gparams, ap)

        items.append({
            "name": q.get("name") or h["name"],
            "code": code,
            "sector": sector_map.get(code, ""),
            "shares": shares,
            "price": price or q.get("prevClose") or 0,
            "change_pct": change,
            "market_value": round(market, 2),
            "grid": grid,
        })

    total_market = sum(i["market_value"] for i in items)

    # 排序：有网格操作建议的优先（clear/buy/reduce 在前），其次按涨跌
    prio = {"clear": 0, "buy": 1, "reduce": 2, "hold": 3, "wait": 4, None: 5}
    items.sort(key=lambda x: (
        prio.get((x.get("grid") or {}).get("action_key"), 5),
        -abs(x.get("change_pct") or 0),
    ))

    return {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market_open": is_market_open(),
        "holdings": holdings,
        "items": items,
        "total_market": round(total_market, 2),
    }


def save_holdings_data(data: Dict) -> str:
    """保存 holdings_data.json。"""
    path = os.path.join(DATA_DIR, "holdings_data.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path
