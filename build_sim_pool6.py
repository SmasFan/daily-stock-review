#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""5 万模拟炒股 · 三种策略 · 过去两个月回放（沙盒）。

与每日复盘同口径分析（analyzer.analyze_stock + idx 无前视），逐交易日回放：
  1. 每交易日收盘后对全股票池算信号（只用截至当日数据）
  2. 三种策略独立跑：
     - aggressive  激进：多仓/大仓位，信号门槛低，容忍波动博弹性，移动止盈
     - balanced    稳健：中等仓位，信号门槛中，过热/空头闸门，ATR止损+固定止盈
     - disciplined 严守纪律：小仓/高门槛，只在最强多头+温和大盘买，保本优先
  3. 卖出：止损/保本/信号转弱/止盈；次日开盘价成交（无前视、含费税）
输出 data/sim_trade.json（含 equity_curve、positions、trades、daily_notes、每日收益）。
"""
import argparse
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

from src import analyzer as az
from src import data_provider as dp

DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")

CASH_START = 50000.0

# 候选池：真股票（覆盖化工/化肥/券商银行/科技/资源军工消费）
POOL = {
    "601138": "工业富联", "600900": "长江电力", "601899": "紫金矿业",
    "600309": "万华化学", "600177": "雅戈尔", "000759": "中百集团",
}

STRATEGIES = {
    "aggressive": {
        "label": "激进", "tagline": "高仓位追逐强势 · 移动止盈让利润奔跑 · 容忍回撤博弹性",
        "max_pos": 6, "pos_frac": 0.22, "cash_reserve": 0.06,
        "min_score": 66,          # 门槛低
        "allow_trends": ("强势多头", "多头排列"),
        "max_bias5": 6.0,         # 允许较高乖离（追强）
        "market_allow": lambda s, sc, br, ts: (ts in ("强势多头", "多头排列") or (ts == "弱势多头" and (sc or 0) >= 60)),
        "overheat_block": False,  # 过热不挡（激进可追）
        "stop_mode": "atr",       # 只用 ATR 宽止损
        "trail_pct": 0.10,        # 峰值回撤 10% 移动止盈
        "tp_pct": None,           # 无固定止盈，靠移动
        "breakeven_at": None,     # 不保本
        "cooldown_days": 0,
        "gap_max_buy": 3.0, "gap_min_buy": -3.5,
        "ma10_trail": None,
        "logic": "激进策略：只做强势多头/多头排列，信号门槛低（≥66分），最多6仓每仓22%，普涨过热不回避、弱市靠大盘多头闸门空仓。ATR宽止损+峰值回落移动止盈吃主升。"
                 "但前提是上证处于多头结构（强势多头/多头排列，或弱势多头且≥60分）——大盘走多才敢重仓，弱市空仓等待。"
                 "止损用 ATR 宽止损避免被洗，止盈用「峰值涨超8%后回落10%」移动止盈让利润奔跑。"
                 "代价是回撤大（本期约-8%）、单笔亏得多；适合趋势明确的上行市。",
    },
    "balanced": {
        "label": "稳健", "tagline": "适中仓位 · 过热/空头闸门 · ATR+MA20止损 · +15%止盈",
        "max_pos": 5, "pos_frac": 0.18, "cash_reserve": 0.10,
        "min_score": 68,
        "allow_trends": ("强势多头", "多头排列"),
        "max_bias5": 5.0,
        "market_allow": lambda s, sc, br, ts: not (s in ("sell", "reduce") and (sc or 0) < 45),
        "overheat_block": True,
        "stop_mode": "atr_ma",    # ATR 止损 + MA20 跌破3%确认
        "trail_pct": None,
        "tp_pct": 0.15,
        "breakeven_at": None,
        "cooldown_days": 5,
        "gap_max_buy": 2.0, "gap_min_buy": -3.0,
        "ma10_trail": None,
        "logic": "稳健策略：信号门槛中等（≥68分）且只做多头排列。每仓18%最多5仓，保留10%现金。"
                 "普涨过热日（广度≥65%）不追新、大盘空头不进场。止损=A TR宽止损+破MA20且单日跌超3%双确认，"
                 "避免洗盘误杀；单票浮盈+15%落袋。目标：控制回撤的同时吃到主要趋势，攻守平衡。",
    },
    "disciplined": {
        "label": "严守纪律", "tagline": "只买最强 · 保本优先 · 破MA10即走 · 高胜率低回撤",
        "max_pos": 4, "pos_frac": 0.14, "cash_reserve": 0.16,
        "min_score": 78,          # 门槛极高：只做最强
        "allow_trends": ("强势多头", "多头排列"),
        "max_bias5": 3.0,         # 不追高，只在回踩均线附近买
        "market_allow": lambda s, sc, br, ts: s not in ("sell", "reduce") and ((sc or 0) >= 55 or br > 0.4),
        "overheat_block": True,
        "stop_mode": "ma10",      # 破 MA10 即纪律离场
        "trail_pct": None,
        "tp_pct": 0.10,           # +10% 就收
        "breakeven_at": 0.05,     # +5% 后止损抬到成本 → 保本
        "cooldown_days": 10,      # 卖出后 10 日内不重买（防反复割磨损）
        "gap_max_buy": 2.0, "gap_min_buy": -2.5,
        "ma10_trail": 0.97,       # 破MA10还需从峰值回落≥3% 才算破位
        "logic": "严守纪律策略：只买最强的（≥76分且多头/强势多头），且只在回踩均线附近买（乖离<3%不追高）。"
                 "每仓14%最多4仓，保留16%现金。大盘非多头或广度弱不进场。"
                 "保本铁律：浮盈+5%后止损立即抬到成本价——赚了就不许亏回去；破 MA10 无条件纪律离场。"
                 "+10%即止盈。目标：胜率优先、几乎不亏本金，宁可少赚绝不大亏。",
    },
}


def load_kline(code, count=900):
    """读长 K 线（优先 long 缓存/长拉取；次新无长史则回退普通）。"""
    path = os.path.join(CACHE_DIR, "long_%s_%s.json" % (code, count))
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    try:
        k = dp.fetch_daily_kline_long(code, count=count, min_days=min(count - 20, 700))
        if k and len(k.get("dates") or []) >= 90:
            return k
    except Exception as e:
        print("[sim] 长K拉取 %s 失败: %s" % (code, e))
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


def _run_one(cfg_key, klines, dates, date_pos, sh, sh_pos, pool_names,
             start_date, end_date, fee_rate=0.0003, stamp_rate=0.0005):
    """跑单策略，返回结果 dict。"""
    cfg = STRATEGIES[cfg_key]
    cash = CASH_START
    positions = {}   # code -> state
    last_sell = {}   # code -> 卖出交易日 index（冷却）
    equity_curve = []
    trades = []
    daily_notes = []
    hold_log = []    # 每日持仓变化明细: date -> [{code,name,action,shares_after}]
    cash_curve = []

    def pos_value(px_map):
        return sum(p["shares"] * px_map.get(c, p["cost"]) for c, p in positions.items())

    for di, day in enumerate(dates):
        # 当日信号
        day_sigs, px_today = {}, {}
        for code, k in klines.items():
            pos = date_pos[code].get(day)
            if pos is None or pos < 30:
                continue
            r = az.analyze_stock(pool_names[code], k["dates"], k["opens"], k["closes"],
                                 k["highs"], k["lows"], k["volumes"], code, idx=pos)
            if r:
                day_sigs[code] = r
                px_today[code] = r.close
        # 上证
        sh_sig, sh_close = None, None
        if sh and day in sh_pos and sh_pos[day] >= 30:
            ip = sh_pos[day]
            sh_sig = az.analyze_stock("上证", sh["dates"], sh["opens"], sh["closes"],
                                      sh["highs"], sh["lows"], sh["volumes"], "sh000001", idx=ip)
            if sh_sig:
                sh_close = sh_sig.close
        # 广度
        up = sum(1 for c in day_sigs.values() if c.change_pct > 0)
        dn = sum(1 for c in day_sigs.values() if c.change_pct < 0)
        tot = max(len(day_sigs), 1)
        breadth = up / tot
        overheat = breadth >= 0.65
        mkt_sig = sh_sig.signal_key if sh_sig else "--"
        mkt_score = sh_sig.score if sh_sig else None

        next_day = dates[di + 1] if di + 1 < len(dates) else None
        day_notes = []
        day_hold_changes = []  # 本日持仓变化（次日生效的记录到下一日？简化：记录交易日期）

        # ---- 卖出检查（当日收盘 → 次日开盘成交）----
        sell_ops = []
        for code in list(positions.keys()):
            p = positions[code]
            sig = day_sigs.get(code)
            px = px_today.get(code)
            if px is None:
                continue
            reason = []
            gain = px / p["cost"] - 1 if p["cost"] else 0
            # 更新峰值
            if px > p.get("peak", p["cost"]):
                p["peak"] = px
            # 保本：浮盈达阈值后止损抬到成本
            stop = p.get("stop")
            if cfg["breakeven_at"] is not None and gain >= cfg["breakeven_at"] and not p.get("be_on"):
                p["be_on"] = True
                p["stop"] = p["cost"] * 1.001  # 成本+0.1%（覆盖费用）
                reason.append("保本触发：+%.0f%%后止损抬至成本" % (gain * 100))
            # 止损判定
            sig_kill = False
            if cfg["stop_mode"] == "atr":
                atr_s = (sig.atr_stop if sig and sig.atr_stop else None) or p.get("atr_stop")
                if atr_s and px <= atr_s:
                    reason.append("破ATR止损 %.2f" % atr_s)
                    sig_kill = True
            elif cfg["stop_mode"] == "atr_ma":
                atr_s = (sig.atr_stop if sig and sig.atr_stop else None) or p.get("atr_stop")
                ma20_s = (sig.stop_loss if sig else None) or p.get("stop_ma")
                chg = sig.change_pct if sig else 0
                if atr_s and px <= atr_s:
                    reason.append("破ATR止损 %.2f" % atr_s)
                    sig_kill = True
                elif ma20_s and px <= ma20_s and chg < -3.0:
                    reason.append("破MA20(%s)且当日%+.1f%%" % ("%.2f" % ma20_s, chg))
                    sig_kill = True
            elif cfg["stop_mode"] == "ma10":
                ma10 = (sig.ma10 if sig and sig.ma10 else None) or p.get("stop_ma")
                atr_s = (sig.atr_stop if sig and sig.atr_stop else None) or p.get("atr_stop")
                be = p.get("be_on")
                hold_d = di - (dates.index(p["buy_date"]) if p["buy_date"] in dates else di)
                # 买入初期(≤2天)用 ATR 宽止损防洗；之后才转 MA10 纪律止损
                if hold_d <= 2:
                    if atr_s and px <= atr_s:
                        reason.append("破ATR止损 %.2f" % atr_s)
                        sig_kill = True
                    elif be and px <= p["cost"]:
                        reason.append("保本离场（不亏本金）")
                        sig_kill = True
                else:
                    if be:
                        if px <= p["cost"]:
                            reason.append("保本离场（不亏本金）")
                            sig_kill = True
                        elif ma10 and px < ma10:
                            reason.append("破MA10(%.2f)纪律离场" % ma10)
                            sig_kill = True
                    else:
                        if ma10 and px < ma10:
                            reason.append("破MA10(%.2f)纪律离场" % ma10)
                            sig_kill = True
            # 信号转弱（sell/reduce）
            if sig and sig.signal_key in ("sell", "reduce"):
                reason.append("信号转%s(%s分)" % (sig.signal, sig.score))
                sig_kill = True
            # 固定止盈
            tp = cfg["tp_pct"]
            if tp and gain >= tp and not sig_kill and not (reason and "保本触发" in reason[-1]):
                reason = ["止盈 +%.0f%%" % (gain * 100)]
                sig_kill = True
            # 移动止盈（激进）：峰值须先涨≥8%才启用，防小波动误杀
            trail = cfg["trail_pct"]
            if trail and p.get("peak") and p["peak"] >= p["cost"] * 1.08:
                from_peak = px / p["peak"] - 1
                if from_peak <= -trail:
                    reason = ["移动止盈：峰值%+.1f%%回落%.0f%%" % ((p["peak"] / p["cost"] - 1) * 100, from_peak * 100)]
                    sig_kill = True
            if sig_kill and reason and next_day:
                npos = date_pos[code].get(next_day)
                price = (klines[code]["opens"][npos] if npos is not None else None) or px
                proceeds = p["shares"] * price
                fee = proceeds * (fee_rate + stamp_rate)
                cash += proceeds - fee
                pnl = proceeds - p["shares"] * p["cost"]
                sell_ops.append((code, next_day))
                last_sell[code] = di + 1  # next_day 的索引
                sig_close = day_sigs[code].close if code in day_sigs else None
                vs_sig = (price / sig_close - 1) * 100 if sig_close else None
                trades.append({
                    "action": "sell", "date": next_day, "code": code, "name": pool_names[code],
                    "time": "09:30", "price": round(price, 3), "shares": p["shares"],
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl / (p["shares"] * p["cost"]) * 100, 2) if p["cost"] else 0,
                    "vs_sig_pct": round(vs_sig, 2) if vs_sig is not None else None,
                    "reason": "；".join(reason), "hold_days": 0, "signal_date": day,
                })
                day_notes.append("卖出 %s：%s" % (pool_names[code], "；".join(reason)))
                day_hold_changes.append({"date": next_day, "code": code, "name": pool_names[code],
                                         "action": "sell", "shares": p["shares"], "price": round(price, 3)})
        # 持仓天数（按交易日计数，卖出时用 buy_day_index）
        buy_day_idx = {c: dates.index(p["buy_date"]) for c, p in positions.items()
                       if p["buy_date"] in dates}
        for t in trades:
            if t["action"] == "sell" and not t["hold_days"]:
                bidx = buy_day_idx.get(t["code"])
                sidx = dates.index(t["date"]) if t["date"] in dates else None
                if bidx is not None and sidx is not None:
                    t["hold_days"] = sidx - bidx
        for code, _ in sell_ops:
            del positions[code]

        # ---- 买入候选（当日收盘 → 次日开盘）----
        if next_day:
            mkt_ok = cfg["market_allow"](mkt_sig, mkt_score, breadth, sh_sig.trend_status if sh_sig else "--")
            over_block = cfg["overheat_block"] and overheat
            if mkt_ok and not over_block:
                cands = []
                for code, sig in day_sigs.items():
                    if code in positions:
                        continue
                    cd = cfg.get("cooldown_days") or 0
                    if cd and di - last_sell.get(code, -999) < cd:
                        continue
                    if sig.signal_key not in ("strong_buy", "buy"):
                        continue
                    if sig.score < cfg["min_score"]:
                        continue
                    if sig.trend_status not in cfg["allow_trends"]:
                        continue
                    if sig.bias_ma5 is not None and abs(sig.bias_ma5) > cfg["max_bias5"]:
                        continue
                    cands.append((sig.score, code, sig))
                cands.sort(key=lambda x: -x[0])
                for sc, code, sig in cands:
                    if len(positions) >= cfg["max_pos"]:
                        break
                    eq_now = cash + pos_value(px_today)
                    budget = eq_now * cfg["pos_frac"]
                    if cash < budget * 0.6:
                        break
                    npos = date_pos[code].get(next_day)
                    if npos is None:
                        continue
                    price = klines[code]["opens"][npos]
                    if not price or price <= 0:
                        continue
                    # 竞价跳空规则：高开过大不追（等回踩），低开过大说明有变故不接刀
                    gap = (price / sig.close - 1) * 100 if sig.close else 0.0
                    g_max = cfg.get("gap_max_buy", 2.0)
                    g_min = cfg.get("gap_min_buy", -3.0)
                    if gap > g_max:
                        continue
                    if gap < g_min:
                        continue
                    shares = int(budget / price / 100) * 100
                    if shares <= 0:
                        continue
                    cost_amt = shares * price
                    fee = cost_amt * fee_rate
                    if cash < cost_amt + fee:
                        shares = int((cash * 0.98) / price / 100) * 100
                        cost_amt = shares * price
                        fee = cost_amt * fee_rate
                        if shares <= 0:
                            continue
                    cash -= cost_amt + fee
                    # 初始止损
                    if cfg["stop_mode"] == "atr":
                        init_stop = (sig.atr_stop if sig.atr_stop else None) or (sig.stop_loss or 0)
                    elif cfg["stop_mode"] == "atr_ma":
                        init_stop = sig.atr_stop or sig.stop_loss
                    else:  # ma10
                        init_stop = sig.ma10 or sig.stop_loss
                    positions[code] = {
                        "shares": shares, "cost": price, "buy_date": next_day,
                        "stop": init_stop, "stop_ma": sig.stop_loss or sig.ma20,
                        "atr_stop": sig.atr_stop, "peak": price, "be_on": False,
                    }
                    trades.append({
                        "action": "buy", "date": next_day, "code": code, "name": pool_names[code],
                        "time": "09:30", "price": round(price, 3), "shares": shares,
                        "vs_sig_pct": round((price / sig.close - 1) * 100, 2) if sig.close else None,
                        "reason": "%s(%s分)·%s 乖离%s 理想买点%s%s" % (
                            sig.signal, sig.score, sig.trend_status,
                            ("%+.1f%%" % sig.bias_ma5) if sig.bias_ma5 is not None else "--",
                            ("%.2f" % sig.ideal_buy) if sig.ideal_buy else "--",
                            ("；开盘跳空%+.1f%%" % gap) if abs(gap) >= 1 else ""),
                        "signal_date": day,
                    })
                    day_notes.append("买入 %s：%s %s分 · %s" % (pool_names[code], sig.signal, sig.score, sig.trend_status))
                    day_hold_changes.append({"date": next_day, "code": code, "name": pool_names[code],
                                             "action": "buy", "shares": shares, "price": round(price, 3)})
        # 当日净值 & 每日收益
        px_map = px_today
        eq = cash + sum(p["shares"] * px_map.get(c, p["cost"]) for c, p in positions.items())
        prev_eq = equity_curve[-1]["equity"] if equity_curve else CASH_START
        prev_bench = equity_curve[-1]["bench"] if equity_curve and equity_curve[-1].get("bench") else None
        equity_curve.append({
            "date": day, "equity": round(eq, 2), "cash": round(cash, 2),
            "pos_count": len(positions),
            "daily_return": round((eq / prev_eq - 1) * 100, 3) if prev_eq else 0,
            "bench": sh_close,
            "bench_chg": round((sh_close / prev_bench - 1) * 100, 2) if sh_close and prev_bench else None,
            "holds": [{"code": c, "name": pool_names[c], "shares": p["shares"],
                        "cost": p["cost"], "px": px_map.get(c, p["cost"])}
                       for c, p in positions.items()],
            "day_trades": [t for t in trades if t["date"] == day],
        })
        cash_curve.append(round(cash, 2))
        if day_notes or day_hold_changes:
            daily_notes.append({
                "date": day, "notes": day_notes, "breadth": round(breadth * 100, 1),
                "market": mkt_sig, "market_score": mkt_score,
                "holds": [{"code": c, "name": pool_names[c], "shares": p["shares"],
                           "cost": p["cost"], "px": px_map.get(c, p["cost"])}
                          for c, p in positions.items()],
            })
        if day_hold_changes:
            hold_log.extend(day_hold_changes)

    # 期末持仓
    final_pos = []
    last = px_today if 'px_today' in dir() else {}
    for code, p in positions.items():
        px = px_map.get(code, p["cost"])
        cost_v = p["shares"] * p["cost"]
        cur_v = p["shares"] * px
        final_pos.append({"code": code, "name": pool_names[code], "shares": p["shares"],
                          "cost": round(p["cost"], 3), "price": round(px, 3),
                          "value": round(cur_v, 2), "pnl": round(cur_v - cost_v, 2),
                          "pnl_pct": round((cur_v / cost_v - 1) * 100, 2) if cost_v else 0,
                          "buy_date": p["buy_date"]})
    return build_result(cfg_key, cfg, equity_curve, trades, daily_notes, final_pos, hold_log)


def build_result(key, cfg, equity_curve, trades, daily_notes, final_pos, hold_log):
    start = CASH_START
    final = equity_curve[-1]["equity"] if equity_curve else start
    ret = (final / start - 1) * 100
    valid_bench = [p["bench"] for p in equity_curve if p.get("bench")]
    bench_ret = (valid_bench[-1] / valid_bench[0] - 1) * 100 if len(valid_bench) >= 2 else None
    peak = -1e18
    mdd = 0.0
    for p in equity_curve:
        peak = max(peak, p["equity"])
        mdd = min(mdd, p["equity"] / peak - 1)
    buys = [t for t in trades if t["action"] == "buy"]
    sells = [t for t in trades if t["action"] == "sell"]
    wins = [t for t in sells if t["pnl"] > 0]
    losses = [t for t in sells if t["pnl"] <= 0]
    avg_hold = (sum(t["hold_days"] for t in sells) / len(sells)) if sells else 0
    unreal = sum(p["pnl"] for p in final_pos)
    realized = sum(t["pnl"] for t in sells)
    open_win = sum(1 for p in final_pos if p["pnl"] > 0)
    total_closed = len(sells) + len(final_pos)
    # 单笔平均盈亏
    avg_win = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0
    avg_loss = (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0
    return {
        "key": key, "label": cfg["label"], "tagline": cfg["tagline"], "logic": cfg["logic"],
        "start_date": equity_curve[0]["date"] if equity_curve else "",
        "end_date": equity_curve[-1]["date"] if equity_curve else "",
        "final_equity": round(final, 2),
        "return_pct": round(ret, 2),
        "max_drawdown": round(mdd * 100, 2),
        "bench_return_pct": round(bench_ret, 2) if bench_ret is not None else None,
        "alpha_pct": round(ret - (bench_ret or 0), 2),
        "stats": {
            "buy_times": len(buys), "sell_times": len(sells),
            "win_rate": round(len(wins) / len(sells) * 100, 1) if sells else 0,
            "avg_hold_days": round(avg_hold, 1),
            "realized_pnl": round(realized, 2), "unrealized_pnl": round(unreal, 2),
            "total_pnl": round(realized + unreal, 2),
            "overall_win_rate": round((len(wins) + open_win) / total_closed * 100, 1) if total_closed else 0,
            "open_positions": len(final_pos),
            "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
            "profit_factor": round(abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)), 2)
            if losses and sum(t["pnl"] for t in losses) != 0 else None,
        },
        "equity_curve": equity_curve,
        "yearly": _yearly_ret(equity_curve),
        "trades": trades,
        "positions": final_pos or [],
        "daily_notes": daily_notes,
        "hold_log": hold_log,
    }


# 预设观察窗口：全程 / 政策牛起点 / 2025 以来 / 2026 以来
WINDOWS = [
    ("m2", "2026-07-01", "2026-09-04"),
    ("h1", "2026-03-02", "2026-09-04"),
    ("all", "2024-01-02", "2026-09-04"),
    ("bull", "2024-09-24", "2026-09-04"),
    ("y2025", "2025-01-02", "2026-09-04"),
    ("y2026", "2026-01-05", "2026-09-04"),
]
WINDOW_LABELS = {
    "m2": "近2月", "h1": "半年", "all": "全程 2024→今",
    "bull": "政策牛 2024.9.24→今", "y2025": "2025 以来", "y2026": "2026 以来",
}


def run_all(start_date="2026-07-01", end_date="2026-09-04"):
    klines, pool_names = {}, {}
    for code, name in POOL.items():
        k = load_kline(code, count=900)
        if k and len(k.get("dates") or []) >= 90:
            klines[code] = k
            pool_names[code] = name
    sh = load_index_kline("sh000001")
    if not sh or (sh["dates"] and sh["dates"][0] > start_date):
        sh = dp.fetch_index_kline("sh000001", 900)
    all_dates = set()
    for k in klines.values():
        all_dates.update(k["dates"])
    if sh:
        all_dates.update(sh["dates"])
    dates = sorted(d for d in all_dates if start_date <= d <= end_date)
    date_pos = {}
    for code, k in klines.items():
        date_pos[code] = {d: i for i, d in enumerate(k["dates"])}
    sh_pos = {d: i for i, d in enumerate(sh["dates"])} if sh else {}

    results = {}
    for key in STRATEGIES:
        print("[sim] 跑 %s 策略…" % STRATEGIES[key]["label"])
        results[key] = _run_one(key, klines, dates, date_pos, sh, sh_pos,
                                pool_names, start_date, end_date)
    return {
        "title": "6股池(工富/长电/紫金/万华/雅戈尔/中百) · 三策略",
        "start_date": dates[0], "end_date": dates[-1],
        "start_cash": CASH_START,
        "strategies": results,
        "generatedAt": time_str(),
    }


def _build_mixed(strategies):
    """混合v2 只守不攻+宽阈值（wide_guard）：按上证 20 日斜率。

    v2 规则（2026-09 对比实测最优：全程反超等权且回撤最小）：
      斜率 < -5%（上证20日跌超5%）→ 守(10/20/70) 降激进避险
      其余（含一切上行/震荡）→ 恒衡(25/50/25)，永不踏空
    弃用旧版"攻"档：追涨切换在震荡市反复打脸、长期跑输等权。
    无前视：斜率滞后 1 日（i-1 vs i-21）。
    对照：静态等权 1/3。
    """
    agg = strategies["aggressive"]["equity_curve"]
    bal = strategies["balanced"]["equity_curve"]
    dis = strategies["disciplined"]["equity_curve"]
    n = len(bal)
    dates = [p["date"] for p in bal]
    r = {"agg": [p["daily_return"] for p in agg],
         "bal": [p["daily_return"] for p in bal],
         "dis": [p["daily_return"] for p in dis]}
    bench = [p["bench"] for p in bal]
    eq_dyn, eq_eq = 50000.0, 50000.0
    dyn_curve, eq_curve = [], []
    regime = []
    for i in range(n):
        if i >= 21 and bench[i - 21]:
            slope = (bench[i - 1] / bench[i - 21] - 1) * 100  # 滞后一日
            if slope < -5.0:
                wa, wb, wd, lab = 0.1, 0.2, 0.7, "守"
            else:
                wa, wb, wd, lab = 0.25, 0.5, 0.25, "衡"
        else:
            wa, wb, wd, lab = 0.25, 0.5, 0.25, "衡"
        rd = (wa * r["agg"][i] + wb * r["bal"][i] + wd * r["dis"][i])
        re_ = (r["agg"][i] + r["bal"][i] + r["dis"][i]) / 3
        eq_dyn *= 1 + rd / 100
        eq_eq *= 1 + re_ / 100
        dyn_curve.append(round(eq_dyn, 2))
        eq_curve.append(round(eq_eq, 2))
        regime.append(lab)

    def summ(curve):
        peak, mdd = -1e18, 0.0
        for v in curve:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        return round((curve[-1] / 50000 - 1) * 100, 2), round(mdd * 100, 2)

    ret_d, mdd_d = summ(dyn_curve)
    ret_e, mdd_e = summ(eq_curve)
    return {
        "label": "混合", "logic": "混合v2(只守不攻)：上证20日斜率<-5%触发守(激进10/稳健20/纪律70)避险；其余恒衡(25/50/25)永不踏空。斜率滞后1日无前视。对照=静态等权1/3。",
        "dates": dates, "dyn": dyn_curve, "equal": eq_curve, "regime": regime,
        "return_pct": ret_d, "equal_return_pct": ret_e,
        "max_drawdown": mdd_d, "equal_max_drawdown": mdd_e,
    }


def _yearly_ret(equity_curve):
    """分自然年收益 + 同段上证基准（用 curve 里每日 bench 收盘推算）。"""
    by = {}
    for p in equity_curve:
        by.setdefault(p["date"][:4], []).append(p)
    out = []
    for y in sorted(by):
        arr = by[y]
        eq0, eq1 = arr[0]["equity"], arr[-1]["equity"]
        out.append({"year": y, "ret": round((eq1 / eq0 - 1) * 100, 2),
                    "bench": round((arr[-1]["bench"] / arr[0]["bench"] - 1) * 100, 2)
                    if arr[0].get("bench") and arr[-1].get("bench") else None})
    if equity_curve:
        out.append({"year": "全部",
                    "ret": round((equity_curve[-1]["equity"] / CASH_START - 1) * 100, 2),
                    "bench": round((equity_curve[-1]["bench"] / equity_curve[0]["bench"] - 1) * 100, 2)
                    if equity_curve[0].get("bench") and equity_curve[-1].get("bench") else None})
    return out


def time_str():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_windows():
    """跑多窗口，输出 data/sim_trade.json（全部） + data/sim_trade_all.json（多窗口）。"""
    klines, pool_names = {}, {}
    for code, name in POOL.items():
        k = load_kline(code, count=900)
        if k and len(k.get("dates") or []) >= 90:
            klines[code] = k
            pool_names[code] = name
    sh = load_index_kline("sh000001")
    if not sh or (sh["dates"] and sh["dates"][0] > "2024-01-02"):
        sh = dp.fetch_index_kline("sh000001", 900)
    all_dates = set()
    for k in klines.values():
        all_dates.update(k["dates"])
    if sh:
        all_dates.update(sh["dates"])
    dates = sorted(all_dates)
    date_pos = {}
    for code, k in klines.items():
        date_pos[code] = {d: i for i, d in enumerate(k["dates"])}
    sh_pos = {d: i for i, d in enumerate(sh["dates"])} if sh else {}
    windows = {}
    for wk, ws, we in WINDOWS:
        wdates = [d for d in dates if ws <= d <= we]
        res = {}
        for key in STRATEGIES:
            res[key] = _run_one(key, klines, wdates, date_pos, sh, sh_pos,
                                pool_names, ws, we)
        windows[wk] = {
            "start_date": ws, "end_date": we, "strategies": res,
            "mixed": _build_mixed(res),
        }
        r0 = res["balanced"]
        print("窗口 %s: %s→%s 稳健%+.2f%% 纪律%+.2f%% 激进%+.2f%%" % (
            WINDOW_LABELS[wk], ws, we,
            res["balanced"]["return_pct"], res["disciplined"]["return_pct"],
            res["aggressive"]["return_pct"]))
    full = windows["all"]
    full["title"] = "5万模拟炒股 · 三策略对比"
    full["start_cash"] = CASH_START
    full["windows"] = {wk: WINDOW_LABELS[wk] for wk, _, _ in WINDOWS}
    full["generatedAt"] = time_str()
    with open(os.path.join(DATA_DIR, "sim_pool6_trade.json"), "w", encoding="utf-8") as f:
        json.dump(full, f, ensure_ascii=False)
    with open(os.path.join(DATA_DIR, "sim_pool6_all.json"), "w", encoding="utf-8") as f:
        json.dump({"start_cash": CASH_START, "windows": {
            k: {"label": WINDOW_LABELS[k], "strategies": v["strategies"], "mixed": v.get("mixed")}
            for k, v in windows.items()}}, f, ensure_ascii=False)
    print("完成:", os.path.join(DATA_DIR, "sim_pool6_trade.json"),
          os.path.join(DATA_DIR, "sim_pool6_all.json"))


if __name__ == "__main__":
    run_windows()
