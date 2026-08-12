#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""选股逻辑组合回测：过去 N 年按当前选股逻辑定期调仓 vs 基准。

验证「技术分六维评分」的可靠性（横截面的 PE/PB 价值因子无历史数据，
回测仅覆盖 K 线可算部分：趋势/动量/RSI/MACD/量能/波动等）。

方法：
- 每 interval 个交易日调仓一次
- 调仓日对池内每只股票用前 120 日 K 线跑 analyzer（与线上同口径）
- 策略 A：全池 tech_score TopN（等权持有）
- 策略 B：仅 strong_buy/buy 信号中评分 TopN（贴近线上"推荐"）
- 基准 1：自选池等权；基准 2：沪深300
- 成本：单边 0.1%（费用+滑点）

用法：
  python3 scripts/backtest_strategy.py                 # 2 年，每 10 日，Top10
  python3 scripts/backtest_strategy.py --years 3 --interval 20 --top 15
  python3 scripts/backtest_strategy.py --codes 002240 600036   # 指定池
  python3 scripts/backtest_strategy.py --offline       # 只用缓存

输出：控制台报告 + data/strategy_backtest.json
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from src import analyzer, data_provider as dp, stock_pool  # noqa: E402

DATA_DIR = os.path.join(BASE_DIR, "data")
OUT_PATH = os.path.join(DATA_DIR, "strategy_backtest.json")
COST = 0.001          # 单边成本（费用+滑点）
WARMUP = 120          # 指标预热根数
LOOKBACK = 500        # 2 年约 500 交易日


def fetch_all(codes, use_cache):
    """拉取全部 K 线（长历史），返回 {code: {dates, opens, closes, highs, lows, volumes}}。

    默认缓存优先（20 小时新鲜度内直接复用，避免腾讯限流）；
    缓存缺失时才实时拉取（--offline 则完全离线）。
    """
    data = {}
    for i, code in enumerate(codes):
        k = dp.fetch_daily_kline_long(code, count=LOOKBACK + WARMUP + 50,
                                      min_days=LOOKBACK + WARMUP, use_cache=True)
        if k is None and not use_cache:
            k = dp.fetch_daily_kline_long(code, count=LOOKBACK + WARMUP + 50,
                                          min_days=LOOKBACK + WARMUP, use_cache=False)
        if k and len(k["closes"]) >= WARMUP + 100:
            data[code] = k
        if (i + 1) % 50 == 0:
            print(f"   拉取进度 {i + 1}/{len(codes)}", flush=True)
    return data


def score_at(k, idx):
    """用截至 idx 的 K 线切片跑分析，返回 (tech_score, signal_key, close)。"""
    s = slice(idx - WARMUP, idx + 1)
    r = analyzer.analyze_stock("", k["dates"][s], k["opens"][s], k["closes"][s],
                               k["highs"][s], k["lows"][s], k["volumes"][s])
    if not r:
        return None
    return r


def run_backtest(data, codes, interval, top_n, use_cache):
    """主回测循环。返回各策略净值序列 + 调仓记录。"""
    # 对齐交易日：用第一只股票（或取所有股票日期并集的最小公共轴）
    # 简化：以数据量最全的股票的日期为轴
    axis_code = max(data, key=lambda c: len(data[c]["dates"]))
    dates = data[axis_code]["dates"]
    # 每个代码建立 date -> 索引
    idx_of = {c: {d: i for i, d in enumerate(data[c]["dates"])} for c in data}
    date_idx = {d: i for i, d in enumerate(dates)}

    # 调仓点：从预热后第一个日期开始，每 interval 个交易日
    first = WARMUP
    rebal_days = list(range(first, len(dates), interval))
    if not rebal_days or rebal_days[-1] < len(dates) - 5:
        rebal_days.append(len(dates) - 1)

    # 沪深300（缓存优先，避免限流；缺失时实时拉）
    bench_data = None
    try:
        bk = dp.fetch_daily_kline_long("sh000300", count=LOOKBACK + WARMUP + 50,
                                       min_days=LOOKBACK + WARMUP, use_cache=True)
        if bk is None and not use_cache:
            bk = dp.fetch_daily_kline_long("sh000300", count=LOOKBACK + WARMUP + 50,
                                           min_days=LOOKBACK + WARMUP, use_cache=False)
        if bk:
            bench_idx = {d: i for i, d in enumerate(bk["dates"])}
            bench_data = bk
    except Exception:
        pass

    # 净值初始化
    nav_a = 1.0          # 策略 A：全池 TopN
    nav_b = 1.0          # 策略 B：信号过滤 TopN
    nav_pool = 1.0       # 池等权
    nav_bench = 1.0      # 沪深300
    pos_a = {}           # code -> 权重
    pos_b = {}
    equity_a, equity_b, equity_pool, equity_bench = [1.0], [1.0], [1.0], [1.0]
    rebalances = []

    for ri, di in enumerate(rebal_days):
        d = dates[di]
        # ---- 调仓：计算全池评分 ----
        scores = {}
        for c in codes:
            if c not in data or d not in idx_of[c]:
                continue
            k = data[c]
            i = idx_of[c][d]
            if i < WARMUP:
                continue
            r = score_at(k, i)
            if r:
                scores[c] = r
        if not scores:
            continue

        # 组合：策略 A 全池 TopN；策略 B 过滤信号
        ranked = sorted(scores.items(), key=lambda x: -x[1].score)
        top_a = [c for c, _ in ranked[:top_n]]
        buy_only = [(c, r) for c, r in ranked if r.signal_key in ("strong_buy", "buy")]
        top_b = [c for c, _ in buy_only[:top_n]]

        # 持有到下次调仓的收益（按当日收盘买入）
        next_di = rebal_days[ri + 1] if ri + 1 < len(rebal_days) else len(dates) - 1
        nd = dates[next_di]
        ret = {}
        for c in list(set(top_a) | set(top_b)):
            if c not in data or nd not in idx_of[c]:
                continue
            i0, i1 = idx_of[c][d], idx_of[c][nd]
            if i1 <= i0:
                continue
            ret[c] = data[c]["closes"][i1] / data[c]["closes"][i0] - 1.0

        # 池等权基准收益
        pool_rets = []
        for c in codes:
            if c not in data or d not in idx_of[c] or nd not in idx_of[c]:
                continue
            i0, i1 = idx_of[c][d], idx_of[c][nd]
            if i1 > i0:
                pool_rets.append(data[c]["closes"][i1] / data[c]["closes"][i0] - 1.0)
        pool_r = sum(pool_rets) / len(pool_rets) if pool_rets else 0.0

        # 沪深300
        bench_r = 0.0
        if bench_data and d in bench_idx and nd in bench_idx:
            b0, b1 = bench_idx[d], bench_idx[nd]
            if b1 > b0:
                bench_r = bench_data["closes"][b1] / bench_data["closes"][b0] - 1.0

        def rebalance_nav(nav, old_pos, new_codes, rets):
            # 换手成本：新旧组合差异的 1/2 换手 × 单边成本（简化：全换手按持仓成本）
            turnover = 1.0
            if old_pos:
                keep = len(set(old_pos) & set(new_codes))
                turnover = 1.0 - keep / max(len(new_codes), 1)
            r = sum(rets.get(c, 0.0) for c in new_codes) / max(len(new_codes), 1)
            return nav * (1 + r) * (1 - turnover * COST), new_codes

        nav_a, pos_a = rebalance_nav(nav_a, pos_a, top_a, ret)
        nav_b, pos_b = rebalance_nav(nav_b, pos_b, top_b, ret)
        nav_pool *= (1 + pool_r)
        nav_bench *= (1 + bench_r)

        equity_a.append(nav_a); equity_b.append(nav_b)
        equity_pool.append(nav_pool); equity_bench.append(nav_bench)
        rebalances.append({
            "date": d, "next": nd,
            "pool_ret": round(pool_r, 4), "bench_ret": round(bench_r, 4),
            "top_a": top_a[:5], "top_b": top_b[:5],
        })

    return {
        "dates": [dates[i] for i in rebal_days],
        "equity": {"strategy_a": equity_a, "strategy_b": equity_b,
                   "pool": equity_pool, "hs300": equity_bench},
        "rebalances": rebalances,
    }


def stats(name, nav, dates):
    n = len(nav)
    total = nav[-1] / nav[0] - 1
    d0 = datetime.strptime(dates[0], "%Y-%m-%d")
    d1 = datetime.strptime(dates[-1], "%Y-%m-%d")
    years = max((d1 - d0).days / 365.25, 1e-9)
    annual = (nav[-1] / nav[0]) ** (1 / years) - 1 if years > 0 else 0
    peak = nav[0]
    mdd = 0.0
    for v in nav:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    rets = [nav[i] / nav[i - 1] - 1 for i in range(1, n)]
    mean = sum(rets) / len(rets) if rets else 0
    std = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5 if rets else 0
    sharpe = mean / std * (244 ** 0.5) if std else 0
    return {"name": name, "total": total, "annual": annual, "mdd": mdd,
            "sharpe": sharpe, "periods": n - 1}


def stats_rows(eq, dates):
    return [stats("策略A 全池TopN", eq["strategy_a"], dates),
            stats("策略B 信号过滤TopN", eq["strategy_b"], dates),
            stats("基准 自选池等权", eq["pool"], dates),
            stats("基准 沪深300", eq["hs300"], dates)]


def main():
    ap = argparse.ArgumentParser(description="选股逻辑组合回测")
    ap.add_argument("--years", type=int, default=2, help="回测年数（默认 2）")
    ap.add_argument("--interval", type=int, default=10, help="调仓间隔交易日（默认 10）")
    ap.add_argument("--top", type=int, default=10, help="每期持股数（默认 10）")
    ap.add_argument("--codes", nargs="*", help="指定股票池（默认全部自选股）")
    ap.add_argument("--offline", action="store_true", help="只用缓存")
    args = ap.parse_args()

    global LOOKBACK
    LOOKBACK = args.years * 244
    codes = args.codes or stock_pool.WATCHLIST_CODES
    print(f"回测 {args.years} 年 · 调仓每 {args.interval} 日 · Top{args.top} · 池 {len(codes)} 只 · 单边成本 {COST:.1%}")
    print("拉取 K 线（首次约 3-5 分钟，之后走缓存）…")
    data = fetch_all(codes, args.offline)
    print(f"成功 {len(data)}/{len(codes)} 只")

    res = run_backtest(data, codes, args.interval, args.top, args.offline)
    eq = res["equity"]
    rows = stats_rows(eq, res["dates"])

    print("\n" + "=" * 78)
    print(f"{'组合':<18}{'总收益':>10}{'年化':>9}{'最大回撤':>10}{'夏普':>8}{'调仓期':>7}")
    print("-" * 78)
    for r in rows:
        print(f"{r['name']:<18}{r['total'] * 100:>+9.1f}%{r['annual'] * 100:>+8.1f}%"
              f"{r['mdd'] * 100:>9.1f}%{r['sharpe']:>8.2f}{r['periods']:>7}")
    print("=" * 78)

    # 胜率：策略 vs 池等权 每个调仓期
    wins = sum(1 for i in range(1, len(eq["strategy_a"]))
               if eq["strategy_a"][i] / eq["strategy_a"][i - 1] - 1
               > eq["pool"][i] / eq["pool"][i - 1] - 1)
    wins_b = sum(1 for i in range(1, len(eq["strategy_b"]))
                 if eq["strategy_b"][i] / eq["strategy_b"][i - 1] - 1
                 > eq["pool"][i] / eq["pool"][i - 1] - 1)
    n = len(eq["strategy_a"]) - 1
    print(f"跑赢自选池等权：策略A {wins}/{n} ({wins / n:.0%}) · 策略B {wins_b}/{n} ({wins_b / n:.0%})")
    a_vs_b = eq["strategy_a"][-1] / eq["pool"][-1] - 1
    b_vs_b = eq["strategy_b"][-1] / eq["pool"][-1] - 1
    print(f"相对池等权累计超额：策略A {a_vs_b * 100:+.1f}% · 策略B {b_vs_b * 100:+.1f}%")

    res["stats"] = rows
    res["generatedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    os.replace(tmp, OUT_PATH)
    print(f"\n明细已写入 {OUT_PATH}")


if __name__ == "__main__":
    main()
