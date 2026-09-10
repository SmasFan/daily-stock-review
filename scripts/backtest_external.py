#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部因子 → A股板块 的权重回测

验证 build_external.py 的板块偏好规则是否真的有效：
  外盘/期货 t-1 日涨跌  →  预测 A股 t 日板块收益

方法（无前视）：
1. 取历史区间内每个交易日 t
2. 用 t-1 日的外盘/期货收盘算各因子涨跌（美股 t-1 夜间收盘 → A股 t 日开盘前已知）
3. 按 FACTOR_RULES 算板块偏好分
4. 目标变量 = 该板块成分股 t 日等权收益（复权收盘价计算）
5. 统计：IC（偏好分 vs 次日收益 的秩相关）、分档收益、逐因子单效应

数据源：
  国内期货日线（新浪 JSONP）→ 原油SC0 / 沪金AU0 / 沪银AG0 / 沪铜CU0（可回溯多年）
  腾讯美股日线 → 纳斯达克 usIXIC / 标普 usINX（约 1.2 年）
  注：美债收益率因子无可用历史源（腾讯 TLT 只有当日），**不参与回测**

用法：
  python3 scripts/backtest_external.py                     # 默认近 300 交易日
  python3 scripts/backtest_external.py --days 500 --min-stocks 3
  python3 scripts/backtest_external.py --codes 601899 600028   # 限定股票
输出：控制台报告 + data/external_backtest.json
"""
import argparse
import json
import os
import statistics
import sys
import time
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

from src import data_provider as dp, futures as fu  # noqa: E402
import build_external as BE                          # noqa: E402

DATA_DIR = os.path.join(BASE_DIR, "data")
OUT_PATH = os.path.join(DATA_DIR, "external_backtest.json")
REVIEW = os.path.join(DATA_DIR, "review_data.json")

# 回测可用的因子（美债收益率无历史源，仅做规则说明不做统计）
FUT_SYMBOLS = {"oil": ("SC0", "原油"), "gold": ("AU0", "沪金"),
               "silver": ("AG0", "沪银"), "copper": ("CU0", "沪铜")}
US_SYMBOLS = {"nasdaq": ("usIXIC", "纳斯达克"), "sp500": ("usINX", "标普500")}


def load_sector_map():
    """股票 → 板块（用当前 review_data 的 sector 字段）。"""
    try:
        with open(REVIEW, encoding="utf-8") as f:
            rev = json.load(f)
    except Exception:
        return {}, {}
    name, sector = {}, {}
    for x in (rev.get("items") or []):
        c = x.get("code")
        if not c:
            continue
        sector[c] = x.get("sector") or "其他"
        name[c] = x.get("name") or c
    return sector, name


def load_futures_hist():
    """各因子 t 日涨跌（%）。返回 {factor: {date: chg}}"""
    out = {}
    for key, (sym, label) in FUT_SYMBOLS.items():
        k = fu.fetch_metal_kline(sym, count=600, use_cache=True)
        if not k or len(k.get("dates") or []) < 30:
            print("  [warn] %s(%s) 历史不足，跳过" % (label, sym))
            continue
        d = {}
        ds, cs = k["dates"], k["closes"]
        for i in range(1, len(ds)):
            if cs[i - 1]:
                d[ds[i]] = (cs[i] / cs[i - 1] - 1) * 100
        out[key] = d
        print("  %-8s %-6s %d 个交易日" % (key, label, len(d)))
    for key, (sym, label) in US_SYMBOLS.items():
        k = dp.fetch_daily_kline_us(sym, count=300, use_cache=True)
        if not k or len(k.get("dates") or []) < 30:
            print("  [warn] %s(%s) 历史不足，跳过" % (label, sym))
            continue
        d = {}
        ds, cs = k["dates"], k["closes"]
        for i in range(1, len(ds)):
            if cs[i - 1]:
                d[ds[i]] = (cs[i] / cs[i - 1] - 1) * 100
        out[key] = d
        print("  %-8s %-6s %d 个交易日" % (key, label, len(d)))
    return out


def load_stock_hist(codes, count=400):
    """股票日收益 {code: {date: ret%}}"""
    out = {}
    for i, c in enumerate(codes):
        k = dp.fetch_daily_kline(c, count=count, use_cache=True)
        if not k or len(k.get("closes") or []) < 40:
            continue
        d = {}
        ds, cs = k["dates"], k["closes"]
        for j in range(1, len(ds)):
            if cs[j - 1]:
                d[ds[j]] = (cs[j] / cs[j - 1] - 1) * 100
        out[c] = d
    return out


def stock_ext_score(code, name, sec, lvls):
    """个股级外部偏好分（与线上 sim_live.ext_bonus 同口径）。

    关键词优先（黄金/石油/矿业股），其次板块。返回原始分（未乘权重）。
    """
    # 与线上 build_external 同口径：金属三因子先合成再计分，避免重复
    METAL_MIX = {"gold": 0.45, "silver": 0.2, "copper": 0.35}
    mks = [(k, lvls[k]) for k in METAL_MIX if lvls.get(k)]
    metals_lvl = 0
    if mks:
        wsum = sum(METAL_MIX[k] for k, _ in mks)
        metals_lvl = int(round(sum(METAL_MIX[k] * l for k, l in mks) / (wsum or 1)))
    kw = [("oil", r"海油|油气|油服|采掘|石油ETF|油气ETF", 1.6),
          ("oilref", r"石化|化纤", 0.9),
          ("metals", r"黄金|金矿|贵金属|白银|银泰|盛达|铜|有色|铝|稀土|矿业", 1.1)]
    for key, pat, w in kw:
        lv = metals_lvl if key == "metals" else lvls.get(key)
        if key == "oilref" and lv is not None:
            lv = -lv      # 炼化反向
        if lv:
            try:
                import re as _re
                if _re.search(pat, name or ""):
                    return w * lv
            except Exception:
                pass
    # 板块
    tot = 0.0
    for key, lv in lvls.items():
        if not lv or key in ("gold", "silver", "copper"):
            continue      # 金属已由 metals 合成计分
        rule = BE.FACTOR_RULES[key]
        if sec in rule["bull"]:
            tot += rule["bull"][sec] * lv
        elif sec in rule["bear"]:
            tot -= rule["bear"][sec] * lv
    return tot


def rank_ic(xs, ys):
    """Spearman 秩相关（无 scipy，手算秩）。"""
    n = len(xs)
    if n < 5:
        return None
    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((rx[i] - mx) ** 2 for i in range(n)) ** 0.5
    dy = sum((ry[i] - my) ** 2 for i in range(n)) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=300, help="回测交易日数")
    ap.add_argument("--min-stocks", type=int, default=3, help="板块最少成分股数")
    ap.add_argument("--codes", nargs="*", help="限定股票代码")
    args = ap.parse_args()

    t0 = time.time()
    print("=== 1. 加载板块映射 ===")
    sector, names = load_sector_map()
    if args.codes:
        sector = {c: sector.get(c, "其他") for c in args.codes}
    if not sector:
        print("无股票池，退出")
        return 1
    print("  股票池 %d 只，板块 %d 个" % (len(sector), len(set(sector.values()))))

    print("\n=== 2. 加载外盘/期货历史 ===")
    fac_hist = load_futures_hist()
    if not fac_hist:
        print("无因子历史，退出")
        return 1

    print("\n=== 3. 加载个股历史（首次较慢）===")
    codes = sorted(sector)
    hist = load_stock_hist(codes, count=args.days + 60)
    print("  成功 %d / %d 只  (%.0fs)" % (len(hist), len(codes), time.time() - t0))

    # 交易日轴：用有数据最多的因子日期，取最近 N 天
    all_dates = sorted(set().union(*[set(d) for d in fac_hist.values()]))
    dates = all_dates[-args.days - 1:]
    print("  回测区间: %s ~ %s (%d 日)" % (dates[0], dates[-1], len(dates) - 1))

    # 板块成分
    by_sector = defaultdict(list)
    for c, s in sector.items():
        if c in hist:
            by_sector[s].append(c)
    by_sector = {s: cs for s, cs in by_sector.items() if len(cs) >= args.min_stocks}
    print("  可用板块 %d 个（≥%d 只成分）" % (len(by_sector), args.min_stocks))

    # ---- 逐日：算偏好分 → 次日板块收益 ----
    # 注意：用 t-1 日因子 → 预测 t 日板块收益（外盘 t-1 夜间收盘，A股 t 日开盘前已知）
    samples = []            # 板块级 [{date, sector, score, ret, by_factor}]
    stock_samples = []      # 个股级（含关键词映射）
    for i in range(1, len(dates)):
        prev, cur = dates[i - 1], dates[i]
        lvls = {}
        for key, rule in BE.FACTOR_RULES.items():
            if key not in fac_hist:
                continue
            chg = fac_hist[key].get(prev)
            if chg is None:
                continue
            lvls[key] = BE._lvl(chg)
        if not lvls:
            continue
        # 板块偏好分
        bias = defaultdict(float)
        for key, lv in lvls.items():
            if lv == 0:
                continue
            rule = BE.FACTOR_RULES[key]
            mag = abs(lv)
            if lv > 0:
                for s, w in rule["bull"].items():
                    bias[s] += w * mag
                for s, w in rule["bear"].items():
                    bias[s] -= w * mag
            else:
                for s, w in rule["bull"].items():
                    bias[s] -= w * mag
                for s, w in rule["bear"].items():
                    bias[s] += w * mag
        for sec, cs in by_sector.items():
            rets = [hist[c][cur] for c in cs if cur in hist[c]]
            if len(rets) < args.min_stocks:
                continue
            samples.append({"date": cur, "sector": sec,
                            "score": round(bias.get(sec, 0.0), 3),
                            "ret": round(statistics.mean(rets), 3),
                            "by_factor": {k: v for k, v in lvls.items() if v != 0}})
        # 个股级（含关键词映射，能测黄金/石油等板块映射不到的股票）
        for c, cs_hist in hist.items():
            if cur not in cs_hist:
                continue
            sc = stock_ext_score(c, names.get(c, ""), sector.get(c, "其他"), lvls)
            if sc == 0:
                continue
            stock_samples.append({"date": cur, "code": c, "name": names.get(c, c),
                                  "sector": sector.get(c, "其他"),
                                  "score": round(sc, 3), "ret": cs_hist[cur]})

    if not samples:
        print("无样本，退出")
        return 1
    print("\n=== 4. 样本 %d 条（板块×交易日）===" % len(samples))

    # ---- 统计 1：全样本 IC ----
    xs = [s["score"] for s in samples]
    ys = [s["ret"] for s in samples]
    ic = rank_ic(xs, ys)
    print("\n--- 全样本 IC（偏好分 vs 次日板块收益）---")
    print("  Spearman IC = %s  (n=%d)" % (
        ("%.4f" % ic) if ic is not None else "N/A", len(samples)))
    if ic is not None:
        print("  解读：|IC|>0.03 有效，>0.05 较强；正=偏好方向正确")

    # ---- 统计 2：分档收益 ----
    print("\n--- 分档平均收益（按偏好分）---")
    buckets = {"强利好(≥2)": lambda v: v >= 2, "温和利好(0~2)": lambda v: 0 < v < 2,
               "中性(0)": lambda v: v == 0, "温和利空(-2~0)": lambda v: -2 < v < 0,
               "强利空(≤-2)": lambda v: v <= -2}
    bucket_ret = {}
    for label, fn in buckets.items():
        sel = [s["ret"] for s in samples if fn(s["score"])]
        if sel:
            bucket_ret[label] = {"n": len(sel), "avg": round(statistics.mean(sel), 3),
                                 "median": round(statistics.median(sel), 3),
                                 "win": round(sum(1 for r in sel if r > 0) / len(sel) * 100, 1)}
            b = bucket_ret[label]
            print("  %-14s n=%5d  均值%+7.3f%%  中位%+7.3f%%  胜率%5.1f%%" % (
                label, b["n"], b["avg"], b["median"], b["win"]))
    # 多空差
    long_r = [s["ret"] for s in samples if s["score"] >= 2]
    short_r = [s["ret"] for s in samples if s["score"] <= -2]
    spread = None
    if long_r and short_r:
        spread = round(statistics.mean(long_r) - statistics.mean(short_r), 3)
        print("  → 多空差（强利好 - 强利空）= %+.3f%%" % spread)

    # ---- 统计 3：逐因子单效应 ----
    print("\n--- 逐因子单效应（因子方向正确性）---")
    per_factor = {}
    for key in fac_hist:
        rule = BE.FACTOR_RULES[key]
        hit, tot, ups, dns = 0, 0, [], []
        for s in samples:
            lv = s["by_factor"].get(key)
            if lv is None:
                continue
            sec = s["sector"]
            if sec in rule["bull"]:
                want_pos = True
            elif sec in rule["bear"]:
                want_pos = False
            else:
                continue
            tot += 1
            good = (s["ret"] > 0) if (want_pos == (lv > 0)) else (s["ret"] < 0)
            if good:
                hit += 1
            (ups if lv > 0 else dns).append(s["ret"])
        if tot:
            per_factor[key] = {
                "label": rule["label"], "n": tot,
                "hit_rate": round(hit / tot * 100, 1),
                "up_avg": round(statistics.mean(ups), 3) if ups else None,
                "down_avg": round(statistics.mean(dns), 3) if dns else None}
            pf = per_factor[key]
            print("  %-9s n=%5d  方向命中%5.1f%%  因子涨时%+7.3f%%  因子跌时%+7.3f%%" % (
                pf["label"], pf["n"], pf["hit_rate"],
                pf["up_avg"] if pf["up_avg"] is not None else 0,
                pf["down_avg"] if pf["down_avg"] is not None else 0))

    # ---- 统计 4：板块级 IC（哪些板块规则更可信）----
    print("\n--- 板块级 IC（各板块自己的偏好分 vs 收益）---")
    per_sector = {}
    for sec in sorted(by_sector):
        sel = [s for s in samples if s["sector"] == sec]
        if len(sel) < 20:
            continue
        c = rank_ic([s["score"] for s in sel], [s["ret"] for s in sel])
        if c is None:
            continue
        per_sector[sec] = {"n": len(sel), "ic": round(c, 4),
                           "avg_score": round(statistics.mean([s["score"] for s in sel]), 2)}
    for sec, v in sorted(per_sector.items(), key=lambda x: -abs(x[1]["ic"]))[:15]:
        print("  %-14s n=%4d  IC=%+.4f  平均偏好%+5.2f" % (sec, v["n"], v["ic"], v["avg_score"]))

    # ---- 统计 5：个股级（含关键词映射）----
    print("\n=== 5. 个股级（含关键词映射，n=%d）===" % len(stock_samples))
    st_ic = None
    st_buckets = {}
    if stock_samples:
        st_ic = rank_ic([s["score"] for s in stock_samples], [s["ret"] for s in stock_samples])
        print("  个股 IC = %s" % (("%.4f" % st_ic) if st_ic is not None else "N/A"))
        for label, fn in (("强利好(≥2)", lambda v: v >= 2), ("利好(0~2)", lambda v: 0 < v < 2),
                          ("利空(-2~0)", lambda v: -2 < v < 0), ("强利空(≤-2)", lambda v: v <= -2)):
            sel = [s["ret"] for s in stock_samples if fn(s["score"])]
            if sel:
                st_buckets[label] = {"n": len(sel), "avg": round(statistics.mean(sel), 3),
                                     "win": round(sum(1 for r in sel if r > 0) / len(sel) * 100, 1)}
                b = st_buckets[label]
                print("  %-14s n=%5d  均值%+7.3f%%  胜率%5.1f%%" % (label, b["n"], b["avg"], b["win"]))
        # 关键词命中组（黄金/石油/矿业这类板块映射不到的）
        print("\n  --- 关键词命中股票（板块规则覆盖不到的那批）---")
        import re as _re
        kw_lists = {"上游油气": r"海油|油气|油服",
                    "炼化化纤": r"石化|化纤",
                    "黄金": r"黄金|金矿", "白银": r"白银|银泰|盛达",
                    "铜|有色|矿业": r"铜|有色|铝|稀土|矿业"}
        kw_stat = {}
        for label, pat in kw_lists.items():
            sel = [s for s in stock_samples if _re.search(pat, s["name"] or "")]
            if len(sel) < 10:
                continue
            ic_k = rank_ic([s["score"] for s in sel], [s["ret"] for s in sel])
            kw_stat[label] = {"n": len(sel), "ic": round(ic_k, 4) if ic_k is not None else None,
                              "avg_ret": round(statistics.mean([s["ret"] for s in sel]), 3)}
            print("  %-12s n=%5d  IC=%s  平均收益%+7.3f%%" % (
                label, len(sel),
                ("%+.4f" % ic_k) if ic_k is not None else "  N/A ",
                kw_stat[label]["avg_ret"]))

    out = {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "range": [dates[0], dates[-1]], "days": len(dates) - 1,
        "samples": len(samples),
        "factors_used": sorted(fac_hist.keys()),
        "factors_missing": ["yield", "dxy", "vix"],
        "note": "美债收益率/美元指数/VIX 无可用历史源，未参与回测",
        "ic": ic, "buckets": bucket_ret, "spread": spread,
        "per_factor": per_factor, "per_sector": per_sector,
        "stock_level": {"n": len(stock_samples), "ic": st_ic, "buckets": st_buckets},
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n→ %s" % OUT_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
