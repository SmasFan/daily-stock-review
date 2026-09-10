#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部因子自动统计（影子效果跟踪）

回答一个问题：**我给的外部分，到底有没有用？**

统计三个层次：
1. 计划层：所有待触发/已成交计划单按 ext 分档 → 实际成交后的表现
2. 持仓层：当前持仓按 ext 分档 → 浮盈（实时）
3. 交易层：已平仓交易按 ext 分档 → 已实现 pnl_pct

数据源：data/sim_live.json（计划项/持仓/成交均已带 ext 字段）

用法：
  python3 scripts/ext_shadow.py            # 打印报告 + 写 data/ext_shadow.json
  python3 scripts/ext_shadow.py --json     # 只输出 JSON
"""
import argparse
import json
import os
import statistics
import sys
import time
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(BASE_DIR, "data", "sim_live.json")
OUT = os.path.join(BASE_DIR, "data", "ext_shadow.json")

BUCKETS = [
    ("强利好(≥2)", lambda v: v >= 2),
    ("利好(0~2)", lambda v: 0 < v < 2),
    ("利空(-2~0)", lambda v: -2 < v < 0),
    ("强利空(≤-2)", lambda v: v <= -2),
]


def load():
    with open(STATE, encoding="utf-8") as f:
        return json.load(f)


def _buckets_of(pairs):
    """pairs: [(ext_score, ret_pct)] → {档位: {n, avg, win}}"""
    out = {}
    for label, fn in BUCKETS:
        sel = [r for s, r in pairs if s is not None and fn(s)]
        if sel:
            out[label] = {"n": len(sel),
                          "avg": round(statistics.mean(sel), 3),
                          "median": round(statistics.median(sel), 3),
                          "win": round(sum(1 for r in sel if r > 0) / len(sel) * 100, 1)}
    return out


def collect(st):
    """从账本抽取 (ext, 收益) 样本。"""
    plan_rows, hold_rows, trade_rows = [], [], []
    for pool, pv in (st.get("pools") or {}).items():
        books = list((pv.get("accounts") or {}).items())
        if pv.get("mix"):
            books.append(("mix", pv["mix"]))
        for key, a in books:
            # 持仓：ext 分 vs 当前浮盈
            for p in (a.get("positions") or []):
                e = (p.get("ext") or {}).get("score")
                px = p.get("last") or p.get("last_close") or p.get("cost")
                if e is None or not p.get("cost"):
                    continue
                ret = (px / p["cost"] - 1) * 100
                hold_rows.append({"pool": pool, "acct": key, "code": p["code"],
                                  "name": p.get("name"), "ext": e, "ret": round(ret, 3),
                                  "since": p.get("buy_date")})
            # 计划：ext 分（未成交，仅看分布）
            for pl in (a.get("plan") or []):
                e = (pl.get("ext") or {}).get("score")
                if e is None:
                    continue
                plan_rows.append({"pool": pool, "acct": key, "code": pl.get("code"),
                                  "name": pl.get("name"), "ext": e,
                                  "status": pl.get("status"), "why": (pl.get("ext") or {}).get("why")})
            # 成交：已平仓的按 ext 统计已实现收益
            for t in (a.get("trades") or []):
                e = (t.get("ext") or {}).get("score")
                if e is None:
                    continue
                trade_rows.append({"pool": pool, "acct": key, "code": t.get("code"),
                                   "name": t.get("name"), "action": t.get("action"),
                                   "ext": e, "date": t.get("date"),
                                   "pnl_pct": t.get("pnl_pct"),
                                   "ret": t.get("chg_at_fill")})
    return plan_rows, hold_rows, trade_rows


def report(st):
    plan_rows, hold_rows, trade_rows = collect(st)

    # 持仓浮盈按 ext 分档
    hold_pairs = [(r["ext"], r["ret"]) for r in hold_rows]
    hold_buckets = _buckets_of(hold_pairs)
    hold_ic = _rank_ic([r["ext"] for r in hold_rows], [r["ret"] for r in hold_rows])

    # 已平仓按 ext 分档
    closed = [r for r in trade_rows if r["action"] == "sell" and r.get("pnl_pct") is not None]
    trade_pairs = [(r["ext"], r["pnl_pct"]) for r in closed]
    trade_buckets = _buckets_of(trade_pairs)
    trade_ic = _rank_ic([r["ext"] for r in closed], [r["pnl_pct"] for r in closed])

    # 买入当日涨跌（ext 对择时的短期效应）
    buys = [r for r in trade_rows if r["action"] == "buy" and r.get("ret") is not None]
    buy_pairs = [(r["ext"], r["ret"]) for r in buys]
    buy_buckets = _buckets_of(buy_pairs)

    dist = defaultdict(int)
    for r in plan_rows:
        dist[round(r["ext"] * 2) / 2] += 1

    return {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "plan": {"n": len(plan_rows), "dist": {str(k): v for k, v in sorted(dist.items())}},
        "hold": {"n": len(hold_rows), "ic": hold_ic, "buckets": hold_buckets,
                 "detail": sorted(hold_rows, key=lambda x: -x["ext"])},
        "closed": {"n": len(closed), "ic": trade_ic, "buckets": trade_buckets,
                   "detail": sorted(closed, key=lambda x: -x["ext"])},
        "buy_day": {"n": len(buys), "buckets": buy_buckets},
        "verdict": _verdict(hold_ic, trade_ic, hold_buckets, trade_buckets),
    }


def _rank_ic(xs, ys):
    n = len(xs)
    if n < 6:
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
    return round(num / (dx * dy), 4) if dx and dy else None


def _verdict(hold_ic, trade_ic, hold_b, trade_b):
    """给一句结论：外部分是否有效。"""
    ics = [x for x in (hold_ic, trade_ic) if x is not None]
    if not ics:
        return {"level": "insufficient", "note": "样本不足（<6），继续积累"}
    avg_ic = sum(ics) / len(ics)
    # 多空差（强利好 - 强利空）
    def spread(b):
        hi = (b.get("强利好(≥2)") or {}).get("avg")
        lo = (b.get("强利空(≤-2)") or {}).get("avg")
        return None if hi is None or lo is None else round(hi - lo, 3)
    sp_h, sp_t = spread(hold_b), spread(trade_b)
    sps = [x for x in (sp_h, sp_t) if x is not None]
    if avg_ic > 0.05:
        lv, note = "effective", "外部因子方向有效（IC %.3f）" % avg_ic
    elif avg_ic > 0.02:
        lv, note = "weak", "外部因子方向弱有效（IC %.3f），继续观察" % avg_ic
    elif avg_ic > -0.02:
        lv, note = "neutral", "外部因子与收益无关（IC %.3f），权重需重估" % avg_ic
    else:
        lv, note = "wrong", "⚠ 外部因子方向可能写反（IC %.3f），建议检查规则" % avg_ic
    if sps:
        note += "；多空差 %+.2f%%" % (sum(sps) / len(sps))
    return {"level": lv, "note": note, "avg_ic": round(avg_ic, 4),
            "spread_hold": sp_h, "spread_closed": sp_t}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()
    st = load()
    rep = report(st)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0

    print("=== 外部因子自动统计（%s）===" % rep["generatedAt"])
    print("\n[计划分布] n=%d" % rep["plan"]["n"])
    for k, v in rep["plan"]["dist"].items():
        print("  ext %s: %d 单" % (k, v))

    h = rep["hold"]
    print("\n[持仓浮盈] n=%d  IC=%s" % (h["n"], h["ic"]))
    for label, b in h["buckets"].items():
        print("  %-14s n=%3d  均值%+7.3f%%  胜率%5.1f%%" % (label, b["n"], b["avg"], b["win"]))

    c = rep["closed"]
    print("\n[已平仓收益] n=%d  IC=%s" % (c["n"], c["ic"]))
    for label, b in c["buckets"].items():
        print("  %-14s n=%3d  均值%+7.3f%%  胜率%5.1f%%" % (label, b["n"], b["avg"], b["win"]))

    bb = rep["buy_day"]
    print("\n[买入当日涨跌] n=%d" % bb["n"])
    for label, b in bb["buckets"].items():
        print("  %-14s n=%3d  均值%+7.3f%%" % (label, b["n"], b["avg"]))

    print("\n[结论] %s" % rep["verdict"]["note"])
    print("\n→ %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
