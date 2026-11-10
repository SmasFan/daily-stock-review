# -*- coding: utf-8 -*-
"""行情：东财 push2 / push2his（美股），腾讯 qt 兜底。

本机 getaddrinfo 目前在 opendirectoryd 故障后会失败，但 nslookup 正常，
所以这里在解析失败时用 nslookup 拿 A 记录（重启修复账户服务后可删掉 _patch_dns）。
"""
import json
import os
import socket
import subprocess
import time
import urllib.parse
import urllib.request

from . import config

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"}
SECID_CACHE = os.path.join(config.DATA_DIR, "secids.json")


# ---------------- 网络 ----------------
def _patch_dns():
    orig = socket.getaddrinfo
    if getattr(orig, "_usim_patched", False):
        return

    def patched(host, port, *a, **kw):
        try:
            return orig(host, port, *a, **kw)
        except OSError:
            ips = []
            try:
                out = subprocess.run(["nslookup", host], capture_output=True, text=True, timeout=8).stdout
                for line in out.splitlines():
                    line = line.strip()
                    if line.startswith("Address:") and "#" not in line:
                        ip = line.split(":", 1)[1].strip()
                        if ip and ":" not in ip and ip not in ips:
                            ips.append(ip)
            except Exception:
                pass
            if not ips:
                raise
            res = []
            for ip in ips:
                res += orig(ip, port, *a, **kw)
            return res

    patched._usim_patched = True
    socket.getaddrinfo = patched


_patch_dns()


def _get(url, timeout=12):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _cache_read(path, ttl):
    try:
        if time.time() - os.path.getmtime(path) < ttl:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _cache_write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------- secid 解析 ----------------
def _load_secids():
    try:
        with open(SECID_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def resolve(symbol):
    """美股代码 → 东财 secid（105=NASDAQ, 106=NYSE, 107=AMEX）。"""
    m = _load_secids()
    if symbol in m:
        return m[symbol]
    for pref in (105, 106, 107):
        secid = "%d.%s" % (pref, symbol)
        try:
            d = json.loads(_get(
                "https://push2.eastmoney.com/api/qt/stock/get?secid=%s&fields=f57,f58,f43" % secid))
            if (d.get("data") or {}).get("f43"):
                m[symbol] = secid
                _cache_write(SECID_CACHE, m)
                return secid
        except Exception:
            continue
    raise RuntimeError("无法解析美股代码: %s" % symbol)


_DIV_CACHE = {}


# ---------------- 实时报价 ----------------
def quotes(symbols, use_cache=True):
    """返回 {symbol: {price, prev_close, open, high, low, chg_pct, ts, name}}"""
    if use_cache:
        c = _cache_read(config.QUOTE_CACHE, config.QUOTE_TTL)
        if c and all(s in c for s in symbols):
            return {s: c[s] for s in symbols}
    secids = ",".join(resolve(s) for s in symbols)
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?secids=%s"
           "&fields=f12,f13,f14,f2,f3,f15,f16,f17,f18,f124,f59" % urllib.parse.quote(secids, safe=",."))
    try:
        d = json.loads(_get(url))
        rows = ((d.get("data") or {}).get("diff") or [])
        if isinstance(rows, dict):
            rows = list(rows.values())
        out = {}
        for r in rows:
            sym = r.get("f12")
            if not sym:
                continue
            # 东财美股报价是放大整数（ulist 的 f59 不可信，恒为 0），
            # 用日线最新收盘价反推除数（1 / 100 / 1000 三选一）
            px = _num(r.get("f2"))
            if not px:
                continue
            scale = _divisor(sym, px)
            out[sym] = {
                "price": px / scale,
                "prev_close": (_num(r.get("f18")) or 0) / scale or None,
                "open": (_num(r.get("f17")) or 0) / scale or None,
                "high": (_num(r.get("f15")) or 0) / scale or None,
                "low": (_num(r.get("f16")) or 0) / scale or None,
                "chg_pct": (_num(r.get("f3")) or 0) / 100.0,
                "name": r.get("f14"),
                "ts": int(r.get("f124") or time.time()),
            }
        if out:
            old = _cache_read(config.QUOTE_CACHE, 10 ** 9) or {}
            old.update(out)
            _cache_write(config.QUOTE_CACHE, old)
            return {s: out[s] for s in symbols if s in out}
    except Exception:
        pass
    return _quotes_tencent(symbols)


def _divisor(symbol, raw):
    """用最近日线收盘价反推东财报价的放大倍数。"""
    cached = _DIV_CACHE.get(symbol)
    if cached:
        return cached
    try:
        bars = daily(symbol, limit=5)
        ref = float(bars[-1]["close"])
        if ref > 0:
            best = min((1.0, 100.0, 1000.0), key=lambda k: abs(raw / k - ref) / ref)
            if abs(raw / best - ref) / ref < 0.5:
                _DIV_CACHE[symbol] = best
                return best
    except Exception:
        pass
    _DIV_CACHE[symbol] = 1000.0
    return 1000.0


def _num(v):
    try:
        return float(v)
    except Exception:
        return None


def _quotes_tencent(symbols):
    url = "https://qt.gtimg.cn/q=" + ",".join("us" + s for s in symbols)
    raw = _get(url).decode("gbk", "ignore")
    out = {}
    for line in raw.strip().split("\n"):
        if '="' not in line:
            continue
        body = line.split('="')[-1].strip('";')
        f = body.split("~")
        if len(f) < 40:
            continue
        sym = (f[2] or "").split(".")[0]
        px = _num(f[3])
        if not sym or not px:
            continue
        out[sym] = {
            "price": px, "prev_close": _num(f[4]), "open": _num(f[5]),
            "high": _num(f[33]), "low": _num(f[34]),
            "chg_pct": _num(f[32]), "name": f[1], "ts": int(time.time()),
        }
    return out


# ---------------- 日线 ----------------
def daily(symbol, limit=320, use_cache=True):
    """日线 bars: [{date, open, close, high, low, volume}]（前复权）。"""
    key = symbol
    if use_cache:
        c = _cache_read(config.KLINE_CACHE, config.KLINE_TTL)
        if c and key in c and len(c[key]) >= limit:
            return c[key][-limit:]
    secid = resolve(symbol)
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56"
           "&klt=101&fqt=1&end=20500101&lmt=%d" % (secid, limit))
    d = json.loads(_get(url))
    rows = ((d.get("data") or {}).get("klines") or [])
    bars = []
    for r in rows:
        p = r.split(",")
        if len(p) < 6:
            continue
        bars.append({
            "date": p[0], "open": _num(p[1]), "close": _num(p[2]),
            "high": _num(p[3]), "low": _num(p[4]), "volume": _num(p[5]),
        })
    if not bars:
        raise RuntimeError("无日线数据: %s" % symbol)
    c = _cache_read(config.KLINE_CACHE, 10 ** 9) or {}
    c[key] = bars
    _cache_write(config.KLINE_CACHE, c)
    return bars[-limit:]


# ---------------- 交易时段 ----------------
def now_et():
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        from datetime import timedelta
        return datetime.utcnow() - timedelta(hours=5)


def market_open(dt=None):
    """美股常规时段：周一至周五 09:30–16:00 ET。"""
    dt = dt or now_et()
    if dt.weekday() >= 5:
        return False
    hm = dt.hour * 60 + dt.minute
    return 9 * 60 + 30 <= hm < 16 * 60
