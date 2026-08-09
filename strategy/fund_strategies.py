#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
资金流/机构数据 → 策略回测（2026-08 新增）。

数据源：
- 龙虎榜机构专用席位（东财数据中心，5年历史）
- 沪深300/上证指数日K（腾讯，5年）
- 个股主力资金流（东财 fflow/kline，样本期约 120 交易日）
- 十大流通股东历史（东财数据中心，季度，覆盖股票池）

策略：
  A 龙虎榜机构净买入：每日机构专用净买入 TopN → 持有 1/5/10/20 日
  B 主力资金流选股：股票池每日主力净流入强度 TopN → 持有 5 日（样本期短，标注局限）
  C 沪深300 MA20 趋势择时：收盘>MA20 持有、否则空仓（5年）
  D 普涨过热反向：上证单日涨幅≥3%（普涨代理）后 1/5/10 日指数表现
  E 国家队增持：社保/汇金/证金/养老金 季度"增持/新进" → 下一季度收益（季度频率，样本少）

统计口径：胜率=单笔持有期收益>0 占比；累计收益=单笔等权复利（含成本）；
年化=按自然日年化；基准=同期沪深300。
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from src import data_provider as dp
from src import fund_flow as ff

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0 Safari/537.36"}
DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
COST = 0.0005  # 单边手续费+滑点


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _dc(report: str, page: int, page_size: int, filt: str = "",
        sort: str = "TRADE_DATE", sort_type: int = -1) -> dict:
    url = (f"{DATACENTER}?reportName={report}&columns=ALL&pageSize={page_size}&pageNumber={page}"
           f"&sortColumns={sort}&sortTypes={sort_type}")
    if filt:
        url += "&filter=" + urllib.parse.quote(filt)
    d = json.loads(_get(url))
    return (d.get("result") or {})


# ---------------- 策略 A：龙虎榜机构净买入（5年） ----------------

def fetch_billboard_inst(start: str, end: str, page_size: int = 500,
                         cache_path: str = "") -> dict:
    """按周拉取机构专用席位买入/卖出明细，返回 {date: {code: {buy, sell, net, close, chg}}}。

    支持断点续跑：cache_path 指向已落盘的 JSON，已完成的周窗口跳过。
    """
    out = defaultdict(dict)
    done = set()
    if cache_path and os.path.exists(cache_path):
        try:
            loaded = json.load(open(cache_path, encoding="utf-8"))
            for d, codes in loaded.items():
                out[d] = codes
            for d in loaded:
                done.add(d[:10])
        except Exception:
            pass
    d0 = datetime.strptime(start, "%Y-%m-%d")
    d1 = datetime.strptime(end, "%Y-%m-%d")
    cur = d0
    n_req = 0
    while cur <= d1:
        wk = cur + timedelta(days=6)
        wkey = cur.strftime("%Y-%m-%d")
        if wkey in done:
            cur = wk + timedelta(days=1)
            continue
        f = (f"(TRADE_DATE>='{cur:%Y-%m-%d}')(TRADE_DATE<='{wk:%Y-%m-%d}')"
             f'(OPERATEDEPT_NAME="机构专用")')
        for report in ("RPT_BILLBOARD_DAILYDETAILSBUY", "RPT_BILLBOARD_DAILYDETAILSSELL"):
            pg = 1
            while True:
                r = _dc(report, pg, page_size, f)
                rows = r.get("data") or []
                n_req += 1
                if not rows:
                    break
                for x in rows:
                    d = (x.get("TRADE_DATE") or "")[:10]
                    code = x.get("SECURITY_CODE")
                    if not d or not code:
                        continue
                    v = out[d].setdefault(code, {"buy": 0.0, "sell": 0.0,
                                                 "close": x.get("CLOSE_PRICE"),
                                                 "chg": x.get("CHANGE_RATE")})
                    v["buy"] += float(x.get("BUY") or 0)
                    v["sell"] += float(x.get("SELL") or 0)
                if len(rows) < page_size or (r.get("pages") or 1) <= pg:
                    break
                pg += 1
            time.sleep(0.25)
        for d in list(out.keys()):
            if wkey <= d[:10] <= wk.strftime("%Y-%m-%d"):
                done.add(wkey)
        if cache_path:
            try:
                json.dump(dict(out), open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
            except Exception:
                pass
        print(f"   周 {wkey} 完成（累计 {len(out)} 天）", flush=True)
        cur = wk + timedelta(days=1)
        if n_req > 350:
            print("   [warn] 请求数超过350，暂停30s防限流", flush=True)
            time.sleep(30)
    for d, codes in out.items():
        for code, v in codes.items():
            v["net"] = round(v["buy"] - v["sell"], 2)
    return dict(out)


_SINA_KLINE_CACHE = {}


def _sina_kline(code: str, count: int = 1400):
    """新浪日K（不复权，单请求长历史），带文件缓存。返回 {dates, closes}。"""
    if code in _SINA_KLINE_CACHE:
        return _SINA_KLINE_CACHE[code]
    cp = os.path.join(BASE_DIR, "data", "cache", f"sina_kline_{code}.json")
    if os.path.exists(cp):
        try:
            d = json.load(open(cp, encoding="utf-8"))
            if len(d.get("closes", [])) >= 500:
                _SINA_KLINE_CACHE[code] = d
                return d
        except Exception:
            pass
    sym = ("sh" if code.startswith(("6", "5", "9", "11")) else "sz") + code
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={count}")
    try:
        raw = _get(url, timeout=25)
        rows = json.loads(raw)
        if not isinstance(rows, list) or not rows:
            return None
        out = {"dates": [r["day"] for r in rows],
               "closes": [float(r["close"]) for r in rows]}
        try:
            json.dump(out, open(cp, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
        _SINA_KLINE_CACHE[code] = out
        return out
    except Exception:
        return None


_TENCENT_KLINE_CACHE = {}


def _tencent_kline_full(code: str, count: int = 1300):
    """腾讯前复权日K稳健拉取（本策略专用）：分页+重试背退，目标覆盖 count 根。

    腾讯批量限流时静默返回空页，这里对空页/异常做最多 3 次重试，页间间隔 0.5s。
    带本地文件缓存（key 含 count）。
    """
    if code in _TENCENT_KLINE_CACHE:
        return _TENCENT_KLINE_CACHE[code]
    cp = os.path.join(BASE_DIR, "data", "cache", f"bk_{code}_{count}.json")
    if os.path.exists(cp):
        try:
            d = json.load(open(cp, encoding="utf-8"))
            if len(d.get("closes", [])) >= count - 200:
                _TENCENT_KLINE_CACHE[code] = d
                return d
        except Exception:
            pass
    sym = dp.tencent_symbol(code)
    PAGE = 640
    rows_all = []
    end = datetime.now().strftime("%Y-%m-%d")
    hosts = ["https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
             "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"]
    hi = 0
    for _ in range(4):
        url = (f"{hosts[hi]}?param={sym},day,1990-01-01,{end},{PAGE},qfq")
        rows = []
        for attempt in range(3):
            try:
                raw = _get(url)
                node = (json.loads(raw).get("data") or {}).get(sym) or {}
                rows = node.get("qfqday") or node.get("day") or []
                if rows:
                    break
            except Exception:
                rows = []
            # 换备用域名再试（web.ifzq 可能被 WAF 拦截）
            hi = 1 - hi
            time.sleep(1.5 * (attempt + 1))
        if not rows:
            break
        rows_all = rows + rows_all
        first = rows[0][0]
        if len(rows) < PAGE:
            break
        end = first
        time.sleep(0.5)
    if not rows_all:
        return None
    out = {"dates": [r[0] for r in rows_all],
           "closes": [float(r[2]) for r in rows_all]}
    try:
        json.dump(out, open(cp, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    _TENCENT_KLINE_CACHE[code] = out
    return out


def _kline_close(code: str, date: str, hold_days: int):
    """date 之后第 hold_days 个交易日的收盘价；返回 (entry_close, exit_close, exit_date)。

    优先腾讯前复权长K线（稳健分页）；不足 900 根时用新浪不复权兜底。
    """
    k = _tencent_kline_full(code)
    if k is None or len(k.get("closes", [])) < 900:
        k = _sina_kline(code)
    if not k or not k.get("closes"):
        return None, None, None
    closes, dates = k["closes"], k["dates"]
    i = None
    for j in range(len(dates) - 1, -1, -1):
        if dates[j] <= date:
            i = j
            break
    if i is None:
        return None, None, None
    j = i + hold_days
    if j >= len(closes):
        return None, None, None
    return closes[i], closes[j], dates[j]


def backtest_billboard(data, hold_days: int = 5, top_n: int = 5,
                       min_net: float = 3e7) -> Dict:
    """每日机构净买入 TopN 买入持有 hold_days 日。"""
    trades = []
    dates = sorted(data.keys())
    for d in dates:
        picked = []
        for code, v in data[d].items():
            if v.get("net", 0) >= min_net:
                picked.append((code, v))
        picked.sort(key=lambda x: x[1]["net"], reverse=True)
        picks = picked[:top_n]
        for code, v in picks:
            entry, exit_, exit_date = _kline_close(code, d, hold_days)
            if entry is None or exit_ is None:
                continue
            ret = exit_ / entry - 1 - 2 * COST
            trades.append({"code": code, "date": d, "entry": entry, "exit": exit_,
                           "exit_date": exit_date, "ret": ret, "net": v["net"]})
    return _summarize(trades, "龙虎榜机构净买入 Top%d 持有%d日" % (top_n, hold_days))


# ---------------- 策略 C：沪深300 MA20 择时 ----------------

def backtest_trend_ma(index: str = "sh000300", ma: int = 20, start: str = "2021-08-01") -> Dict:
    k = dp.fetch_index_kline(index, count=1300)
    closes, dates = k["closes"], k["dates"]
    rets = []
    pos = False
    equity = 1.0
    dates_out = [d for d in dates if d >= start]
    for i, d in enumerate(dates):
        if d < start:
            continue
        if i < ma:
            continue
        ma_v = sum(closes[i - ma:i]) / ma
        sig = closes[i] > ma_v
        nxt = rets_today = 0.0
        if i + 1 < len(dates) and dates[i + 1] >= start:
            nxt_ret = closes[i + 1] / closes[i] - 1
            nxt = nxt_ret if sig else 0.0
            rets_today = nxt_ret
        rets.append({"date": d, "signal": 1 if sig else 0,
                     "strategy": nxt, "bench": rets_today})
    # 简化：按月统计胜率
    sret = [r["strategy"] for r in rets]
    brets = [r["bench"] for r in rets]
    def stats(rl):
        eq = 1.0
        for r in rl:
            eq *= (1 + r)
        days = len(rl)
        years = days / 244
        return {"cum": eq - 1, "annual": eq ** (1 / years) - 1 if years else 0,
                "win": sum(1 for r in rl if r > 0) / max(len(rl), 1),
                "n": len(rl)}
    return {"name": "沪深300 MA%d 趋势择时" % ma, "strategy": stats(sret),
            "benchmark": stats(brets), "trades": len(rets)}


# ---------------- 策略 D：普涨过热反向 ----------------

def backtest_overheat(index: str = "sh000001", thresholds=(0.015, 0.02, 0.03)) -> Dict:
    """上证单日涨幅≥threshold 视为普涨过热日，统计其后 1/5/10 日指数收益。"""
    k = dp.fetch_index_kline(index, count=1300)
    closes, dates = k["closes"], k["dates"]
    out = {"thresholds": {}}
    for th in thresholds:
        rows = []
        for i in range(1, len(dates)):
            chg = closes[i] / closes[i - 1] - 1
            if chg >= th:
                row = {"date": dates[i], "chg": chg}
                for hd in (1, 5, 10):
                    j = i + hd
                    row["fwd%d" % hd] = (closes[j] / closes[i] - 1) if j < len(closes) else None
                rows.append(row)
        t = {"threshold": th, "days": rows}
        for hd in (1, 5, 10):
            rl = [r["fwd%d" % hd] for r in rows if r["fwd%d" % hd] is not None]
            if rl:
                t["fwd%d" % hd] = {
                    "n": len(rl),
                    "avg": sum(rl) / len(rl),
                    "win": sum(1 for r in rl if r > 0) / len(rl),
                    "median": sorted(rl)[len(rl) // 2],
                }
        out["thresholds"][th] = t
    return out


# ---------------- 策略 B：主力资金流选股（样本期短） ----------------

def backtest_fflow_pick(hold_days: int = 5, top_n: int = 10,
                        max_days: int = 110) -> Dict:
    """股票池主力净流入 TopN 持有 hold_days 日。样本期为可获取的资金流历史长度。"""
    from src.stock_pool import WATCHLIST_CODES
    codes = WATCHLIST_CODES
    print(f"   拉取 {len(codes)} 只资金流历史…")
    h = {}
    for c in codes:
        f = ff.fetch_stock_fflow_history(c, days=0)
        if f:
            h[c] = f
        time.sleep(0.15)
    # 按日期对齐信号
    dates = sorted({d for f in h.values() if f for d in f.get("dates", [])})
    if len(dates) > max_days:
        dates = dates[-max_days:]
    trades = []
    for d in dates:
        strengths = []
        for c, f in h.items():
            t = f.get("today") if f else None
            if not t or t.get("date") != d:
                continue
            if t.get("main_net") is None or t.get("main_net") <= 0:
                continue
            strengths.append((c, t["main_net"]))
        strengths.sort(key=lambda x: x[1], reverse=True)
        for c, net in strengths[:top_n]:
            entry, exit_, exit_date = _kline_close(c, d, hold_days)
            if entry is None or exit_ is None:
                continue
            trades.append({"code": c, "date": d, "ret": exit_ / entry - 1 - 2 * COST,
                           "net": net, "exit_date": exit_date})
    return _summarize(trades, "主力资金流选股 Top%d 持有%d日（样本期 %s~%s，共 %d 日）"
                      % (top_n, hold_days, dates[0] if dates else "-", dates[-1] if dates else "-", len(dates)))


# ---------------- 策略 E：国家队增持（季度） ----------------

def fetch_holders_history(code: str) -> list:
    """股票十大流通股东全历史（东财数据中心，分页拉全）。"""
    rows_all = []
    pg = 1
    while True:
        filt = f'(SECURITY_CODE="{code}")'
        r = _dc("RPT_F10_EH_FREEHOLDERS", pg, 100, filt, sort="END_DATE", sort_type=-1)
        rows = r.get("data") or []
        if not rows:
            break
        rows_all += rows
        if len(rows) < 100 or (r.get("pages") or 1) <= pg:
            break
        pg += 1
        time.sleep(0.2)
    return rows_all


def backtest_national_picks() -> Dict:
    """季度口径：社保/汇金/证金/养老金 增持（持股数环比增加）或新进 → 季报截止日+60交易日收益。"""
    from src.stock_pool import WATCHLIST_CODES, MARKET_POOL_CODES
    codes = list(dict.fromkeys(WATCHLIST_CODES + MARKET_POOL_CODES))
    nat_kw = ("社保", "汇金", "证券金融", "养老")
    trades = []
    print(f"   扫描 {len(codes)} 只十大流通股东历史…", flush=True)
    for c in codes:
        try:
            rows = fetch_holders_history(c)
        except Exception:
            continue
        # 按持有者分组，按期排序，计算环比持股变化
        holders = defaultdict(list)
        for r in rows:
            name = r.get("HOLDER_NAME") or ""
            if not any(k in name for k in nat_kw):
                continue
            end = (r.get("END_DATE") or "")[:10]
            if not end or end < "2021-01-01":
                continue
            holders[name].append({"end": end, "num": r.get("HOLD_NUM"),
                                  "chg": r.get("HOLD_NUM_CHANGE") or r.get("HOLD_CHANGE") or ""})
        for name, qs in holders.items():
            qs.sort(key=lambda x: x["end"])
            for i, q in enumerate(qs):
                prev = qs[i - 1] if i > 0 else None
                num = q["num"]
                is_new = q["chg"] == "新进" and prev is None
                is_inc = False
                if prev and prev.get("num") is not None and num is not None:
                    try:
                        is_inc = float(num) > float(prev["num"]) * 1.001
                    except (TypeError, ValueError):
                        is_inc = False
                if not (is_new or is_inc):
                    continue
                entry, exit_, exit_date = _kline_close(c, q["end"], 60)
                if entry is None or exit_ is None:
                    continue
                trades.append({"code": c, "date": q["end"], "holder": name,
                               "chg": "新进" if is_new else "增持",
                               "ret": exit_ / entry - 1 - 2 * COST, "exit_date": exit_date})
        time.sleep(0.1)
    return _summarize(trades, "国家队(社保/汇金/证金/养老)季度增持/新进 → 下季度")


# ---------------- 汇总 ----------------

def _summarize(trades, name: str) -> Dict:
    if not trades:
        return {"name": name, "trades": 0}
    n = len(trades)
    wins = sum(1 for t in trades if t["ret"] > 0)
    rets = [t["ret"] for t in trades]
    rets_sorted = sorted(rets)
    dates = [t["date"] for t in trades if t.get("date")]
    span_days = 0
    if len(dates) >= 2:
        try:
            span_days = (datetime.strptime(max(dates), "%Y-%m-%d")
                         - datetime.strptime(min(dates), "%Y-%m-%d")).days
        except ValueError:
            span_days = 0
    years = span_days / 365 if span_days else 1
    # 按日分组等权组合（实际可执行口径）：同日多笔取平均，逐日复利
    by_day = {}
    for t in trades:
        by_day.setdefault(t["date"], []).append(t["ret"])
    day_rets = [sum(v) / len(v) for v in by_day.values()]
    eq = 1.0
    for r in day_rets:
        eq *= (1 + r)
    annual = (eq ** (1 / years) - 1) if years and eq > 0 else 0
    day_wins = sum(1 for r in day_rets if r > 0)
    return {"name": name, "trades": n, "win_rate": wins / n,
            "avg_ret": sum(rets) / n, "median_ret": rets_sorted[n // 2],
            "day_cum": eq - 1, "day_annual": annual, "day_win_rate": day_wins / len(day_rets),
            "day_count": len(day_rets), "span_days": span_days,
            "best": max(rets), "worst": min(rets),
            "sample": trades[:5]}


def run_all():
    print("== 策略回测（资金流/机构）==")
    results = {}

    print("== 拉取龙虎榜机构历史（5年，断点续跑）==", flush=True)
    bb_path = os.path.join(BASE_DIR, "data", "billboard_inst_history.json")
    bb = fetch_billboard_inst("2021-08-09", "2026-08-07", cache_path=bb_path)
    days = len(bb)
    codes = len({c for d in bb.values() for c in d})
    print(f"   交易日 {days} 天，机构参与股票 {codes} 只", flush=True)
    for hd in (1, 5, 10, 20):
        results["A_%dd" % hd] = backtest_billboard(bb, hold_days=hd)

    print("== 指数择时 / 普涨过热 ==")
    results["C_ma20"] = backtest_trend_ma()
    results["D_overheat"] = backtest_overheat()

    print("== 主力资金流选股（样本期有限）==")
    try:
        results["B_fflow"] = backtest_fflow_pick()
    except Exception as e:
        print("   [skip] 资金流选股:", e)

    print("== 国家队季度增持 ==")
    try:
        results["E_national"] = backtest_national_picks()
    except Exception as e:
        print("   [skip] 国家队增持:", e)

    print()
    print("=" * 78)
    for k, v in results.items():
        if "thresholds" in v:
            for th, t in v["thresholds"].items():
                print(f"\n【普涨过热（上证单日涨幅≥{th*100:.1f}%）】过热日 {len(t.get('days', []))} 次")
                for hd in (1, 5, 10):
                    s = t.get("fwd%d" % hd)
                    if s:
                        print(f"   后{hd}日: 平均 {s['avg']*100:+.2f}%  胜率 {s['win']*100:.0f}%  中位数 {s['median']*100:+.2f}%  (n={s['n']})")
            continue
        if "benchmark" in v:
            b = v["benchmark"]
            print(f"\n【{v['name']}】")
            print(f"   择时版: 累计 {v['strategy']['cum']*100:+.1f}% | 年化 {v['strategy']['annual']*100:+.1f}% | 胜率(日) {v['strategy']['win']*100:.0f}%")
            print(f"   对比满仓基准: 累计 {b['cum']*100:+.1f}% | 年化 {b['annual']*100:+.1f}% | 胜率(日) {b['win']*100:.0f}%")
            continue
        print(f"\n【{v['name']}】")
        if v.get("trades", 0) == 0:
            print("   无有效交易")
            continue
        print(f"   单笔: 胜率 {v['win_rate']*100:.1f}% | 平均 {v['avg_ret']*100:+.2f}% | 中位 {v['median_ret']*100:+.2f}% (n={v['trades']})")
        print(f"   每日等权组合: 胜率 {v['day_win_rate']*100:.0f}% | 累计 {v['day_cum']*100:+.1f}% | 年化 {v['day_annual']*100:+.1f}% | 周期 {v['span_days']} 天")
        print(f"   单笔最好 {v['best']*100:+.1f}% / 最差 {v['worst']*100:+.1f}%")
    json.dump(results,
              open(os.path.join(BASE_DIR, "data", "strategy_results.json"), "w"),
              ensure_ascii=False, indent=1)


if __name__ == "__main__":
    run_all()
