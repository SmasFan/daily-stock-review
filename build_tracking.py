#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
历史跟踪数据构建：每日推荐 Top10（买入信号）持久化到 SQLite，
拉取日 K 线，生成 data/tracking_data.json。

数据流（2026-08 优化：SQLite 为主，不再依赖 git 历史）：
1. 初始化 data/tracking.db（表 daily_picks：day+code 主键）
2. 首次运行时用 git 历史一次性导入存量快照（之后 git 仅作冗余）
3. 每次运行自动把当前 data/recommend_data.json 快照追加进库
4. 从 SQLite 读取全部日期 → 拉K线算跟踪收益 → 输出 JSON

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
import sqlite3
import subprocess
import sys
from collections import Counter, OrderedDict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "tracking.db")
sys.path.insert(0, BASE_DIR)
from src import data_provider as dp  # noqa: E402

BUY_KEYS = ("strong_buy", "buy")
TOPN = 10
KLINE_BARS = 150


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS daily_picks (
        day TEXT NOT NULL,
        rank INTEGER NOT NULL,
        name TEXT NOT NULL,
        code TEXT NOT NULL,
        total_score REAL,
        rating TEXT,
        signal_key TEXT,
        signal TEXT,
        price REAL,
        sector TEXT,
        PRIMARY KEY (day, code))""")
    conn.commit()
    return conn


def upsert_day(conn, day, items):
    """写入一天快照（day+code 主键，重复运行幂等）。"""
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO daily_picks
               (day, rank, name, code, total_score, rating, signal_key, signal, price, sector)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [(day, i + 1, it.get("name", ""), it.get("code", ""),
              it.get("total_score"), it.get("rating", ""),
              it.get("signal_key", ""), it.get("signal", ""),
              it.get("price"), it.get("sector", ""))
             for i, it in enumerate(items)])


def load_days(conn):
    """从 SQLite 读全部快照，按日期升序返回 [{date, items}]。"""
    rows = conn.execute(
        "SELECT day, rank, name, code, total_score, rating, signal_key, signal, price, sector "
        "FROM daily_picks ORDER BY day, rank").fetchall()
    days = OrderedDict()
    for r in rows:
        day, rank, name, code, total, rating, sk, signal, price, sector = r
        days.setdefault(day, []).append({
            "rank": rank, "name": name, "code": code,
            "total_score": total, "rating": rating or "",
            "signal_key": sk or "", "signal": signal or "",
            "price": price, "sector": sector or ""})
    return [{"date": d, "items": items} for d, items in days.items()]


def import_git_history(conn):
    """首次迁移：用 git 历史导入存量快照（重复导入幂等）。

    2026-08-09 修正：按数据内 generatedAt 日期归档而非 commit 日期——
    旧 commit 里可能夹带"重新生成/紧凑化"的旧数据（如 08-09 的 commit 内容
    仍是 08-07 的推荐），按 commit 日期会误建周末/节假日快照。
    """
    commits = git_recommend_commits()
    imported = 0
    seen = set()
    for dt, h in commits:
        try:
            raw = subprocess.check_output(["git", "show", f"{h}:data/recommend_data.json"],
                                          cwd=BASE_DIR)
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        day = (data.get("generatedAt") or "")[:10]
        if not day or day in seen:
            continue
        seen.add(day)
        items = top10_buys(data.get("picks") or [])
        upsert_day(conn, day, items)
        imported += 1
    return imported, len(seen)


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
    """推荐日收盘(<=rec_date 最近一根) → 最新收盘 的涨幅%。

    2026-08-09 修正：推荐日已是最后一个交易日（无后续行情）返回 None，
    前端显示 '--' 而不是 0%，避免把"还没走出的行情"误读为"收益为零"。
    """
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
    if idx >= len(dates) - 1:
        return None
    base = closes[idx]
    last = closes[-1]
    if not base:
        return None
    return round((last / base - 1) * 100, 2)


def main():
    conn = init_db()
    imported, snap_total = import_git_history(conn)
    print(f"git 历史导入 {imported}/{snap_total} 天快照")

    # 追加当前推荐快照（推荐文件与 build_tracking 同日时覆盖当日旧快照）
    rec_path = os.path.join(DATA_DIR, "recommend_data.json")
    if os.path.exists(rec_path):
        try:
            with open(rec_path, "r", encoding="utf-8") as f:
                rec = json.load(f)
            today = (rec.get("generatedAt") or "")[:10]
            if today:
                upsert_day(conn, today, top10_buys(rec.get("picks") or []))
                print(f"  追加当日快照 {today}")
        except Exception as e:
            print(f"  [skip] 当日快照追加失败: {e}")

    days = load_days(conn)
    if not days:
        print("无历史快照，请先运行 run_review.py --mode recommend")
        return
    print(f"SQLite 快照 {len(days)} 天: {days[0]['date']} ~ {days[-1]['date']}")

    pool_codes = set()
    for day in days:
        for it in day["items"]:
            pool_codes.add(it.get("code"))
        print(f"  {day['date']}: {len(day['items'])} 只")

    print(f"抓取 K 线（{len(pool_codes)} 个代码）…")
    kl_full = fetch_klines(sorted(pool_codes))

    # 交易日归一化（2026-08-09 修正）：周末/节假日被 auto commit 误当交易日
    # 入库（如 08-09 周日），按 K 线交易日历把它们归并到最近交易日
    trade_days = sorted({d for k in kl_full.values() for d in (k.get("dates") or [])})
    if trade_days:
        norm = {}
        for day in days:
            nd = day["date"]
            for td in reversed(trade_days):
                if td <= day["date"]:
                    nd = td
                    break
            items = norm.setdefault(nd, [])
            for it in day["items"]:
                if not any(x["code"] == it["code"] for x in items):
                    items.append(it)
        days = [{"date": d, "items": items} for d, items in sorted(norm.items())]
        merged = sum(len(d["items"]) for d in days)
        print(f"   交易日归一化: {len(days)} 天（合并重复快照，共 {merged} 条）")

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
    last_seen = {}
    for day in days:
        for it in day["items"]:
            c = it["code"]
            cnt[c] += 1
            latest[c] = it
            first_seen.setdefault(c, day["date"])
            last_seen[c] = day["date"]
    # 累计跟踪 = 首次上榜日收盘 → 最新收盘；最近跟踪 = 最后一次上榜日收盘 → 最新收盘
    first_ret, last_ret = {}, {}
    for day in days:
        for it in day["items"]:
            c = it["code"]
            if day["date"] == first_seen[c]:
                first_ret[c] = it["track_return"]
            if day["date"] == last_seen[c]:
                last_ret[c] = it["track_return"]
    total = len(days)
    stable = sorted(
        ({ "code": c, "name": latest[c]["name"], "sector": latest[c]["sector"],
           "count": n, "rate": round(n / total * 100) if total else 0,
           "first_day": first_seen[c], "latest_day": last_seen[c],
           "latest_score": latest[c]["total_score"], "latest_signal": latest[c]["signal_key"],
           "cum_track_return": first_ret.get(c), "latest_track_return": last_ret.get(c) }
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
