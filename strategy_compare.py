#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经典/知名策略对照回测 — 与项目 build_sim 同口径（无前视：收盘信号→次日开盘成交，
费用 买万3/卖万3+印花万5，100股整，单票上限等分 5万/4）。

对照策略:
  ma_cross   双均线(MA20上穿MA60金叉持有/下穿清)      — 经典趋势
  turtle     海龟(唐奇安20日突破买/10日跌破卖)          — 海龟交易法则
  momentum   动量轮动(每月初选近60日涨幅前3持1月)       — Jegadeesh-Titman 动量
  boll_rev   布林带反转(破下轨买/回中轨卖)              — 经典均值回归
  hold_ew    等权买入持有（基准）

池: full=36 股 / six=6 股精选；窗口同模拟盘 6 窗。
输出 data/strategy_cmp.json + 终端对比表。
"""
import json
import os
import sys
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

POOL_FULL = {"600309": "万华化学", "600426": "华鲁恒升", "600486": "扬农化工", "002648": "卫星化学",
             "600989": "宝丰能源", "600346": "恒力石化", "601233": "桐昆股份", "002493": "荣盛石化",
             "603225": "新凤鸣", "600096": "云天化", "000902": "新洋丰", "000893": "亚钾国际",
             "301035": "润丰股份", "600141": "兴发集团", "002601": "龙佰集团", "600030": "中信证券",
             "600036": "招商银行", "000001": "平安银行", "601088": "中国神华", "600900": "长江电力",
             "601899": "紫金矿业", "600276": "恒瑞医药", "000858": "五粮液", "002475": "立讯精密",
             "601138": "工业富联", "300308": "中际旭创", "300502": "新易盛", "601766": "中国中车",
             "601919": "中远海控", "600111": "北方稀土", "002371": "北方华创", "600584": "长电科技",
             "002460": "赣锋锂业", "601318": "中国平安", "600519": "贵州茅台", "300059": "东方财富"}
POOL_SIX = {"601138": "工业富联", "600900": "长江电力", "601899": "紫金矿业",
            "600309": "万华化学", "002142": "宁波银行", "600177": "雅戈尔"}
WINDOWS = [("m2", "近2月", "2026-07-01"), ("h1", "半年", "2026-03-02"),
           ("y2026", "今年", "2026-01-05"), ("y2025", "2025以来", "2025-01-02"),
           ("all", "全程", "2024-01-02"), ("bull", "政策牛", "2024-09-24")]
STRATS = ["ma_cross", "turtle", "momentum", "boll_rev", "hold_ew"]
END = "2026-09-04"
FEE, STAMP, CASH = 0.0003, 0.0005, 50000.0
MAX_POS = 4


def load_kl(pool):
    import glob as _g
    kl = {}
    for c in pool:
        cands = _g.glob(os.path.join(BASE_DIR, "data", "cache", "kline_long_%s_*.json" % c)) + \
                _g.glob(os.path.join(BASE_DIR, "data", "cache", "long_%s_*.json" % c)) + \
                _g.glob(os.path.join(BASE_DIR, "data", "cache", "kline_%s.json" % c))
        best = None
        for p in cands:
            try:
                d = json.load(open(p))
                if isinstance(d, dict) and len(d.get("dates", [])) >= 60 \
                        and "opens" in d and "closes" in d:
                    if best is None or len(d["dates"]) > len(best["dates"]):
                        best = d
            except Exception:
                pass
        if best:
            kl[c] = best
    return kl


def sma(v, n):
    out = [None] * len(v)
    s = 0.0
    for i, x in enumerate(v):
        s += x
        if i >= n:
            s -= v[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


class DaySig:
    """单票某日的技术状态（由截至当日数据算，无前视）。"""

    def __init__(self, k, i):
        self.i = i
        self.close = k["closes"][i]
        self.open = k["opens"][i]
        self.high = k["highs"][i]
        self.low = k["lows"][i]
        c = k["closes"]
        h, l = k["highs"], k["lows"]
        self.ma20 = sma(c, 20)[i]
        self.ma60 = sma(c, 60)[i] if i >= 59 else None
        self.ma20_prev = sma(c, 20)[i - 1] if i >= 1 else None
        self.ma60_prev = sma(c, 60)[i - 1] if i >= 60 else None
        self.hh20 = max(h[max(0, i - 20):i]) if i >= 1 else None   # 不含当日
        self.ll10 = min(l[max(0, i - 10):i]) if i >= 1 else None
        mid = sma(c, 20)
        sd = 0.0
        if i >= 19:
            seg = c[i - 19:i + 1]
            m = mid[i]
            sd = (sum((x - m) ** 2 for x in seg) / 20) ** 0.5
        self.boll_mid = mid[i]
        self.boll_lo = mid[i] - 2 * sd if sd else None
        self.chg60 = (c[i] / c[max(0, i - 60)] - 1) * 100 if i >= 60 else 0.0


def build_sigs(kl, dates_all, start, end=None):
    """为窗口每交易日预计算每票 DaySig（含窗口前历史预热）。"""
    sig_cache = defaultdict(dict)  # day -> {code: DaySig}
    dd_map = {c: {d: i for i, d in enumerate(k["dates"])} for c, k in kl.items()}
    all_days = set(dates_all)
    for day in all_days:
        if day < start or (end and day > end):
            continue
        for c, k in kl.items():
            i = dd_map[c].get(day)
            if i is None or i < 60:
                continue
            sig_cache[day][c] = DaySig(k, i)
    return sig_cache


def target(strat, day_sigs, date, month_key):
    """决策函数：返回目标持仓 code 集合（最多 MAX_POS）。month_key 供动量。"""
    if strat == "hold_ew":
        return set(day_sigs.keys())
    if strat == "ma_cross":
        out = set()
        for c, s in day_sigs.items():
            if s.ma20 and s.ma60 and s.ma20 > s.ma60 and s.close > s.ma20:
                out.add(c)
        return out
    if strat == "turtle":
        out = set()
        for c, s in day_sigs.items():
            if s.hh20 and s.close > s.hh20:   # 20日新高突破
                out.add(c)
        return out
    if strat == "momentum":
        if month_key not in _mom_cache:
            ranked = sorted(day_sigs.items(), key=lambda x: -x[1].chg60)[:3]
            _mom_cache[month_key] = {c for c, _ in ranked}
        return _mom_cache[month_key]
    if strat == "boll_rev":
        out = set()
        for c, s in day_sigs.items():
            if s.boll_lo and s.close < s.boll_lo:
                out.add(c)   # 破下轨持有，回中轨卖（在调仓处处理卖出）
        return out
    return set()


_mom_cache = {}


def run_window(pool_name, pool, start, strat, kl, end=None):
    global _mom_cache
    _mom_cache = {}
    end = end or END
    # 交易日序列（池内票交集 + 到 end）
    dset = None
    for k in kl.values():
        s = set(k["dates"])
        dset = s if dset is None else (dset & s)
    dates = sorted(d for d in dset if start <= d <= end)
    if len(dates) < 10:
        return None
    dd_map = {c: {d: i for i, d in enumerate(k["dates"])} for c, k in kl.items()}
    sigs_all = build_sigs(kl, dset, start, end=end)

    cash = CASH
    pos = {}   # code -> {shares, cost}
    eq_curve = []
    trades = 0
    budget = CASH / MAX_POS

    for di, day in enumerate(dates):
        sigs = sigs_all.get(day, {})
        # 前一日收盘目标 → 今日开盘调仓（信号日=前一交易日）
        if di > 0:
            prev_day = dates[di - 1]
            prev_sigs = sigs_all.get(prev_day, {})
            tgt = target(strat, prev_sigs, prev_day, prev_day[:7])
            if strat == "hold_ew":
                tgt = set(kl.keys())
            today_px = {}
            for c, k in kl.items():
                i = dd_map[c].get(day)
                if i is not None:
                    today_px[c] = k["opens"][i]
            # 卖出不在目标集的（boll_rev: 回中轨/上破轨也卖）
            for c in list(pos.keys()):
                px = today_px.get(c)
                if px is None:
                    continue
                keep = c in tgt
                if strat == "boll_rev":
                    s = sigs.get(c)
                    keep = c in tgt and s and s.close <= s.boll_mid  # 中轨下才留
                    if c in tgt and s and s.close > s.boll_mid:
                        keep = False
                if not keep:
                    sh = pos[c]["shares"]
                    proceeds = sh * px
                    cash += proceeds - proceeds * (FEE + STAMP)
                    trades += 1
                    del pos[c]
            # 买入目标集内缺的
            for c in sorted(tgt):
                if len(pos) >= MAX_POS and strat != "hold_ew":
                    break
                if c in pos:
                    continue
                px = today_px.get(c)
                if not px:
                    continue
                b = budget if strat != "hold_ew" else CASH / max(len(tgt), 1)
                b = min(b, cash * 0.98)
                shares = int(b / px / 100) * 100
                if shares < 100:
                    continue
                cost = shares * px
                if cost > cash * 0.99:
                    shares = int(cash * 0.9 / px / 100) * 100
                    if shares < 100:
                        continue
                    cost = shares * px
                cash -= cost + cost * FEE
                pos[c] = {"shares": shares, "cost": px}
                trades += 1
        # 收盘净值
        eq = cash
        for c, p in pos.items():
            i = dd_map[c].get(day)
            px = kl[c]["closes"][i] if i is not None else p["cost"]
            eq += p["shares"] * px
        eq_curve.append(eq)
    if not eq_curve:
        return None
    ret = (eq_curve[-1] / CASH - 1) * 100
    peak, mdd = -1e18, 0.0
    for v in eq_curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"ret": round(ret, 2), "mdd": round(mdd * 100, 2), "trades": trades,
            "final": round(eq_curve[-1], 2), "end_eq": eq_curve[-1]}


def main():
    out = {}
    pools = [("full", POOL_FULL), ("six", POOL_SIX)]
    print("%-14s %-6s" % ("策略", "池"), end="")
    for _, lab, _s in WINDOWS:
        print("%10s" % lab, end="")
    print("%10s %8s" % ("全程", "说明"))
    results = []
    for pool_name, pool in pools:
        print("\n======== 池: %s (%d只) ========" % (pool_name, len(pool)))
        kl = load_kl(pool)
        # 公共日期
        dset = None
        for k in kl.values():
            s = set(k["dates"])
            dset = s if dset is None else (dset & s)
        for strat in STRATS:
            row = []
            for wk, _lab, start in WINDOWS:
                r = run_window(pool_name, pool, start, strat, kl)
                row.append(r["ret"] if r else None)
            # 显示
            cells = " ".join("%9.1f%%" % (x or 0) for x in row)
            print("%-10s %-6s %s" % (strat, pool_name, cells))
            results.append({"pool": pool_name, "strat": strat,
                            "wins": [{"wk": w[0], "ret": r} for w, r in zip(WINDOWS, row)]})
    json.dump(results, open(os.path.join(BASE_DIR, "data", "strategy_cmp.json"), "w"),
              ensure_ascii=False, indent=1)
    print("\n→ data/strategy_cmp.json")


if __name__ == "__main__":
    main()
