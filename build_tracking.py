#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
历史跟踪数据构建：从 git 历史提取每日推荐 Top10（买入信号），拉取日 K 线，生成 data/tracking_data.json。

输出结构：
- days:    [{date, items: [{rank, name, code, total_score, rating, signal_key, signal, price, sector, track_return}]}]
- stable:  [{code, name, sector, count(上榜天数), rate(上榜率), latest_score, latest_signal}] 按上榜天数倒序
- klines:  {code: {dates, o, h, l, c}}  最近 150 个交易日

用法：
  python build_tracking.py
  python run_review.py --mode tracking
"""
import json
import os
import subprocess
import sys
from collections import Counter, OrderedDict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
sys.path.insert(0, BASE_DIR)
from src import data_provider as dp  # noqa: E402

BUY_KEYS = ("strong_buy", "buy")
TOPN = 10
KLINE_BARS = 150


def git_recommend_commits():
    """返回 [(datetime_str, commit_hash)]，datetime_str 形如 '2026-08-06 09:13:56'。"""
    out = subprocess.check_output(
        ["git", "log", "--format=%ad %h", "--date=format:%Y-%m-%d %H:%M:%S",
         "--", "data/recommend_data.json"],
        cwd=BASE_DIR, text=True)
    commits = []
    for line in out.strip().splitlines():
        dt, h = line.rsplit(" ", 1)
        commits.append((dt, h))
    return commits


def pick_daily_snapshots(commits):
    """按日期分组，每天取 08:30-11:59 内最早的一个快照；无则取当天最早。返回 {date: commit}。"""
    by_day = OrderedDict()
    for dt, h in commits:
        by_day.setdefault(dt[:10], []).append((dt[11:], h))
    snaps = {}
    for day, lst in by_day.items():
        lst.sort()
        chosen = next(((t, h) for t, h in lst if "08:30:00" <= t <= "11:59:59"), lst[0])
        snaps[day] = chosen[1]
    return snaps


def top10_buys(picks):
    """推荐里前十名"推荐购买"：先保留买入/强烈买入信号，再取前10。"""
    buys = [p for p in picks if p.get("signal_key") in BUY_KEYS]
    if not buys:
        buys = picks
    return buys[:TOPN]


def fetch_klines(codes, count=250):
    """抓取并缓存日K，返回 {code: kline_dict}。失败跳过。"""
    out = {}
    for c in codes:
        try:
            k = dp.fetch_daily_kline(c, count=count)
        except Exception:
            k = None
        if k and len(k.get("dates") or []) > 30:
            out[c] = k
    return out


def track_return(kline, rec_date):
    """推荐日收盘(<=rec_date 最近一根) → 最新收盘 的涨幅%。"""
    if not kline:
        return None
    dates, closes = kline["dates"], kline["closes"]
    idx = None
    for i, d in enumerate(dates):
        if d <= rec_date:
            idx = i
        else:
            break
    if idx is None or not closes:
        return None
    base = closes[idx]
    last = closes[-1]
    if not base:
        return None
    return round((last / base - 1) * 100, 2)


def main():
    commits = git_recommend_commits()
    snaps = pick_daily_snapshots(commits)
    print(f"历史快照 {len(snaps)} 天: {list(snaps.keys())}")

    days = []
    pool_codes = set()
    for day, h in snaps.items():
        try:
            raw = subprocess.check_output(["git", "show", f"{h}:data/recommend_data.json"],
                                          cwd=BASE_DIR)
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            print(f"  [skip] {day} 解析失败: {e}")
            continue
        items = top10_buys(data.get("picks") or [])
        for it in items:
            pool_codes.add(it.get("code"))
        days.append({"date": day, "items": [
            {"rank": i + 1,
             "name": it.get("name", ""), "code": it.get("code", ""),
             "total_score": it.get("total_score"), "rating": it.get("rating", ""),
             "signal_key": it.get("signal_key", ""), "signal": it.get("signal", ""),
             "price": it.get("price"), "sector": it.get("sector", "")}
            for i, it in enumerate(items)]})
        print(f"  {day}: {len(items)} 只")

    print(f"抓取 K 线（{len(pool_codes)} 个代码）…")
    kl_full = fetch_klines(sorted(pool_codes))

    # 计算跟踪收益 + 精简 K 线
    klines = {}
    for day in days:
        for it in day["items"]:
            k = kl_full.get(it["code"])
            it["track_return"] = track_return(k, day["date"])
    for code, k in kl_full.items():
        n = len(k["dates"])
        s = max(0, n - KLINE_BARS)
        klines[code] = {
            "dates": k["dates"][s:],
            "o": [round(v, 2) for v in k["opens"][s:]],
            "h": [round(v, 2) for v in k["highs"][s:]],
            "l": [round(v, 2) for v in k["lows"][s:]],
            "c": [round(v, 2) for v in k["closes"][s:]],
            "v": [int(v) for v in k["volumes"][s:]],
        }

    # 稳定榜：上榜天数倒序，同天按最近评分倒序
    cnt = Counter()
    latest = {}
    first_seen = {}
    for day in days:
        for it in day["items"]:
            c = it["code"]
            cnt[c] += 1
            latest[c] = it
            first_seen.setdefault(c, day["date"])
    total = len(days)
    stable = sorted(
        ({ "code": c, "name": latest[c]["name"], "sector": latest[c]["sector"],
           "count": n, "rate": round(n / total * 100) if total else 0,
           "first_day": first_seen[c], "latest_day": latest[c]["name"],
           "latest_score": latest[c]["total_score"], "latest_signal": latest[c]["signal_key"],
           "latest_track_return": latest[c]["track_return"] }
         for c, n in cnt.items()),
        key=lambda x: (-x["count"], -(x["latest_score"] or 0)))

    out = {
        "generatedAt": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_days": total,
        "days": days,
        "stable": stable,
        "klines": klines,
    }
    path = os.path.join(DATA_DIR, "tracking_data.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"  写入 {path}  ({os.path.getsize(path)} bytes, 天数{total}, 代码{len(klines)})")
    return out


if __name__ == "__main__":
    main()
