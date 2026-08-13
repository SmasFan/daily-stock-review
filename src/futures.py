# -*- coding: utf-8 -*-
"""
有色金属期货数据模块（新浪期货日线）。

数据源：新浪 finance.sina.com.cn 期货日线 JSONP 接口
  https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var _p=/
    InnerFuturesNewService.getDailyKLine?symbol=CU0
返回字段：{d 日期, o 开, h 高, l 低, c 收, v 成交量, p 持仓量, s 结算}

覆盖 SHFE（上海期货交易所）主要有色金属品种主力连续合约：
  CU0 沪铜 / AL0 沪铝 / ZN0 沪锌 / PB0 沪铅 / NI0 沪镍 / SN0 沪锡
  AU0 沪金 / AG0 沪银 / LC0 碳酸锂（GFEX 广州期货交易所）
"""
import json
import os
import re
import sqlite3
import time
import urllib.request
from collections import OrderedDict
from datetime import datetime
from typing import Dict, List, Optional

from . import indicators as ind

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "cache")
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(CACHE_DIR, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}

# 有色金属主力连续合约（主力合约0；沪=上海期货交易所 SHFE）
METALS: List[Dict[str, str]] = [
    {"symbol": "CU0", "name": "沪铜", "en": "Copper", "unit": "元/吨", "exchange": "SHFE"},
    {"symbol": "AL0", "name": "沪铝", "en": "Aluminum", "unit": "元/吨", "exchange": "SHFE"},
    {"symbol": "ZN0", "name": "沪锌", "en": "Zinc", "unit": "元/吨", "exchange": "SHFE"},
    {"symbol": "PB0", "name": "沪铅", "en": "Lead", "unit": "元/吨", "exchange": "SHFE"},
    {"symbol": "NI0", "name": "沪镍", "en": "Nickel", "unit": "元/吨", "exchange": "SHFE"},
    {"symbol": "SN0", "name": "沪锡", "en": "Tin", "unit": "元/吨", "exchange": "SHFE"},
    {"symbol": "AU0", "name": "沪金", "en": "Gold", "unit": "元/克", "exchange": "SHFE"},
    {"symbol": "AG0", "name": "沪银", "en": "Silver", "unit": "元/千克", "exchange": "SHFE"},
    {"symbol": "LC0", "name": "碳酸锂", "en": "Lithium Carbonate", "unit": "元/吨", "exchange": "GFEX"},
]

# 有色股票池（A股个股/ETF，对应期货品种联动；sector 用于宏观映射）
# 来自自选 watchlist 的有色相关标的：铜/铝/锌/镍/锡/金/银/锂/稀土
METALS_STOCKS: List[Dict[str, str]] = [
    {"code": "601899", "name": "紫金矿业", "link": "沪铜/沪金", "sector": "周期资源"},
    {"code": "600362", "name": "江西铜业", "link": "沪铜", "sector": "周期资源"},
    {"code": "000630", "name": "铜陵有色", "link": "沪铜", "sector": "周期资源"},
    {"code": "000737", "name": "北方铜业", "link": "沪铜", "sector": "周期资源"},
    {"code": "601600", "name": "中国铝业", "link": "沪铝", "sector": "周期资源"},
    {"code": "601212", "name": "白银有色", "link": "沪银/沪铜", "sector": "周期资源"},
    {"code": "600988", "name": "赤峰黄金", "link": "沪金", "sector": "周期资源"},
    {"code": "600916", "name": "中国黄金", "link": "沪金", "sector": "周期资源"},
    {"code": "601069", "name": "西部黄金", "link": "沪金", "sector": "周期资源"},
    {"code": "002155", "name": "湖南黄金", "link": "沪金/沪锑", "sector": "周期资源"},
    {"code": "600111", "name": "北方稀土", "link": "稀土", "sector": "周期资源"},
    {"code": "000792", "name": "盐湖股份", "link": "碳酸锂/钾", "sector": "周期资源"},
    {"code": "000408", "name": "藏格矿业", "link": "碳酸锂", "sector": "周期资源"},
    {"code": "002466", "name": "天齐锂业", "link": "碳酸锂", "sector": "新能源电力"},
    {"code": "002240", "name": "盛新锂能", "link": "碳酸锂", "sector": "新能源电力"},
    {"code": "159934", "name": "黄金ETF易方达", "link": "沪金", "sector": "周期资源"},
    {"code": "517400", "name": "黄金股ETF国泰", "link": "沪金", "sector": "周期资源"},
    {"code": "161226", "name": "国投白银LOF", "link": "沪银", "sector": "周期资源"},
    {"code": "159980", "name": "有色ETF大成", "link": "有色金属指数", "sector": "周期资源"},
    {"code": "512400", "name": "有色金属ETF南方", "link": "有色金属指数", "sector": "周期资源"},
]

METALS_STOCK_CODES = [m["code"] for m in METALS_STOCKS]


def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, f"metals_{key}.json")


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def fetch_metal_kline(symbol: str, count: int = 600, use_cache: bool = True,
                      cache_max_age_hours: int = 20) -> Optional[Dict]:
    """拉取期货主力连续日K线，升序返回 OHLCV。带本地缓存。"""
    cp = _cache_path(f"{symbol}_{count}")
    if use_cache and os.path.exists(cp):
        age = time.time() - os.path.getmtime(cp)
        if age < cache_max_age_hours * 3600:
            try:
                with open(cp, "r", encoding="utf-8") as fp:
                    return json.load(fp)
            except Exception:
                pass
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_p=/"
           f"InnerFuturesNewService.getDailyKLine?symbol={symbol}")
    try:
        raw = _get(url)
        # JSONP: /*<script>...</script>*/\nvar _p=([{...},{...},...]);
        # 直接定位 var _p=( 后的 JSON 数组，避免正则结尾符号干扰
        key = "var _p=("
        start = raw.find(key)
        if start < 0:
            print(f"[metals] {symbol} 解析失败: response 无 JSONP 体")
            return None
        end = raw.rfind(");", start)
        if end < 0:
            end = len(raw)
        json_text = raw[start + len(key):end].strip()
        rows = json.loads(json_text)
        if not rows:
            return None
        dates, opens, closes, highs, lows, vols = [], [], [], [], [], []
        for r in rows:
            dates.append(r.get("d", ""))
            opens.append(float(r.get("o", 0) or 0))
            highs.append(float(r.get("h", 0) or 0))
            lows.append(float(r.get("l", 0) or 0))
            closes.append(float(r.get("c", 0) or 0))
            vols.append(float(r.get("v", 0) or 0))
        out = {"dates": dates, "opens": opens, "closes": closes,
               "highs": highs, "lows": lows, "volumes": vols}
        try:
            with open(cp, "w", encoding="utf-8") as fp:
                json.dump(out, fp)
        except Exception:
            pass
        return out
    except Exception as e:
        print(f"[metals] {symbol} 拉取失败: {e}")
        return None


def _trend_status(ma5: Optional[float], ma20: Optional[float], ma60: Optional[float]) -> str:
    """简化趋势档：MA 排列判断多头/空头/震荡。"""
    if ma20 is None or ma60 is None:
        return "数据不足"
    if ma5 is None:
        return "震荡"
    if ma5 > ma20 > ma60:
        return "强势多头"
    if ma5 > ma20 and ma20 < ma60:
        return "转多"
    if ma5 < ma20 < ma60:
        return "强势空头"
    if ma5 < ma20 and ma20 > ma60:
        return "转空"
    return "震荡"


def _range_position(price: float, high: float, low: float) -> Optional[float]:
    """(price - low) / (high - low) ∈ [0,1]，区间内位置。"""
    if high <= low:
        return 0.5
    return max(0.0, min(1.0, (price - low) / (high - low)))


def analyze_metal(meta: Dict[str, str], kline_days: int = 600, chart_points: int = 250) -> Optional[Dict]:
    """对单品种分析并生成前端数据。"""
    sym, name = meta["symbol"], meta["name"]
    k = fetch_metal_kline(sym, count=kline_days)
    if not k or len(k["closes"]) < 60:
        return None

    dates = k["dates"]; opens = k["opens"]; closes = k["closes"]
    highs = k["highs"]; lows = k["lows"]; vols = k["volumes"]

    ma5 = ind.sma(closes, 5); ma10 = ind.sma(closes, 10)
    ma20 = ind.sma(closes, 20); ma60 = ind.sma(closes, 60)
    dif, dea, bar = ind.macd(closes)
    rsi6 = ind.rsi(closes, 6); rsi12 = ind.rsi(closes, 12); rsi24 = ind.rsi(closes, 24)

    i = len(closes) - 1
    price = closes[i]; prev_close = closes[i - 1] if i > 0 else price
    change_pct = ((price / prev_close - 1) * 100) if prev_close else 0.0

    # 区间统计
    h20 = max(highs[-20:]); l20 = min(lows[-20:])
    h60 = max(highs[-60:]); l60 = min(lows[-60:])
    pos20 = _range_position(price, h20, l20)
    pos60 = _range_position(price, h60, l60)
    chg5 = ind.pct_change(closes, i, 5)
    chg20 = ind.pct_change(closes, i, 20)
    chg60 = ind.pct_change(closes, i, 60)
    mdd60 = ind.max_drawdown(closes, 60)

    # 偏离乖离
    bias5 = ind.bias(price, ma5[i]); bias20 = ind.bias(price, ma20[i]); bias60 = ind.bias(price, ma60[i])

    trend = _trend_status(ma5[i], ma20[i], ma60[i])
    # MACD 状态
    macd_status = "金叉" if dif[i] > dea[i] else "死叉"
    macd_bar = bar[i]
    # RSI 状态
    rsi_val = rsi12[i]
    if rsi_val >= 70:
        rsi_status = "超买"
    elif rsi_val <= 30:
        rsi_status = "超卖"
    else:
        rsi_status = "中性"

    # 抽稀近 chart_points 个交易日供前端绘制
    n = len(closes)
    start = max(0, n - chart_points)
    chart = {
        "dates": dates[start:],
        "opens": [round(v, 2) for v in opens[start:]],
        "closes": [round(v, 2) for v in closes[start:]],
        "highs": [round(v, 2) for v in highs[start:]],
        "lows": [round(v, 2) for v in lows[start:]],
        "volumes": [int(v) for v in vols[start:]],
        "ma5": [round(v, 2) if v is not None else None for v in ma5[start:]],
        "ma20": [round(v, 2) if v is not None else None for v in ma20[start:]],
        "ma60": [round(v, 2) if v is not None else None for v in ma60[start:]],
    }

    return {
        "symbol": sym, "name": name, "en": meta.get("en", ""),
        "unit": meta.get("unit", "元/吨"), "exchange": meta.get("exchange", "SHFE"),
        "date": dates[i],
        "price": round(price, 2),
        "prevClose": round(prev_close, 2),
        "open": round(opens[i], 2),
        "high": round(highs[i], 2),
        "low": round(lows[i], 2),
        "change": round(change_pct, 2),
        "volume": int(vols[i]),
        "ma5": round(ma5[i], 2) if ma5[i] is not None else None,
        "ma10": round(ma10[i], 2) if ma10[i] is not None else None,
        "ma20": round(ma20[i], 2) if ma20[i] is not None else None,
        "ma60": round(ma60[i], 2) if ma60[i] is not None else None,
        "bias_ma5": round(bias5, 2) if bias5 is not None else None,
        "bias_ma20": round(bias20, 2) if bias20 is not None else None,
        "bias_ma60": round(bias60, 2) if bias60 is not None else None,
        "macd_status": macd_status,
        "macd_dif": round(dif[i], 2),
        "macd_dea": round(dea[i], 2),
        "macd_bar": round(macd_bar, 2),
        "rsi6": round(rsi6[i], 1),
        "rsi12": round(rsi_val, 1),
        "rsi24": round(rsi24[i], 1),
        "rsi_status": rsi_status,
        "trend_status": trend,
        "high20": round(h20, 2), "low20": round(l20, 2),
        "high60": round(h60, 2), "low60": round(l60, 2),
        "pos20": round(pos20 * 100, 1),
        "pos60": round(pos60 * 100, 1),
        "change5": round(chg5, 2) if chg5 is not None else None,
        "change20": round(chg20, 2) if chg20 is not None else None,
        "change60": round(chg60, 2) if chg60 is not None else None,
        "max_dd60": round(mdd60, 2) if mdd60 is not None else None,
        "chart": chart,
    }


def build_metals_data() -> Dict:
    """聚合所有有色金属品种，生成前端 JSON。"""
    items = []
    for meta in METALS:
        try:
            m = analyze_metal(meta)
        except Exception as e:
            print(f"[metals] {meta['name']} 分析失败: {e}")
            m = None
        if m:
            items.append(m)
            print(f"   {m['name']}({m['symbol']}) 现价{m['price']} {m['change']:+.2f}% 趋势{m['trend_status']} MACD{m['macd_status']} RSI{m['rsi12']}")

    up = [m for m in items if m["change"] > 0]
    down = [m for m in items if m["change"] < 0]
    avg = sum(m["change"] for m in items) / len(items) if items else 0.0
    leader = max(items, key=lambda x: x["change"]) if items else None
    laggard = min(items, key=lambda x: x["change"]) if items else None

    return {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "title": "有色金属期货",
        "stats": {
            "total": len(items),
            "up": len(up),
            "down": len(down),
            "avg_change": round(avg, 2),
            "leader": (leader or {}).get("name", "--"),
            "leader_change": (leader or {}).get("change", 0.0),
            "laggard": (laggard or {}).get("name", "--"),
            "laggard_change": (laggard or {}).get("change", 0.0),
        },
        "items": items,
    }


def save_metals_data(data: Dict) -> str:
    path = os.path.join(BASE_DIR, "data", "metals_data.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


# ---------------- 有色股票推荐 + 宏观影响 + 历史跟踪 ----------------

def _macro_sector_index() -> Dict:
    """读取 macro_data.json 的板块宏观分索引（无数据时返回空）。"""
    path = os.path.join(DATA_DIR, "macro_data.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d.get("_index", {}).get("sectors", {})
    except Exception:
        return {}


def _metal_name_map() -> Dict[str, str]:
    """{code: name}（腾讯快照优先，静态表兜底）。"""
    try:
        from . import data_provider as dp
        qs = dp.fetch_quotes(METALS_STOCK_CODES)
        if qs:
            return {c: (q.get("name") or n) for c, q in qs.items()
                    if (q or {}).get("name")}
    except Exception:
        pass
    return {m["code"]: m["name"] for m in METALS_STOCKS}


def build_metals_stocks_data(offline: bool = False) -> Dict:
    """有色股票推荐分析：技术分析 + 资金流 + 宏观板块分 → 排序推荐。

    返回 {generatedAt, macro: {温度/板块净分}, items: [{code,name,sector,link,
    price,change_pct,trend_status,tech_score,signal_key,signal,macro_net,macro_bulls,
    macro_risks,macro_key,fund_net,ideal_buy,secondary_buy,stop_loss,take_profit,
    total_score,rating}]}
    """
    from . import analyzer as az
    from . import data_provider as dp
    from . import screener as scr

    names = _metal_name_map()
    if not offline:
        quotes = dp.fetch_quotes(METALS_STOCK_CODES)
    else:
        quotes = {}
    klines = dp.fetch_daily_kline_batch(METALS_STOCK_CODES, count=320)

    # 资金流（主力=超大单+大单）
    fflows = {}
    if not offline:
        try:
            from . import fund_flow as ff
            fflows = ff.fetch_fflow_batch(METALS_STOCK_CODES)
        except Exception as e:
            print(f"   [warn] 有色股票资金流失败: {e}")

    # 宏观板块分（"周期资源" 覆盖大部分有色股）
    sec_idx = _macro_sector_index()
    macro_sector = sec_idx.get("周期资源") or {}
    macro_sector2 = sec_idx.get("新能源电力") or {}

    items = []
    for meta in METALS_STOCKS:
        code = meta["code"]
        k = klines.get(code)
        if not k or len(k["closes"]) < 30:
            continue
        try:
            a = az.analyze_stock(meta["name"], k["dates"], k["opens"], k["closes"],
                                 k["highs"], k["lows"], k["volumes"], code=code)
        except Exception:
            a = None
        if a is None:
            continue
        q = quotes.get(code) or {}
        # 宏观分：按个股所属板块取（锂电归新能源电力，其余归周期资源）
        ms = macro_sector if meta["sector"] == "周期资源" else macro_sector2
        key = (ms.get("key") or [])[:3]
        items.append({
            "code": code, "name": meta["name"], "sector": meta["sector"],
            "link": meta["link"],
            "price": (q.get("price") or a.close),
            "change_pct": (q.get("change") or a.change_pct),
            "trend_status": a.trend_status,
            "tech_score": a.score,
            "signal_key": a.signal_key, "signal": a.signal,
            "macro_net": ms.get("net"),
            "macro_bulls": ms.get("bulls", 0), "macro_risks": ms.get("risks", 0),
            "macro_key": key,
            "fund_net": (fflows.get(code) or {}).get("main_net"),
            "ideal_buy": a.ideal_buy, "secondary_buy": a.secondary_buy,
            "stop_loss": a.stop_loss, "take_profit": a.take_profit,
        })

    # 横截面打分（技术面 50% + 横截面因子 50%，含资金流/宏观加权）
    if not items:
        return {"generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "macro": {}, "items": []}
    screen_items = []
    for it in items:
        si = scr.ScreenItem(
            name=it["name"], code=it["code"], sector=it["sector"],
            price=it["price"], change_pct=it["change_pct"],
            amount=0, tech_score=it["tech_score"],
            signal_key=it["signal_key"], signal=it["signal"],
            trend_status=it["trend_status"],
        )
        # 宏观分映射到 tech_score 调整（±6 封顶）：板块偏多加分，偏空减分
        mn = it["macro_net"]
        if mn is not None:
            si.tech_score = max(0, min(100, si.tech_score + max(-6.0, min(6.0, mn / 3))))
        screen_items.append(si)
    ranked = scr.screen(screen_items, tech_weight=0.5, top_n=len(screen_items))
    rank_map = {r.code: r for r in ranked}
    for it in items:
        r = rank_map.get(it["code"])
        if r:
            it["total_score"] = r.total_score
            it["rating"] = r.rating
        else:
            it["total_score"] = 50.0
            it["rating"] = "C"
    items.sort(key=lambda x: (-x["total_score"], x["code"]))

    macro_temp = None
    try:
        with open(os.path.join(DATA_DIR, "macro_data.json"), "r", encoding="utf-8") as f:
            mraw = json.load(f)
        macro_temp = mraw.get("overview", {}).get("temperature")
    except Exception:
        pass

    return {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "macro": {
            "temperature": macro_temp,
            "sector_net": macro_sector.get("net"),
            "sector_bulls": macro_sector.get("bulls", 0),
            "sector_risks": macro_sector.get("risks", 0),
            "sector_key": (macro_sector.get("key") or [])[:3],
        },
        "items": items,
    }


# ---------------- 有色股票历史跟踪（SQLite 快照） ----------------

METAL_DB_PATH = os.path.join(DATA_DIR, "tracking.db")


def _metal_db() -> sqlite3.Connection:
    conn = sqlite3.connect(METAL_DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS metal_picks (
        day TEXT NOT NULL,
        rank INTEGER NOT NULL,
        code TEXT NOT NULL,
        name TEXT NOT NULL,
        total_score REAL,
        rating TEXT,
        signal_key TEXT,
        price REAL,
        sector TEXT,
        PRIMARY KEY (day, code))""")
    conn.commit()
    return conn


def snapshot_metal_picks(items: List[Dict], day: str) -> int:
    """把当日有色推荐快照写入 SQLite（day+code 幂等）。"""
    conn = _metal_db()
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO metal_picks
               (day, rank, code, name, total_score, rating, signal_key, price, sector)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [(day, i + 1, it["code"], it["name"], it.get("total_score"),
              it.get("rating", ""), it.get("signal_key", ""),
              it.get("price"), it.get("sector", ""))
             for i, it in enumerate(items)])
    return len(items)


def _track_return(kline: Dict, rec_date: str) -> Optional[float]:
    """推荐日收盘 → 最新收盘 的涨幅%。

    2026-08-09 修正：推荐日若已是最后一个交易日（无后续行情，如当天就是
    最近交易日或周末快照），返回 None —— 前端显示 '--' 而不是 0%，避免
    把"还没走出的行情"误读成"收益为零"。
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
    if idx is None or not closes or not closes[idx]:
        return None
    if idx >= len(dates) - 1:
        return None
    return round((closes[-1] / closes[idx] - 1) * 100, 2)


def build_metals_tracking(limit_days: int = 120) -> Dict:
    """读取 SQLite 有色快照历史 → 拉K线 → 跟踪收益，输出 {days, stable}。"""
    from . import data_provider as dp

    conn = _metal_db()
    rows = conn.execute(
        "SELECT day, rank, code, name, total_score, rating, signal_key, price, sector "
        "FROM metal_picks ORDER BY day, rank").fetchall()
    conn.close()
    if not rows:
        return {"days": [], "stable": []}

    days = OrderedDict()
    for r in rows:
        day, rank, code, name, total, rating, sk, price, sector = r
        days.setdefault(day, []).append({
            "rank": rank, "code": code, "name": name, "total_score": total,
            "rating": rating or "", "signal_key": sk or "", "price": price,
            "sector": sector or ""})
    day_list = list(days.keys())[-limit_days:]
    days = OrderedDict((d, days[d]) for d in day_list)

    codes = sorted({it["code"] for lst in days.values() for it in lst})
    kl = {}
    for c in codes:
        try:
            k = dp.fetch_daily_kline(c, count=250)
        except Exception:
            k = None
        if k and len(k.get("dates") or []) > 30:
            kl[c] = k

    # 交易日归一化（与 build_tracking 一致）：周末/节假日快照归并到最近交易日
    trade_days = sorted({d for k in kl.values() for d in (k.get("dates") or [])})
    if trade_days:
        norm = OrderedDict()
        for d, lst in days.items():
            nd = d
            for td in reversed(trade_days):
                if td <= d:
                    nd = td
                    break
            items = norm.setdefault(nd, [])
            for it in lst:
                if not any(x["code"] == it["code"] for x in items):
                    items.append(it)
        days = OrderedDict((d, lst) for d, lst in sorted(norm.items()))

    for d, lst in days.items():
        for it in lst:
            it["track_return"] = _track_return(kl.get(it["code"]), d)

    # 稳定榜：上榜天数 + 累计/最近跟踪收益
    from collections import Counter
    cnt, latest, first_seen, last_seen = Counter(), {}, {}, {}
    for d, lst in days.items():
        for it in lst:
            c = it["code"]
            cnt[c] += 1
            latest[c] = it
            first_seen.setdefault(c, d)
            last_seen[c] = d
    first_ret, last_ret = {}, {}
    for d, lst in days.items():
        for it in lst:
            c = it["code"]
            if d == first_seen[c]:
                first_ret[c] = it["track_return"]
            if d == last_seen[c]:
                last_ret[c] = it["track_return"]
    total_days = len(days)
    stable = sorted(
        ({"code": c, "name": latest[c]["name"], "sector": latest[c]["sector"],
          "count": n, "rate": round(n / total_days * 100) if total_days else 0,
          "first_day": first_seen[c], "latest_day": last_seen[c],
          "latest_score": latest[c]["total_score"],
          "latest_signal": latest[c]["signal_key"],
          "cum_track_return": first_ret.get(c),
          "latest_track_return": last_ret.get(c)}
         for c, n in cnt.items()),
        key=lambda x: (-x["count"], -(x["latest_score"] or 0)))

    return {"days": [{"date": d, "items": lst} for d, lst in days.items()],
            "stable": stable}


def extend_metals_data(data: Dict, offline: bool = False) -> Dict:
    """给 metals_data.json 附加股票推荐 + 宏观 + 跟踪区块。"""
    stocks = build_metals_stocks_data(offline=offline)
    data["stocks"] = stocks["items"]
    data["stock_macro"] = stocks["macro"]
    # 当日快照入库（幂等；周末/节假日归一到最近交易日，与 build_tracking 一致）
    day = _nearest_trade_day((data.get("generatedAt") or "")[:10])
    if stocks["items"] and day:
        snapshot_metal_picks(stocks["items"], day)
        print(f"   有色股票快照 {day} 已入库 {len(stocks['items'])} 只")
    data["stock_tracking"] = build_metals_tracking()
    return data


def _nearest_trade_day(day: str) -> Optional[str]:
    """把日期归一到 <= 它的最近交易日（用任一有色股票 K 线日期判断）。"""
    if not day:
        return None
    try:
        from . import data_provider as dp
        k = dp.fetch_daily_kline(METALS_STOCK_CODES[0], count=250)
        if not k:
            return day
        trade_days = sorted(d for d in (k.get("dates") or []) if d <= day)
        return trade_days[-1] if trade_days else day
    except Exception:
        return day

# ================= 面板周期（LCD 景气度代理） =================
PANEL_STOCKS = [
    ("000725", "京东方A"),
    ("000100", "TCL科技"),
    ("600707", "彩虹股份"),
    ("000050", "深天马A"),
    ("002387", "维信诺"),
]
PANEL_DAYS = 250  # 52 周观察窗口


def build_panel_data() -> Dict:
    """面板周期数据：龙头股技术面 + 52 周位置 + 板块等权净值指数。

    面板价格（WitsView/群智）无免费 API，用面板厂股价作为景气度代理：
    - 每只：现价/涨跌/技术评分/趋势/60日涨幅/52周高低/位置
    - 板块等权净值：反映面板板块整体周期位置
    """
    from . import analyzer as az
    from . import data_provider as dp

    klines = {}
    for code, name in PANEL_STOCKS:
        k = dp.fetch_daily_kline_long(code, count=550, min_days=500, use_cache=True)
        if k is None:
            k = dp.fetch_daily_kline_long(code, count=550, min_days=500, use_cache=False)
        if k and len(k["closes"]) >= PANEL_DAYS + 30:
            klines[code] = k

    stocks = []
    for code, name in PANEL_STOCKS:
        k = klines.get(code)
        if not k:
            continue
        r = az.analyze_stock(name, k["dates"], k["opens"], k["closes"],
                             k["highs"], k["lows"], k["volumes"], code=code)
        if not r:
            continue
        closes = k["closes"]
        seg = closes[-PANEL_DAYS:]
        high52, low52 = max(seg), min(seg)
        pos52 = round((closes[-1] - low52) / (high52 - low52) * 100, 1) if high52 > low52 else 50.0
        stocks.append({
            "name": name, "code": code,
            "close": closes[-1],
            "change_pct": r.change_pct,
            "score": r.score,
            "signal_key": r.signal_key,
            "signal": r.signal,
            "trend_status": r.trend_status,
            "change_60d": r.change_60d,
            "high52": high52, "low52": low52,
            "dist_high": round((closes[-1] / high52 - 1) * 100, 1),
            "dist_low": round((closes[-1] / low52 - 1) * 100, 1),
            "pos52": pos52,
        })

    # 板块等权净值指数（以第一只股票的日期为轴，其余按日期映射，缺失前值填充）
    axis_code = next(iter(klines)) if klines else None
    index = {"dates": [], "nav": []}
    if axis_code:
        axis = klines[axis_code]
        dates = axis["dates"][-PANEL_DAYS:]
        d2i = {c: {d: i for i, d in enumerate(k["dates"])} for c, k in klines.items()}
        base = {c: klines[c]["closes"][-PANEL_DAYS] for c in klines}
        navs = []
        for d in dates:
            vals = []
            for c, k in klines.items():
                i = d2i[c].get(d)
                if i is not None:
                    vals.append(k["closes"][i] / base[c])
            navs.append(sum(vals) / len(vals) if vals else None)
        # 前值填充
        last = None
        nav_clean = []
        for v in navs:
            if v is not None:
                last = v
            nav_clean.append(round(last, 4) if last else None)
        index = {"dates": dates, "nav": nav_clean}

    n = len(stocks)
    up = sum(1 for s in stocks if s["change_pct"] > 0)
    avg_pos = round(sum(s["pos52"] for s in stocks) / n, 1) if n else 0
    best = max(stocks, key=lambda s: s["score"]) if stocks else None
    return {
        "stocks": stocks,
        "index": index,
        "stats": {
            "count": n, "up": up, "down": n - up,
            "avg_pos52": avg_pos,
            "best": best["name"] if best else "--",
            "best_score": best["score"] if best else 0,
            "index_60d": round((index["nav"][-1] / index["nav"][-61] - 1) * 100, 1)
            if len(index["nav"]) > 61 and index["nav"][-61] else None,
        },
        "note": "面板价格（WitsView/群智）无公开免费接口，用面板厂股价作为景气代理："
                "股价周期位置≈面板价格周期位置；真正的价格见顶信号以月度 TV 面板报价为准。",
    }
