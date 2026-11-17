#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""美股收盘后刷新三大指数（道指/纳指/标普），patch 进推荐/宽基两份 JSON。

背景：recommend(09:05)/review(15:40) 都在北京时间白天跑，美股收盘（美东 16:00 =
北京次日 05:00 冬令/04:00 夏令）后没有任务刷新，导致市场页的美股指数一直停在
上一交易日或白天快照。本脚本在美股收盘后单独拉一次，原地更新：

  - data/recommend_data.json 的 indices[]（usDJI/usIXIC/usINX）
  - data/wide_index_data.json 的 items[]（kind == "美股"）

并写入 usAt 时间戳，供页面单独展示"美股收盘更新"。

用法：python3 refresh_us_index.py [--dry]
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import data_provider as dp          # noqa: E402
from src import analyzer as az              # noqa: E402
from src import wide_index as wi            # noqa: E402

US_CODES = ["usDJI", "usIXIC", "usINX"]
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
# 独立的实时快照：A 股 recommend/review 任务会整份重写 recommend_data.json /
# wide_index_data.json 并抹掉 usAt 与美股实时价，smasfx 优先读这个文件。
LIVE_PATH = os.path.join(DATA_DIR, "us_index_live.json")


def _factors(code, name):
    k = dp.fetch_index_kline(code, count=320)
    if not (k and len(k["closes"]) >= 30):
        return None
    a = az.analyze_stock(name, k["dates"], k["opens"], k["closes"],
                         k["highs"], k["lows"], k["volumes"], code=code)
    if a is None:
        return None
    return {
        "date": a.date,
        "trend_status": a.trend_status, "trend_strength": a.trend_strength,
        "ma5": a.ma5, "ma10": a.ma10, "ma20": a.ma20, "ma60": a.ma60,
        "bias_ma5": a.bias_ma5, "bias_ma20": a.bias_ma20,
        "volume_ratio": a.volume_ratio, "volume_status": a.volume_status,
        "macd_status": a.macd_status,
        "macd_dif": a.macd_dif, "macd_dea": a.macd_dea, "macd_bar": a.macd_bar,
        "rsi6": a.rsi6, "rsi12": a.rsi12, "rsi24": a.rsi24, "rsi_status": a.rsi_status,
        "score": a.score, "signal_key": a.signal_key, "signal": a.signal,
        "ideal_buy": a.ideal_buy, "secondary_buy": a.secondary_buy,
        "stop_loss": a.stop_loss, "take_profit": a.take_profit,
        "support": a.support, "resistance": a.resistance,
        "high20": a.high20, "low20": a.low20, "change_60d": a.change_60d,
        "boll_pos": a.boll_pos,
    }


def fetch_us_rows():
    """拉三大美股指数行情 + 因子 -> {code: {close,change_pct,prev_close,factors}}。"""
    qs = dp.fetch_index_quotes(US_CODES)
    out = {}
    for code in US_CODES:
        q = qs.get(code)
        if not q:
            continue
        row = {
            "close": q.get("price"),
            "change_pct": q.get("change"),
            "prev_close": q.get("prevClose"),
        }
        try:
            f = _factors(code, q.get("name") or code)
            if f:
                row["factors"] = f
        except Exception as e:
            print(f"   [warn] {code} 因子失败: {e}")
        out[code] = row
    return out


def patch_recommend(rows, us_at):
    path = os.path.join(DATA_DIR, "recommend_data.json")
    if not os.path.exists(path):
        print("  [skip] recommend_data.json 不存在")
        return False
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    changed = 0
    for item in d.get("indices") or []:
        r = rows.get(item.get("code"))
        if not r:
            continue
        item["close"] = r["close"]
        item["change_pct"] = r["change_pct"]
        if r.get("factors"):
            item["factors"] = r["factors"]
        changed += 1
    d["usAt"] = us_at
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    print(f"  recommend_data.json: 更新 {changed} 个美股指数, usAt={us_at}")
    return changed > 0


def patch_wide(rows, us_at):
    path = os.path.join(DATA_DIR, "wide_index_data.json")
    if not os.path.exists(path):
        print("  [skip] wide_index_data.json 不存在")
        return False
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    us_name = {w["code"]: w["name"] for w in wi.WIDE_INDEXES if w["code"] in US_CODES}
    changed = 0
    for item in d.get("items") or []:
        if item.get("code") not in us_name:
            continue
        r = rows.get(item["code"])
        if not r:
            continue
        item["close"] = r["close"]
        item["change_pct"] = r["change_pct"]
        item["prev_close"] = r["prev_close"]
        if r.get("factors"):
            item["factors"] = r["factors"]
            item["price_ma20"] = r["factors"].get("ma20")
        changed += 1
    d["usAt"] = us_at
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print(f"  wide_index_data.json: 更新 {changed} 个美股指数, usAt={us_at}")
    return changed > 0


def write_live(rows, us_at):
    """把三大指数实时快照写到独立文件，供 smasfx 优先读取（不被 A 股任务覆盖）。"""
    d = {
        "usAt": us_at,
        "codes": {
            code: {
                "close": r.get("close"),
                "change_pct": r.get("change_pct"),
                "prev_close": r.get("prev_close"),
                "factors": r.get("factors"),
            }
            for code, r in rows.items()
        },
    }
    with open(LIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    print(f"  us_index_live.json: 写入 {len(rows)} 个指数快照, usAt={us_at}")
    return True


def in_us_session():
    """美股活跃窗口（北京时间）：夏令 21:30–04:00、冬令 22:30–05:00。
    取并集 21:00–06:00，覆盖盘前盘后，白天休眠跳过。周末也跳过（美股不开盘）。"""
    now = datetime.now()
    if now.weekday() == 5 or (now.weekday() == 6 and now.hour < 6):
        return False  # 周六全天、周日凌晨跳过
    h = now.hour + now.minute / 60.0
    return h >= 21.0 or h < 6.0


def main():
    dry = "--dry" in sys.argv
    us_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 盘中模式在拉数据前先判时段，非活跃窗口完全不请求
    if "--session-only" in sys.argv and not in_us_session():
        return 0
    print(f"=== 刷新美股三大指数 @ {us_at} ===")
    rows = fetch_us_rows()
    if not rows:
        print("  [错误] 未取到任何美股指数行情，放弃")
        return 1
    for code, r in rows.items():
        fd = (r.get("factors") or {}).get("date")
        print(f"  {code}: {r['close']} ({r['change_pct']}%, K线={fd})")
    if dry:
        print("  [dry] 不写文件")
        return 0
    ok1 = patch_recommend(rows, us_at)
    ok2 = patch_wide(rows, us_at)
    ok3 = write_live(rows, us_at)
    return 0 if (ok1 or ok2 or ok3) else 1


if __name__ == "__main__":
    sys.exit(main())
