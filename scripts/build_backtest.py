#!/usr/bin/env python3
"""盘中/独立运行网格回测，生成 backtest_index.json + data/backtest/*.json。

与 run_review.py 的回测段同逻辑（单一真源：grid_backtest 模块），
独立运行以便盘中定时更新（回测页包含当天数据）。

K 线策略：长历史走 24 小时缓存（3200 根历史不变，避免重拉触发限流），
fetch_daily_kline_long 内部会用小请求补齐最新交易日 → 结果包含当天。

用法:
  python3 scripts/build_backtest.py
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from src import data_provider as dp  # noqa: E402
from src import grid_backtest as gbt  # noqa: E402
from src import report as rp  # noqa: E402
from src.stock_pool import BACKTEST_CODES  # noqa: E402


def kline_with_latest(code):
    """长历史走 24h 缓存（3200 根避免限流），再用实时短请求补齐最新交易日。

    fetch_daily_kline_long 缓存命中时直接返回不补最新，这里手动合并。
    """
    k = dp.fetch_daily_kline_long(code, count=3200, cache_max_age_hours=24)
    if not k:
        return None
    try:
        latest = dp.fetch_daily_kline(code, count=320, use_cache=False)
    except Exception:
        latest = None
    if latest and latest["dates"] and latest["dates"][-1] > k["dates"][-1]:
        have = set(k["dates"])
        for i, d in enumerate(latest["dates"]):
            if d in have:
                continue
            k["dates"].append(d)
            k["opens"].append(latest["opens"][i])
            k["closes"].append(latest["closes"][i])
            k["highs"].append(latest["highs"][i])
            k["lows"].append(latest["lows"][i])
            k["volumes"].append(latest["volumes"][i])
        order = sorted(range(len(k["dates"])), key=lambda i: k["dates"][i])
        for key in ("dates", "opens", "closes", "highs", "lows", "volumes"):
            k[key] = [k[key][i] for i in order]
    return k


def main():
    per_stock = {}
    gparams = gbt.GridParams()
    ap = gbt.AnchorParams(lookback_days=750, min_periods=500)
    cfg = gbt.BacktestConfig(cost_rate=0.0005, slippage_rate=0.0005, cash_rate=0.0, rf=0.0)
    for meta in BACKTEST_CODES:
        name, code = meta["name"], meta["code"]
        k = kline_with_latest(code)
        if not k or len(k["closes"]) < 300:
            print(f"   [跳过] {name}: K线不足")
            continue
        panel = gbt.build_panel(name, code, k["dates"], k["opens"], k["closes"],
                                k["highs"], k["lows"], k["volumes"])
        if not panel:
            print(f"   [跳过] {name}: 面板构建失败")
            continue
        try:
            res = gbt.run_grid_backtest(name, panel, gparams, ap, cfg)
        except Exception:
            print(f"   [跳过] {name}: 均值线样本不足")
            continue
        per_stock[name] = res
        s = gbt.summary_metrics(res)
        print(f"   {name}: 年化{s['annual_return']*100:.1f}% 回撤{s['max_drawdown']*100:.1f}% "
              f"夏普{s['sharpe']} 交易{s['trade_count']}次")

    bt_index, bt_stocks = gbt.build_backtest_split(per_stock)
    p = rp.save("backtest_index.json", bt_index)
    bt_dir = os.path.join(os.path.dirname(p), "backtest")
    os.makedirs(bt_dir, exist_ok=True)
    for name, data in bt_stocks.items():
        with open(os.path.join(bt_dir, name + ".json"), "w", encoding="utf-8") as f:
            import json
            json.dump(data, f, ensure_ascii=False)
    print(f"   {p}  共 {len(per_stock)} 只 / 含最新交易日")


if __name__ == "__main__":
    main()
