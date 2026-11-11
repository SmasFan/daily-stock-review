#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""红利低波池 · 独立实时模拟（仅 6 个标的，回踩建仓 + 长持退出）。

标的（用户 2026-11-09 指定，仅买这些）：
  515080 中证红利ETF招商 / 600900 长江电力 / 159525 红利低波ETF富国
  512800 银行ETF华宝     / 600177 雅戈尔   / 000333 美的集团

买法（2026-11-09 用 641 根日K 2024-03-18~2026-11-09 回测选出，训练 70% / 样本外 30%）：
  · 每标独立预算 = 初始资金/6，分 3 档，每档预算 = 预算/3
  · 买：现价 ≤ MA20×(1-4%) 且 > MA60×(1-15%)  → 建 1 档（不接飞刀）
        MA20/MA60 一律用【已收盘】K 线算，盘中当日未完成 K 线不算进来（防未来函数）
        同标的一日最多建 1 档
  · 减档：浮盈 ≥ +15% 且手上 > 1 档 → 卖掉 1 档，其余继续持有
  · 清仓：现价 < MA60×(1-12%) 或 浮亏 ≤ -10%  → 全清该标
  · 费用：单边 0.08%（含佣金/印花，ETF 与股票统一保守口径）

为什么不用「回踩 +6% 就止盈」：低波红利是慢牛，早止盈只拿 +16.4%；
放开长持后 +38.5%，最大回撤 3.9%（买入持有 9.0%），样本外 Sharpe 1.84。
详见 backtest/dividend_buy.md 与本文件 meta.backtest。

命令：
  python3 sim_dividend.py --init --replay     # 用历史 K 线回放建账（生成今日之前的净值/持仓）
  python3 sim_dividend.py --plan             # 重算 MA 与买点（盘前/收盘）
  python3 sim_dividend.py --intraday         # 盘中巡检（cron 每 2 分钟，自动确保当日 plan）
  python3 sim_dividend.py --status           # 打印账户
  python3 sim_dividend.py --review           # 收盘净值曲线补点
账本: data/sim_dividend.json ｜ 页面: sim_dividend.html
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE = os.path.join(DATA_DIR, "sim_dividend.json")

POOL = [
    ("515080", "中证红利ETF招商"),
    ("600900", "长江电力"),
    ("159525", "红利低波ETF富国"),
    ("512800", "银行ETF华宝"),
    ("600177", "雅戈尔"),
    ("000333", "美的集团"),
]
CODES = [c for c, _ in POOL]
NAMES = dict(POOL)

RULE = {
    "label": "红利回踩·长持",
    "start_cash": 100000.0,
    "fee": 0.0008,
    "dip": 0.04,        # 回踩 MA20 下方 4%
    "floor": 0.15,      # 仍在 MA60 上方 15% 内，否则不接飞刀
    "maxl": 3,          # 每标最多 3 档
    "tp": 0.15,         # +15% 减一档
    "sl": 0.10,         # -10% 全清
    "ex": 0.12,         # 跌破 MA60 下方 12% 全清
    "lot": 100,         # 一手 100 股/份
}

# 回测结论（backtest/dividend_buy.md），写入账本供页面展示
BACKTEST = {
    "window": "2024-03-18 ~ 2026-11-09 (641根日K)",
    "split": "前70%训练 / 后30%样本外",
    "chosen": {"total": 0.385, "mdd": 0.039, "sharpe": 1.57,
               "train": 0.229, "train_sh": 1.42, "oos": 0.122, "oos_sh": 1.84, "trades": 76},
    "buy_hold": {"total": 0.421, "mdd": 0.090, "sharpe": 1.05,
                 "train": 0.236, "train_sh": 0.89, "oos": 0.136, "oos_sh": 1.38},
    "early_tp": {"total": 0.164, "mdd": 0.017, "sharpe": 1.90,
                 "train": 0.115, "train_sh": 1.85, "oos": 0.044, "oos_sh": 2.07, "trades": 183},
    "note": "早止盈把慢牛红利卖飞；长持退出少赚 3.6 点但回撤腰斩、样本外 Sharpe 1.38→1.84",
}


def now_ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def today():
    return time.strftime("%Y-%m-%d")


def load():
    if not os.path.exists(STATE):
        return None
    with open(STATE, "r", encoding="utf-8") as f:
        return json.load(f)


def save(st):
    st["meta"]["updated"] = now_ts()
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def new_state():
    return {
        "meta": {
            "created": now_ts(), "updated": now_ts(), "label": RULE["label"],
            "start_cash": RULE["start_cash"], "rule": RULE,
            "pool": [{"code": c, "name": n} for c, n in POOL],
            "backtest": BACKTEST, "asof": "", "plan_date": "",
            "note": "MA 用已收盘 K 线（排除当日未完成 K 线）；盘中仅用实时快照触发",
        },
        "cash": RULE["start_cash"],
        "positions": [],
        "plan": [],
        "trades": [],
        "equity_curve": [],
        "daily_log": [],
        "log": [],
    }


def log(st, msg):
    st["log"].append({"ts": now_ts(), "msg": msg})
    if len(st["log"]) > 400:
        st["log"] = st["log"][-400:]


def ma(arr, n):
    """最近 n 根已收盘均线的均值；不足返回 None。"""
    if len(arr) < n:
        return None
    return sum(float(x) for x in arr[-n:]) / n


def closed_closes(code, count=1000, use_cache=True):
    """取【已收盘】日线收盘价序列，去掉当日未完成 K 线。返回 (dates, closes)。"""
    from src import data_provider as dp
    k = dp.fetch_daily_kline(code, count=count, use_cache=use_cache) or {}
    dates = list(k.get("dates") or [])
    closes = list(k.get("closes") or [])
    if not dates or not closes:
        return [], []
    # 腾讯会把当日写进最后一根（盘中未完成）。这里【一律】剔除当日：
    # 保证回放(用 closes[:i])与盘中巡检拿到同一套 MA，绝不把当日 K 线算进当日 MA。
    t = today()
    keep = [i for i, d in enumerate(dates) if d < t]
    if len(keep) != len(dates):
        dates = [dates[i] for i in keep]
        closes = [closes[i] for i in keep]
    return dates, closes


def levels(closes):
    """由已收盘序列算 MA20/MA60 与各触发价。"""
    m20, m60 = ma(closes, 20), ma(closes, 60)
    if m20 is None or m60 is None:
        return None
    return {
        "ma20": round(m20, 4), "ma60": round(m60, 4),
        "dip_price": round(m20 * (1 - RULE["dip"]), 4),
        "floor_price": round(m60 * (1 - RULE["floor"]), 4),
        "exit_price": round(m60 * (1 - RULE["ex"]), 4),
    }


def position_of(st, code):
    return next((p for p in st["positions"] if p["code"] == code), None)


def equity(st):
    return st["cash"] + sum(p["shares"] * (p.get("last") or p["avg"]) for p in st["positions"])


def lot_shares(price, budget, cash=None):
    """按一手 100 股取整；预算买不到 1 手时，只要总现金够就买 1 手。

    高价股（如美的 89.6 元，每档预算仅 5555 元）否则永远买不进，
    回测的「连续份额」假设在实盘不成立。
    """
    unit = price * (1 + RULE["fee"])
    n = int(budget / (unit * RULE["lot"])) * RULE["lot"]
    if cash is not None:
        # 预算与实际可用现金取小，否则「预算够、现金不够」会被 buy_lot 拒掉，漏掉本该成交的档
        n = min(n, int(cash / (unit * RULE["lot"])) * RULE["lot"])
        if n <= 0 and cash >= RULE["lot"] * unit:
            n = RULE["lot"]
    return max(0, n)


def buy_lot(st, code, price, date, hms, why=""):
    pos = position_of(st, code)
    lots = pos["lots"] if pos else 0
    if lots >= RULE["maxl"]:
        return 0
    budget = RULE["start_cash"] / len(CODES) / RULE["maxl"]
    shares = lot_shares(price, min(budget, st["cash"]), cash=st["cash"])
    if shares <= 0:
        return 0
    cost = shares * price * (1 + RULE["fee"])
    if cost > st["cash"] + 1e-6:
        return 0
    st["cash"] -= cost
    if pos is None:
        pos = {"code": code, "name": NAMES.get(code, code), "shares": 0,
               "avg": 0.0, "lots": 0, "last": price, "last_chg": None,
               "buy_date": date, "ts": now_ts(), "levels": {}}
        st["positions"].append(pos)
    pos["avg"] = (pos["avg"] * pos["shares"] + price * shares) / (pos["shares"] + shares)
    pos["shares"] += shares
    pos["lots"] += 1
    pos["last"] = price
    pos["ts"] = now_ts()
    st["trades"].append({"action": "buy", "ts": "%s %s" % (date, hms), "code": code,
                         "name": pos["name"], "price": round(price, 4), "shares": shares,
                         "lots": pos["lots"], "amount": round(cost, 2), "reason": why})
    log(st, "买入 %s %s %d股 @%.3f（第%d档）%s" % (code, pos["name"], shares, price, pos["lots"], why))
    return shares


def sell_part(st, pos, price, shares, date, hms, why=""):
    shares = min(shares, pos["shares"])
    if shares <= 0:
        return 0
    proceeds = shares * price * (1 - RULE["fee"])
    st["cash"] += proceeds
    # 净盈亏：卖出净额 - 买入成本（含费），与现金变动对得上账
    pnl = proceeds - shares * pos["avg"] * (1 + RULE["fee"])
    pos["shares"] -= shares
    pos["last"] = price
    st["trades"].append({"action": "sell", "ts": "%s %s" % (date, hms), "code": pos["code"],
                         "name": pos["name"], "price": round(price, 4), "shares": shares,
                         "pnl": round(pnl, 2), "reason": why})
    log(st, "%s %s %d股 @%.3f 盈亏%+.2f" % (why, pos["code"], shares, price, pnl))
    if pos["shares"] <= 0:
        st["positions"].remove(pos)
    return shares


def step(st, code, price, lv, date, hms, allow_buy=True):
    """单标的单步决策：先按信号建 1 档，再判减档/清仓。返回动作列表。"""
    acts = []
    pos = position_of(st, code)
    if pos:
        pos["last"] = price
        pos["levels"] = lv or {}
    lots = pos["lots"] if pos else 0
    # 同标的一日最多建 1 档：只看当日已成交的买单，不依赖 pos['buy_date']（它可能仍是首档日期）
    day_bought = any(
        t["code"] == code and t["action"] == "buy" and t["ts"].startswith(date)
        for t in st["trades"])

    # 建仓：回踩 MA20 下方 dip，且未跌破 MA60 地板（不接飞刀）
    if (allow_buy and lv and lots < RULE["maxl"] and not day_bought
            and price <= lv["dip_price"] and price > lv["floor_price"]):
        n = buy_lot(st, code, price, date, hms, why="回踩MA20-4%建仓")
        if n:
            acts.append("buy")

    pos = position_of(st, code)
    if not pos or not lv:
        return acts
    gain = price / pos["avg"] - 1 if pos["avg"] else 0
    # 减一档：+15% 且不止 1 档
    if gain >= RULE["tp"] and pos["lots"] > 1:
        per = int(pos["shares"] / pos["lots"] / RULE["lot"]) * RULE["lot"]
        if per <= 0:                       # 档位份额不足 1 手时至少卖 1 手，绝不回退成清仓
            per = min(RULE["lot"], pos["shares"])
        sell_part(st, pos, price, per, date, hms, why="浮盈+15%减一档")
        pos["lots"] -= 1
        acts.append("trim")
        pos = position_of(st, code)
        if not pos:
            return acts
        gain = price / pos["avg"] - 1 if pos["avg"] else 0
    # 全清：跌破 MA60 下方 12% 或亏 10%
    if price < lv["exit_price"] or gain <= -RULE["sl"]:
        why = "跌破MA60-12%清仓" if price < lv["exit_price"] else "亏10%止损清仓"
        sell_part(st, pos, price, pos["shares"], date, hms, why=why)
        acts.append("clear")
    return acts


def refresh_plan(st, quotes=None, use_cache=True):
    """重算 MA/买点，写 st['plan']。quotes 用于填现价。"""
    plan = []
    for code, name in POOL:
        _d, closes = closed_closes(code, use_cache=use_cache)
        lv = levels(closes) or {}
        q = (quotes or {}).get(code) or {}
        last = q.get("price") or (closes[-1] if closes else None)
        pos = position_of(st, code)
        row = {
            "code": code, "name": q.get("name") or name,
            "last": last, "chg": q.get("change"),
            "ma20": lv.get("ma20"), "ma60": lv.get("ma60"),
            "dip_price": lv.get("dip_price"), "floor_price": lv.get("floor_price"),
            "exit_price": lv.get("exit_price"),
            "lots": pos["lots"] if pos else 0,
            "status": "full" if (pos and pos["lots"] >= RULE["maxl"]) else "wait",
        }
        if last and row["dip_price"]:
            row["dist_pct"] = round((last / row["dip_price"] - 1) * 100, 2)
        else:
            row["dist_pct"] = None
        plan.append(row)
    st["plan"] = plan
    st["meta"]["plan_date"] = today()
    st["meta"]["asof"] = now_ts()
    if quotes:
        for p in st["positions"]:
            q = quotes.get(p["code"])
            if q and q.get("price"):
                p["last"] = q["price"]
                p["last_chg"] = q.get("change")
    return plan


def snapshot_curve(st, date=None):
    """按日期覆盖写净值曲线点（同日重复巡检只更新最后一个点）。"""
    date = date or today()
    eq = equity(st)
    pos_val = sum(p["shares"] * (p.get("last") or p["avg"]) for p in st["positions"])
    pt = {"date": date, "equity": round(eq, 2), "cash": round(st["cash"], 2),
          "pos_value": round(pos_val, 2)}
    curve = st["equity_curve"]
    if curve and curve[-1]["date"] == date:
        curve[-1] = pt
    else:
        curve.append(pt)
    if len(curve) > 900:
        st["equity_curve"] = curve[-900:]
    return pt


def replay(st, verbose=True):
    """用历史日 K 回放建账：第 i 日用 closes[:i]（已收盘）算 MA，closes[i] 触发。"""
    bars, idx, cmap = {}, {}, {}
    for code in CODES:
        d, c = closed_closes(code, count=1000, use_cache=True)
        bars[code] = (d, [float(x) for x in c])
        idx[code] = {dt: i for i, dt in enumerate(d)}     # 日期 → 下标，按日期对齐
        cmap[code] = dict(zip(d, bars[code][1]))
    dates = sorted(set().union(*[set(bars[c][0]) for c in CODES]))
    st["positions"], st["trades"], st["equity_curve"] = [], [], []
    st["cash"] = RULE["start_cash"]
    for date in dates:
        for code in CODES:
            j = idx[code].get(date)
            if j is None:                              # 该标的当日无 K（停牌/未上市）
                continue
            px = bars[code][1][j]
            hist = bars[code][1][:j]                   # 严格排除当日及以后
            lv = levels(hist)
            if lv is None:
                continue
            step(st, code, px, lv, date, "15:00")
        # 用当日各标收盘价刷新持仓市值；当日无价沿用上一个有效价
        for p in st["positions"]:
            px = cmap[p["code"]].get(date)
            if px is not None:
                p["last"] = px
        snapshot_curve(st, date)
    st["meta"]["asof"] = now_ts()
    if verbose:
        eq = equity(st)
        print("回放完成：%d 个交易日，净值 %.2f（%+.2f%%），成交 %d 笔，持仓 %d 只" % (
            len(dates), eq, (eq / RULE["start_cash"] - 1) * 100,
            len(st["trades"]), len(st["positions"])))
    return st


def in_session(now=None):
    """A 股交易时段（含集合竞价与尾盘缓冲）；非交易时段只刷行情、不交易。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.strftime("%H:%M")
    return ("09:15" <= hm <= "11:35") or ("12:55" <= hm <= "15:05")


def _live_quotes():
    """实时快照（带 change 当日涨跌幅）；失败返回空 dict，不阻塞写账本。"""
    try:
        from src import data_provider as dp
        return dp.fetch_quotes(CODES) or {}
    except Exception as e:
        print("快照失败:", e)
        return {}


def intraday(st):
    """盘中巡检：先刷现价/买点；只在交易时段逐标 step，非交易时段仅刷新行情。"""
    date, hms = today(), time.strftime("%H:%M:%S")
    quotes = _live_quotes()
    if quotes:
        refresh_plan(st, quotes, use_cache=True)   # 每次都刷新现价/买点/当日涨幅（K 线走缓存）
    else:
        refresh_plan(st, use_cache=True)           # 无行情时退回 K 线收盘价
    if not in_session():
        snapshot_curve(st, date)
        log(st, "非交易时段：仅刷新行情 %d/%d" % (len(quotes), len(CODES)))
        return 0
    lv_by_code = {}
    for row in st["plan"]:
        if row["ma20"] and row["ma60"]:
            lv_by_code[row["code"]] = {
                "ma20": row["ma20"], "ma60": row["ma60"],
                "dip_price": row["dip_price"], "floor_price": row["floor_price"],
                "exit_price": row["exit_price"],
            }
    n = 0
    for code in CODES:
        q = quotes.get(code)
        if not q or not q.get("price"):
            continue
        acts = step(st, code, float(q["price"]), lv_by_code.get(code), date, hms)
        n += len(acts)
        if acts:
            print("  %s %s %s @%.3f" % (hms, code, "/".join(acts), float(q["price"])))
    snapshot_curve(st, date)
    log(st, "巡检 %s：行情 %d/%d，触发 %d 笔" % (hms, len(quotes), len(CODES), n))
    return n


def status(st):
    eq = equity(st)
    print("=== 红利回踩·长持 %s ===" % st["meta"]["updated"])
    print("净值 %.2f（%+.2f%%） 现金 %.2f 持仓 %d 只 成交 %d 笔" % (
        eq, (eq / RULE["start_cash"] - 1) * 100, st["cash"],
        len(st["positions"]), len(st["trades"])))
    for p in st["positions"]:
        px = p.get("last") or p["avg"]
        print("  %s %-14s %6d股 成本%.3f 现价%.3f 浮盈%+6.2f%% %d档" % (
            p["code"], p["name"], p["shares"], p["avg"], px,
            (px / p["avg"] - 1) * 100, p["lots"]))
    if st["equity_curve"]:
        c = st["equity_curve"]
        print("曲线 %d 点：%s %.2f → %s %.2f" % (
            len(c), c[0]["date"], c[0]["equity"], c[-1]["date"], c[-1]["equity"]))


def main():
    ap = argparse.ArgumentParser(description="红利低波池实时模拟")
    ap.add_argument("--init", action="store_true", help="新建账本")
    ap.add_argument("--replay", action="store_true", help="用历史日K回放建账")
    ap.add_argument("--plan", action="store_true", help="重算 MA 与买点")
    ap.add_argument("--intraday", action="store_true", help="盘中巡检（cron）")
    ap.add_argument("--review", action="store_true", help="收盘补净值点")
    ap.add_argument("--status", action="store_true", help="打印账户")
    args = ap.parse_args()

    st = load()
    if args.init or st is None:
        st = new_state()
        print("新建账本:", STATE)
    if args.replay:
        replay(st)
    if args.plan:
        refresh_plan(st, _live_quotes(), use_cache=True)
        print("买点已更新:", len(st["plan"]), "只")
    if args.intraday:
        intraday(st)
    if args.review:
        refresh_plan(st, _live_quotes(), use_cache=True)
        snapshot_curve(st)
        st["daily_log"].append({"date": today(), "ts": now_ts(),
                                "equity": round(equity(st), 2),
                                "positions": len(st["positions"]),
                                "trades": len(st["trades"])})
    if args.status or not any([args.replay, args.plan, args.intraday, args.review]):
        if not any([args.init, args.replay, args.plan, args.intraday, args.review]):
            print("用法: --init --replay / --plan / --intraday / --review / --status")
    if args.status:
        status(st)
    if any([args.init, args.replay, args.plan, args.intraday, args.review]):
        save(st)
        print("已保存:", STATE)


if __name__ == "__main__":
    main()
