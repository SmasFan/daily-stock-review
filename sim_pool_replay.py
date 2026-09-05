#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""固定5只池 × 三策略 × 盘中触发 · 多窗口历史演算（独立脚本，不改 sim_live.json）

池：长江电力600900 / 紫金矿业601899 / 宝丰能源600989 / 万华化学600309 / 工业富联601138

规则（与 sim_live.py v3 实时引擎同口径，历史用日K高低价做盘中触发近似）：
  买入：每日收盘 analyzer 出信号(无前视) → 各策略计划回踩买点 buy_below
        后续交易日盘中 low ≤ buy_below 且 未高开>3% → 按 buy_below 成交（限价单）
        计划每收盘重建（未触发自动作废，与 live 一致）
  卖出：持仓每日检查（盘中触发，卖价保守取触发价）：
        - 止盈：当日 high ≥ tp → 按 tp 卖
        - 破ATR/保本止损：当日 low ≤ stop → 按 stop 卖（若开盘跳空低于 stop 按 open 卖）
        - 移动止盈(激进)：峰值>+8% 后回落≥10%（低点破峰值*(1-trail)）
        - 保本(纪律)：浮盈≥+5% 后止损抬成本+0.2%
        - 信号转弱(sell/reduce)：次日低点触发卖（按 min(开盘?, 昨收?) —— 保守按当日 open 卖）
  同日买卖冲突：保守按"先触止损后触买点"处理（不重复乐观）
  大盘闸门：上证 卖出/减仓且分<45 → 稳健/纪律不开新仓（激进不受限）；
            普涨过热(全市场 breadth≥65%) 历史无每日快照 → 不用（近似，见 README）
  费用：买 万3，卖 万3+印花 万5；100 股整
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

CASH_START = 50000.0
FEE = 0.0003
STAMP = 0.0005


def time_now():
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")
POOL = {
    "600900": "长江电力", "601899": "紫金矿业",
    "600989": "宝丰能源", "600309": "万华化学", "601138": "工业富联",
}
WINDOWS = {
    "近2月": "2026-07-01", "半年": "2026-03-01", "今年": "2026-01-01",
    "2025以来": "2025-01-01", "2024以来": "2024-01-01",
}

ACCOUNTS = {
    "aggressive": {
        "label": "激进", "budget_frac": 0.25, "max_pos": 5, "cash_reserve": 0.05,
        "buy_bias": 0.01, "stop_ma": False, "tp_pct": None,
        "trail": 0.15, "be_at": None, "min_score": 66, "trail_on": 0.12,
        "mkt_gate": False,
    },
    "balanced": {
        "label": "稳健", "budget_frac": 0.20, "max_pos": 5, "cash_reserve": 0.10,
        "buy_bias": 0.012, "stop_ma": True, "tp_pct": 0.25,
        "trail": 0.15, "be_at": None, "min_score": 66, "trail_on": 0.12,
        "mkt_gate": True,
    },
    "disciplined": {
        "label": "严守纪律", "budget_frac": 0.16, "max_pos": 4, "cash_reserve": 0.16,
        "buy_bias": 0.02, "stop_ma": True, "tp_pct": 0.18,
        "trail": 0.18, "be_at": 0.08, "min_score": 74, "trail_on": 0.15,
        "mkt_gate": True,
    },
}
ORDER = ["aggressive", "balanced", "disciplined"]


def new_acct(cfg):
    return {"key": cfg["label"], "cash": CASH_START, "positions": {}, "plan": {},
            "trades": [], "equity": [], "notes": []}


def load_kline(code, n=900):
    k = dp.fetch_daily_kline_long(code, count=n, min_days=700, use_cache=True)
    return k


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", default=",".join(WINDOWS.keys()))
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--v4", action="store_true", help="加跑等权满仓·宏观过滤对照")
    args = ap.parse_args()

    wins = [w for w in args.windows.split(",") if w in WINDOWS] or list(WINDOWS.keys())
    print("池: %s" % "、".join(POOL.values()))
    print("窗口: %s\n" % ", ".join(wins))

    # 载入 K 线（5只 + 上证）
    kl = {}
    for c in POOL:
        kl[c] = load_kline(c)
        print("%s %s %d根 %s→%s" % (c, POOL[c], len(kl[c]["dates"]),
                                    kl[c]["dates"][0], kl[c]["dates"][-1]))
    # 上证用指数专用缓存（取最新的 900 根；若末尾早于个股则拉新）
    import glob
    cand = sorted(glob.glob(os.path.join("data", "cache", "kline_idx_sh000001_*.json")),
                  key=lambda f: os.path.getmtime(f))
    sh = None
    for f in reversed(cand):
        d = json.load(open(f))
        if (d.get("dates") or []) and d["dates"][-1] >= "2026-09-03":
            sh = d
            break
    if sh is None:
        try:
            sh = dp.fetch_index_kline("sh000001", count=900, use_cache=False)
        except Exception as e:
            print("! 上证K线拉取失败: %s" % e)
    if sh:
        print("上证 %d根 %s→%s" % (len(sh["dates"]), sh["dates"][0], sh["dates"][-1]))
    else:
        print("! 无上证指数K线")
        return

    for wk in wins:
        if args.v4:
            run_v4(wk, WINDOWS[wk], kl, sh)
        run_window(wk, WINDOWS[wk], kl, sh)
        print()





def run_v4(name, start, kl, sh):
    """v4 宏观过滤等权满仓：大盘非防御期一次性 5只等权买入持有；
    个股破 ATR3 止损或 信号转 sell → 卖出该股等回踩/新信号再入。不砍涨单。"""
    dset = set(kl["600900"]["dates"])
    for c in kl:
        dset &= set(kl[c]["dates"])
    dset &= set(sh["dates"])
    dates = sorted(d for d in dset if d >= start)
    if len(dates) < 30:
        print("%s: 交易日不足" % name); return None
    seq_pos = {c: {d: i for i, d in enumerate(k["dates"])} for c, k in kl.items()}
    seq_pos["sh000001"] = {d: i for i, d in enumerate(sh["dates"])}
    pre = sorted(d for d in dset if d < start)[-90:] if any(d < start for d in dset) else []
    seq = pre + dates
    cash = CASH_START
    pos = {}   # code -> {shares, cost, peak}
    trades = []
    curve = []
    ready = False
    eq_prev = CASH_START
    def pxof(code, day):
        j = seq_pos[code].get(day)
        return kl[code]["closes"][j] if j is not None else None
    for di, day in enumerate(seq):
        in_win = day in dates
        sigs = {}
        for c in POOL:
            i = seq_pos[c].get(day)
            if i is None or i < 30: continue
            k = kl[c]
            r = az.analyze_stock(POOL[c], k["dates"], k["opens"], k["closes"],
                                 k["highs"], k["lows"], k["volumes"], c, idx=i)
            if r: sigs[c] = r
        sh_sig = None
        i = seq_pos["sh000001"].get(day)
        if i is not None and i >= 30:
            r = az.analyze_stock("上证", sh["dates"], sh["opens"], sh["closes"],
                                 sh["highs"], sh["lows"], sh["volumes"], "sh000001", idx=i)
            if r: sh_sig = r
        mkt_bear = bool(sh_sig and sh_sig.signal_key in ("sell", "reduce") and (sh_sig.score or 0) < 45)
        slope = None
        ip = seq_pos["sh000001"].get(day)
        if ip is not None and ip >= 20 and sh["closes"][ip-20]:
            slope = (sh["closes"][ip]/sh["closes"][ip-20]-1)*100
        macro_def = slope is not None and slope < -8.0
        # 卖出：破 ATR 止损(2.5xATR) 或 信号sell/reduce → 次日开盘卖
        for code in list(pos.keys()):
            p = pos[code]
            sig = sigs.get(code)
            k = kl[code]
            j = seq_pos[code].get(day)
            if j is None: continue
            o, hi, lo = k["opens"][j], k["highs"][j], k["lows"][j]
            if hi > p.get("peak", p["cost"]): p["peak"] = hi
            reason = None
            if p.get("stop") and lo <= p["stop"]:
                reason = "破止损%.2f" % p["stop"]
                price = o if o <= p["stop"] else p["stop"]
            if not reason and sig and sig.signal_key in ("sell", "reduce") and in_win:
                nxt = seq[di+1] if di+1 < len(seq) else None
                if nxt:
                    jn = seq_pos[code].get(nxt)
                    if jn is not None:
                        reason = "信号转%s(%d分)" % (sig.signal, sig.score)
                        price = kl[code]["opens"][jn]
            if reason and in_win:
                shares = p["shares"]
                proceeds = shares*price
                fee = proceeds*(FEE+STAMP)
                pnl = proceeds - shares*p["cost"]
                cash += proceeds - fee
                trades.append({"action":"sell","date":day,"code":code,"name":POOL[code],
                               "price":round(price,3),"shares":shares,"pnl":round(pnl,2),
                               "pnl_pct":round((price/p["cost"]-1)*100,2),"reason":reason})
                del pos[code]
        # 买入/建仓: 非防御 & 非大盘空头 → 建/补 5只等权
        if in_win and not macro_def and not mkt_bear:
            for c in POOL:
                if c in pos: continue
                j = seq_pos[c].get(day)
                if j is None: continue
                close = kl[c]["closes"][j]
                sig = sigs.get(c)
                # 允许买: 无信号限制(大盘ok即配), 但强势空头跳过
                if sig and sig.trend_status in ("空头排列","强势空头"): continue
                budget = CASH_START/5.0
                # 首日/空窗补仓价: 用当日收盘价成交近似(日K无法盘中…用收盘,注明近似)
                budget = min(budget, cash*0.98)
                shares = int(budget/close/100)*100
                if shares < 100: shares = int(cash*0.9/close/100)*100
                if shares < 100: continue
                cost = shares*close
                if cost > cash*0.99: continue
                cash -= cost + cost*FEE
                sig2 = sigs.get(c)
                stop = None
                if sig2 and sig2.atr_stop:
                    stop = sig2.atr_stop
                pos[c] = {"shares":shares,"cost":close,"peak":close,"stop":stop,
                          "buy_date":day}
                trades.append({"action":"buy","date":day,"code":c,"name":POOL[c],
                               "price":round(close,3),"shares":shares,
                               "reason":"宏观非防御 等权建仓(收盘近似)"})
        # 净值
        if in_win:
            eq = cash + sum(p["shares"]*(pxof(c, day) or p["cost"]) for c, p in pos.items())
            curve.append({"date":day,"equity":round(eq,2),"cash":round(cash,2),
                          "daily_return":round((eq/eq_prev-1)*100,3),
                          "pos_count":len(pos)})
            eq_prev = eq
    # 汇总
    end = curve[-1]["equity"] if curve else CASH_START
    ret = (end/CASH_START-1)*100
    peak = -1e18; mdd = 0
    for p in curve:
        peak = max(peak, p["equity"]); mdd = min(mdd, p["equity"]/peak-1)
    buys = [t for t in trades if t["action"]=="buy"]; sells=[t for t in trades if t["action"]=="sell"]
    wins=[t for t in sells if t["pnl"]>0]
    unreal = sum(( (pxof(c,dates[-1]) or p["cost"])-p["cost"])*p["shares"] for c,p in pos.items())
    print("\n【等权满仓·宏观过滤】收益 %+7.2f%%   期末 ¥%10.2f   回撤 %6.2f%%   买%d/卖%d   持仓%d只"
          % (ret, end, mdd*100, len(buys), len(sells), len(pos)))
    print("   卖出胜率 %3.0f%% (%d/%d)   已实现 %+9.2f   持仓浮盈 %+9.2f"
          % (len(wins)/len(sells)*100 if sells else 0, len(wins), len(sells),
             sum(t["pnl"] for t in sells), unreal))
    for t in sells[-4:]:
        print("    卖 %s %-6s @%8.2f  %+8.0f  %s" % (t["date"], POOL.get(t["code"],"?"), t["price"], t["pnl"], t["reason"][:36]))
    for c,p in pos.items():
        cur = pxof(c,dates[-1]) or p["cost"]
        print("    持 %-6s %d股 @%.2f 现%.2f (%+.1f%%)" % (POOL[c], p["shares"], p["cost"], cur, (cur/p["cost"]-1)*100))
    out = {"window":name,"mode":"v4-equal-macro","start":start,"end":dates[-1],
           "generated":time_now(),
           "accounts":{"v4":{"label":"等权满仓·宏观过滤","equity_curve":curve,
                             "trades":trades,"positions":[{"code":c,"name":POOL[c],
                             "shares":p["shares"],"cost":p["cost"],"buy_date":p["buy_date"]}
                             for c,p in pos.items()]}}}
    fn = os.path.join(BASE_DIR,"data","sim_pool_%s.json"%name)
    # 与三策略合并存(同文件覆盖会丢) → 分开存 v4
    fn = os.path.join(BASE_DIR,"data","sim_v4_%s.json"%name)
    with open(fn,"w",encoding="utf-8") as f: json.dump(out,f,ensure_ascii=False,indent=1)
    print("    → 已存 %s"%fn)


def run_window(name, start, kl, sh):
    """单窗口：三账户独立 5 万，盘中触发近似回放。"""
    # 公共交易日（5只+上证都有）
    dset = set(kl["600900"]["dates"])
    for c in kl:
        dset &= set(kl[c]["dates"])
    dset &= set(sh["dates"])
    dates = sorted(d for d in dset if d >= start)
    if len(dates) < 30:
        print("%s: 交易日不足" % name)
        return
    seq_pos = {c: {d: i for i, d in enumerate(k["dates"])} for c, k in kl.items()}
    seq_pos["sh000001"] = {d: i for i, d in enumerate(sh["dates"])}
    sh_pos = seq_pos["sh000001"]
    n_stock = len(POOL)

    # 预热期：窗口起点前推 90 交易日（analyze ≥30 + MA60），只走信号不记账
    pre = sorted(d for d in dset if d < start)[-90:] if any(d < start for d in dset) else []
    seq = pre + dates
    warm = set(pre)

    # 账户
    accts = {k: {"label": cfg["label"], "cash": CASH_START, "positions": {}, "plan": {},
                 "trades": [], "equity": [], "notes": []} for k, cfg in ACCOUNTS.items()}
    buy_plan = {}  # code -> plan
    curves = {k: [] for k in ORDER}
    plan_date = None

    for di, day in enumerate(seq):
        in_win = day not in warm
        # 信号
        sigs, px = {}, {}
        for c in POOL:
            i = seq_pos[c].get(day)
            if i is None or i < 30:
                continue
            k = kl[c]
            r = az.analyze_stock(POOL[c], k["dates"], k["opens"], k["closes"],
                                 k["highs"], k["lows"], k["volumes"], c, idx=i)
            if r:
                sigs[c] = r
                px[c] = r.close
        sh_sig = None
        i = sh_pos.get(day)
        if i is not None and i >= 30:
            r = az.analyze_stock("上证", sh["dates"], sh["opens"], sh["closes"],
                                 sh["highs"], sh["lows"], sh["volumes"], "sh000001", idx=i)
            if r:
                sh_sig = r
        mkt_bear = bool(sh_sig and sh_sig.signal_key in ("sell", "reduce")
                        and (sh_sig.score or 0) < 45)
        # 宏观防御闸门（上证20日斜率近似宏观流动性/风险偏好；LLM 实时层见 macro_llm.py）
        slope = None
        if sh_pos.get(day) is not None:
            ip = sh_pos[day]
            if ip >= 20 and sh["closes"][ip - 20]:
                slope = (sh["closes"][ip] / sh["closes"][ip - 20] - 1) * 100
        macro_def = slope is not None and slope < -8.0
        overheat = False  # 无全市场历史 breadth，关闭（近似）

        # ===== 卖出（盘中触发）先于买入（warm 期不交易，仅算信号） =====
        if not in_win:
            continue  # warm：下一日继续推进
        for key in ORDER:
            cfg = ACCOUNTS[key]
            a = accts[key]
            for code in list(a["positions"].keys()):
                p = a["positions"][code]
                k = kl[code]
                j = seq_pos[code].get(day)
                if j is None:
                    continue
                o, hi, lo = k["opens"][j], k["highs"][j], k["lows"][j]
                sig = sigs.get(code)
                if hi > p.get("peak", p["cost"]):
                    p["peak"] = hi
                gain = hi / p["cost"] - 1
                sell = None
                # 止盈：盘中 high ≥ tp → 减半仓（让剩余仓位继续跑）
                if p.get("tp") and hi >= p["tp"]:
                    sell = ("half", p["tp"], "止盈减半 %.2f(+%.0f%%)" % (p["tp"], (p["tp"] / p["cost"] - 1) * 100))
                # 保本抬止损（纪律）
                if not sell and p.get("be_at") and gain >= p["be_at"] * 100 and not p.get("be_on"):
                    p["be_on"] = True
                    p["stop_atr"] = p["cost"] * 1.002
                # 破止损（低点 ≤ stop；开盘跳空低于 stop 按开盘）→ 全清
                if not sell and p.get("stop_atr"):
                    st = p["stop_atr"]
                    if lo <= st:
                        sell = ("full", o if o <= st else st, "破止损 %.2f" % st)
                if not sell and p.get("stop_ma"):
                    st = p["stop_ma"]
                    if lo <= st and sig and sig.change_pct < -3.0:
                        sell = ("full", st, "破MA20 %.2f 当日%+.1f%%" % (st, sig.change_pct))
                # 移动止盈：浮盈≥trail_on 后峰值回落 trail → 减半仓（主升中只减不加抛）
                if not sell and p.get("trail") and p.get("peak", 0) > p["cost"] * (1 + (p.get("trail_on") or 0.12)) \
                        and lo / p["peak"] - 1 <= -p["trail"]:
                    sell = ("half", p["peak"] * (1 - p["trail"]),
                            "移动止盈减半 峰+%.0f%%回落%.0f%%" % ((p["peak"] / p["cost"] - 1) * 100,
                                                              p["trail"] * 100))
                # 信号转弱：次日开盘全清
                if not sell and sig and sig.signal_key in ("sell", "reduce") and in_win:
                    nxt = seq[di + 1] if di + 1 < len(seq) else None
                    if nxt:
                        jn = seq_pos[code].get(nxt)
                        if jn is not None:
                            opx = k["opens"][jn]
                            sell = ("full", opx, "信号转%s(%d分) 次日开%.2f" % (sig.signal, sig.score, opx))
                if sell:
                    kind, price, reason = sell
                    shares = p["shares"]
                    if kind == "half":
                        shares = int(shares / 2 / 100) * 100  # 减半（100股整）
                        if shares < 100:  # 不足100股则全清
                            shares = p["shares"]
                    proceeds = shares * price
                    fee = proceeds * (FEE + STAMP)
                    pnl = proceeds - shares * p["cost"]
                    a["cash"] += proceeds - fee
                    hd = seq_pos[code][day] - seq_pos[code].get(p["buy_date"], seq_pos[code][day])
                    a["trades"].append({"action": "sell", "date": day, "code": code,
                                        "name": POOL[code], "price": round(price, 3),
                                        "shares": shares, "pnl": round(pnl, 2),
                                        "pnl_pct": round((price / p["cost"] - 1) * 100, 2),
                                        "reason": reason, "strategy": key,
                                        "hold_days": hd})
                    if kind == "half" and shares < p["shares"]:
                        p["shares"] -= shares  # 保留剩余仓
                    else:
                        del a["positions"][code]

        # ===== 买入：昨日收盘计划 → 今日 low ≤ buy_below 触发 =====
        for key in ORDER:
            cfg = ACCOUNTS[key]
            a = accts[key]
            if macro_def:
                continue  # 宏观防御期：不建新仓
            if mkt_bear and cfg["mkt_gate"]:
                continue
            for ck, pl in list(buy_plan.items()):
                if ck[1] != key:
                    continue
                code = ck[0]
                if code in a["positions"] or len(a["positions"]) >= cfg["max_pos"]:
                    continue
                k = kl[code]
                j = seq_pos[code].get(day)
                if j is None:
                    continue
                o, lo = k["opens"][j], k["lows"][j]
                if o > pl["sig_close"] * 1.03:
                    continue  # 高开>3% 当日放弃（计划收盘重建）
                if pl.get("mode") == "breakout":
                    # 突破单：次日开盘直接追（突破当日收盘→次日开盘确认）
                    if o < pl["sig_close"] * 0.995:
                        continue  # 低开>0.5% 突破失败放弃
                    price = o
                    trig = True
                else:
                    trig = lo <= pl["buy_below"]
                    price = pl["buy_below"]
                if trig:
                    budget = min(CASH_START * cfg["budget_frac"], a["cash"] * 0.98)
                    shares = int(budget / price / 100) * 100
                    if shares > 0:
                        cost = shares * price
                        a["cash"] -= cost + cost * FEE
                        a["positions"][code] = {
                            "code": code, "name": POOL[code], "shares": shares,
                            "cost": round(price, 3), "buy_date": day,
                            "stop_atr": pl["stop_atr"], "stop_ma": pl["stop_ma"],
                            "tp": pl["tp"], "trail": cfg.get("trail"),
                            "trail_on": cfg.get("trail_on"),
                            "be_at": cfg.get("be_at"), "be_on": False,
                            "peak": price, "score": pl["score"],
                        }
                        a["trades"].append({"action": "buy", "date": day, "code": code,
                                            "name": POOL[code], "price": round(price, 3),
                                            "shares": shares, "reason": pl["reason"],
                                            "strategy": key, "mode": pl.get("mode", "retest")})
                    del buy_plan[ck]

        # ===== 收盘：重建明日计划（当前信号；覆盖未触发旧单） =====
        buy_plan = {}
        for key in ORDER:
            cfg = ACCOUNTS[key]
            if macro_def:
                continue
            if mkt_bear and cfg["mkt_gate"]:
                continue
            cands = []
            brokes = []
            for c, sig in sigs.items():
                if sig.signal_key not in ("strong_buy", "buy"):
                    # 突破形态不受 analyzer watch 压制：score 达标+强多头+创新高即可追
                    j = seq_pos[c].get(day)
                    broke = False
                    if j is not None and j >= 20:
                        prev_high = max(kl[c]["highs"][j - 20:j])
                        if sig.close > prev_high and sig.score >= cfg["min_score"] \
                                and sig.trend_status in ("强势多头", "多头排列"):
                            broke = True
                    if broke:
                        brokes.append((sig.score, c, sig))
                    continue
                if sig.score < cfg["min_score"]:
                    continue
                if sig.trend_status not in ("强势多头", "多头排列"):
                    continue
                cands.append(sig)
            cands.sort(key=lambda x: -x.score)
            for sig in cands[:8]:
                close = sig.close
                # 突破判定：当日 close 创 20 日新高 → 强势单边，次日开盘追
                j = seq_pos[sig.code].get(day)
                k = kl[sig.code]
                broke = False
                if j is not None and j >= 20:
                    prev_high = max(k["highs"][j - 20:j])  # 不含当日
                    if close > prev_high:
                        broke = True
                ideal = sig.ideal_buy or sig.secondary_buy or close
                bb = close * (1 - cfg["buy_bias"])
                if ideal and ideal < bb:
                    bb = ideal
                bb = max(bb, close * 0.96)
                if bb >= close:
                    bb = close * 0.99
                if broke:
                    # 突破单：次日开盘价成交（回踩单并存，价高者得开盘价优先突破）
                    buy_plan[(sig.code, key)] = {
                        "key": key, "score": sig.score, "buy_below": round(bb, 3),
                        "mode": "breakout", "sig_close": close,
                        "stop_atr": sig.atr_stop,
                        "stop_ma": (sig.stop_loss if cfg["stop_ma"] else None),
                        "tp": round(close * (1 + cfg["tp_pct"]), 3) if cfg["tp_pct"] else None,
                        "reason": "突破20日高 %.2f→次日开追(%d分)·%s" % (prev_high, sig.score, sig.trend_status),
                    }
                else:
                    buy_plan[(sig.code, key)] = {
                        "key": key, "score": sig.score, "buy_below": round(bb, 3),
                        "mode": "retest", "sig_close": close,
                        "stop_atr": sig.atr_stop,
                        "stop_ma": (sig.stop_loss if cfg["stop_ma"] else None),
                        "tp": round(close * (1 + cfg["tp_pct"]), 3) if cfg["tp_pct"] else None,
                        "reason": "%s(%d分)·%s 回踩≤%.2f" % (sig.signal, sig.score,
                                                           sig.trend_status, bb),
                    }
            # 突破候选（analyzer 不产生 buy 的新高股）补充进计划
            for sc2, c2, sig2 in sorted(brokes, key=lambda x: -x[0])[:4]:
                if (c2, key) in buy_plan:
                    continue
                j2 = seq_pos[c2].get(day)
                prev_high = max(kl[c2]["highs"][j2 - 20:j2])
                buy_plan[(c2, key)] = {
                    "key": key, "score": sig2.score, "buy_below": round(sig2.close * (1 - cfg["buy_bias"]), 3),
                    "mode": "breakout", "sig_close": sig2.close,
                    "stop_atr": sig2.atr_stop,
                    "stop_ma": (sig2.stop_loss if cfg["stop_ma"] else None),
                    "tp": round(sig2.close * (1 + cfg["tp_pct"]), 3) if cfg["tp_pct"] else None,
                    "reason": "突破20日高%.2f→开盘追(%d分)·%s" % (prev_high, sig2.score, sig2.trend_status),
                }

        # ===== 净值（仅窗口期记账） =====
        if in_win:
            for key in ORDER:
                a = accts[key]
                eq = a["cash"] + sum(p["shares"] * px.get(c, p["cost"])
                                     for c, p in a["positions"].items())
                prev = curves[key][-1]["equity"] if curves[key] else CASH_START
                curves[key].append({"date": day, "equity": round(eq, 2),
                                    "cash": round(a["cash"], 2),
                                    "daily_return": round((eq / prev - 1) * 100, 3),
                                    "pos_count": len(a["positions"])})

    # ===== 输出 =====
    j0 = sh_pos[dates[0]] if dates[0] in sh_pos else None
    j1 = sh_pos[dates[-1]] if dates[-1] in sh_pos else None
    bench = (sh["closes"][j1] / sh["closes"][j0] - 1) * 100 if j0 is not None and j1 is not None else None
    print("=" * 80)
    print("窗口 %s  %s → %s  (%d交易日)   上证基准 %+.2f%%" %
          (name, start, dates[-1], len(dates), bench if bench is not None else 0))
    print("=" * 80)
    for key in ORDER:
        cfg = ACCOUNTS[key]
        a = accts[key]
        c = curves[key]
        end_eq = c[-1]["equity"] if c else CASH_START
        ret = (end_eq / CASH_START - 1) * 100
        peak = -1e18
        mdd = 0.0
        for p in c:
            peak = max(peak, p["equity"])
            mdd = min(mdd, p["equity"] / peak - 1)
        buys = [t for t in a["trades"] if t["action"] == "buy"]
        sells = [t for t in a["trades"] if t["action"] == "sell"]
        wins = [t for t in sells if t["pnl"] > 0]
        realized = sum(t["pnl"] for t in sells)
        unreal = 0
        for code, p in a["positions"].items():
            j = seq_pos[code].get(dates[-1])
            cur = kl[code]["closes"][j] if j is not None else p["cost"]
            unreal += (cur - p["cost"]) * p["shares"]
        print("\n【%s】收益 %+7.2f%%   期末 ¥%10.2f   回撤 %6.2f%%   买%d/卖%d   持仓%d只"
              % (cfg["label"], ret, end_eq, mdd * 100, len(buys), len(sells),
                 len(a["positions"])))
        print("   卖出胜率 %3.0f%% (%d/%d)   已实现 %+9.2f   持仓浮盈 %+9.2f"
              % (len(wins) / len(sells) * 100 if sells else 0, len(wins), len(sells),
                 realized, unreal))
        for t in sells[-5:]:
            print("    卖 %s %-6s @%8.2f  %+8.0f  %s"
                  % (t["date"], POOL.get(t["code"], "?"), t["price"], t["pnl"],
                     t["reason"][:40]))
        for code, p in a["positions"].items():
            j = seq_pos[code].get(dates[-1])
            cur = kl[code]["closes"][j] if j is not None else p["cost"]
            print("    持 %-6s %d股 @%.2f 现%.2f (%+.1f%%) 买%s"
                  % (POOL[code], p["shares"], p["cost"], cur,
                     (cur / p["cost"] - 1) * 100, p["buy_date"]))

    out = {"window": name, "start": start, "end": dates[-1], "bench": bench,
           "generated": time_now(), "accounts": {}}
    for key in ORDER:
        a = accts[key]
        out["accounts"][key] = {
            "label": ACCOUNTS[key]["label"],
            "equity_curve": curves[key], "trades": a["trades"],
            "positions": [{"code": c, "name": POOL[c], "shares": p["shares"],
                           "cost": p["cost"], "buy_date": p["buy_date"]}
                          for c, p in a["positions"].items()],
        }
    fn = os.path.join(BASE_DIR, "data", "sim_pool_%s.json" % name)
    with open(fn, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("    → 已存 %s" % fn)


if __name__ == "__main__":
    main()
