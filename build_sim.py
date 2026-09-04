#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""5 万模拟炒股 · 过去两个月回放（沙盒）。

用与每日复盘完全一致的分析口径（analyzer.analyze_stock，含 idx 无前视支持），
逐交易日回放 2026-07-01 → 至今，模拟「如果给我 5 万会怎么操作」：
  1. 每交易日收盘后对全股票池算信号（只依赖截至当日的数据 → 无前视）
  2. 买入：strong_buy/buy + 乖离 < 5% + 普涨过热日闸门 + 大盘空头禁新仓
  3. 卖出：跌破 ATR/MA20 止损、sell 信号、止盈
  4. 成交一律用「次日开盘价」（信号当日收盘产生，次日才可成交 → 真实）
输出 data/sim_trade.json，供 sim.html 渲染。
"""
import argparse
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

from src import analyzer as az
from src import indicators as ind
from src import data_provider as dp

DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")

CASH_START = 50000.0
MAX_POSITIONS = 5
POSITION_FRACTION = 0.18        # 单票目标仓位 18%
CASH_RESERVE = 0.10             # 保留 10% 现金
MIN_SCORE_BUY = 68              # buy 阈值（与 DECISION_SCALE 一致）
OVERHEAT_RATIO = 0.65           # 普涨过热闸门
SELL_SCORE = 20                 # sell 阈值
TAKE_PROFIT_PCT = 0.15          # 单票 +15% 止盈（部分）

# 候选池：真股票（35 只，含缓存 K 线），覆盖化工/化肥/券商银行/科技/资源军工消费
POOL = {
    "600309": "万华化学", "600426": "华鲁恒升", "600486": "扬农化工", "002648": "卫星化学",
    "600989": "宝丰能源", "600346": "恒力石化", "601233": "桐昆股份", "002493": "荣盛石化",
    "603225": "新凤鸣", "600096": "云天化", "000902": "新洋丰", "000893": "亚钾国际",
    "301035": "润丰股份", "600141": "兴发集团", "002601": "龙佰集团", "600030": "中信证券",
    "600036": "招商银行", "000001": "平安银行", "601088": "中国神华", "600900": "长江电力",
    "601899": "紫金矿业", "600276": "恒瑞医药", "000858": "五粮液", "002475": "立讯精密",
    "601138": "工业富联", "300308": "中际旭创", "300502": "新易盛", "601766": "中国中车",
    "601919": "中远海控", "600111": "北方稀土", "002371": "北方华创", "600584": "长电科技",
    "002460": "赣锋锂业", "601318": "中国平安", "600519": "贵州茅台",
    "300059": "东方财富",
}


def load_kline(code):
    """读 K 线缓存，缺则现拉。返回 dict 或 None。"""
    path = os.path.join(CACHE_DIR, "kline_%s.json" % code)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    try:
        k = dp.fetch_daily_kline(code, count=320)
        return k
    except Exception as e:
        print("[sim] 拉取 %s K线失败: %s" % (code, e))
        return None


def load_index_kline(code):
    path = os.path.join(CACHE_DIR, "kline_idx_%s_320.json" % code)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def fmt_money(v):
    return "%.0f" % v


def run_sim(start_date="2026-07-01", end_date="2026-09-04", fee_rate=0.0003, stamp_rate=0.0005):
    # ---------- 1. 加载数据 ----------
    klines = {}
    for code in POOL:
        k = load_kline(code)
        if k and len(k.get("dates") or []) >= 90:
            klines[code] = k

    # 上证指数 K 线做大盘过滤
    sh = load_index_kline("sh000001")
    if not sh:
        sh = dp.fetch_index_kline("sh000001", 320)
    # 交易日轴 = 全池 K 线日期的并集，过滤 >= start_date
    all_dates = set()
    for k in klines.values():
        all_dates.update(k["dates"])
    idx_dates = sh["dates"] if sh else []
    dates = sorted(d for d in (all_dates | set(idx_dates)) if d >= start_date and d <= "2026-09-04")

    # 每只股票: dates -> idx 映射, 便于当日定位 analyze
    date_pos = {}  # code -> {date: pos}
    for code, k in klines.items():
        date_pos[code] = {d: i for i, d in enumerate(k["dates"])}
    sh_pos = {d: i for i, d in enumerate(idx_dates)} if sh else {}

    # ---------- 2. 状态 ----------
    cash = CASH_START
    positions = {}   # code -> {"shares": int, "cost": float, "buy_date": str, "stop": float, "peak": float}
    equity_curve = []
    trades = []
    daily_notes = []
    prev_signals = {}  # 前一交易日各票信号（用于判断次日操作时还须记住次日信号是否仍 buy？简化：次日开盘直接按前日信号成交）

    def position_value(code, price):
        p = positions.get(code)
        return p["shares"] * price if p else 0.0

    def total_equity(px_map):
        return cash + sum(position_value(c, px_map[c]) for c in positions)

    # ---------- 3. 逐日回放 ----------
    for di, day in enumerate(dates):
        # 3a. 获取当日每只候选的收盘信号（只依赖 <= day 的数据）
        day_sigs = {}    # code -> AnalysisResult
        px_today = {}    # code -> 当日收盘价
        for code, k in klines.items():
            pos = date_pos[code].get(day)
            if pos is None or pos < 30:
                continue
            r = az.analyze_stock(POOL[code], k["dates"], k["opens"], k["closes"],
                                 k["highs"], k["lows"], k["volumes"], code, idx=pos)
            if r:
                day_sigs[code] = r
                px_today[code] = r.close

        # 大盘状态（上证 analyze）
        sh_sig = None
        sh_close = None
        if sh and day in sh_pos and sh_pos[day] >= 30:
            ip = sh_pos[day]
            sh_sig = az.analyze_stock("上证", sh["dates"], sh["opens"], sh["closes"],
                                      sh["highs"], sh["lows"], sh["volumes"], "sh000001", idx=ip)
            if sh_sig:
                sh_close = sh_sig.close

        # 广度（过热闸门）：全池上涨占比
        up = sum(1 for c in day_sigs.values() if c.change_pct > 0)
        dn = sum(1 for c in day_sigs.values() if c.change_pct < 0)
        tot = max(len(day_sigs), 1)
        breadth = up / tot
        overheat = breadth >= OVERHEAT_RATIO
        market_bear = bool(sh_sig and sh_sig.signal_key in ("sell", "reduce") and (sh_sig.score or 0) < 45)

        # 3b. 卖出检查（当日收盘信号 / 止损，次日开盘成交）
        day_notes = []
        sells_today = []
        next_day = dates[di + 1] if di + 1 < len(dates) else None
        for code in list(positions.keys()):
            p = positions[code]
            sig = day_sigs.get(code)
            px = px_today.get(code, p["cost"])
            # 止损：ATR 自适应止损为主（较 MA20 宽容，避免正常洗盘被杀）
            sig = day_sigs.get(code)
            atr_s = (sig.atr_stop if sig and sig.atr_stop else None) or p.get("atr_stop")
            ma20_s = (sig.stop_loss if sig else None) or p["stop"]
            # 若当日跌破 ATR 止损或跌破 MA20 且当日跌幅>3%（确认破位）→ 卖
            stop = atr_s or ma20_s
            broken_hard = atr_s is not None and px <= atr_s
            chg_today = sig.change_pct if sig else 0
            broken_ma = bool(ma20_s and px <= ma20_s and chg_today < -3.0)
            broken = broken_hard or broken_ma
            sig_sell = bool(sig and sig.signal_key in ("sell", "reduce"))
            # 止盈：相对成本 +15%（信号不弱时也止盈落袋，纪律优先）
            peak_gain = px / p["cost"] - 1 if p["cost"] else 0
            tp_hit = peak_gain >= TAKE_PROFIT_PCT
            if broken or sig_sell or tp_hit:
                reason = []
                if broken:
                    reason.append("破ATR/MA20止损 %.2f" % (atr_s or ma20_s))
                if sig_sell:
                    reason.append("信号转%s(%s分)" % (sig.signal, sig.score))
                if tp_hit and not sig_sell and not broken_hard:
                    reason.append("止盈 +%.0f%%" % (peak_gain * 100))
                if reason and next_day:
                    # 次日开盘价成交
                    npos = date_pos[code].get(next_day)
                    np_ = None
                    if npos is not None and klines[code]["opens"]:
                        np_ = klines[code]["opens"][npos]
                    price = np_ or px
                    proceeds = p["shares"] * price
                    fee = proceeds * (fee_rate + stamp_rate)
                    cash += proceeds - fee
                    pnl = proceeds - p["shares"] * p["cost"]
                    trades.append({
                        "action": "sell", "date": next_day, "code": code, "name": POOL[code],
                        "price": round(price, 3), "shares": p["shares"],
                        "pnl": round(pnl, 2), "pnl_pct": round(pnl / (p["shares"] * p["cost"]) * 100, 2),
                        "reason": "；".join(reason), "hold_days": (di - dates.index(p["buy_date"])) if p["buy_date"] in dates else 0,
                        "signal_date": day,
                    })
                    sells_today.append((code, next_day))
                    day_notes.append("卖出 %s：%s" % (POOL[code], "；".join(reason)))

        for code, _ in sells_today:
            del positions[code]

        # 3c. 买入候选（当日收盘信号 → 次日开盘成交）
        if not market_bear and not overheat and next_day:
            # 候选：signal buy/strong_buy、bias<5、未持有、非涨停过压
            cands = []
            for code, sig in day_sigs.items():
                if code in positions:
                    continue
                if sig.signal_key not in ("strong_buy", "buy"):
                    continue
                if sig.bias_ma5 is not None and abs(sig.bias_ma5) >= 5.0:
                    continue
                # 现价未越过止盈位（analyzer 已处理，防御重复）
                cands.append((sig.score or 0, code, sig))
            cands.sort(key=lambda x: -x[0])
            # 按分数从高到低，直到仓位满
            for sc, code, sig in cands:
                if len(positions) >= MAX_POSITIONS:
                    break
                # 单票预算
                budget = total_equity(px_today) * POSITION_FRACTION
                if cash < budget * 0.5:
                    break
                npos = date_pos[code].get(next_day)
                if npos is None or not klines[code]["opens"]:
                    continue
                price = klines[code]["opens"][npos]
                if isinstance(price, (list, tuple)):
                    raise TypeError("price list for %s at %s: npos=%r type=%s" % (code, next_day, npos, type(price)))
                if not price or price <= 0:
                    continue
                shares = int(budget / price / 100) * 100
                if shares <= 0:
                    continue
                cost_amt = shares * price
                fee = cost_amt * fee_rate
                if cash < cost_amt + fee:
                    shares = int((cash * 0.99 - fee) / price / 100) * 100
                    cost_amt = shares * price
                    if shares <= 0:
                        continue
                cash -= cost_amt + fee
                positions[code] = {"shares": shares, "cost": price, "buy_date": next_day,
                                   "stop": sig.stop_loss, "atr_stop": sig.atr_stop, "peak": price}
                trades.append({
                    "action": "buy", "date": next_day, "code": code, "name": POOL[code],
                    "price": round(price, 3), "shares": shares,
                    "reason": "%s(%s分) 现价距理想买点%s" % (
                        sig.signal, sig.score,
                        ("%.2f" % sig.ideal_buy) if sig.ideal_buy else "--"),
                    "signal_date": day,
                })
                day_notes.append("买入 %s：%s %s分" % (POOL[code], sig.signal, sig.score))

        # 3d. 当日净值（收盘）
        eq = cash + sum(position_value(c, px_today[c]) for c in positions if c in px_today)
        # 持仓股当日可能无信号(停牌)用持仓成本近似
        for c in positions:
            if c not in px_today and day in date_pos[c]:
                pos = date_pos[c][day]
                px_today[c] = klines[c]["closes"][pos]
        equity_curve.append({"date": day, "equity": round(eq, 2),
                             "cash": round(cash, 2), "pos_count": len(positions),
                             "bench": sh_close})
        if day_notes:
            daily_notes.append({"date": day, "notes": day_notes,
                                "breadth": round(breadth * 100, 1),
                                "market": sh_sig.signal if sh_sig else "--",
                                "market_score": sh_sig.score if sh_sig else None})

    # 期末持仓快照
    final_pos = []
    last_px = px_today
    for code, p in positions.items():
        px = last_px.get(code)
        if px is None:
            pos = date_pos[code].get(dates[-1])
            if pos is not None:
                px = klines[code]["closes"][pos]
        if px is None:
            px = p["cost"]
        cost_v = p["shares"] * p["cost"]
        cur_v = p["shares"] * px
        final_pos.append({"code": code, "name": POOL[code], "shares": p["shares"],
                          "cost": round(p["cost"], 3), "price": round(px, 3),
                          "value": round(cur_v, 2),
                          "pnl": round(cur_v - cost_v, 2),
                          "pnl_pct": round((cur_v / cost_v - 1) * 100, 2) if cost_v else 0,
                          "buy_date": p["buy_date"], "unrealized": True})

    return build_result(equity_curve, trades, dates, daily_notes, final_pos)


def _buy_day_index(dates, buy_date, di):
    try:
        return di - dates.index(buy_date)
    except ValueError:
        return 0


def build_result(equity_curve, trades, dates, daily_notes, final_pos=None):
    start = CASH_START
    final = equity_curve[-1]["equity"] if equity_curve else start
    ret = (final / start - 1) * 100
    # 基准：上证区间涨跌（首末日归一）
    bench_ret = None
    valid_bench = [p["bench"] for p in equity_curve if p.get("bench")]
    if len(valid_bench) >= 2:
        bench_ret = (valid_bench[-1] / valid_bench[0] - 1) * 100
    # 最大回撤
    peak = -1e18
    mdd = 0.0
    for p in equity_curve:
        peak = max(peak, p["equity"])
        mdd = min(mdd, p["equity"] / peak - 1)
    # 交易统计
    buys = [t for t in trades if t["action"] == "buy"]
    sells = [t for t in trades if t["action"] == "sell"]
    closed_pnl = [t for t in sells if t["pnl"] is not None]
    wins = [t for t in closed_pnl if t["pnl"] > 0]
    avg_hold = sum(t["hold_days"] for t in closed_pnl) / len(closed_pnl) if closed_pnl else 0
    # 期末持仓浮盈
    unreal_pnl = sum(p["pnl"] for p in (final_pos or []))
    realized = sum(t["pnl"] for t in closed_pnl)
    # 已了结+当前持仓整体胜率（持仓按浮盈计）
    open_win = sum(1 for p in (final_pos or []) if p["pnl"] > 0)
    total_closed = len(closed_pnl) + len(final_pos or [])
    return {
        "title": "5万模拟盘 · 过去两个月回放",
        "start_date": dates[0], "end_date": dates[-1],
        "start_cash": start, "final_equity": round(final, 2),
        "return_pct": round(ret, 2),
        "max_drawdown": round(mdd * 100, 2),
        "bench_return_pct": bench_ret,
        "alpha_pct": round(ret - (bench_ret or 0), 2),
        "stats": {
            "buy_times": len(buys), "sell_times": len(sells),
            "win_trades": len(wins), "win_rate": round(len(wins) / len(closed_pnl) * 100, 1) if closed_pnl else 0,
            "avg_hold_days": round(avg_hold, 1),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unreal_pnl, 2),
            "total_pnl": round(realized + unreal_pnl, 2),
            "overall_win_rate": round((len(wins) + open_win) / total_closed * 100, 1) if total_closed else 0,
            "open_positions": len(final_pos or []),
        },
        "equity_curve": equity_curve,
        "trades": trades,
        "positions": final_pos or [],
        "daily_notes": daily_notes,
        "generatedAt": time_str(),
    }


def time_str():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-07-01")
    ap.add_argument("--end", default="2026-09-04")
    args = ap.parse_args()
    res = run_sim(args.start, end_date=args.end)
    out_path = os.path.join(DATA_DIR, "sim_trade.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print("模拟完成:", out_path)
    print("区间", res["start_date"], "→", res["end_date"])
    print("期末净值 %.0f  收益 %+.2f%%  最大回撤 %.2f%%" % (res["final_equity"], res["return_pct"], res["max_drawdown"]))
    print("交易 %s 笔（买%s/卖%s） 胜率 %s%%  均持仓 %s 天" % (
        len(res["trades"]), res["stats"]["buy_times"], res["stats"]["sell_times"],
        res["stats"]["win_rate"], res["stats"]["avg_hold_days"]))
