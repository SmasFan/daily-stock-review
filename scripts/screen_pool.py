#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自选池多维筛选（强势 + 未超买 + 资金持续流入 + 外部因子支持）

用法：
  python3 scripts/screen_pool.py                  # 默认四档（推荐/进攻/潜伏/超跌）
  python3 scripts/screen_pool.py --mode strong    # 只出强势突破档
  python3 scripts/screen_pool.py --mode all       # 全部档位 + 更多条数
  python3 scripts/screen_pool.py --top 20
"""
import argparse
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
import sim_live as S  # noqa: E402

REVIEW = os.path.join(BASE_DIR, "data", "review_data.json")


QUANT = os.path.join(BASE_DIR, "data", "quant_filter.json")
QUANT_TH = 75.0      # 量化度 ≥ 此值 → 视为量化/做市主导，硬剔除


def load_quant():
    """载入量化度缓存（scripts/quant_filter.py 产出）。"""
    try:
        with open(QUANT, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("items") or {}, d.get("generatedAt") or ""
    except Exception:
        return {}, ""


def load():
    with open(REVIEW, encoding="utf-8") as f:
        rev = json.load(f)
    ext = S.load_external()
    st = S.load_state()
    gate, gwhy = S.market_gate(rev, state=st, date=(rev.get("generatedAt") or "")[:10])
    return rev, ext, gate, gwhy


def ff(it):
    return it.get("fund_flow") or {}


def n(it, k, d=0.0):
    v = it.get(k)
    return v if isinstance(v, (int, float)) else d


def flow(it, k):
    return n(ff(it), k)


def enrich(it, ext, quant=None):
    """算派生指标（含量化度、确定性/弹性双维度）。"""
    quant = quant or {}
    px = n(it, "close")
    e = {
        "code": it.get("code"), "name": it.get("name"), "sector": it.get("sector"),
        "px": px, "chg": n(it, "change_pct"), "score": n(it, "score"),
        "signal": it.get("signal"), "trend": it.get("trend_status"),
        "ts": n(it, "trend_strength"), "rsi6": n(it, "rsi6"), "rsi12": n(it, "rsi12"),
        "macd": it.get("macd_status"), "vol": it.get("volume_status"),
        "vr": n(it, "volume_ratio"), "bias5": n(it, "bias_ma5"), "bias20": n(it, "bias_ma20"),
        "chg60": n(it, "change_60d"),
        "ma5": n(it, "ma5"), "ma10": n(it, "ma10"), "ma20": n(it, "ma20"), "ma60": n(it, "ma60"),
        "atr": n(it, "atr14"), "atr_stop": n(it, "atr_stop"),
        "m1": flow(it, "main_net"), "m5": flow(it, "main_net_5d"), "m10": flow(it, "main_net_10d"),
        "pct_to_ma5": (px / n(it, "ma5", px) - 1) * 100 if n(it, "ma5") else 0,
    }
    # ---- 弹性 / 赔率（v3.12 新增）----
    # 教训：只按 score 排序会选出「ETF 化」标的（中国核电 ATR 1.32%、到压力位仅 +0.8%，
    # 赔率 0.58:1）—— 波动小所以技术形态"干净"，评分虚高，但根本不可交易。
    e["atr_pct"] = round((e["atr"] / px) * 100, 2) if px else 0
    lo20 = n(it, "low20")
    e["amp20"] = round((n(it, "high20") / lo20 - 1) * 100, 1) if lo20 else 0
    e["space"] = round((n(it, "high20") / px - 1) * 100, 2) if px else 0
    # 赔率 = 到 20 日高空间 / ATR%（承担一天波动能换多少空间）
    e["odds"] = round(e["space"] / e["atr_pct"], 2) if e["atr_pct"] else 0
    # 弹性综合分：日波动 + 近期振幅
    e["elas"] = round(e["atr_pct"] * 10 + e["amp20"] * 0.4, 1)
    # ---- 量化痕迹（quant_filter.py）----
    q = quant.get(e["code"]) or {}
    e["quant"] = q.get("quant")
    e["q_amp"] = q.get("amp")
    e["q_gap"] = q.get("gap")
    # 量化盘硬剔除：日内振幅被套利压平 + 跳空≈0 + 无趋势动量
    e["is_quant"] = (e["quant"] is not None and e["quant"] >= QUANT_TH)
    # 可交易性：赔率 ≥1.5 且 ATR ≥1.8% 且 非量化盘
    e["tradable"] = (e["odds"] >= 1.5 and e["atr_pct"] >= 1.8 and not e["is_quant"])
    # ---- 确定性 / 弹性 双维度 ----
    # 确定性 = 趋势强度 + 评分 + 资金持续（不看波动，独立评估"方向靠谱程度"）
    e["cert"] = round(e["ts"] * 0.4 + e["score"] * 0.4
                      + (8 if e["m5"] > 0 and e["m10"] > 0 else (3 if e["m5"] > 0 else -8)), 1)
    # 弹性 = 日波动 + 振幅 + 赔率（能不能给出可观的波动空间）
    e["flex"] = round(e["atr_pct"] * 8 + e["amp20"] * 0.5 + min(e["odds"], 3) * 6, 1)
    e["ext"] = S.ext_bonus({"name": e["name"], "sector": e["sector"]}, ext)
    e["ext_why"] = S._ext_score_of({"name": e["name"], "sector": e["sector"]}, ext)[1]
    # 止损距离
    st_ = e["atr_stop"] or e["ma20"]
    e["stop"] = round(st_, 2)
    e["stop_pct"] = round((st_ / px - 1) * 100, 2) if px and st_ else 0
    # 风险收益（到 20 日高）
    hi = n(it, "high20")
    e["rr"] = round((hi / px - 1) / abs(e["stop_pct"] / 100), 2) if hi and px and e["stop_pct"] else 0
    return e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="all",
                    choices=["all", "both", "strong", "cert", "flex"])
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    rev, ext, gate, gwhy = load()
    quant, qgen = load_quant()
    items = rev.get("items") or []
    t = rev.get("temperature") or {}
    print("═" * 100)
    print("自选池筛选   %s   池内 %d 只" % (rev.get("generatedAt"), len(items)))
    print("大盘: 涨%s 跌%s（涨占比 %.1f%%）温度 %s(%s) | 闸门 %s（%s）" % (
        t.get("market_up"), t.get("market_down"), t.get("breadth") or 0,
        t.get("score"), t.get("label"), gate, gwhy[:30]))
    print("外部: %s (%+d) | 量化库: %s（%d 只，≥%.0f 剔除 %d 只）" % (
        (ext.get("risk_pref") or {}).get("tone"),
        (ext.get("risk_pref") or {}).get("score") or 0,
        (qgen or "未生成")[5:16], len(quant), QUANT_TH,
        sum(1 for v in quant.values() if (v.get("quant") or 0) >= QUANT_TH)))
    print("═" * 100)
    if not quant:
        print("⚠ 无量化度数据 → 请先跑: python3 scripts/quant_filter.py --pool")
        return 1

    allE = [enrich(x, ext, quant) for x in items
            if not str(x.get("code", "")).startswith(("5", "1"))]
    dropped = [e for e in allE if e["is_quant"]]
    E = [e for e in allE if not e["is_quant"]]
    print("\n量化过滤: 剔除 %d 只（占比 %.0f%%）—— 这些标的技术评分虚高但不可交易" % (
        len(dropped), len(dropped) / max(len(allE), 1) * 100))
    print("  被剔除示例: %s" % "、".join("%s(%.0f)" % (d["name"], d["quant"]) for d in
                                        sorted(dropped, key=lambda x: -x["quant"])[:8]))

    def table(rows, title, note, key, limit=12, cols=("cert", "flex")):
        rows = sorted(rows, key=key)[:limit]
        print("\n【%s】%d 只 → 前 %d" % (title, len(rows), min(limit, len(rows))))
        print("  %s" % note)
        if not rows:
            print("  （无）")
            return
        print("  %-8s%-9s%4s%5s%5s%6s%5s%7s%6s%6s%8s %s" % (
            "代码", "名称", "评分", "确定", "弹性", "ATR%", "赔率", "空间", "量化", "RSI", "5日主力", "板块"))
        for r in rows:
            print("  %-8s%-9s%4d%5.0f%5.0f%5.1f%%%6.2f%+5.1f%%%6.0f%6.0f%+7.2f亿 %s" % (
                r["code"], r["name"][:8], r["score"], r["cert"], r["flex"],
                r["atr_pct"], r["odds"], r["space"], r["quant"] or 0, r["rsi6"],
                r["m5"] / 1e8, (r["sector"] or "-")[:10]))

    # 双重门槛：确定性 + 弹性
    both = [r for r in E if r["cert"] >= 72 and r["flex"] >= 55
            and r["trend"] in ("强势多头", "多头排列") and r["rsi6"] < 75 and r["m5"] > 0]
    cert_only = [r for r in E if r["trend"] in ("强势多头", "多头排列")
                 and r["score"] >= 70 and r["cert"] >= 75 and r["rsi6"] < 72]
    flex_only = [r for r in E if r["trend"] in ("强势多头", "多头排列")
                 and r["odds"] >= 1.8 and r["atr_pct"] >= 3.0 and r["rsi6"] < 75 and r["m5"] > 0]

    if args.mode in ("all", "both"):
        table(both, "★ 双高（确定性≥72 且 弹性≥55）",
              "确定性与弹性兼备 —— 既要方向靠谱、也要有波动空间",
              key=lambda r: -(r["cert"] * 0.5 + r["flex"] * 0.5))
    if args.mode in ("all", "strong", "cert"):
        table(cert_only, "A 确定性优先（趋势强+评分≥70+RSI<72）",
              "方向最靠谱：多头结构 + 高评分 + 未超买（已剔除量化盘）",
              key=lambda r: -(r["cert"] * 0.7 + r["flex"] * 0.3))
    if args.mode in ("all", "strong", "flex"):
        table(flex_only, "B 弹性优先（赔率≥1.8 且 ATR≥3%）",
              "波动空间最大：适合做波段（⚠ 弹性高=回撤也大）",
              key=lambda r: -(r["flex"] * 0.7 + r["cert"] * 0.3))

    # 量化嫌疑但其他条件好的（供参考，说明为何被剔）
    if args.mode == "all" and dropped:
        good_q = [d for d in dropped if d["score"] >= 70 and d["trend"] in ("强势多头", "多头排列")]
        if good_q:
            print("\n【附：因量化嫌疑被剔除、但技术分高的 %d 只（对照说明）】" % len(good_q))
            print("  这些票评分高是因为低波动→形态'干净'，不是真的强；赔率普遍<1，主力资金多为对冲流量")
            print("  %-8s%-9s%4s%6s%5s%7s%6s%6s %s" % (
                "代码", "名称", "评分", "量化度", "ATR%", "赔率", "跳空", "振幅", "板块"))
            for d in sorted(good_q, key=lambda x: -x["score"])[:10]:
                print("  %-8s%-9s%4d%6.1f%5.1f%%%7.2f%5.2f%%%5.1f%% %s" % (
                    d["code"], d["name"][:8], d["score"], d["quant"], d["atr_pct"],
                    d["odds"], d["q_gap"] or 0, d["atr_pct"], (d["sector"] or "-")[:10]))

    print("\n" + "═" * 96)
    print("说明：评分=系统技术分(0-100)；外部分=板块/品种因子×1.5；止损=ATR止损价；")
    print("      空间=距ATR止损的跌幅。当前闸门 %s，若为 block 则系统不开新仓。" % gate)
    print("      ⚠ 仅为数据筛选，不构成投资建议。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
