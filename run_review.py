#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日复盘统一入口（v2：自选 + 大盘双模块）。

用法：
  python run_review.py --mode review       # 盘后：复盘 + 回测（自选+大盘）
  python run_review.py --mode recommend    # 开盘：推荐 + 当日购买原因
  python run_review.py --mode metals       # 有色金属期货行情 + 分析
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
from src import holdings as hd
from src import report as rp
from src import futures as fm
from src import stock_pool as sp
from src import market_breadth as mb
from src.stock_pool import WATCHLIST_CODES, INDEX_CODES, MARKET_POOL, MARKET_POOL_CODES, BACKTEST_CODES, US_INDEX_CODES


def fetch_index_rows():
    """拉取大盘指数行情 + 技术分析因子，返回 [{name, code, close, change_pct, factors}]。

    对能拉到日K的指数（含纳指/标普）附加技术因子：
    trend_status/score/signal/ma5/ma20/macd_status/rsi12/bias_ma20 等。
    """
    rows = []
    symbols = [ix["code"] for ix in INDEX_CODES]
    qs = dp.fetch_index_quotes(symbols)
    for ix in INDEX_CODES:
        q = qs.get(ix["code"])
        if not q:
            continue
        row = {
            "name": ix["name"], "code": ix["code"],
            "close": q.get("price"), "change_pct": q.get("change"),
        }
        # 技术分析因子：拉指数日K，失败则跳过因子（仅保留行情）
        try:
            k = dp.fetch_index_kline(ix["code"], count=320)
            if k and len(k["closes"]) >= 30:
                a = az.analyze_stock(ix["name"], k["dates"], k["opens"], k["closes"],
                                     k["highs"], k["lows"], k["volumes"], code=ix["code"])
                if a is not None:
                    row["factors"] = {
                        "date": a.date,
                        "trend_status": a.trend_status,
                        "trend_strength": a.trend_strength,
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
        except Exception as e:
            print(f"   [warn] 指数 {ix['name']} 因子分析失败: {e}")
        rows.append(row)
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
        open=(q or {}).get("open") or a.open,
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
    name_map = sp.get_code_name()
    return {c: (quotes.get(c) or {}).get("name") or name_map.get(c) or c for c in codes}, quotes


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
    # 复盘列表按推荐强度排序（强买>买入>观望>减仓>卖出），同级按评分降序
    analyses.sort(key=lambda a: (scr.strength_key(a), -a.score))
    market_analyses.sort(key=lambda a: (scr.strength_key(a), -a.score))

    # 板块映射（复盘按板块分组展示）
    sector_map = sp.get_code_sector()
    for code, v in pool.items():
        v[2].sector = sector_map.get(code, "")
    for code, v in market_pool.items():
        v[2].sector = sector_map.get(code, "")

    print("== 生成复盘数据 ==")
    breadth = mb.fetch_market_breadth(use_cache=True)
    if breadth and breadth.get("total"):
        print(f"   全市场涨跌家数: 涨{breadth['up']} 跌{breadth['down']} "
              f"平{breadth['flat']} 共{breadth['total']} 源={breadth.get('source')}")
    review = rp.build_review("A股", analyses, "post",
                             indices=indices, market_analyses=market_analyses,
                             breadth=breadth)
    p = rp.save("review_data.json", review)
    print(f"   {p}  自选温度 {review['temperature']['score']}({review['temperature']['label']}) "
          f"广度源={review['temperature'].get('source')}")

    if not args.no_backtest:
        print("== 网格均值回归回测（移植自 dividend_grid_strategy）==")
        per_stock = {}
        gparams = gbt.GridParams()
        ap = gbt.AnchorParams(lookback_days=750, min_periods=500)
        cfg = gbt.BacktestConfig(cost_rate=0.0005, slippage_rate=0.0005, cash_rate=0.0, rf=0.0)
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
    """给板块打分（2026-08 修正）：绝对 PE 改为跨板块分位（行业间绝对 PE 不可比），
    估值档位升权，当日动量降权（单日动量是噪音）。"""
    level_score = {"低估": 3, "合理": 1, "偏高": -1, "高估": -3}
    valid_pes = sorted(v for v in (s.get("pe") for s in sectors) if v and v > 0)

    def pe_score_of(pe):
        # PE 越低分越高（0→5），按全板块分位而不是绝对数值
        if not pe or pe <= 0 or not valid_pes:
            return 2.5
        rank = sum(1 for v in valid_pes if v < pe) / len(valid_pes)
        return round((1 - rank) * 5, 2)

    for s in sectors:
        chg = s.get("change") or 0
        mom = max(-3, min(3, chg * 0.5))
        lev = level_score.get(s.get("level", ""), 0)
        s["sector_score"] = round(pe_score_of(s.get("pe")) * 0.45 + lev * 0.35 + mom * 0.20, 2)
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
    "CSSW电子": ["半导体", "PCB/覆铜板", "CPO/光模块", "科技-通信电子"],
}


def pick_sector_stocks(sector_recs, screen_items, top_n=10):
    """按推荐板块分组，从对应自选分类中选出综合分最高的 top_n 只，返回 {板块名: [个股...]}。"""
    result = {}
    for s in sector_recs:
        cats = SECTOR_RECO_MAP.get(s.get("name", ""), [])
        if not cats:
            continue
        matched = [it for it in screen_items if it.sector in cats]
        matched.sort(key=lambda it: it.total_score, reverse=True)
        if matched:
            result[s["name"]] = matched[:top_n]
    return result


def load_holding_codes():
    """读取 holdings.json 的持仓代码集合。"""
    hpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "holdings.json")
    try:
        with open(hpath, "r", encoding="utf-8") as f:
            d = json.load(f)
        return {str(h.get("code", "")).zfill(6) for h in (d.get("holdings") or [])}
    except Exception:
        return set()


def _sector_rank(sector: str) -> int:
    """板块优先级：红利 > 蓝筹(宽基/大消费/银行) > 其他。"""
    if not sector:
        return 3
    s = sector
    if "红利" in s or "银行" in s:
        return 0
    if "宽基" in s or "大消费" in s or "沪深" in s:
        return 1
    return 2


def prioritize_picks(picks, holding_codes):
    """标记持仓并排序推荐列表：按推荐强度降序（强买>买入>观望>减仓>卖出），同级按综合分降序。"""
    for it in picks:
        it.is_holding = it.code in holding_codes
    picks.sort(key=lambda it: (scr.strength_key(it), -it.total_score))
    return picks


def run_recommend(args):
    print("== 拉取自选股数据（推荐）==")
    names, quotes = build_names_from_quotes(WATCHLIST_CODES, args.offline)
    pool = analyze_codes(WATCHLIST_CODES, names, args.offline)
    print(f"   自选池成功分析 {len(pool)} 只")

    indices = fetch_index_rows()

    screen_items = [to_screen_item(c, v) for c, v in pool.items()]

    # 自选推荐：先对全部自选股打分，再按板块分组，每板块挑综合分 TopN
    all_scored = scr.screen(screen_items, tech_weight=0.5, top_n=len(screen_items))
    sector_top = args.top  # 每板块推荐数量
    sec_groups = {}
    for it in all_scored:
        sec = (it.sector or "").strip() or "其他"
        sec_groups.setdefault(sec, []).append(it)
    picks = []
    for sec, items in sec_groups.items():
        picks.extend(items[:sector_top])
    print(f"   自选推荐按板块分组：{len(sec_groups)} 个板块，每板块 Top{sector_top}，共 {len(picks)} 只")

    # 持仓股置顶：把持仓股并入自选推荐（若不在推荐中则补入），并打持仓标记
    holding_codes = load_holding_codes()
    if holding_codes:
        picked = {it.code for it in picks}
        pool_by_code = {it.code: it for it in screen_items}
        for code in sorted(holding_codes):
            if code in picked or code not in pool_by_code:
                continue
            picks.append(pool_by_code[code])
        picks = prioritize_picks(picks, holding_codes)
        print(f"   持仓股置顶: {[it.name for it in picks if it.is_holding]}")

    # 大盘推荐：从大盘池中选，默认前 5
    pool_names = {m['code']: m['name'] for m in MARKET_POOL}
    market_pool = analyze_codes(MARKET_POOL_CODES, pool_names, args.offline)
    market_items = [to_screen_item(c, v) for c, v in market_pool.items()]
    market_top = min(5, len(market_items))
    market_picks = scr.screen(market_items, tech_weight=0.5, top_n=market_top)
    market_picks.sort(key=lambda it: (scr.strength_key(it), -it.total_score))

    # 板块推荐：基于估值 + 动量
    sector_recs = _rank_sectors(_load_sector_valuation())

    # 板块推荐对应个股：按推荐板块分组，每板块挑综合分 Top10
    sector_stocks = pick_sector_stocks(sector_recs, screen_items, top_n=10)

    # 生成当日购买原因
    for it in picks:
        it.reasons = scr.build_buy_reason(it)
    for it in market_picks:
        it.reasons = scr.build_buy_reason(it)
    for it in [x for lst in sector_stocks.values() for x in lst]:
        it.reasons = scr.build_buy_reason(it)

    # 网格策略操作提醒：对推荐候选（自选+大盘+板块选股+持仓股）计算均值线偏离信号
    # 优先复用已有回测结果，避免重复拉取长K线/估值（未覆盖的标的现场计算）
    print("== 网格策略操作提醒 ==")
    gparams = gbt.GridParams()
    gap = gbt.AnchorParams(lookback_days=750, min_periods=500)
    cand = {}
    for it in list(picks) + list(market_picks) + [x for lst in sector_stocks.values() for x in lst]:
        if it.code and it.code not in cand:
            cand[it.code] = (it.name, it.code)
    # 持仓股强制加入网格信号（即使不在推荐候选里）
    holding_codes = load_holding_codes()
    if holding_codes:
        pool_by_code = {it.code: it for it in screen_items}
        for hc in sorted(holding_codes):
            if hc in cand:
                continue
            hit = pool_by_code.get(hc)
            if hit:
                cand[hc] = (hit.name, hc)
    bt_data_prev = None
    bt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "backtest_data.json")
    if os.path.exists(bt_path):
        try:
            with open(bt_path, "r", encoding="utf-8") as fp:
                bt_data_prev = json.load(fp)
        except Exception:
            bt_data_prev = None
    grid_signals = gs.build_grid_signals(list(cand.values()), gparams, gap, bt_data_prev, holding_codes)
    print(f"   推荐候选 {len(cand)} 只，网格信号成功 {len(grid_signals)} 只")
    for s in grid_signals:
        _dev = s.get("dev")
        _pos = s.get("position")
        _dev_s = "" if _dev is None else f"{_dev*100:+.1f}%"
        _pos_s = "" if _pos is None else f"{_pos*100:.0f}%"
        print(f"   {s['name']}: dev={_dev_s} 仓位{_pos_s} → {s.get('action')}")

    rec = rp.build_recommend(picks, market_items=market_picks, indices=indices,
                             sectors=sector_recs, sector_stocks=sector_stocks,
                             grid_signals=grid_signals,
                             temperature=rp._market_temperature(screen_items,
                                                                mb.fetch_market_breadth(use_cache=True)))
    p = rp.save("recommend_data.json", rec)
    n_picks = sum(len(v) for v in sector_stocks.values())
    print(f"   {p}  自选 Top{len(picks)} + 大盘 Top{len(market_picks)} + 板块 {len(sector_recs)} + 板块选股 {n_picks}")
    for it in picks[:5]:
        print(f"     {it.rating} {it.total_score:5.1f}  {it.name}  {it.signal}")
        print(f"       💡 {it.reasons}")


def run_holdings():
    """生成持仓页面数据：盘中实时跟踪 + 盘后复盘 + 网格提醒。"""
    print("== 持仓跟踪 ==")
    gparams = gbt.GridParams()
    gap = gbt.AnchorParams(lookback_days=750, min_periods=500)
    bt_data_prev = None
    bt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "backtest_data.json")
    if os.path.exists(bt_path):
        try:
            with open(bt_path, "r", encoding="utf-8") as fp:
                bt_data_prev = json.load(fp)
        except Exception:
            bt_data_prev = None
    data = hd.build_holdings_data(gparams, gap, bt_data_prev)
    p = hd.save_holdings_data(data)
    open_now = "盘中" if data.get("market_open") else "盘后"
    print(f"   {p}  [{open_now}] 持仓 {len(data['items'])} 只")
    for it in data["items"]:
        g = it.get("grid") or {}
        print(f"   {it['name']}: 现价{it['price']} 涨跌{it['change_pct']:+.2f}% → {g.get('action') or '--'}")


def run_metals():
    """生成有色金属期货页面数据：行情概览 + 各品种技术因子 + K线。"""
    print("== 有色金属期货分析 ==")
    data = fm.build_metals_data()
    p = fm.save_metals_data(data)
    s = data["stats"]
    print(f"   {p}  共 {s['total']} 个品种  平均 {s['avg_change']:+.2f}%  "
          f"上涨 {s['up']}/下跌 {s['down']}  领涨 {s['leader']}({s['leader_change']:+.2f}%) "
          f"领跌 {s['laggard']}({s['laggard_change']:+.2f}%)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["review", "recommend", "holdings", "metals", "tracking", "all"], default="all")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--no-backtest", action="store_true")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--eval-window", type=int, default=10)
    args = ap.parse_args()

    if args.mode in ("review", "all"):
        run_review(args)
    if args.mode in ("recommend", "all"):
        run_recommend(args)
    if args.mode in ("holdings", "all"):
        run_holdings()
    if args.mode in ("metals", "all"):
        run_metals()
    if args.mode == "tracking":
        import build_tracking
        build_tracking.main()
    print("完成。")


if __name__ == "__main__":
    main()
