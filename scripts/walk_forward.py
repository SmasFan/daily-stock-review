#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Walk-forward 参数敏感性分析（P2：回测池扩充后验证参数稳定性）。

方法：
- 对回测池每只标的，按时间切分 F 折滚动窗口（默认 3 折：50-66.7 / 66.7-83.3 / 83.3-100%）
- 每折：在训练段（窗口前段）跑全参数网格，用「训练段年化超额」选最优参数；
  再用选定参数在训练+测试段重跑，评估「测试段（OOS）超额收益」
- 汇总：每折各参数组合的 OOS 超额、最优参数被选中频率、相对基准参数（固定 0.5% 步长）的胜率

参数网格（对齐 GridParams）：
- dynamic_step: False（旧版固定 0.5%） vs True
- atr_mult: 0.8 / 1.0 / 1.2 / 1.5（dynamic_step=True 时有效）
- use_lock: True / False
- adx_gate: False / True（实验开关）

用法：
  python scripts/walk_forward.py               # 全池全折（31 只 × 3 折）
  python scripts/walk_forward.py --top 5       # 只跑前 5 只（快速调试）
  python scripts/walk_forward.py --folds 2     # 2 折
  python scripts/walk_forward.py --no-download # 仅用缓存K线（离线）

输出：
- data/walk_forward_data.json  逐标的分折结果 + 参数汇总
- 控制台汇总表
"""
import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from src import data_provider as dp  # noqa: E402
from src import grid_backtest as gbt  # noqa: E402
from src.stock_pool import BACKTEST_CODES  # noqa: E402

DATA_DIR = os.path.join(BASE_DIR, "data")
OUT_PATH = os.path.join(DATA_DIR, "walk_forward_data.json")

# 生产口径成本（含 P2 滑点）
CFG = gbt.BacktestConfig(cost_rate=0.0005, slippage_rate=0.0005, cash_rate=0.0, rf=0.0)


def param_grid():
    """参数网格 → [{label, params}]。"""
    grid = []
    # 基准：旧版固定 0.5% 步长、无锁仓、无闸门
    grid.append(("baseline(固定0.5%)",
                 gbt.GridParams(dynamic_step=False, use_lock=False, adx_gate=False)))
    for lock in (True, False):
        for mult in (0.8, 1.0, 1.2, 1.5):
            grid.append((f"ATR{mult:.1f}+锁仓{'开' if lock else '关'}",
                         gbt.GridParams(dynamic_step=True, atr_mult=mult,
                                        use_lock=lock, adx_gate=False)))
    # ADX 闸门实验（在默认 ATR1.2+锁仓 上叠加）
    grid.append(("ATR1.2+锁仓开+ADX闸门",
                 gbt.GridParams(dynamic_step=True, atr_mult=1.2, use_lock=True, adx_gate=True)))
    return grid


def _slice(panel: dict, end_idx: int):
    return {k: v[:end_idx] for k, v in panel.items()}


def _oos_metric(res: gbt.BacktestResult, train_end_date: str) -> dict:
    """测试段（OOS）收益：从 train_end_date 之后第一个 bar 到末尾。"""
    n = len(res.dates)
    idx0 = None
    for i in range(n):
        if res.dates[i] > train_end_date:
            idx0 = i
            break
    if idx0 is None or idx0 >= n - 1:
        return {"strategy": None, "benchmark": None}
    strat = res.equity[-1] / res.equity[idx0] - 1.0
    bench = res.benchmark[-1] / res.benchmark[idx0] - 1.0
    return {"strategy": round(strat, 4), "benchmark": round(bench, 4),
            "excess": round(strat - bench, 4), "bars": n - idx0}


def walk_forward_stock(name: str, code: str, panel: dict, folds: int = 3,
                       grid=None) -> dict:
    """对单只标的跑 walk-forward。返回 {folds: [...], chosen_counts, avg_oos_excess}。"""
    grid = grid or param_grid()
    n = len(panel["close"])
    chosen_counts = {label: 0 for label, _ in grid}
    fold_results = []
    total_oos = []
    for k in range(folds):
        train_end = int(n * (0.5 + k / folds * (0.5 - 0.5 / folds)))
        test_end = int(n * (0.5 + (k + 1) / folds * (0.5 - 0.5 / folds)))
        train_end = max(train_end, 120)
        test_end = min(test_end, n)
        if test_end - train_end < 60 or train_end - 120 < 60:
            continue
        train_date = panel["date"][train_end - 1]

        best_label, best_metric, best_spot = None, None, None
        spot_results = {}
        for label, params in grid:
            try:
                tr = gbt.run_grid_backtest(name, _slice(panel, train_end),
                                           params, gbt.AnchorParams(), CFG)
                tm = gbt.summary_metrics(tr)
                in_metric = tm["annual_return"] - tm["benchmark_annual"]
            except Exception:
                continue
            spot_results[label] = in_metric
            if best_metric is None or in_metric > best_metric:
                best_metric, best_label = in_metric, label

        if best_label is None:
            continue
        chosen_counts[best_label] += 1

        # 用最优参数跑 训练+测试 段，评估 OOS
        chosen_params = dict(grid)[best_label]
        try:
            res = gbt.run_grid_backtest(name, _slice(panel, test_end),
                                        chosen_params, gbt.AnchorParams(), CFG)
        except Exception:
            continue
        oos = _oos_metric(res, train_date)
        if oos.get("excess") is None:
            continue
        total_oos.append(oos["excess"])
        fold_results.append({
            "fold": k + 1, "train_bars": train_end, "test_bars": test_end - train_end,
            "chosen": best_label, "train_excess": round(best_metric, 4),
            "oos": oos,
        })
    return {
        "name": name, "code": code,
        "folds": fold_results,
        "chosen_counts": chosen_counts,
        "avg_oos_excess": round(sum(total_oos) / len(total_oos), 4) if total_oos else None,
        "valid_folds": len(total_oos),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=0, help="只跑前 N 只（调试）")
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--no-download", action="store_true", help="仅用缓存K线")
    args = ap.parse_args()

    metas = BACKTEST_CODES[:args.top] if args.top else BACKTEST_CODES
    grid = param_grid()
    print(f"Walk-forward 参数敏感性: {len(metas)} 只 × {args.folds} 折 × {len(grid)} 参数组合")
    print(f"成本: 佣金{CFG.cost_rate:.2%} + 滑点{CFG.slippage_rate:.2%} 单边")

    per_stock = {}
    combo_oos = {label: [] for label, _ in grid}
    for meta in metas:
        name, code = meta["name"], meta["code"]
        try:
            k = dp.fetch_daily_kline_long(code, count=3200, use_cache=not args.no_download)
            if not k or len(k["closes"]) < 300:
                print(f"  [跳过] {name}: K线不足")
                continue
            panel = gbt.build_panel(name, code, k["dates"], k["opens"], k["closes"],
                                    k["highs"], k["lows"], k["volumes"])
            if not panel:
                print(f"  [跳过] {name}: 面板构建失败")
                continue
        except Exception as e:
            print(f"  [跳过] {name}: {e}")
            continue
        st = walk_forward_stock(name, code, panel, folds=args.folds, grid=grid)
        per_stock[name] = st
        oos_s = f"{st['avg_oos_excess']*100:+.2f}%" if st["avg_oos_excess"] is not None else "N/A"
        print(f"  {name}: {st['valid_folds']} 折有效, OOS平均超额 {oos_s}")

    # 汇总：每个参数组合在所有 有效折 × 股票 上的 OOS 超额分布（用各自选中组合归集）
    for name, st in per_stock.items():
        for fold in st["folds"]:
            combo_oos[fold["chosen"]].append(fold["oos"]["excess"])
    rows = []
    for label, _ in grid:
        vals = combo_oos[label]
        if not vals:
            rows.append({"combo": label, "samples": 0})
            continue
        import statistics
        avg = statistics.mean(vals)
        # 相对基准参数（baseline 固定0.5%）的胜率
        base_vals = combo_oos[grid[0][0]]
        wins = sum(1 for v in vals if base_vals and v > statistics.median(base_vals)) if base_vals else 0
        rows.append({"combo": label, "samples": len(vals),
                     "avg_oos_excess": round(avg, 4),
                     "median_oos_excess": round(statistics.median(vals), 4),
                     "best_fold_win_rate": round(wins / len(vals), 3)})
    rows.sort(key=lambda r: r.get("avg_oos_excess", -1), reverse=True)

    # 全局最优参数 = 平均 OOS 超额最高的组合
    best = next((r for r in rows if r.get("avg_oos_excess") is not None), None)
    out = {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "folds": args.folds,
        "cost_rate": CFG.cost_rate, "slippage_rate": CFG.slippage_rate,
        "pool_size": len(metas),
        "best_combo": best["combo"] if best else None,
        "per_combo": rows,
        "per_stock": per_stock,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("\n== 参数组合汇总（按平均 OOS 超额排序）==")
    print(f"{'组合':<22}{'样本':>5}{'均值OOS超额':>12}{'中位':>9}{'胜率(对基准)':>12}")
    for r in rows:
        if not r.get("samples"):
            continue
        print(f"{r['combo']:<22}{r['samples']:>5}"
              f"{r['avg_oos_excess']*100:>+11.2f}%"
              f"{r['median_oos_excess']*100:>+8.2f}%"
              f"{r['best_fold_win_rate']*100:>+11.1f}%")
    print(f"\n全局最优: {out['best_combo']}  →  {OUT_PATH}")


if __name__ == "__main__":
    main()
