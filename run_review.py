#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日复盘统一入口（v2：自选 + 大盘双模块）。

用法：
  python run_review.py --mode review       # 盘后：复盘 + 回测（自选+大盘）
  python run_review.py --mode recommend    # 开盘：推荐 + 当日购买原因
  python run_review.py --mode all          # 全部（默认）

  python run_review.py --top 15
  python run_review.py --offline
  python run_review.py --no-backtest
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import data_provider as dp
from src import analyzer as az
from src import screener as scr
from src import backtest as bt
from src import grid_backtest as gbt
from src import grid_signal as gs
from src import report as rp
from src import stock_pool as sp
from src.stock_pool import WATCHLIST_CODES, INDEX_CODES, MARKET_POOL, MARKET_POOL_CODES, BACKTEST_CODES


def fetch_index_rows():
    """拉取大盘指数行情，返回 [{name, code, close, change_pct}]。"""
    rows = []
    symbols = [ix["code"] for ix in INDEX_CODES]
    qs = dp.fetch_index_quotes(symbols)
    for ix in INDEX_CODES:
        q = qs.get(ix["code"])
        if not q:
            continue
        rows.append({
            "name": ix["name"], "code": ix["code"],
            "close": q.get("price"), "change_pct": q.get("change"),
        })
    return rows


def analyze_codes(codes, names, offline):
    """对一组代码做技术分析。names: {code: name}。返回 {code: (name, kline, analysis, quote)}。"""
    quotes = {} if offline else dp.fetch_quotes([c for c in codes])
    klines = dp.fetch_daily_kline_batch(codes, count=320)
    out = {}
    for code, k in klines.items():
        a = az.analyze_stock(names.get(code, code), k["dates"], k["opens"], k["closes"],
                             k["highs"], k["lows"], k["volumes"], code=code)
        if a is None:
            continue
        out[code] = (names.get(code, code), k, a, quotes.get(code))
    return out


def to_screen_item(code, item):
    name, k, a, q = item
    sector_map = sp.get_code_sector()
    return scr.ScreenItem(
        name=name, code=code, sector=sector_map.get(code, ""),
        price=(q or {}).get("price") or a.close,
        change_pct=(q or {}).get("change") or a.change_pct,
        amount=(q or {}).get("amount") or 0,
        turnover=(q or {}).get("turnover") or 0,
        pe=(q or {}).get("pe"), pb=(q or {}).get("pb"),
        volume_ratio=a.volume_ratio,
        tech_score=a.score, signal_key=a.signal_key, signal=a.signal,
        trend_status=a.trend_status, change_60d=a.change_60d,
        ideal_buy=a.ideal_buy, secondary_buy=a.secondary_buy,
        stop_loss=a.stop_loss, take_profit=a.take_profit,
    )


def build_names_from_quotes(codes, offline):
    quotes = {} if offline else dp.fetch_quotes(codes)
    return {c: (quotes.get(c) or {}).get("name") or c for c in codes}, quotes


def run_review(args):
    print("== 拉取自选股数据并技术分析 ==")
    names, quotes = build_names_from_quotes(WATCHLIST_CODES, args.offline)
    pool = analyze_codes(WATCHLIST_CODES, names, args.offline)
    print(f"   自选池成功分析 {len(pool)} / {len(WATCHLIST_CODES)} 只")

    # 大盘模块：指数 + 大盘池
    print("== 大盘模块 ==")
    indices = fetch_index_rows()
    print(f"   指数 {len(indices)} 个")
    pool_names = {m['code']: m['name'] for m in MARKET_POOL}
    market_pool = analyze_codes(MARKET_POOL_CODES, pool_names, args.offline)
    print(f"   大盘池成功分析 {len(market_pool)} 只")

    analyses = [v[2] for v in pool.values()]
    market_analyses = [v[2] for v in market_pool.values()]

    # 板块映射（复盘按板块分组展示）
    sector_map = sp.get_code_sector()
    for code, v in pool.items():
        v[2].sector = sector_map.get(code, "")
    for code, v in market_pool.items():
        v[2].sector = sector_map.get(code, "")

    print("== 生成复盘数据 ==")
    review = rp.build_review("A股", analyses, "post",
                             indices=indices, market_analyses=market_analyses)
    p = rp.save("review_data.json", review)
    print(f"   {p}  自选温度 {review['temperature']['score']}({review['temperature']['label']})")

    if not args.no_backtest:
        print("== 网格均值回归回测（移植自 dividend_grid_strategy）==")
        per_stock = {}
        gparams = gbt.GridParams()
        ap = gbt.AnchorParams(lookback_days=750, min_periods=500)
        cfg = gbt.BacktestConfig(cost_rate=0.0005, cash_rate=0.0, rf=0.0)
        for meta in BACKTEST_CODES:
            name, code = meta["name"], meta["code"]
            k = dp.fetch_daily_kline_long(code, count=3200)
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
            except Exception as e:
                print(f"   [跳过] {name}: 均值线样本不足")
                continue
            per_stock[name] = res
            s = gbt.summary_metrics(res)
            print(f"   {name}: 年化{s['annual_return']*100:.1f}% 回撤{s['max_drawdown']*100:.1f}% "
                  f"夏普{s['sharpe']} 交易{s['trade_count']}次 基准年化{s['benchmark_annual']*100:.1f}%")
        bt_data = gbt.build_backtest_data(per_stock)
        p = rp.save("backtest_data.json", bt_data)
        print(f"   {p}  共 {len(per_stock)} 只 / {bt_data['overall'].get('total', 0)}")

    return pool, indices, market_analyses


def _load_sector_valuation():
    """读取 sector_valuation_data.js，返回板块估值记录列表。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sector_valuation_data.js")
    try:
        raw = open(path, "r", encoding="utf-8").read()
        m = re.search(r"window\.sectorValuationData\s*=\s*(\{.*?\});", raw, re.S)
        if m:
            return json.loads(m.group(1)).get("sectors", [])
    except Exception as e:
        print(f"   [warn] 板块估值读取失败: {e}")
    return []


def _rank_sectors(sectors):
    """给板块打分：估值越便宜(PE低/低估档)越靠前，结合当日涨跌幅动量。"""
    level_score = {"低估": 3, "合理": 1, "偏高": -1, "高估": -3}
    for s in sectors:
        pe = s.get("pe")
        pe_score = 0
        if pe and pe > 0:
            # PE 越低分越高（0→5, 60→0）
            pe_score = max(0, min(5, 5 - pe / 12))
        chg = s.get("change") or 0
        mom = max(-3, min(3, chg * 0.5))
        lev = level_score.get(s.get("level", ""), 0)
        s["sector_score"] = round(pe_score * 0.5 + lev * 0.3 + mom * 0.2, 2)
    sectors = [s for s in sectors if s.get("category") not in ("宽基指数",)]
    sectors.sort(key=lambda x: x.get("sector_score", 0), reverse=True)
    return sectors


# 板块推荐名（估值指数） -> watchlist 个股分类，用于从推荐板块中挑出对应个股
SECTOR_RECO_MAP = {
    "中证银行": ["红利金融"],
    "非银金融": ["红利金融"],
    "证券公司": ["红利金融"],
    "内地地产": ["房地产"],
    "300基建": ["基建交通"],
    "一带一路": ["基建交通"],
    "养老产业": ["医药医疗", "大消费"],
    "中证白酒": ["大消费"],
    "中证医疗": ["医药医疗"],
    "中证医药": ["医药医疗"],
    "中证新能": ["新能源电力"],
    "中证军工": ["军工"],
    "CSSW电子": ["科技-半导体芯片", "科技-通信电子"],
}


def pick_sector_stocks(sector_recs, screen_items, top_n=5):
    """从板块推荐的对应个股分类中，选出综合分最高的 top_n 只（按板块推荐名过滤）。"""
    reco_cats = set()
    for s in sector_recs:
        reco_cats.update(SECTOR_RECO_MAP.get(s.get("name", ""), []))
    if not reco_cats:
        return []
    matched = [it for it in screen_items if it.sector in reco_cats]
    matched.sort(key=lambda it: it.total_score, reverse=True)
    return matched[:top_n]


def run_recommend(args):
    print("== 拉取自选股数据（推荐）==")
    names, quotes = build_names_from_quotes(WATCHLIST_CODES, args.offline)
    pool = analyze_codes(WATCHLIST_CODES, names, args.offline)
    print(f"   自选池成功分析 {len(pool)} 只")

    indices = fetch_index_rows()

    screen_items = [to_screen_item(c, v) for c, v in pool.items()]
    picks = scr.screen(screen_items, tech_weight=0.5, top_n=args.top)

    # 大盘推荐：从大盘池中选，默认前 5
    pool_names = {m['code']: m['name'] for m in MARKET_POOL}
    market_pool = analyze_codes(MARKET_POOL_CODES, pool_names, args.offline)
    market_items = [to_screen_item(c, v) for c, v in market_pool.items()]
    market_top = min(5, len(market_items))
    market_picks = scr.screen(market_items, tech_weight=0.5, top_n=market_top)

    # 板块推荐：基于估值 + 动量
    sector_recs = _rank_sectors(_load_sector_valuation())

    # 板块推荐对应个股：从推荐板块分类中挑综合分 Top5
    sector_picks = pick_sector_stocks(sector_recs, screen_items, top_n=5)

    # 生成当日购买原因
    for it in picks:
        it.reasons = scr.build_buy_reason(it)
    for it in market_picks:
        it.reasons = scr.build_buy_reason(it)
    for it in sector_picks:
        it.reasons = scr.build_buy_reason(it)

    # 网格策略操作提醒：对推荐候选（自选+大盘+板块选股）计算均值线偏离信号
    # 优先复用已有回测结果，避免重复拉取长K线/估值（未覆盖的标的现场计算）
    print("== 网格策略操作提醒 ==")
    gparams = gbt.GridParams()
    gap = gbt.AnchorParams(lookback_days=750, min_periods=500)
    cand = {}
    for it in list(picks) + list(market_picks) + list(sector_picks):
        if it.code and it.code not in cand:
            cand[it.code] = (it.name, it.code)
    bt_data_prev = None
    bt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "backtest_data.json")
    if os.path.exists(bt_path):
        try:
            with open(bt_path, "r", encoding="utf-8") as fp:
                bt_data_prev = json.load(fp)
        except Exception:
            bt_data_prev = None
    grid_signals = gs.build_grid_signals(list(cand.values()), gparams, gap, bt_data_prev)
    print(f"   推荐候选 {len(cand)} 只，网格信号成功 {len(grid_signals)} 只")
    for s in grid_signals:
        print(f"   {s['name']}: dev={s['dev']*100:+.1f}% 仓位{s['position']*100:.0f}% → {s['action']}")

    rec = rp.build_recommend(picks, market_items=market_picks, indices=indices,
                             sectors=sector_recs, sector_picks=sector_picks,
                             grid_signals=grid_signals)
    p = rp.save("recommend_data.json", rec)
    print(f"   {p}  自选 Top{len(picks)} + 大盘 Top{len(market_picks)} + 板块 {len(sector_recs)} + 板块选股 {len(sector_picks)}")
    for it in picks[:5]:
        print(f"     {it.rating} {it.total_score:5.1f}  {it.name}  {it.signal}")
        print(f"       💡 {it.reasons}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["review", "recommend", "all"], default="all")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--no-backtest", action="store_true")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--eval-window", type=int, default=10)
    args = ap.parse_args()

    if args.mode in ("review", "all"):
        run_review(args)
    if args.mode in ("recommend", "all"):
        run_recommend(args)
    print("完成。")


if __name__ == "__main__":
    main()
