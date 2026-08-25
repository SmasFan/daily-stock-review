#!/usr/bin/env python3
"""低估值选股：好公司 + 低估值 + 横盘蓄势（全市场）。

分层漏斗（控制 API 调用量）：
1. 拉全市场标的列表 → 排除 ST/退市/北交所/次新股
2. 批量估值 → PE/PB 横截面低分位（后 35% 内）→ 初筛 ~1500 只
3. 全市场快照 → 排除成交额过低/停牌 → ~1000 只
4. 估值分 Top 300 → 拉日K → 算横盘分（60日振幅/波动率/均线粘合/缩量）
5. 综合分 Top 120 → 拉财务指标 → 质量分（ROE/毛利率/净利率/负债率）
6. 三维综合分排序输出 data/lowval_data.json

评分（各 1/3）：
- 质量分：ROE/毛利率/净利率/负债率（同花顺财务指标）
- 估值分：PE/PB 横截面低分位（同花顺估值快照）
- 横盘分：区间振幅小 + 波动率低 + 均线粘合 + 缩量（日K计算）
"""
import json
import math
import os
import re
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
sys.path.insert(0, BASE_DIR)

from src import data_provider as dp
from src import ths_api
try:
    from src import stock_pool as sp
except Exception:
    sp = None


def code_sector(code):
    try:
        if sp is not None:
            return sp.get_code_sector().get(code, "")
    except Exception:
        pass
    return ""


def pct_rank(values, higher_is_better=True):
    """横截面百分位排名（0-100）。"""
    n = len(values)
    if n == 0:
        return {}
    import statistics
    out = {}
    for i, v in enumerate(values):
        less = sum(1 for x in values if x is not None and v is not None and
                   (x < v if higher_is_better else x > v))
        out[i] = less / n * 100
    return out


def is_st_or_bad(name):
    """排除 ST/退市/风险警示。"""
    if not name:
        return True
    return bool(re.search(r"ST|退|PT|风险警示", name, re.I))


def fetch_klines_limited(codes, count=120):
    """批量拉日K（带限速），返回 {code: kline}。"""
    out = {}
    for c in codes:
        try:
            k = dp.fetch_daily_kline(c, count=count, use_cache=True)
            if k and len(k.get("dates") or []) >= 60:
                out[c] = k
        except Exception:
            pass
        time.sleep(0.15)
    return out


def sideways_score(k):
    """横盘分 0-100：区间振幅小 + 波动率低 + 均线粘合 + 缩量。

    温和版：近60日区间涨跌幅 ±15% 内；波动率 < 池内中位；MA20/MA60 乖离 < 5%；近5日均量 < 60日均量。
    """
    dates, closes, vols = k["dates"], k["closes"], k.get("volumes") or []
    n = min(60, len(closes))
    if n < 30:
        return 0.0
    seg = closes[-n:]
    seg_dates = dates[-n:]
    # 1) 区间振幅
    ret = (seg[-1] / seg[0] - 1) * 100 if seg[0] else 0
    amp_score = max(0, 100 - abs(ret) * 6)  # ±0%→100, ±15%→10, ±20%→0
    # 2) 波动率（日收益率标准差）
    rets = [seg[i + 1] / seg[i] - 1 for i in range(len(seg) - 1) if seg[i]]
    vol = (sum((r - sum(rets) / len(rets)) ** 2 for r in rets) / len(rets)) ** 0.5 if rets else 99
    vol_score = max(0, min(100, (0.03 - vol) / 0.025 * 100))  # vol 3%→0, 0.5%→100
    # 3) 均线粘合（MA20/MA60 乖离）
    ma20 = sum(seg[-20:]) / 20 if len(seg) >= 20 else seg[-1]
    ma60 = sum(closes[-min(60, len(closes)):]) / min(60, len(closes))
    dev = abs(ma20 / ma60 - 1) * 100 if ma60 else 99
    ma_score = max(0, 100 - dev * 15)  # dev 0%→100, 5%→25
    # 4) 缩量（近5日均量 / 60日均量）
    v5 = sum(vols[-5:]) / 5 if len(vols) >= 5 and sum(vols[-5:]) else 0
    v60 = sum(vols[-60:]) / 60 if len(vols) >= 60 and sum(vols[-60:]) else 0
    vol_ratio = v5 / v60 if v60 else 1
    shrink_score = max(0, min(100, (1.3 - vol_ratio) / 0.8 * 100))  # 1.3→0, 0.5→100
    return round((amp_score * 0.3 + vol_score * 0.25 + ma_score * 0.25 + shrink_score * 0.2), 1)


def quality_score(fin):
    """质量分 0-100：ROE/毛利率/净利率/负债率。

    财务指标为最近报告期单季值：ROE 单季×4 年化近似；金融股净利率口径特殊
    （>100% 视为异常），用 ROE+毛利率 主导。
    """
    if not fin:
        return 0.0
    def f(v, d=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d
    roe_q = f(fin.get("roe"))
    roe = min(roe_q * 4, 60) if roe_q > 0 else 0      # 单季→年化，上限 60
    gm = f(fin.get("gross_margin"))
    nm = f(fin.get("net_margin"))
    nm = nm if nm < 100 else 0                          # 异常净利率按 0
    debt = f(fin.get("debt_ratio"), -1)
    roe_s = min(100, max(0, roe * 2.2))                 # 年化 ROE 45%→100
    gm_s = min(100, max(0, gm * 2.0))                   # 毛利率 50%→100
    nm_s = min(100, max(0, nm * 3.0))                   # 净利率 33%→100
    debt_s = 50
    if debt >= 0:
        debt_s = min(100, max(0, (60 - debt) / 0.6))    # 负债率 0→100, 60%→0
    return round(roe_s * 0.4 + gm_s * 0.3 + nm_s * 0.15 + debt_s * 0.15, 1)


def main():
    print("== 低估值选股（好公司 + 低估值 + 横盘）==")
    if not ths_api.available():
        print("[fatal] 未配置同花顺 API Key，退出")
        return 1

    # 1) 全市场标的列表
    print("拉取全市场标的列表…")
    tickers = ths_api.fetch_ticker_list("a-share", limit=10000)
    codes = [t["ticker"] for t in tickers if t.get("ticker")]
    names = {t["ticker"]: t.get("name", "") for t in tickers}
    codes = [c for c in codes if not is_st_or_bad(names.get(c, ""))]
    codes = [c for c in codes if len(c) == 6]
    print(f"   全市场 {len(tickers)} 只 → 剔除 ST/退市后 {len(codes)} 只")

    # 2) 批量估值 → 低分位初筛
    print("拉取估值快照（分批）…")
    vals = ths_api.fetch_valuations(codes)
    print(f"   估值命中 {len(vals)} 只")
    candidates = []
    for c in codes:
        v = vals.get(c)
        if not v:
            continue
        pe, pb = v.get("pe_ttm"), v.get("pb_mrq")
        if pe is None or pb is None:
            continue
        try:
            pe_f, pb_f = float(pe), float(pb)
        except (TypeError, ValueError):
            continue
        if pe_f <= 0 or pb_f <= 0 or pe_f > 200:   # 排除亏损与离谱 PE
            continue
        candidates.append((c, pe_f, pb_f))
    if not candidates:
        print("[fatal] 无有效估值数据")
        return 1
    pes = [x[1] for x in candidates]
    pbs = [x[2] for x in candidates]
    pe_rank = pct_rank(pes, higher_is_better=False)   # 低 PE → 高排名
    pb_rank = pct_rank(pbs, higher_is_better=False)
    scored = []
    for i, (c, pe, pb) in enumerate(candidates):
        val_score = (pe_rank.get(i, 0) + pb_rank.get(i, 0)) / 2
        scored.append((c, pe, pb, val_score))
    # 估值分前 35% 作为初筛
    scored.sort(key=lambda x: -x[3])
    keep = max(100, int(len(scored) * 0.35))
    prelim = scored[:keep]
    print(f"   估值低分位初筛：{len(scored)} → {len(prelim)} 只")

    # 3) 行情快照 → 排除停牌/成交过低（分页遍历全市场，名称从标的列表补）
    print("拉取行情快照（分页遍历）…")
    all_quotes = {}
    offset = 0
    while True:
        qp, total = ths_api.fetch_snapshot_paged(limit=100, offset=offset)
        if not qp:
            break
        all_quotes.update(qp)
        offset += 100
        if offset >= total or len(qp) < 100:
            break
    print(f"   快照命中 {len(all_quotes)}/{total}")
    pool = []
    for c, pe, pb, val_score in prelim:
        q = all_quotes.get(c)
        if not q or not q.get("price"):
            continue
        amount = q.get("amount") or 0
        if amount < 5e7:      # 日成交额 < 5000万 排除（流动性不足）
            continue
        pool.append({
            "code": c, "name": names.get(c, c),
            "price": q.get("price"), "change_pct": q.get("change"),
            "amount": amount, "pe_ttm": pe, "pb_mrq": pb,
            "val_score": val_score, "sector": code_sector(c),
        })
    print(f"   剔除停牌/低流动性后 {len(pool)} 只")

    # 4) 估值分 Top300 拉日K → 横盘分
    pool.sort(key=lambda x: -x["val_score"])
    kline_codes = [x["code"] for x in pool[:300]]
    print(f"   拉取 Top{len(kline_codes)} 日K线（计算横盘分）…")
    kl = fetch_klines_limited(kline_codes, count=120)
    for it in pool:
        k = kl.get(it["code"])
        it["sideways_score"] = sideways_score(k) if k else 0.0
    pool = [it for it in pool if it["sideways_score"] > 0]
    # 横盘分排序取 Top 120
    pool.sort(key=lambda x: -x["sideways_score"])
    final_codes = [x["code"] for x in pool[:120]]
    print(f"   横盘分 Top {len(final_codes)}")

    # 5) 财务指标 → 质量分
    print(f"   拉取 Top{len(final_codes)} 财务指标…")
    fins = {}
    for c in final_codes:
        fi = ths_api.fetch_financial_indicators(c)
        if fi:
            g = fi.get("growth", {})
            p = fi.get("profitability", {})
            s = fi.get("solvency", {})
            fins[c] = {
                "revenue_yoy": g.get("calculate_operating_income_yoy_growth_ratio"),
                "profit_yoy": g.get("calculate_parent_holder_net_profit_yoy_growth_ratio"),
                "roe": p.get("index_weighted_avg_roe"),
                "gross_margin": p.get("sale_gross_margin"),
                "net_margin": p.get("sale_net_interest_ratio"),
                "debt_ratio": s.get("asset_liability_ratio"),
            }
        time.sleep(0.15)
    # 质量门槛：ROE>0 且 净利增速>-30%（排除明显亏损/衰退）
    good = []
    for it in pool:
        if it["code"] not in final_codes:
            continue
        fin = fins.get(it["code"])
        it["fin"] = fin
        it["quality_score"] = quality_score(fin)
        if fin:
            try:
                roe_q = float(fin.get("roe") or 0)
                py = float(fin.get("profit_yoy") or 0)
            except (TypeError, ValueError):
                roe_q, py = 0, 0
            if roe_q * 4 <= 0 or py < -30:      # 年化 ROE≤0 或净利大降 → 质量分清零
                it["quality_score"] = 0.0
        good.append(it)

    # 6) 综合分 = 质量 1/3 + 估值 1/3 + 横盘 1/3
    for it in good:
        it["total_score"] = round(
            it["quality_score"] * 0.35 + it["val_score"] * 0.35 + it["sideways_score"] * 0.3, 1)
    good.sort(key=lambda x: -x["total_score"])
    print(f"   最终入选（质量分>0）：{len(good)} 只")

    out = {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "method": "好公司(ROE/毛利/负债率) + 低估值(PE/PB横截面低分位) + 横盘蓄势(振幅/波动/均线粘合/缩量)",
        "items": [
            {
                "code": it["code"], "name": it["name"],
                "sector": it.get("sector", ""),
                "price": it["price"], "change_pct": it["change_pct"],
                "pe_ttm": it["pe_ttm"], "pb_mrq": it["pb_mrq"],
                "quality_score": it["quality_score"],
                "val_score": round(it["val_score"], 1),
                "sideways_score": it["sideways_score"],
                "total_score": it["total_score"],
                "amount": it["amount"],
                "fin": it.get("fin") or {},
            }
            for it in good
        ],
    }
    path = os.path.join(DATA_DIR, "lowval_data.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"写入 {path}（{len(out['items'])} 只）")
    for it in out["items"][:10]:
        print("  %s %s 综合%.1f 质量%.1f 估值%.1f 横盘%.1f PE%s PB%s" % (
            it["code"], it["name"][:10], it["total_score"], it["quality_score"],
            it["val_score"], it["sideways_score"], it["pe_ttm"], it["pb_mrq"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
