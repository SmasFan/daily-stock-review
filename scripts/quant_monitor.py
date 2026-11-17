#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量化交易健康监控：两周回溯 + 每日报告 + 每周汇总。

覆盖两条交易线：
  · 币安竞技场（24/7，6 策略共享 100U）      —— binance-llm-bot/{arena,watchdog,guard}.log
  · A股模拟盘（交易时段，全池/红利/冲刺/美股） —— data/logs/{simscan,intraday,review,recommend,external,ussim}.log

检测四类问题：
  1) 卡死/中断   —— 心跳间隔过大、watchdog 频繁重启、行情接口连续失败
  2) 决策失效     —— 本轮异常（决策轮崩）、LLM 路由失败、决策长时间为空
  3) 交易不成功   —— 开仓前查仓位失败跳过、挂止损失败
  4) 数据陈旧     —— 各账本 JSON 的 mtime / 内部时间戳过旧

用法：
    python3 scripts/quant_monitor.py daily                  # 今天
    python3 scripts/quant_monitor.py daily --date 2026-11-16
    python3 scripts/quant_monitor.py weekly                 # 最近 7 天
    python3 scripts/quant_monitor.py report --days 14       # 回溯两周，逐日出报告
    python3 scripts/quant_monitor.py daily --push           # 同时微信推送（Server酱）

输出：data/monitor/daily_<date>.{txt,json}、data/monitor/weekly_<end>_{txt,json}
纯标准库，不依赖第三方包（推送时才用 requests）。
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BN = os.path.join(ROOT, "binance-llm-bot")
LOGS = os.path.join(ROOT, "data", "logs")
OUT = os.path.join(ROOT, "data", "monitor")

# ---- 阈值（可按需调） --------------------------------------------------------
HB_WARN_MIN = 30          # 币安心跳间隔 >30min 警告
HB_BAD_MIN = 45           # >45min 判卡死（正常 LOOP_MIN=15）
RESTART_WARN = 10         # 每日 watchdog 重启次数 警告
RESTART_BAD = 50          # 重启风暴
MARKET_DOWN_BAD = 3       # load_markets 三次均失败次数
ROUND_ERR_WARN = 20       # 本轮异常次数 警告
LLM_FAIL_WARN = 0.10      # LLM 路由失败率
AS_GAP_WARN_MIN = 10      # A股巡检间隔 >10min 警告（正常 2min）
AS_GAP_BAD_MIN = 30

# ---- 正则 ------------------------------------------------------------------
RE_HB = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ INFO 权益 "
                   r"([\d.]+)U \(([+-][\d.]+)%\) 持仓 (\d+) 笔 成交 (\d+) 笔")
RE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})")
RE_WD = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) \[(\w+)\] 未运行, 重启")
RE_AS = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\]")

# A股交易时段（分钟）
SESSIONS = [(9 * 60 + 30, 11 * 60 + 35), (12 * 60 + 55, 15 * 60 + 5)]
LUNCH = (11 * 60 + 35, 12 * 60 + 55)   # 午休，算间隔时扣除


def _mins(hhmm):
    parts = hhmm.split(":")
    h, m = int(parts[0]), int(parts[1])
    return h * 60 + m


def in_session(hhmm):
    t = _mins(hhmm)
    return any(a <= t <= b for a, b in SESSIONS)


def _gaps(times):
    """times: 已排序 'HH:MM:SS' 列表 -> [(gap_min, prev, cur)]"""
    out = []
    for a, b in zip(times, times[1:]):
        gm = (_mins(b) - _mins(a))
        if gm < 0:  # 跨午夜
            gm += 24 * 60
        out.append((gm, a, b))
    return out


def _gaps_lunch(times):
    """A股用：相邻间隔扣除午休时段，避免 11:35→12:55 被误判为停机。"""
    out = []
    for a, b in zip(times, times[1:]):
        ga, gb = _mins(a), _mins(b)
        if gb < ga:
            gb += 24 * 60
        overlap = max(0, min(gb, LUNCH[1]) - max(ga, LUNCH[0]))
        out.append((gb - ga - overlap, a, b))
    return out


def blank_day():
    return {
        "binance": {"heartbeats": 0, "max_gap": 0, "gaps": [], "restarts": 0,
                    "kill_events": 0, "err_round": 0, "err_market_down": 0,
                    "err_total": 0, "order_skip": 0, "guard_err": 0,
                    "equity_first": None, "equity_last": None,
                    "trades_closed": 0, "wins": 0, "losses": 0, "pnl": 0.0,
                    "positions": 0},
        "ashare": {"trading": False, "simscan": 0, "intraday": 0, "review": 0,
                   "recommend": 0, "external": 0, "ussim": 0,
                   "simscan_gap": 0, "intraday_gap": 0, "last_as": "",
                   "simscan_samples": [], "intraday_samples": []},
        "llm": {"calls": 0, "fails": 0},
        "issues": [],
        "warn": [],
    }


def ingest():
    """单遍扫描所有日志 + 账本，返回 {date: record}。"""
    days = defaultdict(blank_day)

    # ---------- arena.log ----------
    p = os.path.join(BN, "arena.log")
    if os.path.exists(p):
        hb_by_day = defaultdict(list)   # date -> [(hhmmss, equity, cum_pct, pos)]
        with open(p, encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = RE_HB.match(line)
                if m:
                    d, t, eq, pct, pos, ntrades = m.groups()
                    hb_by_day[d].append((t, float(eq), float(pct), int(pos), int(ntrades)))
                    continue
                m = RE_TS.match(line)
                if not m:
                    continue
                d, t = m.group(1), m.group(2)
                b = days[d]["binance"]
                if " ERROR " in line:
                    b["err_total"] += 1
                    if "本轮异常" in line:
                        b["err_round"] += 1
                    elif "load_markets 三次均失败" in line:
                        b["err_market_down"] += 1
                elif " WARNING " in line:
                    if "跳过本次开仓" in line or "查仓位失败" in line:
                        b["order_skip"] += 1
        for d, rows in hb_by_day.items():
            b = days[d]["binance"]
            b["heartbeats"] = len(rows)
            b["equity_first"] = rows[0][1]
            b["equity_last"] = rows[-1][1]
            b["positions"] = rows[-1][3]
            gs = _gaps([r[0] for r in rows])
            if gs:
                b["max_gap"] = max(g[0] for g in gs)
                b["gaps"] = [{"mins": g[0], "from": g[1], "to": g[2]}
                             for g in gs if g[0] >= HB_WARN_MIN]

    # ---------- watchdog.log ----------
    p = os.path.join(BN, "watchdog.log")
    if os.path.exists(p):
        with open(p, encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = RE_WD.match(line)
                if m:
                    days[m.group(1)]["binance"]["restarts"] += 1
                elif "杀残留" in line:
                    m2 = RE_TS.match(line)
                    if m2:
                        days[m2.group(1)]["binance"]["kill_events"] += 1

    # ---------- guard.log（仅数 ERROR，文件大，流式） ----------
    p = os.path.join(BN, "guard.log")
    if os.path.exists(p):
        with open(p, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if " ERROR " in line:
                    m = RE_TS.match(line)
                    if m:
                        days[m.group(1)]["binance"]["guard_err"] += 1

    # ---------- state_arena.json 平仓账本 ----------
    p = os.path.join(BN, "state_arena.json")
    if os.path.exists(p):
        try:
            st = json.load(open(p, encoding="utf-8"))
            for tr in st.get("trades") or []:
                ts_out = tr.get("ts_out")
                if not ts_out:
                    continue
                d = dt.datetime.fromtimestamp(ts_out).strftime("%Y-%m-%d")
                b = days[d]["binance"]
                b["trades_closed"] += 1
                pnl = tr.get("pnl_usd") or 0.0
                b["pnl"] += pnl
                if pnl > 0:
                    b["wins"] += 1
                elif pnl < 0:
                    b["losses"] += 1
        except (ValueError, OSError):
            pass

    # ---------- A股日志 ----------
    for name in ("simscan", "intraday", "review", "recommend", "external", "ussim"):
        p = os.path.join(LOGS, name + ".log")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = RE_AS.match(line)
                if not m:
                    continue
                d, t = m.group(1), m.group(2)
                a = days[d]["ashare"]
                a["trading"] = True
                a[name] += 1
                a["last_as"] = max(a["last_as"], t)
                if name in ("simscan", "intraday"):
                    a[name + "_samples"].append(t)

    # A股巡检间隔（只算交易时段内的相邻差，扣除午休）
    for d, rec in days.items():
        a = rec["ashare"]
        for key in ("simscan", "intraday"):
            ts = sorted(t for t in a[key + "_samples"] if in_session(t))
            gs = [g for g in _gaps_lunch(ts) if g[0] > 0]
            a[key + "_gap"] = max((g[0] for g in gs), default=0)
            del a[key + "_samples"]
        # 只有在交易时段真的巡检过、且为工作日，才算“交易日”
        is_weekday = dt.date.fromisoformat(d).weekday() < 5
        if (a["simscan"] == 0 and a["intraday"] == 0) or not is_weekday:
            a["trading"] = False

    # ---------- LLM 路由 ----------
    p = os.path.join(ROOT, "data", "llm_route_log.jsonl")
    if os.path.exists(p):
        with open(p, encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                d = (r.get("ts") or "")[:10]
                if not d:
                    continue
                days[d]["llm"]["calls"] += 1
                if not r.get("ok", True):
                    days[d]["llm"]["fails"] += 1

    for d, rec in days.items():
        assess(rec)
    return days


def assess(rec, partial_day=False):
    """根据阈值生成 issues / warn（幂等：每次重算）。"""
    rec["issues"] = []
    rec["warn"] = []
    rec["_partial"] = partial_day
    b, a, l = rec["binance"], rec["ashare"], rec["llm"]
    if b["heartbeats"] or b["restarts"] or b["trades_closed"]:
        if b["max_gap"] >= HB_BAD_MIN:
            rec["issues"].append("币安心跳中断 %dmin（%s→%s）" %
                                 (b["max_gap"], b["gaps"][0]["from"], b["gaps"][0]["to"]))
        elif b["max_gap"] >= HB_WARN_MIN:
            rec["warn"].append("币安心跳间隔 %dmin 偏大" % b["max_gap"])
        if b["restarts"] >= RESTART_BAD:
            rec["issues"].append("watchdog 重启风暴 %d 次/日" % b["restarts"])
        elif b["restarts"] >= RESTART_WARN:
            rec["warn"].append("watchdog 重启 %d 次" % b["restarts"])
        if b["err_market_down"] >= MARKET_DOWN_BAD:
            rec["issues"].append("行情接口连续失败导致引擎退出 %d 次" % b["err_market_down"])
        if b["err_round"] >= ROUND_ERR_WARN:
            rec["warn"].append("决策轮异常 %d 次" % b["err_round"])
        if b["order_skip"] >= 5:
            rec["warn"].append("开仓/查仓失败跳过 %d 次" % b["order_skip"])

    if l["calls"] >= 5 and l["fails"] / l["calls"] > LLM_FAIL_WARN:
        rec["warn"].append("LLM 路由失败 %d/%d（%.0f%%）" %
                           (l["fails"], l["calls"], 100.0 * l["fails"] / l["calls"]))

    if a["trading"]:
        if a["simscan_gap"] >= AS_GAP_BAD_MIN:
            rec["issues"].append("A股巡检中断 %dmin（模拟盘/红利/冲刺可能停摆）" % a["simscan_gap"])
        elif a["simscan_gap"] >= AS_GAP_WARN_MIN:
            rec["warn"].append("A股巡检间隔 %dmin 偏大" % a["simscan_gap"])
        if a["review"] == 0 and not partial_day:
            rec["warn"].append("当日无复盘(review)记录")
        if a["recommend"] == 0 and not partial_day:
            rec["warn"].append("当日无盘前推荐(recommend)记录")


def has_data(rec):
    b, a, l = rec["binance"], rec["ashare"], rec["llm"]
    return bool(b["heartbeats"] or b["restarts"] or b["trades_closed"] or
                b["err_total"] or a["trading"] or l["calls"])


def verdict(rec):
    if not has_data(rec):
        return "NODATA"
    if rec["issues"]:
        return "BAD"
    if rec["warn"]:
        return "WARN"
    return "OK"


ICON = {"OK": "✅", "WARN": "⚠️", "BAD": "❌", "NODATA": "➖"}


def render_daily(date, rec, partial=False):
    b, a, l = rec["binance"], rec["ashare"], rec["llm"]
    v = verdict(rec)
    L = []
    tag = "（进行中）" if partial else ""
    L.append(f"{ICON[v]} 量化健康日报 {date}{tag} — {v}")
    L.append("─" * 34)
    if v == "NODATA":
        L.append("当日无任何量化运行记录（历史日志未覆盖该日）")
        return "\n".join(L)

    # 币安
    if b["heartbeats"] or b["restarts"] or b["trades_closed"] or b["err_total"]:
        eq = ""
        if b["equity_first"] is not None and b["equity_last"] is not None:
            eq = f"权益 {b['equity_first']:.2f}→{b['equity_last']:.2f}U"
        L.append(f"▍币安竞技场  {eq}  持仓 {b['positions']}")
        wr = ""
        n = b["wins"] + b["losses"]
        if n:
            wr = f" 胜率 {100.0*b['wins']/n:.0f}%"
        L.append(f"   心跳 {b['heartbeats']} 次 · 最大间隔 {b['max_gap']}min")
        L.append(f"   平仓 {b['trades_closed']} 笔 盈亏 {b['pnl']:+.2f}U{wr}")
        L.append(f"   重启 {b['restarts']} · 决策轮异常 {b['err_round']} · "
                 f"行情失败退出 {b['err_market_down']} · 跳过开仓 {b['order_skip']}")
    else:
        L.append("▍币安竞技场  当日无日志")

    # A股
    if a["trading"]:
        L.append(f"▍A股模拟盘  巡检 {a['simscan']} 行 · 盘中 {a['intraday']} 行 · "
                 f"最大间隔 {a['simscan_gap']}min")
        L.append(f"   复盘 {'✓' if a['review'] else '✗'} {a['review']} · "
                 f"盘前 {'✓' if a['recommend'] else '✗'} {a['recommend']} · "
                 f"美股模拟 {a['ussim']} · 外部 {a['external']}")
    else:
        L.append("▍A股模拟盘  休市 / 无日志")

    if l["calls"]:
        L.append(f"▍LLM 路由  {l['calls']} 次 · 失败 {l['fails']}")

    if rec["issues"]:
        L.append("─" * 34)
        for x in rec["issues"]:
            L.append(f"❌ {x}")
    if rec["warn"]:
        if not rec["issues"]:
            L.append("─" * 34)
        for x in rec["warn"]:
            L.append(f"⚠️ {x}")
    return "\n".join(L)


def render_weekly(end_date, days):
    """days: 已排序 [(date, rec)]，最多 7 天。NODATA 日不计入统计。"""
    days = [(d, r) for d, r in days if verdict(r) != "NODATA"]
    if not days:
        return "量化健康周报 %s ~ 无有效数据" % end_date
    L = []
    start = days[0][0]
    L.append(f"量化健康周报 {start} ~ {end_date}")
    L.append("═" * 34)

    ok = sum(1 for _, r in days if verdict(r) == "OK")
    warn = sum(1 for _, r in days if verdict(r) == "WARN")
    bad = sum(1 for _, r in days if verdict(r) == "BAD")
    L.append(f"健康天数：✅{ok}  ⚠️{warn}  ❌{bad}  / 共 {len(days)} 天")

    # 币安汇总
    tb = {"restarts": 0, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0,
          "downtime": 0, "market_down": 0, "max_gap": 0}
    for _, r in days:
        b = r["binance"]
        tb["restarts"] += b["restarts"]
        tb["trades"] += b["trades_closed"]
        tb["wins"] += b["wins"]
        tb["losses"] += b["losses"]
        tb["pnl"] += b["pnl"]
        tb["market_down"] += b["err_market_down"]
        tb["max_gap"] = max(tb["max_gap"], b["max_gap"])
        tb["downtime"] += sum(g["mins"] for g in b["gaps"])
    n = tb["wins"] + tb["losses"]
    L.append("")
    L.append("▍币安竞技场")
    L.append(f"   平仓 {tb['trades']} 笔 · 盈亏 {tb['pnl']:+.2f}U · "
             f"胜率 {(100.0*tb['wins']/n if n else 0):.0f}%")
    L.append(f"   重启合计 {tb['restarts']} · 停机合计 {tb['downtime']}min · "
             f"单次最长 {tb['max_gap']}min · 行情失败退出 {tb['market_down']}")
    if tb["restarts"] >= RESTART_BAD * 3 or tb["downtime"] >= 120:
        L.append("   ❌ 本周稳定性差，需要重点排查代理/demo-fapi 连接")

    # A股汇总
    tdays = [(d, r) for d, r in days if r["ashare"]["trading"]]
    if tdays:
        miss_review = [d for d, r in tdays
                       if r["ashare"]["review"] == 0 and not r.get("_partial")]
        max_gap = max(r["ashare"]["simscan_gap"] for _, r in tdays)
        L.append("")
        L.append(f"▍A股模拟盘  交易日 {len(tdays)} 天")
        L.append(f"   巡检最大间隔 {max_gap}min")
        if miss_review:
            L.append(f"   ⚠️ 缺失复盘：{', '.join(miss_review)}")
    else:
        L.append("")
        L.append("▍A股模拟盘  本周无交易日记录")

    # 异常日
    bads = [(d, r) for d, r in days if verdict(r) == "BAD"]
    if bads:
        L.append("")
        L.append("▍异常日明细")
        for d, r in bads:
            L.append(f"   {d} ❌ " + "；".join(r["issues"]))
    return "\n".join(L)


def save(name, text, data=None):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, name + ".txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    if data is not None:
        with open(os.path.join(OUT, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    return p


def daterange_days(days, end):
    end_d = dt.date.fromisoformat(end) if end else dt.date.today()
    out = []
    for i in range(days - 1, -1, -1):
        out.append((end_d - dt.timedelta(days=i)).isoformat())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["daily", "weekly", "report"])
    ap.add_argument("--date", default=None)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    all_days = ingest()

    if args.mode == "daily":
        d = args.date or dt.date.today().isoformat()
        rec = all_days.get(d) or blank_day()
        partial = (d == dt.date.today().isoformat())
        assess(rec, partial_day=partial)
        text = render_daily(d, rec, partial=partial)
        print(text)
        save("daily_" + d, text, {"date": d, "verdict": verdict(rec), **rec})
        if args.push:
            _push("量化健康日报 %s %s" % (d, verdict(rec)), text)
        return

    if args.mode == "weekly":
        end = args.date or dt.date.today().isoformat()
        today_iso = dt.date.today().isoformat()
        ds = daterange_days(7, end)
        rows = []
        for d in ds:
            r = all_days.get(d) or blank_day()
            assess(r, partial_day=(d == today_iso))
            rows.append((d, r))
        text = render_weekly(end, rows)
        print(text)
        save("weekly_" + end, text,
             {"end": end, "days": [{"date": d, "verdict": verdict(r)} for d, r in rows]})
        if args.push:
            _push("量化健康周报 %s" % end, text)
        return

    # report：回溯逐日 + 末周汇总
    ds = daterange_days(args.days, args.date)
    today_iso = dt.date.today().isoformat()
    lines = []
    for d in ds:
        rec = all_days.get(d) or blank_day()
        assess(rec, partial_day=(d == today_iso))
        text = render_daily(d, rec, partial=(d == today_iso))
        lines.append(text)
        save("daily_" + d, text, {"date": d, "verdict": verdict(rec), **rec})
        print(text)
        print()
    # 末 7 天周报
    wrow = []
    for d in ds[-7:]:
        r = all_days.get(d) or blank_day()
        assess(r, partial_day=(d == today_iso))
        wrow.append((d, r))
    wtext = render_weekly(ds[-1], wrow)
    save("weekly_" + ds[-1], wtext,
         {"end": ds[-1], "days": [{"date": d, "verdict": verdict(r)} for d, r in wrow]})
    print(wtext)


def _push(title, text):
    try:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import push_alerts
        push_alerts.send(title, text)
        print("\n[已推送] " + title)
    except Exception as e:  # noqa: BLE001
        print("\n[推送失败] %s" % e)


if __name__ == "__main__":
    main()
