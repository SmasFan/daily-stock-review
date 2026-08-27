#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""养老金持仓买入胜率回测（专门针对"养老"关键词，独立于国家队合并统计）。

数据源：东财数据中心 RPT_F10_EH_FREEHOLDERS（十大流通股东，季度历史）
股票池：daily-stock-review 的 WATCHLIST_CODES + MARKET_POOL_CODES（259 只）

口径：
- 信号：季度报告期，十大流通股东中持有者名称含"养老"（基本养老保险基金等），
  相对上一季度"新进"或"增持"（持股数环比增加）为买入信号
- 收益：季报截止日 + hold_days 个交易日收盘价 / 季报截止日收盘价 - 1 - 2*成本
- 胜率：单笔收益 > 0 占比；另按日分组等权组合口径
- 基准：同期沪深300

用法:
  python3 pension_backtest.py [--hold 60] [--only-inc] [--save]
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(BASE_DIR))  # 项目根

from strategy.fund_strategies import fetch_holders_history, _kline_close, _summarize, COST
from src import data_provider as dp

PENSION_KW = ("养老",)  # 基本养老保险基金一二零五组合等


def is_pension(name):
    return name and any(k in name for k in PENSION_KW)


def fetch_benchmark():
    """沪深300 近5年日K（腾讯），用于同期基准。"""
    k = dp.fetch_index_kline("sh000300", count=1300, use_cache=True)
    if not k:
        return None
    return k


def benchmark_ret(bench, date, hold_days):
    """date 之后第 hold_days 个交易日基准收益。"""
    if not bench:
        return None
    dates = bench["dates"]
    i = None
    for j in range(len(dates) - 1, -1, -1):
        if dates[j] <= date:
            i = j
            break
    if i is None:
        return None
    j = i + hold_days
    if j >= len(dates):
        return None
    return bench["closes"][j] / bench["closes"][i] - 1


def main():
    ap = argparse.ArgumentParser(description="养老金持仓买入胜率回测")
    ap.add_argument("--hold", type=int, default=60, help="持有交易日数（默认60≈一季度）")
    ap.add_argument("--only-inc", action="store_true", help="只看增持+新进（默认含所有持仓变化）")
    ap.add_argument("--save", action="store_true", help="结果保存到 data/pension_backtest.json")
    ap.add_argument("--limit", type=int, default=0, help="扫描股票数上限（调试用）")
    ap.add_argument("--penscan", help="用预扫描结果文件（data/pension_scan.json）限定股票范围")
    args = ap.parse_args()

    if args.penscan:
        scan = json.load(open(args.penscan, encoding="utf-8"))
        codes = list(scan.keys())
        print(f"用预扫描文件限定 {len(codes)} 只有养老金持仓的股票", flush=True)
    else:
        from src.stock_pool import WATCHLIST_CODES, MARKET_POOL_CODES
        codes = list(dict.fromkeys(WATCHLIST_CODES + MARKET_POOL_CODES))
        if args.limit:
            codes = codes[:args.limit]
        print(f"扫描 {len(codes)} 只十大流通股东历史，筛养老金持仓…", flush=True)
    trades = []       # 全部养老金持仓（新进/增持/减持/维持）
    trades_act = []   # 仅增持+新进（主动买入信号）
    bench = fetch_benchmark()
    bench_rets = []

    for c in codes:
        try:
            rows = fetch_holders_history(c)
        except Exception:
            continue
        holders = defaultdict(list)
        for r in rows:
            name = r.get("HOLDER_NAME") or ""
            if not is_pension(name):
                continue
            end = (r.get("END_DATE") or "")[:10]
            if not end or end < "2021-01-01":
                continue
            holders[name].append({
                "end": end, "num": r.get("HOLD_NUM"),
                "chg": r.get("HOLD_NUM_CHANGE") or r.get("HOLD_CHANGE") or "",
            })
        if not holders:
            continue
        for name, qs in holders.items():
            qs.sort(key=lambda x: x["end"])
            for i, q in enumerate(qs):
                prev = qs[i - 1] if i > 0 else None
                num = q["num"]
                chg = q["chg"]
                # 动作分类
                is_new = (chg == "新进") or (prev is None and num is not None)
                is_inc = False
                is_dec = False
                if prev and prev.get("num") is not None and num is not None:
                    try:
                        d = float(num) - float(prev["num"])
                        is_inc = d > 0
                        is_dec = d < 0
                    except (TypeError, ValueError):
                        pass
                action = "新进" if is_new else ("增持" if is_inc else ("减持" if is_dec else "维持"))
                if args.only_inc and action not in ("新进", "增持"):
                    continue
                entry, exit_, exit_date = _kline_close(c, q["end"], args.hold)
                if entry is None or exit_ is None:
                    continue
                ret = exit_ / entry - 1 - 2 * COST
                bret = benchmark_ret(bench, q["end"], args.hold)
                t = {"code": c, "date": q["end"], "holder": name,
                     "action": action, "ret": ret, "exit_date": exit_date,
                     "bench_ret": bret}
                trades.append(t)
                if action in ("新进", "增持"):
                    trades_act.append(t)
                    if bret is not None:
                        bench_rets.append(bret)
        time.sleep(0.05)

    print(f"\n{'='*80}")
    print(f"持有期 {args.hold} 交易日 · 仅增持/新进: {args.only_inc}")
    for label, ts in (("全部养老金持仓变化", trades), ("养老金增持/新进", trades_act)):
        if not ts:
            print(f"\n【{label}】无样本")
            continue
        r = _summarize(ts, label)
        print(f"\n【{label}】")
        print(f"  样本数: {r['trades']}")
        print(f"  胜率(单笔>0): {r['win_rate']*100:.1f}%")
        print(f"  平均收益: {r['avg_ret']*100:+.2f}%   中位: {r['median_ret']*100:+.2f}%")
        print(f"  最好: {r['best']*100:+.2f}%  最差: {r['worst']*100:+.2f}%")
        print(f"  按日等权组合累计: {r['day_cum']*100:+.1f}%  年化: {r['day_annual']*100:+.1f}%")
        print(f"  按日胜率: {r['day_win_rate']*100:.1f}%  区间: {r['span_days']}天")
    # 超额 vs 基准
    if trades_act and bench_rets:
        avg_excess = sum(t["ret"] - t["bench_ret"] for t in trades_act if t["bench_ret"] is not None) / len(bench_rets)
        print(f"\n【超额 vs 沪深300】")
        print(f"  有基准样本: {len(bench_rets)}  平均超额: {avg_excess*100:+.2f}%")
    # 按动作细分
    by_act = defaultdict(list)
    for t in trades:
        by_act[t["action"]].append(t["ret"])
    if by_act:
        print(f"\n【按动作细分】")
        for act, rets in sorted(by_act.items()):
            wr = sum(1 for x in rets if x > 0) / len(rets)
            print(f"  {act}: {len(rets)}笔  胜率{wr*100:.1f}%  均值{sum(rets)/len(rets)*100:+.2f}%")

    if args.save:
        out = {"hold": args.hold, "trades": trades}
        path = os.path.join(os.path.dirname(BASE_DIR), "data", "pension_backtest.json")
        json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"\n已保存: {path}")


if __name__ == "__main__":
    main()
