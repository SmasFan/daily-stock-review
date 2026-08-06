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
import time
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional

from . import indicators as ind

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "cache")
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