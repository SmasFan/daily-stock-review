#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实时模拟炒股 · 每日持续账本 + 自学习复盘。

每天收盘后运行（cron 15:40 周一至五）：
  1. 读当日复盘数据（review_data.json 信号/点位/温度/广度 + recommend 推荐 + 资金流）
  2. 决策：按当前策略版本产生「买入/卖出/持有」意向 → 记录 signal_date
  3. 结算：前一日意向在今日开盘价成交（用今日日K开盘 vs 昨日收盘确认 gap）
  4. 记账：现金/持仓/已实现盈亏/净值曲线/流水（时间点/原因/跳空/策略版本）
  5. 复盘学习：每周与每满一定交易笔数，输出「自我复盘」（亏损共性、规则建议）
  6. 持久化 data/sim_live.json + 策略变更日志（改动规则时手动 push 版本）

用法： python3 sim_live.py            # 正常日更
      python3 sim_live.py --init      # 初始化账本（以最近交易日建首仓意向）
      python3 sim_live.py --review    # 只跑复盘学习，不交易
      python3 sim_live.py --strategy-log "理由"  # 记录策略版本变更
"""
import argparse
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
DATA_DIR = os.path.join(BASE_DIR, "data")

STATE_FILE = os.path.join(DATA_DIR, "sim_live.json")
CASH_START = 50000.0

# 当前策略版本（改动规则 → 用 --strategy-log 记录并升版本）
STRATEGY = {
    "version": "v1.0",
    "name": "稳健精选",
    "created": "2026-09-04",
    "rules": [
        "池：自选池 256 只 + 每日推荐，剔除 ST/新股",
        "买：signal∈(强烈买入,买入) 且 score≥68 且 多头/强势多头",
        "买：bias_ma5<5（不追高），现价 ≤ 理想买点*1.02",
        "竞价：次日开盘相对信号日跳空>2% 放弃买入（不追高开）",
        "竞价：跳空低开>3% 放弃（可能有变故）",
        "大盘：上证 score<45 且卖出/减仓 → 不开新仓（防守）",
        "普涨过热（全市场上涨占比≥65%）→ 当日不追新买入",
        "单票 ≤ 总资 20%，最多持 5 只，现金 ≥15%",
        "止损：跌破 ATR 止损 或 跌破 MA20 且当日跌>3% → 次日卖",
        "信号转卖出/减仓 → 次日卖",
        "止盈：浮盈 ≥15% → 次日卖（落袋）；若已+8%后从峰值回落8%也走",
        "同票卖出后 5 日内不重买（防反复割）",
    ],
    "log": [],
}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def init_state(asof_date, day_sigs, market):
    """建首仓意向：把当日信号算出的候选写进 pending_buys（T+1 开盘成交）。
    遵守 v1.0 大盘防守规则：上证卖出/减仓且<45分 或普涨过热 → 不建仓等机会。"""
    # 大盘防守
    if market.get("sh_signal") in ("卖出", "减仓") and (market.get("sh_score") or 0) < 45:
        pass  # 允许记录但不买入；下方候选仍生成但标记 blocked
    cands = rank_candidates(day_sigs, market)
    pending = []
    n_budget = CASH_START * 0.18
    blocked = False
    if market.get("sh_signal") in ("卖出", "减仓") and (market.get("sh_score") or 0) < 45:
        blocked = True
    for c in cands[:5]:
        # 可读买入理由（数据给不出 review reasons 时自生成）
        rsn = c.get("reasons") or "%s(%s分)·%s" % (
            c.get("signal", "--"), c.get("score", "--"), c.get("trend_status", ""))
        if c.get("bias_ma5") is not None:
            rsn += " 乖离%+.1f%%" % c["bias_ma5"]
        if c.get("ideal_buy"):
            rsn += " 理想买点%.2f" % c["ideal_buy"]
        pending.append({
            "code": c["code"], "name": c["name"],
            "signal_date": asof_date, "signal": c["signal"],
            "score": c["score"], "ideal_buy": c.get("ideal_buy"),
            "bias_ma5": c.get("bias_ma5"), "reason": rsn,
            "atr_stop": c.get("atr_stop"), "stop_loss": c.get("stop_loss"),
            "sig_close": c.get("close"),
            "intended_amount": round(n_budget, 2), "status": "blocked" if blocked else "pending",
        })
    return {
        "meta": {
            "created": time_str(), "start_date": asof_date,
            "start_cash": CASH_START, "cash": CASH_START,
            "strategy": STRATEGY,
        },
        "positions": [],          # 持仓
        "pending_buys": pending,  # T+1 开盘待买
        "pending_sells": [],      # T+1 开盘待卖
        "trades": [],             # 已成交
        "equity_curve": [],
        "daily_log": [],          # 每日决策日志（含大盘/温度/广度）
        "review_log": [],         # 周复盘/学习
        "version_history": [],    # 策略变更
    }


def rank_candidates(day_sigs, market):
    """从当日 review 信号池选买入候选（按 score 降序，过滤规则）。排除 ETF/基金（5/1 开头）。"""
    out = []
    for it in day_sigs:
        code = str(it.get("code", ""))
        if code.startswith(("5", "1")):  # ETF/基金/债
            continue
        sig = it.get("signal_key")
        if sig not in ("strong_buy", "buy"):
            continue
        if it.get("score", 0) < 68:
            continue
        if it.get("trend_status") not in ("强势多头", "多头排列"):
            continue
        # 不追高：现价相对 ideal_buy 偏离控制 + bias
        if it.get("bias_ma5") is not None and abs(it["bias_ma5"]) >= 5.0:
            continue
        out.append(it)
    out.sort(key=lambda x: -x.get("score", 0))
    return out


def run_day(state, review, recommend, date):
    """对单个交易日跑一次：结算昨日 pending（用今日开盘价）→ 决策今日新意向。"""
    # 1. 从 K 线缓存拿今日开盘价，结算 pending
    klines_cache = build_kline_cache(date, [b["code"] for b in state.get("pending_buys", [])]
                                     + [b["code"] for b in state.get("pending_sells", [])]
                                     + [p["code"] for p in state.get("positions", [])])
    settle_pending(state, review, klines_cache, date)
    # 2. 大盘/温度上下文
    market = build_market(review)
    overheat = (market.get("breadth") or 0) >= 65
    # 3. 检查持仓（卖出决策基于当日收盘信号）
    decide_sells(state, review, market, overheat)
    # 4. 建新买入意向（若大盘允许）
    decide_buys(state, review, market, overheat, recommend)
    # 5. 记净值（持仓按当日收盘）
    record_equity(state, review, market, date)
    return market


def build_market(review):
    """从当日复盘数据提取大盘/温度上下文。"""
    temp = review.get("temperature", {})
    idx = {x.get("code"): x for x in review.get("indices", [])}
    sh = idx.get("sh000001") or (review.get("indices") or [{}])[0]
    shf = sh.get("factors") or {}
    return {
        "temp_score": temp.get("score"), "label": temp.get("label"),
        "breadth": temp.get("breadth"),
        "market_up": temp.get("market_up"), "market_down": temp.get("market_down"),
        "sh_score": shf.get("score") if shf.get("score") is not None else sh.get("score"),
        "sh_signal": shf.get("signal") if shf.get("signal") else sh.get("signal"),
        "sh_close": sh.get("close"), "sh_chg": sh.get("change_pct"),
    }


def build_kline_cache(date, codes):
    """为待成交代码拉当日K线（读缓存，缺则现拉短K）。返回 {code: kline}。"""
    sys.path.insert(0, BASE_DIR)
    sys.path.insert(0, os.path.join(BASE_DIR, "src"))
    from src import data_provider as dp
    cache = {}
    for code in set(codes or []):
        if not code or not str(code).isdigit():
            continue
        try:
            k = dp.fetch_daily_kline(str(code), count=8, use_cache=True)
        except Exception:
            k = None
        # kline 可能不含最新日期（缓存），保证日期存在；若缺用现拉
        if k and date not in k["dates"]:
            try:
                k = dp.fetch_daily_kline(str(code), count=8, use_cache=False)
            except Exception:
                k = None
        if k:
            cache[str(code)] = k
    return cache


def settle_pending(state, review, klines_cache, date):
    """昨日 pending 买单/卖单用今日开盘成交。"""
    # 先处理卖单（若同一天有买有卖，先卖腾现金）
    sells = state.get("pending_sells", [])
    new_sells = []
    for s in sells:
        fill_sell(state, s, date, klines_cache)
    state["pending_sells"] = []
    buys = state.get("pending_buys", [])
    filled = []
    for b in buys:
        code = b["code"]
        # 用缓存拿今日开盘
        op = kline_open(klines_cache, code, date)
        if op is None:
            continue  # 停牌/无数据 → 留到下一日
        sig_close = b.get("sig_close")
        gap = (op / sig_close - 1) * 100 if sig_close else 0
        # 竞价规则复核
        if gap > 2.0 or gap < -3.0:
            b["status"] = "skipped_gap"
            b["gap"] = round(gap, 2)
            state["daily_log"].append({
                "date": date, "kind": "skip", "code": code, "name": b["name"],
                "note": "竞价跳空%+.2f%% 放弃买入" % gap})
            continue
        # 买入
        amount = min(b["intended_amount"], state["meta"]["cash"])
        price = op
        shares = int(amount / price / 100) * 100
        if shares <= 0:
            b["status"] = "skipped_cash"
            continue
        cost = shares * price
        fee = cost * 0.0003
        state["meta"]["cash"] -= (cost + fee)
        pos = {
            "code": code, "name": b["name"], "shares": shares,
            "cost": round(price, 3), "buy_date": date,
            "signal_date": b["signal_date"], "signal": b["signal"],
            "score": b.get("score"), "atr_stop": b.get("atr_stop"),
            "stop_ma": b.get("stop_loss"), "peak": price,
            "status": "holding",
        }
        state["positions"].append(pos)
        filled.append(b)
        state["trades"].append({
            "action": "buy", "date": date, "time": "09:30(开盘)",
            "code": code, "name": b["name"], "price": round(price, 3),
            "shares": shares, "cost": round(cost + fee, 2),
            "signal_date": b["signal_date"], "signal": b["signal"],
            "score": b.get("score"), "gap": round(gap, 2),
            "reason": b.get("reason", ""), "strategy_ver": state["meta"]["strategy"]["version"],
        })
        state["daily_log"].append({"date": date, "kind": "buy", "code": code,
                                   "name": b["name"], "note": "开盘%s 跳空%+.2f%%" % (price, gap)})
    state["pending_buys"] = [b for b in state["pending_buys"] if b not in filled]


def fill_sell(state, s, date, klines_cache=None):
    code = s["code"]
    pos = next((p for p in state["positions"] if p["code"] == code), None)
    if not pos:
        return
    op = kline_open(klines_cache, code, date) if klines_cache else None
    price = op if op else s.get("price")
    shares = pos["shares"]
    proceeds = shares * price
    fee = proceeds * (0.0003 + 0.0005)
    state["meta"]["cash"] += proceeds - fee
    pnl = proceeds - shares * pos["cost"]
    pnl_pct = (price / pos["cost"] - 1) * 100
    state["trades"].append({
        "action": "sell", "date": date, "time": "09:30(开盘)",
        "code": code, "name": pos["name"], "price": round(price, 3),
        "shares": shares, "proceeds": round(proceeds - fee, 2),
        "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
        "hold_days": hold_days(state, pos["buy_date"], date),
        "signal_date": s.get("signal_date"), "reason": s.get("reason", ""),
        "strategy_ver": state["meta"]["strategy"]["version"],
    })
    state["daily_log"].append({"date": date, "kind": "sell", "code": code,
                               "name": pos["name"], "pnl": round(pnl, 2),
                               "reason": s.get("reason")})
    state["positions"] = [p for p in state["positions"] if p["code"] != code]


def decide_sells(state, review, market, overheat):
    items = {x.get("code"): x for x in review.get("items", [])}
    for pos in state["positions"]:
        it = items.get(pos["code"])
        if not it:
            continue
        px = it.get("close")
        reason = None
        sig = it.get("signal_key")
        # 更新峰值 & 移动止盈
        if px and px > pos.get("peak", 0):
            pos["peak"] = px
        gain = (px / pos["cost"] - 1) * 100 if pos["cost"] else 0
        # 止损
        atr_s = it.get("atr_stop")
        ma20 = it.get("stop_loss") or it.get("ma20")
        if atr_s and px and px <= atr_s:
            reason = "破ATR止损 %.2f（收盘%.2f）" % (atr_s, px)
        elif ma20 and px and px <= ma20 and (it.get("change_pct") or 0) < -3:
            reason = "破MA20(%.2f)且当日跌%.1f%%" % (ma20, it.get("change_pct"))
        elif sig in ("sell", "reduce"):
            reason = "信号转%s(%s分)" % (sig, it.get("score"))
        elif gain >= 15:
            reason = "止盈 +%.1f%%（≥15%%落袋）" % gain
        else:
            # 移动止盈：曾+8%以上，从峰值回落8%
            if pos.get("peak") and pos["peak"] > pos["cost"] * 1.08 and px:
                if px / pos["peak"] - 1 <= -0.08:
                    reason = "峰值+%.1f%%后回落8%%移动止盈" % ((pos["peak"] / pos["cost"] - 1) * 100)
        if reason:
            state["pending_sells"].append({
                "code": pos["code"], "reason": reason, "signal_date": pos["buy_date"],
                "price": px,
            })


def decide_buys(state, review, market, overheat, recommend):
    # 大盘防守
    if market["sh_signal"] in ("卖出", "减仓") and (market["sh_score"] or 0) < 45:
        return
    if overheat:
        state["daily_log"].append({"date": review.get("generatedAt", "")[:10],
                                   "kind": "note", "note": "普涨过热日，不追新买入"})
        return
    if len(state["positions"]) >= 5:
        return
    # 从候选池选：自选已有持仓的排除 + 冷却
    held = {p["code"] for p in state["positions"]}
    cands = rank_candidates(review.get("items", []), market)
    cool = 5
    for it in cands:
        code = it["code"]
        if code in held:
            continue
        if any(t["code"] == code and t["action"] == "sell" and
               hold_days(state, t["date"], review.get("generatedAt", "")[:10]) <= cool
               for t in state["trades"] if t["action"] == "sell"):
            continue
        # 检查现金/预算
        budget = state["meta"]["cash"] * 0.18
        if budget < 5000 or state["meta"]["cash"] < 8000:
            break
        if len(state["pending_buys"]) + len(state["positions"]) >= 5:
            break
        state["pending_buys"].append({
            "code": code, "name": it["name"],
            "signal_date": review.get("generatedAt", "")[:10],
            "signal": it.get("signal"), "score": it.get("score"),
            "ideal_buy": it.get("ideal_buy"), "bias_ma5": it.get("bias_ma5"),
            "sig_close": it.get("close"), "atr_stop": it.get("atr_stop"),
            "stop_loss": it.get("stop_loss"), "reason": it.get("reasons", ""),
            "intended_amount": round(budget, 2), "status": "pending",
        })


def record_equity(state, review, market, date):
    positions_value = 0
    items = {x.get("code"): x for x in review.get("items", [])}
    for pos in state["positions"]:
        it = items.get(pos["code"])
        px = it.get("close") if it else pos["cost"]
        positions_value += pos["shares"] * (px or pos["cost"])
    eq = state["meta"]["cash"] + positions_value
    prev = state["equity_curve"][-1]["equity"] if state["equity_curve"] else CASH_START
    state["equity_curve"].append({
        "date": date, "equity": round(eq, 2), "cash": round(state["meta"]["cash"], 2),
        "daily_return": round((eq / prev - 1) * 100, 3),
        "pos_count": len(state["positions"]),
    })


def kline_open(klines_cache, code, date):
    k = klines_cache.get(code)
    if not k:
        return None
    try:
        i = k["dates"].index(date)
        return k["opens"][i]
    except (ValueError, IndexError):
        return None


def hold_days(state, buy_date, date):
    # 用 equity_curve dates 数交易日
    ds = [p["date"] for p in state["equity_curve"]]
    try:
        return max(0, ds.index(date) - ds.index(buy_date))
    except ValueError:
        return 0


def do_review(state):
    """周期性自我复盘：找出亏单共性、卖飞、规则建议。"""
    trades = state["trades"]
    sells = [t for t in trades if t["action"] == "sell"]
    if not sells:
        return None
    wins = [t for t in sells if t["pnl"] > 0]
    losses = [t for t in sells if t["pnl"] <= 0]
    note = []
    note.append("已平仓 %s 笔：胜 %s / 负 %s，胜率 %.0f%%，总已实现 %+.0f 元" % (
        len(sells), len(wins), len(losses),
        (len(wins) / len(sells) * 100) if sells else 0,
        sum(t["pnl"] for t in sells)))
    if losses:
        avg_loss = sum(t["pnl"] for t in losses) / len(losses)
        loss_reasons = {}
        for t in losses:
            r = t["reason"].split("（")[0][:10]
            loss_reasons[r] = loss_reasons.get(r, 0) + 1
        top = sorted(loss_reasons.items(), key=lambda x: -x[1])[:3]
        note.append("亏损主因：%s" % "、".join("%s×%d" % (k, v) for k, v in top))
        note.append("平均单笔亏损 %.0f 元" % avg_loss)
        # 改进建议
        sugg = []
        if losses and (len(losses) >= 3) and (sum(t["pnl"] for t in losses) / len(losses)) < -300:
            sugg.append("单笔亏损偏大 → 考虑提高买入门槛(score≥72) 或缩小单票仓位")
        stops = sum(1 for t in losses if "止损" in t["reason"] or "破" in t["reason"])
        if losses and stops / len(losses) > 0.6:
            sugg.append("止损单占比高 → 检查是否买在反弹高点，要求回踩 MA5/MA10 附近再买")
        if sugg:
            note.append("改进建议：" + "；".join(sugg))
    state["review_log"].append({
        "date": time_str()[:10], "note": "\n".join(note),
        "trades_since_last": len(sells),
    })
    return note


def time_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--review", action="store_true")
    ap.add_argument("--strategy-log", default=None, metavar="MSG")
    ap.add_argument("--date", default=None)
    args = ap.parse_args()

    # 策略变更日志
    if args.strategy_log:
        st = load_state()
        if not st:
            print("账本未初始化，先 --init")
            return
        v = st["meta"]["strategy"]["version"]
        # bump minor
        m = v.split("."); m[1] = str(int(m[1]) + 1)
        nv = ".".join(m)
        old = dict(st["meta"]["strategy"])
        st["meta"]["strategy"]["version"] = nv
        st["meta"]["strategy"]["log"].append({
            "from": v, "to": nv, "date": time_str()[:10], "reason": args.strategy_log})
        st["version_history"].append({
            "date": time_str()[:10], "from": v, "to": nv, "change": args.strategy_log,
            "rules": st["meta"]["strategy"]["rules"]})
        save(st)
        print("策略 %s → %s：%s" % (v, nv, args.strategy_log))
        return

    # 加载当日复盘
    review = load_json("review_data.json")
    if not review:
        print("无 review_data.json")
        return
    date = args.date or (review.get("generatedAt") or "")[:10]
    recommend = load_json("recommend_data.json")

    state = load_state()
    if args.init or not state:
        print("初始化账本（%s）" % date)
        temp = review.get("temperature", {})
        ix0 = (review.get("indices") or [{}])[0]
        ix0f = ix0.get("factors") or {}
        market = {"temp_score": temp.get("score"), "breadth": temp.get("breadth"),
                  "sh_signal": ix0f.get("signal") if ix0f.get("signal") else ix0.get("signal"),
                  "sh_score": ix0f.get("score") if ix0f.get("score") is not None else ix0.get("score")}
        state = init_state(date, review.get("items", []), market)
        # 首日不结算，只记录大盘上下文 + 净值基线
        state["equity_curve"] = [{"date": date, "equity": CASH_START,
                                  "cash": CASH_START, "daily_return": 0.0, "pos_count": 0}]
        state["daily_log"].append({
            "date": date, "kind": "note",
            "note": "初始化：生成 %d 个买入意向（待下个交易日开盘成交）；策略 %s" % (
                len(state["pending_buys"]), STRATEGY["version"])})
        # 每笔意向入日志（含买入理由），供页面周五可见
        for b in state["pending_buys"]:
            st = "大盘防守暂缓" if b["status"] == "blocked" else "挂单待买"
            state["daily_log"].append({
                "date": date, "kind": "intent", "code": b["code"], "name": b["name"],
                "note": "[%s] 意向买入 ~¥%.0f：%s" % (st, b["intended_amount"], b["reason"])})
        save(state)
        print("初始化完成：%d 意向待成交" % len(state["pending_buys"]))
        for b in state["pending_buys"]:
            print("  - %s(%s) %s %s分 意向预算%.0f" % (b["name"], b["code"], b["signal"], b["score"], b["intended_amount"]))
        return

    # --review：只复盘总结，不改交易意向（供手动/周复盘）
    if args.review:
        market = build_market(review)
        note = daily_summary(state, review, market, date)
        do_review(state)  # 强制完整复盘
        save(state)
        print("复盘完成 %s：%s" % (date, note))
        return

    # 正常日更：结算昨日 → 今日决策 → 每日收盘复盘总结
    market = run_day(state, review, recommend, date)
    today_review = daily_summary(state, review, market, date)
    do_review_auto(state, date)
    save(state)
    print("日更完成 %s：净值 %.0f（%+.2f%%） 持仓%d 现金%.0f" % (
        date, state["equity_curve"][-1]["equity"] if state["equity_curve"] else 0,
        state["equity_curve"][-1].get("daily_return", 0) if state["equity_curve"] else 0,
        len(state["positions"]), state["meta"]["cash"]))


def daily_summary(state, review, market, date):
    """每日收盘总结：当日净值/操作/大盘环境，附简短自我点评，写入 daily_log。"""
    ec = state["equity_curve"]
    last = ec[-1] if ec else {}
    day_trades = [t for t in state["trades"] if t["date"] == date]
    sells_today = [t for t in day_trades if t["action"] == "sell"]
    buys_today = [t for t in day_trades if t["action"] == "buy"]
    parts = []
    if buys_today:
        parts.append("买入 %d 只" % len(buys_today))
    if sells_today:
        pnl = sum(t["pnl"] for t in sells_today)
        parts.append("卖出 %d 只，实现 %+.0f" % (len(sells_today), pnl))
    if not parts:
        parts.append("无成交")
    # 持仓盈亏
    items = {x.get("code"): x for x in review.get("items", [])}
    hold_note = []
    for p in state["positions"]:
        it = items.get(p["code"])
        px = it.get("close") if it else p["cost"]
        g = (px / p["cost"] - 1) * 100 if p["cost"] else 0
        hold_note.append("%s%+.1f%%" % (p["name"], g))
    note = "收盘总结：%s · 净值¥%.0f · 持仓 %s%s · 大盘温度%s(%s)" % (
        "、".join(parts), last.get("equity", 0),
        len(state["positions"]), ("[" + " ".join(hold_note) + "]") if hold_note else "",
        market.get("temp_score", "--"), market.get("label", ""))
    state["daily_log"].append({"date": date, "kind": "review", "note": note})
    return note


def do_review_auto(state, date):
    """有平仓时跑自我复盘（同日不重复）。"""
    sells = [t for t in state["trades"] if t["action"] == "sell"]
    if not sells:
        return
    if state["review_log"] and state["review_log"][-1]["date"] == date:
        return
    do_review(state)


def load_json(name):
    p = os.path.join(DATA_DIR, name)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
