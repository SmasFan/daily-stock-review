"""
数据源模块：腾讯行情快照 + 腾讯日K线，带本地 JSON 缓存。

- 实时快照: qt.gtimg.cn（量比/换手/PE/PB/成交额等）
- 日K线: web.ifzq.gtimg.cn（前复权日线，含 OHLCV）
- 缓存: data/cache/ 下按日期缓存，避免重复请求
"""
import json
import os
import re
import time
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# K 线缓存最大存活小时数，可用环境变量 CACHE_MAX_AGE_HOURS 覆盖（如 99999 表示只用缓存）。
CACHE_MAX_AGE_HOURS = float(os.environ.get("CACHE_MAX_AGE_HOURS", "20"))


def tencent_symbol(code: str) -> str:
    """腾讯行情前缀：sh=沪市/沪基金/可转债, sz=深市, bj=北交所。"""
    if code.startswith(("sh", "sz", "bj")):
        return code
    if code.startswith(("8", "4", "920", "921")):
        return f"bj{code}"
    if code.startswith(("6", "5", "9", "11", "12", "58", "118")):
        return f"sh{code}"
    return f"sz{code}"


def _get(url: str, timeout: int = 20, encoding: str = "utf-8") -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode(encoding, errors="ignore")


# ---------------- 实时快照 ----------------

def fetch_quotes(codes):
    """批量拉取实时快照，返回 {code: quote_dict}。"""
    symbols = [tencent_symbol(c) for c in codes]
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    try:
        raw = _get(url, encoding="gbk")
    except Exception as e:
        print(f"[data] 快照拉取失败: {e}")
        return {}
    out = {}
    for code in codes:
        q = parse_quote(raw, code)
        if q:
            out[code] = q
    return out


def fetch_index_quotes(symbols):
    """批量拉取指数快照（symbol 为完整前缀+代码，如 sh000001 / hkHSI / usIXIC）。"""
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    try:
        raw = _get(url, encoding="gbk")
    except Exception as e:
        print(f"[data] 指数快照拉取失败: {e}")
        return {}
    out = {}
    for sym in symbols:
        q = parse_quote(raw, sym, full_symbol=True)
        if q:
            out[sym] = q
    return out


def parse_quote(raw: str, code: str, full_symbol: bool = False):
    sym = code if full_symbol else tencent_symbol(code)
    m = re.search(rf'v_{sym}="([^"]*)";', raw)
    if not m:
        return None
    p = m.group(1).split("~")
    if len(p) < 6:
        return None

    def f(i, d=None):
        try:
            v = p[i]
            return float(v) if v != "" else d
        except (ValueError, IndexError):
            return d

    def s(i, d=""):
        try:
            return p[i] if p[i] != "" else d
        except IndexError:
            return d

    amount_10k = f(37, 0) or 0
    price = f(3, 0) or 0
    prev_close = f(4, 0) or 0
    high = f(33, 0) or 0
    low = f(34, 0) or 0
    change_pct = ((price / prev_close - 1) * 100) if prev_close else (f(32, 0) or 0)
    return {
        "name": s(1, code),
        "code": code,
        "price": price,
        "prevClose": prev_close,
        "open": f(5, 0) or 0,
        "high": high,
        "low": low,
        "change": round(change_pct, 2),
        "volume": int(f(36, 0) or 0),
        "amount": amount_10k * 10000,
        "turnover": f(38, 0) or 0,
        "pe": f(39, None),
        "pb": f(46, None),
        "weiBi": f(49, 0) or 0,
        "volumeRatio": f(51, None),
    }


# ---------------- 日K线 ----------------

def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, f"kline_{key}.json")


def fetch_daily_kline(code: str, count: int = 320, use_cache: bool = True,
                      cache_max_age_hours: float = None):
    """拉取前复权日K线，返回 {dates, opens, closes, highs, lows, volumes}。带缓存。"""
    cp = _cache_path(code)
    if use_cache and os.path.exists(cp):
        age = time.time() - os.path.getmtime(cp)
        if age < (cache_max_age_hours if cache_max_age_hours is not None else CACHE_MAX_AGE_HOURS) * 3600:
            try:
                with open(cp, "r", encoding="utf-8") as fp:
                    return json.load(fp)
            except Exception:
                pass
    sym = tencent_symbol(code)
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={sym},day,,,{count},qfq")
    try:
        raw = _get(url)
        data = json.loads(raw)
        node = data["data"][sym]
        rows = node.get("qfqday") or node.get("day") or []
        dates, opens, closes, highs, lows, vols = [], [], [], [], [], []
        for r in rows:
            # r: [date, open, close, high, low, volume, ...]
            dates.append(r[0])
            opens.append(float(r[1]))
            closes.append(float(r[2]))
            highs.append(float(r[3]))
            lows.append(float(r[4]))
            vols.append(float(r[5]) if len(r) > 5 else 0.0)
        out = {"dates": dates, "opens": opens, "closes": closes,
               "highs": highs, "lows": lows, "volumes": vols}
        try:
            with open(cp, "w", encoding="utf-8") as fp:
                json.dump(out, fp)
        except Exception:
            pass
        return out
    except Exception as e:
        print(f"[data] {code} 日K拉取失败: {e}")
        return None


def fetch_daily_kline_batch(codes, count: int = 320, sleep: float = 0.3):
    """批量拉取，带限速防反爬。返回 {code: kline_dict}。"""
    out = {}
    for c in codes:
        k = fetch_daily_kline(c, count=count)
        if k and len(k["closes"]) >= 30:
            out[c] = k
        time.sleep(sleep)
    return out


def fetch_daily_kline_long(code: str, count: int = 320, min_days: int = 750,
                           use_cache: bool = True, cache_max_age_hours: float = None):
    """拉取前复权日K，支持超过 640 根的长历史（腾讯单次上限 640，分页向后翻）。

    返回 {dates, opens, closes, highs, lows, volumes}，升序。带缓存（key 含 count）。
    当单次接口返回不足 min_days 时，按最旧日期向前翻页直至凑够 count 或到最早数据。
    """
    cp = _cache_path(f"long_{code}_{count}")
    if use_cache and os.path.exists(cp):
        age = time.time() - os.path.getmtime(cp)
        if age < (cache_max_age_hours if cache_max_age_hours is not None else CACHE_MAX_AGE_HOURS) * 3600:
            try:
                with open(cp, "r", encoding="utf-8") as fp:
                    return json.load(fp)
            except Exception:
                pass
    sym = tencent_symbol(code)
    PAGE = 640
    rows_all = []
    start_hint = "1990-01-01"
    # 腾讯接口：param=sym,day,start,end,count,qfq。count 上限 640，返回的是 [start,end] 内最后 count 根。
    # 通过循环把 end 不断设为当前最旧日期的前一天来翻页。
    end = datetime.now().strftime("%Y-%m-%d")
    for _ in range(12):
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={sym},day,{start_hint},{end},{PAGE},qfq")
        try:
            raw = _get(url)
            data = json.loads(raw)
            node = data["data"][sym]
            rows = node.get("qfqday") or node.get("day") or []
        except Exception:
            break
        if not rows:
            break
        rows_all = rows + rows_all
        first_date = rows[0][0]
        if len(rows) < PAGE:
            break
        end = first_date
        if len(rows_all) >= count and count > 0:
            break
        time.sleep(0.2)

    if not rows_all:
        return None
    # 截取需要的根数（保留最近 count 根）
    rows = rows_all[-count:] if count > 0 and len(rows_all) > count else rows_all
    # 分页接口对大 count 请求存在数据更新延迟（最新 1-2 个交易日可能缺失），
    # 用单次小请求(320根)核对并补齐最新交易日，避免回测数据滞后。
    try:
        url_latest = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                      f"?param={sym},day,,,{320},qfq")
        raw_l = _get(url_latest)
        data_l = json.loads(raw_l)
        node_l = data_l["data"][sym]
        rows_l = node_l.get("qfqday") or node_l.get("day") or []
        if rows_l:
            latest_existing = rows[-1][0] if rows else ""
            # 用日期映射补齐缺失的最新交易日（按日期去重，保留小请求的更新版本）
            merged = {r[0]: r for r in rows}
            for r in rows_l:
                if r[0] > latest_existing:
                    merged[r[0]] = r
            rows = [merged[k] for k in sorted(merged.keys())]
    except Exception:
        pass
    dates, opens, closes, highs, lows, vols = [], [], [], [], [], []
    for r in rows:
        dates.append(r[0])
        opens.append(float(r[1]))
        closes.append(float(r[2]))
        highs.append(float(r[3]))
        lows.append(float(r[4]))
        vols.append(float(r[5]) if len(r) > 5 else 0.0)
    out = {"dates": dates, "opens": opens, "closes": closes,
           "highs": highs, "lows": lows, "volumes": vols}
    try:
        with open(cp, "w", encoding="utf-8") as fp:
            json.dump(out, fp)
    except Exception:
        pass
    return out


def fetch_daily_kline_us(sym: str, count: int = 320, use_cache: bool = True,
                         cache_max_age_hours: float = None):
    """拉取美股指数/个股日K（腾讯 usfqkline 接口），支持长历史翻页。

    sym 为完整前缀+代码（如 usDJI / usIXIC / usINX）。
    返回 {dates, opens, closes, highs, lows, volumes}，升序。带缓存（key 含 count）。
    """
    cp = _cache_path(f"us_{sym}_{count}")
    if use_cache and os.path.exists(cp):
        age = time.time() - os.path.getmtime(cp)
        if age < (cache_max_age_hours if cache_max_age_hours is not None else CACHE_MAX_AGE_HOURS) * 3600:
            try:
                with open(cp, "r", encoding="utf-8") as fp:
                    return json.load(fp)
            except Exception:
                pass
    PAGE = 640
    rows_all = []
    end = datetime.now().strftime("%Y-%m-%d")
    for _ in range(12):
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get"
               f"?param={sym},day,1990-01-01,{end},{PAGE},qfq")
        try:
            raw = _get(url)
            data = json.loads(raw)
            node = data["data"][sym]
            rows = node.get("qfqday") or node.get("day") or []
        except Exception:
            break
        if not rows:
            break
        rows_all = rows + rows_all
        first_date = rows[0][0]
        if len(rows) < PAGE:
            break
        end = first_date
        if len(rows_all) >= count and count > 0:
            break
        time.sleep(0.2)
    if not rows_all:
        return None
    rows = rows_all[-count:] if count > 0 and len(rows_all) > count else rows_all
    dates, opens, closes, highs, lows, vols = [], [], [], [], [], []
    for r in rows:
        dates.append(r[0])
        opens.append(float(r[1]))
        closes.append(float(r[2]))
        highs.append(float(r[3]))
        lows.append(float(r[4]))
        vols.append(float(r[5]) if len(r) > 5 else 0.0)
    out = {"dates": dates, "opens": opens, "closes": closes,
           "highs": highs, "lows": lows, "volumes": vols}
    try:
        with open(cp, "w", encoding="utf-8") as fp:
            json.dump(out, fp)
    except Exception:
        pass
    return out


def fetch_index_kline(sym: str, count: int = 320, use_cache: bool = True,
                      cache_max_age_hours: float = None):
    """拉取指数日K（含前缀的完整符号，如 sh000001 / sz399001 / hkHSI / usIXIC）。

    根据前缀选择接口：us 前缀走 usfqkline（美股），其余走 fqkline。
    返回 {dates, opens, closes, highs, lows, volumes}，升序。带缓存（key 含 count）。
    """
    if sym.startswith("us"):
        return fetch_daily_kline_us(sym, count=count, use_cache=use_cache,
                                    cache_max_age_hours=cache_max_age_hours)
    cp = _cache_path(f"idx_{sym}_{count}")
    if use_cache and os.path.exists(cp):
        age = time.time() - os.path.getmtime(cp)
        if age < (cache_max_age_hours if cache_max_age_hours is not None else CACHE_MAX_AGE_HOURS) * 3600:
            try:
                with open(cp, "r", encoding="utf-8") as fp:
                    return json.load(fp)
            except Exception:
                pass
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={sym},day,,,{count},qfq")
    try:
        raw = _get(url)
        data = json.loads(raw)
        node = data["data"][sym]
        rows = node.get("qfqday") or node.get("day") or []
    except Exception:
        return None
    if not rows:
        return None
    dates, opens, closes, highs, lows, vols = [], [], [], [], [], []
    for r in rows:
        dates.append(r[0])
        opens.append(float(r[1]))
        closes.append(float(r[2]))
        highs.append(float(r[3]))
        lows.append(float(r[4]))
        vols.append(float(r[5]) if len(r) > 5 else 0.0)
    out = {"dates": dates, "opens": opens, "closes": closes,
           "highs": highs, "lows": lows, "volumes": vols}
    try:
        with open(cp, "w", encoding="utf-8") as fp:
            json.dump(out, fp)
    except Exception:
        pass
    return out
