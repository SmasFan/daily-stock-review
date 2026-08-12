#!/usr/bin/env python3
"""生成板块温度 & 个股/板块走势联动数据 data/market_heat.json。

- 板块温度：板块成分股等权净值的「近250日滚动20日收益百分位」（0-100），
  每个板块各自的温度序列；
- 个股/板块走势：近 N 日（默认 120）前复权收盘 / 板块等权净值；
- 个股弹窗显示其所属板块的温度。

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
TEMP_WINDOW = 250      # 温度百分位窗口
TEMP_LOOKBACK = 20     # 温度收益回看


def synth_temperature(nav, window=TEMP_WINDOW, lookback=TEMP_LOOKBACK):
    """温度 = 近 window 日滚动 lookback 收益百分位（0-100）。"""
    rets = [nav[i] / nav[i - lookback] - 1.0 for i in range(lookback, len(nav))]
    temps = [50.0] * lookback
    for i, r in enumerate(rets):
        w = rets[max(0, i - window + 1):i + 1]
        temps.append(round(sum(1 for x in w if x <= r) / len(w) * 100, 1))
    return temps


def build_stock_series(code, axis_dates, days, use_cache):
    # count 固定 658：复用回测脚本已建的 long_{code}_658 缓存
    k = dp.fetch_daily_kline_long(code, count=658, min_days=608, use_cache=True)
    if k is None and not use_cache:
        k = dp.fetch_daily_kline_long(code, count=658, min_days=608, use_cache=False)
    if not k:
        return None
    d2i = {d: i for i, d in enumerate(k["dates"])}
    close = []
    for d in axis_dates:
        i = d2i.get(d)
        close.append(k["closes"][i] if i is not None else None)
    return close


def main():
    ap = argparse.ArgumentParser(description="生成板块温度&走势联动数据")
    ap.add_argument("--days", type=int, default=120, help="走势天数（默认120）")
    ap.add_argument("--offline", action="store_true", help="只用缓存")
    args = ap.parse_args()
    use_cache = args.offline
    if args.offline:
        os.environ.setdefault("CACHE_MAX_AGE_HOURS", "999999")

    print("拉取沪深300（全局温度备用）…")
    idx = dp.fetch_daily_kline_long("sh000300", count=900, min_days=750, use_cache=True)
    if idx is None:
        idx = dp.fetch_daily_kline_long("sh000300", count=900, min_days=750, use_cache=False)
    if not idx:
        print("沪深300 拉取失败"); return

    full_dates = idx["dates"]
    axis_dates = full_dates[-args.days:]          # 走势展示轴
    global_temps = synth_temperature(idx["closes"])[-args.days:]

    print("拉取自选股走势（长历史，板块温度需 250 日窗口）…")
    codes = stock_pool.WATCHLIST_CODES
    name_map = stock_pool.get_code_name()
    sec_map = stock_pool.get_code_sector()
    # count 固定 658：复用回测脚本已建的 long_{code}_658 缓存
    need = 658
    stocks = {}
    for i, code in enumerate(codes):
        k = dp.fetch_daily_kline_long(code, count=658, min_days=608, use_cache=True)
        if k is None and not use_cache:
            k = dp.fetch_daily_kline_long(code, count=658, min_days=608, use_cache=False)
        if not k or len(k["closes"]) < TEMP_WINDOW + 40:
            continue
        stocks[code] = {
            "name": name_map.get(code, code),
            "sector": (sec_map.get(code, "") or "").strip() or "其他",
            "close": build_stock_series(code, axis_dates, args.days, use_cache),
            "close_full": k["closes"],   # 全历史用于板块温度合成
        }
        if (i + 1) % 50 == 0:
            print(f"   {i + 1}/{len(codes)}", flush=True)

    print("合成板块净值与温度…")
    sec_groups = {}
    for code, s in stocks.items():
        sec_groups.setdefault(s["sector"], []).append(code)
    sectors = {}
    for sec, members in sec_groups.items():
        n = max(len(s["close_full"]) for s in (stocks[c] for c in members))
        nav_full = [0.0] * n
        cnt = [0] * n
        for code in members:
            cf = stocks[code]["close_full"]
            base = cf[0]
            for j, v in enumerate(cf):
                if v is not None:
                    nav_full[j] += v / base
                    cnt[j] += 1
        nav = []
        last = None
        for j in range(n):
            if cnt[j]:
                last = nav_full[j] / cnt[j]
            nav.append(last if last else 0.0)
        # 板块温度：净值全历史合成，截尾部展示窗口
        temps = synth_temperature(nav)[-args.days:]
        # 展示用板块净值：与个股轴对齐（按日期映射）
        d2i = {d: i for i, d in enumerate(idx["dates"][-n:])}
        nav_show = []
        base_val = next((v for v in nav if v), 1.0)
        for d in axis_dates:
            i = d2i.get(d)
            nav_show.append(round(nav[i] / base_val, 4) if i is not None else None)
        sectors[sec] = {"members": len(members), "nav": nav_show, "temps": temps}

    # 去掉全历史字段（减小文件体积），补上所属板块温度索引
    for code, s in stocks.items():
        s.pop("close_full", None)

    out = {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tempNote": "板块温度=板块成分等权净值近250日20日收益百分位（各自独立计算）",
        "tempDates": axis_dates,
        "globalTemps": global_temps,
        "dates": axis_dates,
        "stocks": stocks,
        "sectors": sectors,
    }
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    size = os.path.getsize(OUT) / 1024
    print(f"写入 {OUT}  ({size:.0f} KB)  个股{len(stocks)} / 板块{len(sectors)}")


if __name__ == "__main__":
    main()
