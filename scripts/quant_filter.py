#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量化交易痕迹识别 —— 从日K特征判断个股是否被量化/做市资金主导，用于选股硬过滤

背景：中国核电（601985）这类标的日K呈现典型量化特征——日内振幅被套利压平、
跳空几乎为零、成交高度稳定、日收益无趋势动量（均值回归）。这类票技术评分虚高
（形态"干净"），但赔率极差（空间 +0.8% / 日波动 1.3% = 0.58:1），且"主力净流入"
多为对冲/期现套利/做市的流量，不代表方向性观点。

识别维度（近 N=60 交易日，越长越稳但需≥40根）：
  1. 日内振幅   mean((h-l)/c)         量化↓（套利压缩波动）
  2. 跳空率     mean(|open/prev-1|)   量化↓（做市连续报价消除缺口）
  3. 量能变异   std(vol)/mean(vol)    量化↓（算法按量下单，不情绪化）
  4. 收益自相关 lag-1                 量化≈0或负（无动量，均值回归）
  5. 振幅变异   std(rng)/mean(rng)    量化↓（波动被程序化稳定住）

综合成 quant_score 0~100（越高越像量化盘）。

用法：
  python3 scripts/quant_filter.py --codes 601985 300498 603083    # 查指定标的
  python3 scripts/quant_filter.py --pool                          # 扫全池并写缓存
  python3 scripts/quant_filter.py --pool --top 30                 # 看最"量化"的30只
"""
import argparse
import json
import os
import statistics
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

from src import data_provider as dp  # noqa: E402

DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE = os.path.join(DATA_DIR, "quant_filter.json")
REVIEW = os.path.join(DATA_DIR, "review_data.json")

WIN = 60          # 特征窗口（交易日）
MIN_BARS = 40     # 最少K线数


def _ac1(xs):
    """一阶自相关。"""
    if len(xs) < 10:
        return 0.0
    m = statistics.mean(xs)
    num = sum((xs[i] - m) * (xs[i - 1] - m) for i in range(1, len(xs)))
    den = sum((x - m) ** 2 for x in xs)
    return num / den if den else 0.0


def features(code):
    """算单只标的的量化特征。返回 dict 或 None（数据不足）。"""
    k = dp.fetch_daily_kline_long(code, count=WIN + 60, min_days=120, use_cache=True)
    if not k:
        k = dp.fetch_daily_kline(code, count=WIN + 60, use_cache=True)
    if not k or len(k.get("dates") or []) < MIN_BARS:
        return None
    n = min(WIN, len(k["dates"]) - 1)
    o = k["opens"][-n:]
    c = k["closes"][-n:]
    h = k["highs"][-n:]
    l = k["lows"][-n:]
    v = k["volumes"][-n:]
    pc = k["closes"][-(n + 1):-1]
    if not c or not pc or len(pc) != n:
        return None
    rng = [(h[i] - l[i]) / c[i] for i in range(n) if c[i]]
    gaps = [abs(o[i] / pc[i] - 1) for i in range(n) if pc[i]]
    rets = [(c[i] / pc[i] - 1) for i in range(n) if pc[i]]
    if len(rng) < MIN_BARS - 5 or not v or not statistics.mean(v):
        return None
    amp = statistics.mean(rng) * 100
    gap = statistics.mean(gaps) * 100
    vcv = statistics.pstdev(v) / statistics.mean(v)
    rcv = statistics.pstdev(rng) / statistics.mean(rng) if statistics.mean(rng) else 1
    ac = _ac1(rets)
    return {"code": code, "bars": n, "amp": round(amp, 2), "gap": round(gap, 2),
            "vcv": round(vcv, 3), "rcv": round(rcv, 3), "ac1": round(ac, 3)}


def score_of(f, ref):
    """用全池分位数把特征映射成 0~100 的量化度（越高越量化）。

    ref = {'amp': [所有值...], 'gap': [...], ...}
    """
    def pct(key, val, reverse):
        arr = ref.get(key) or []
        if len(arr) < 20:
            return 50.0
        s = sorted(arr)
        i = sum(1 for x in s if x < val)
        p = i / len(s) * 100
        return (100 - p) if reverse else p
    s = (pct("amp", f["amp"], True) * 0.35
         + pct("gap", f["gap"], True) * 0.25
         + pct("vcv", f["vcv"], True) * 0.2
         + pct("rcv", f["rcv"], True) * 0.1
         + pct("ac1_nomom", max(0.0, -f["ac1"]) + (0.0 if f["ac1"] > 0.15 else 0.1), False) * 0.1)
    return round(s, 1)


def load_review_codes():
    with open(REVIEW, encoding="utf-8") as f:
        rev = json.load(f)
    out = []
    for x in (rev.get("items") or []):
        c = str(x.get("code") or "")
        if not c or c.startswith(("5", "1")):      # 排除 ETF/LOF
            continue
        out.append((c, x.get("name") or c, x.get("sector") or ""))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", nargs="*")
    ap.add_argument("--pool", action="store_true", help="扫全池并写缓存")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--threshold", type=float, default=75,
                    help="量化度超过此值视为量化盘（默认75）")
    args = ap.parse_args()

    if args.codes:
        tgt = [(c, c, "") for c in args.codes]
    else:
        tgt = load_review_codes()
        if not args.pool and not args.codes:
            print("用 --codes 查指定标的，或 --pool 扫全池")

    t0 = time.time()
    feats, fails = [], 0
    for code, name, sec in tgt:
        f = features(code)
        if f:
            f["name"] = name
            f["sector"] = sec
            feats.append(f)
        else:
            fails += 1
    print("特征计算: %d 只成功 / %d 失败  (%.0fs)" % (len(feats), fails, time.time() - t0))
    if not feats:
        return 1

    ref = {k: [f[k] for f in feats] for k in ("amp", "gap", "vcv", "rcv", "ac1")}
    for f in feats:
        f["quant"] = score_of(f, ref)

    feats.sort(key=lambda x: -x["quant"])
    print("\n=== 最像量化盘的 %d 只（quant 越高越量化）===" % args.top)
    print("  %-8s%-10s%6s%8s%7s%6s%7s%6s  %s" % (
        "代码", "名称", "量化度", "日内振幅", "跳空", "量CV", "振幅CV", "自相关", "板块"))
    for f in feats[:args.top]:
        mark = "  ← 量化嫌疑" if f["quant"] >= args.threshold else ""
        print("  %-8s%-10s%6.1f%8.2f%%%6.2f%%%6.2f%7.2f%+7.3f  %s%s" % (
            f["code"], f["name"][:9], f["quant"], f["amp"], f["gap"],
            f["vcv"], f["rcv"], f["ac1"], f["sector"][:10], mark))

    print("\n=== 最不像量化的 %d 只（真实资金博弈）===" % min(args.top, len(feats)))
    print("  %-8s%-10s%6s%8s%7s%6s%7s%6s  %s" % (
        "代码", "名称", "量化度", "日内振幅", "跳空", "量CV", "振幅CV", "自相关", "板块"))
    for f in feats[-args.top:]:
        print("  %-8s%-10s%6.1f%8.2f%%%6.2f%%%6.2f%7.2f%+7.3f  %s" % (
            f["code"], f["name"][:9], f["quant"], f["amp"], f["gap"],
            f["vcv"], f["rcv"], f["ac1"], f["sector"][:10]))

    out = {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "window": WIN, "threshold": args.threshold,
        "note": "quant=量化度(0-100，越高越像量化/做市主导)；≥threshold 建议从选股中剔除",
        "items": {f["code"]: {"name": f["name"], "sector": f["sector"],
                              "quant": f["quant"], "amp": f["amp"], "gap": f["gap"],
                              "vcv": f["vcv"], "rcv": f["rcv"], "ac1": f["ac1"]}
                  for f in feats},
        "stats": {"n": len(feats), "quant_ge_th": sum(1 for f in feats if f["quant"] >= args.threshold)},
    }
    if args.pool or args.codes:
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print("\n→ %s（%d 只，其中量化度≥%.0f 的 %d 只）" % (
            CACHE, len(feats), args.threshold, out["stats"]["quant_ge_th"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
