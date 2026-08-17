#!/usr/bin/env python3
"""主线板块分析：主线判定 + 龙头 + 资金 + 异动 + 升浪。

数据源：
- 东财行业板块资金（institution_data.json）：板块主力净流入/涨幅/占比
- 个股主力资金榜（stock_rank）：资金龙头
- 自选池复盘/趋势数据：板块赚钱效应、量比异动、升浪（20/60日涨幅）

主线评分（0-100）= 主力净流入 40% + 板块涨幅 20% + 净占比 20% + 强势股效应 20%
升浪判定（基于自选池个股）：
  启动浪  多头形成、60日涨幅 <15%
  主升浪  60日 15-40%、趋势多头
  加速浪  60日 >40%、量能放大/创新高
  滞涨  60日 >40% 但 20日涨幅 <5%（高位钝化）

用法: python3 build_mainline.py
输出: data/mainline_data.json
"""
import json
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
sys.path.insert(0, BASE_DIR)

from src import data_provider as dp  # noqa: E402

OUT = os.path.join(DATA_DIR, "mainline_data.json")


def load_json(name):
    p = os.path.join(DATA_DIR, name)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def sector_score(m):
    """板块主线评分：净流入(40) + 涨幅(20) + 净占比(20) + 强势效应(20)。"""
    net = m.get("main_net") or 0
    chg = m.get("change_pct") or 0
    ratio = m.get("main_ratio") or 0
    s_net = max(0, min(100, net / 3e9 * 100))            # 30亿净流入≈满分
    s_chg = max(0, min(100, (chg + 3) / 6 * 100))        # +3%≈满分
    s_ratio = max(0, min(100, ratio / 5 * 100))          # 5%净占比≈满分
    return round(s_net * 0.4 + s_chg * 0.2 + s_ratio * 0.2, 1), s_net


def classify_wave(change_20d, change_60d, trend_status, volume_ratio):
    """升浪分类。"""
    if change_60d is None:
        return "—"
    if change_60d > 40:
        if change_20d is not None and change_20d < 5:
            return "滞涨"
        if volume_ratio and volume_ratio > 1.5:
            return "加速浪"
        return "主升浪"
    if change_60d >= 15:
        return "主升浪"
    if trend_status in ("强势多头", "多头排列"):
        return "启动浪"
    return "—"


def build_waves():
    """升浪分析：基于 uptrend 强势股 + K线算 20 日涨幅。"""
    u = load_json("uptrend_data.json")
    waves = []
    for it in (u.get("items") or [])[:80]:
        code = it.get("code")
        k = dp.fetch_daily_kline(code, count=80, use_cache=True)
        chg20 = None
        if k and len(k["closes"]) > 20:
            chg20 = round((k["closes"][-1] / k["closes"][-21] - 1) * 100, 1)
        wave = classify_wave(chg20, it.get("change_60d"), it.get("trend_status"), it.get("volume_ratio"))
        if wave == "—":
            continue
        waves.append({
            "name": it.get("name"), "code": code,
            "sector": it.get("sector", ""),
            "close": it.get("close"),
            "change_pct": it.get("change_pct"),
            "score": it.get("score"),
            "change_20d": chg20,
            "change_60d": it.get("change_60d"),
            "volume_ratio": it.get("volume_ratio"),
            "wave": wave,
        })
    order = {"加速浪": 0, "主升浪": 1, "启动浪": 2, "滞涨": 3}
    waves.sort(key=lambda w: (order.get(w["wave"], 9), -(w.get("score") or 0)))
    return waves


def build_abnormal(review_items, stock_rank):
    """异动分析：放量拉升 / 资金异动 / 大跌。"""
    ab = {"volume_surge": [], "money_surge": [], "drop": []}
    # 放量拉升：量比>2 且涨幅>3%
    for it in review_items:
        vr = it.get("volume_ratio") or 0
        chg = it.get("change_pct") or 0
        if vr >= 2 and chg >= 3:
            ab["volume_surge"].append({"name": it.get("name"), "code": it.get("code"),
                                       "change_pct": chg, "volume_ratio": vr,
                                       "sector": it.get("sector", "")})
        elif chg <= -4:
            ab["drop"].append({"name": it.get("name"), "code": it.get("code"),
                               "change_pct": chg, "sector": it.get("sector", "")})
    # 资金异动：主力净流入榜前列（个股）
    for x in (stock_rank.get("inflow") or [])[:8]:
        ab["money_surge"].append({"name": x.get("name"), "code": x.get("code"),
                                  "change_pct": x.get("change_pct"), "main_net": x.get("main_net"),
                                  "main_ratio": x.get("main_ratio")})
    return ab


def build_periods(review_items):
    """板块周期涨幅：自选池板块成分股等权合成指数，算 5/10/20 日涨幅。"""
    from collections import defaultdict
    sec_groups = defaultdict(list)
    for it in review_items:
        sec_groups[it.get("sector", "其他")].append(it)
    periods = []
    for sec, lst in sec_groups.items():
        if len(lst) < 2:
            continue
        codes = [it["code"] for it in lst[:10]]
        nav = None
        cnt = 0
        n_min = 999
        for code in codes:
            k = dp.fetch_daily_kline(code, count=30, use_cache=True)
            if not k or len(k["closes"]) < 25:
                continue
            c = k["closes"]
            n_min = min(n_min, len(c))
            if nav is None:
                nav = [0.0] * len(c)
            base = c[0]
            for i, v in enumerate(c):
                if i < len(nav):
                    nav[i] += v / base
            cnt += 1
        if not cnt or nav is None:
            continue
        nav = [v / cnt for v in nav[:n_min]]
        n = len(nav)
        chg = lambda span: round((nav[-1] / nav[-span - 1] - 1) * 100, 2) if n > span else None
        today = round((nav[-1] / nav[-2] - 1) * 100, 2) if n > 1 else None
        strong = sum(1 for it in lst if (it.get("score") or 0) >= 60 or (it.get("change_pct") or 0) >= 5)
        periods.append({
            "sector": sec,
            "count": len(lst),
            "strong": strong,
            "chg_today": today,
            "chg_5d": chg(5),
            "chg_10d": chg(10),
            "chg_20d": chg(20),
        })
    return periods


def main():
    inst = load_json("institution_data.json")
    ind = (inst.get("sector") or {}).get("industry") or {}
    review = load_json("review_data.json")
    review_items = review.get("items") or []

    # 1. 主线板块（东财行业板块资金榜）
    sectors = []
    for x in (ind.get("inflow") or []) + (ind.get("outflow") or []):
        sc, s_net = sector_score(x)
        sectors.append({
            "name": x.get("name"), "code": x.get("code"),
            "main_net": x.get("main_net"), "main_ratio": x.get("main_ratio"),
            "change_pct": x.get("change_pct"), "score": sc,
            "net_score": round(s_net, 1),
        })
    sectors.sort(key=lambda s: -s["score"])
    mainline = sectors[:10]

    # 2. 资金龙头（个股主力净流入榜）
    sr = inst.get("stock_rank") or {}
    leaders = [{
        "name": x.get("name"), "code": x.get("code"),
        "price": x.get("price"), "change_pct": x.get("change_pct"),
        "main_net": x.get("main_net"), "main_ratio": x.get("main_ratio"),
    } for x in (sr.get("inflow") or [])[:10]]

    # 3. 自选池内板块龙头（涨幅第一 + 评分最高）
    from collections import defaultdict
    sec_groups = defaultdict(list)
    for it in review_items:
        sec_groups[it.get("sector", "其他")].append(it)
    pool_leaders = {}
    for sec, lst in sec_groups.items():
        if not lst:
            continue
        by_chg = max(lst, key=lambda x: x.get("change_pct") or 0)
        by_score = max(lst, key=lambda x: x.get("score") or 0)
        pool_leaders[sec] = {
            "change_leader": {"name": by_chg.get("name"), "code": by_chg.get("code"),
                              "change_pct": by_chg.get("change_pct")},
            "score_leader": {"name": by_score.get("name"), "code": by_score.get("code"),
                             "score": by_score.get("score")},
            "up_count": sum(1 for x in lst if (x.get("change_pct") or 0) > 0),
            "total": len(lst),
        }

    # 4. 升浪 + 5. 异动
    waves = build_waves()
    abnormal = build_abnormal(review_items, sr)

    # 6. 板块周期主线（5/10/20 日涨幅）
    periods = build_periods(review_items)
    period_rank = {}
    for span, key in (("5d", "chg_5d"), ("10d", "chg_10d"), ("20d", "chg_20d")):
        ranked = [p for p in periods if p.get(key) is not None]
        ranked.sort(key=lambda p: -p[key])
        period_rank[span] = [{"sector": p["sector"], "chg": p[key],
                              "chg_today": p["chg_today"], "count": p["count"],
                              "strong": p["strong"]} for p in ranked[:10]]

    out = {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "主线评分 = 主力净流入40% + 涨幅20% + 净占比20% + 强势股效应20%；升浪基于自选池强势股 20/60 日涨幅",
        "mainline": mainline,
        "leaders": leaders,
        "pool_leaders": pool_leaders,
        "waves": waves,
        "abnormal": abnormal,
        "periods": periods,
        "period_rank": period_rank,
    }
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    print(f"写入 {OUT}  主线板块{len(mainline)} / 资金龙头{len(leaders)} / 升浪{len(waves)} / 异动 放量{len(abnormal['volume_surge'])} 资金{len(abnormal['money_surge'])} 大跌{len(abnormal['drop'])}")


if __name__ == "__main__":
    main()
