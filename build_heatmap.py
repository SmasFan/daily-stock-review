#!/usr/bin/env python3
"""生成市场温度 & 个股/板块走势联动数据 data/market_heat.json。

- 市场温度历史：基于沪深300 的「近250日滚动20日收益百分位」合成（0-100），
  近期与复盘页温度计同口径显示；
- 个股走势：自选池每只近 N 日收盘（前复权）；
- 板块走势：板块成分股等权净值合成（缺失日按前值填充）。

前端各页面板块/个股点击「温度」按钮 → 双Y轴折线图（温度 + 走势）。

用法:
  python3 build_heatmap.py              # 默认 120 日走势
  python3 build_heatmap.py --days 250
"""
import argparse
import json
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
sys.path.insert(0, BASE_DIR)

from src import data_provider as dp, stock_pool  # noqa: E402

OUT = os.path.join(DATA_DIR, "market_heat.json")


def synth_temperature(closes, window=250, lookback=20):
    """市场温度 = 近 window 日滚动 lookback 收益百分位（0-100）。"""
    rets = [closes[i] / closes[i - lookback] - 1.0 for i in range(lookback, len(closes))]
    temps = [50.0] * lookback
    for i, r in enumerate(rets):
        w = rets[max(0, i - window + 1):i + 1]
        temps.append(round(sum(1 for x in w if x <= r) / len(w) * 100, 1))
    return temps


def build_stock_series(code, axis_dates, days, use_cache):
    k = dp.fetch_daily_kline(code, count=days + 30, use_cache=use_cache)
    if not k:
        return None
    d2i = {d: i for i, d in enumerate(k["dates"])}
    close = []
    for d in axis_dates:
        i = d2i.get(d)
        close.append(k["closes"][i] if i is not None else None)
    return close


def main():
    ap = argparse.ArgumentParser(description="生成市场温度&走势联动数据")
    ap.add_argument("--days", type=int, default=120, help="走势天数（默认120）")
    ap.add_argument("--offline", action="store_true", help="只用缓存")
    args = ap.parse_args()
    use_cache = args.offline
    if args.offline:
        os.environ.setdefault("CACHE_MAX_AGE_HOURS", "999999")

    print("拉取沪深300（合成温度）…")
    idx = dp.fetch_daily_kline_long("sh000300", count=900, min_days=750, use_cache=True)
    if idx is None:
        idx = dp.fetch_daily_kline_long("sh000300", count=900, min_days=750, use_cache=False)
    if not idx:
        print("沪深300 拉取失败"); return
    temps = synth_temperature(idx["closes"])
    axis_dates = idx["dates"][-args.days:]          # 走势轴（与温度尾部对齐）
    temp_dates = idx["dates"][-args.days:]
    temp_vals = temps[-args.days:]
    # 近期真实温度计（review_data.json，若有则覆盖尾部）
    try:
        with open(os.path.join(DATA_DIR, "review_data.json"), encoding="utf-8") as f:
            rev = json.load(f)
        rt = rev.get("temperature") or {}
        if rt.get("score") is not None:
            today = (rev.get("generatedAt") or "")[:10]
            if today in temp_dates:
                temp_vals[temp_dates.index(today)] = rt["score"]
    except Exception:
        pass

    print("拉取自选股走势…")
    codes = stock_pool.WATCHLIST_CODES
    name_map = stock_pool.get_code_name()
    stocks = {}
    for i, code in enumerate(codes):
        close = build_stock_series(code, axis_dates, args.days, use_cache)
        if close and any(v is not None for v in close):
            stocks[code] = {"name": name_map.get(code, code), "close": close}
        if (i + 1) % 50 == 0:
            print(f"   {i + 1}/{len(codes)}", flush=True)

    print("合成板块走势…")
    sec_groups = {}
    for code in codes:
        sec = (stock_pool.get_code_sector().get(code, "") or "").strip() or "其他"
        if code in stocks:
            sec_groups.setdefault(sec, []).append(code)
    sectors = {}
    for sec, members in sec_groups.items():
        nav = [0.0] * len(axis_dates)
        cnt = [0] * len(axis_dates)
        for code in members:
            close = stocks[code]["close"]
            base = next((v for v in close if v is not None), None)
            if not base:
                continue
            for j, v in enumerate(close):
                if v is not None:
                    nav[j] += v / base
                    cnt[j] += 1
        last = None
        out = []
        for j in range(len(axis_dates)):
            if cnt[j]:
                last = nav[j] / cnt[j]
            out.append(round(last, 4) if last else None)
        sectors[sec] = {"members": len(members), "nav": out}

    out = {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tempLabel": "市场温度",
        "tempNote": "合成温度=沪深300近250日20日收益百分位，尾部覆盖复盘真实温度计",
        "tempDates": temp_dates,
        "temps": temp_vals,
        "dates": axis_dates,
        "stocks": stocks,
        "sectors": sectors,
    }
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    print(f"写入 {OUT}  温度{len(temp_dates)}天 / 个股{len(stocks)} / 板块{len(sectors)}")


if __name__ == "__main__":
    main()
