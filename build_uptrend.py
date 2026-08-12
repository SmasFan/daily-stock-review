#!/usr/bin/env python3
"""生成上升趋势股票页面数据 data/uptrend_data.json。

扫描自选股池，筛出强势多头/多头排列的股票，按技术评分降序，
输出前端页面（uptrend.html）可消费的 JSON。与复盘系统同口径。

用法:
  python3 build_uptrend.py            # 实时拉取（约 80 秒）
  CACHE_MAX_AGE_HOURS=999999 python3 build_uptrend.py   # 只用缓存（秒出）
"""
import json
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
sys.path.insert(0, BASE_DIR)

from src import analyzer, data_provider as dp, stock_pool  # noqa: E402

UPTREND = ("强势多头", "多头排列")


def scan() -> list:
    codes = stock_pool.WATCHLIST_CODES
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
    items = []
    for code in codes:
        k = dp.fetch_daily_kline(code, count=120, use_cache=True)
        if not k or len(k["closes"]) < 30:
            continue
        name = name_map.get(code, code)
        r = analyzer.analyze_stock(name, k["dates"], k["opens"], k["closes"],
                                   k["highs"], k["lows"], k["volumes"], code=code)
        if not r:
            continue
        if r.trend_status not in UPTREND:
            continue
        items.append({
            "name": r.name,
            "code": r.code,
            "sector": stock_pool.get_code_sector().get(code, ""),
            "date": r.date,
            "close": r.close,
            "change_pct": r.change_pct,
            "trend_status": r.trend_status,
            "trend_strength": r.trend_strength,
            "score": r.score,
            "signal_key": analyzer.signal_key_for_score(r.score),
            "signal": analyzer.signal_label_for_key(analyzer.signal_key_for_score(r.score)),
            "rsi6": r.rsi6,
            "rsi12": r.rsi12,
            "boll_pos": r.boll_pos,
            "volume_ratio": r.volume_ratio,
            "volume_status": r.volume_status,
            "bias_ma5": r.bias_ma5,
            "bias_ma20": r.bias_ma20,
            "macd_status": r.macd_status,
            "macd_bar": r.macd_bar,
            "change_60d": r.change_60d,
            "ideal_buy": r.ideal_buy,
            "secondary_buy": r.secondary_buy,
            "stop_loss": r.stop_loss,
            "take_profit": r.take_profit,
        })
    items.sort(key=lambda x: -x["score"])
    return items


def main():
    items = scan()
    strong = sum(1 for it in items if it["trend_status"] == "强势多头")
    hot = sum(1 for it in items if it["rsi6"] > 75 or it["boll_pos"] > 0.9)
    out = {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(items),
        "strong": strong,
        "hot": hot,
        "avg_score": round(sum(it["score"] for it in items) / len(items), 1) if items else 0,
        "items": items,
    }
    path = os.path.join(DATA_DIR, "uptrend_data.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, path)
    print(f"写入 {path}  ({len(items)} 只上升趋势 / {strong} 强势多头 / {hot} 短期过热)")


if __name__ == "__main__":
    main()
