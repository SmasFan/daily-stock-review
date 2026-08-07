"""
全市场涨跌家数模块（P1：大盘温度计去偏）。

替代旧版"250 只自选池广度"：
- 主数据源：东财全市场涨跌幅分布 getTopicZDFenBu（含沪深京全部 A 股）
- 兜底：东财指数 ulist f104/f105/f106（上证综指 + 深证成指 涨跌家数之和）
- 缓存 15 分钟，失败时回退旧数据（stale 数据可用，标 source/age）

返回结构：{up, down, flat, total, breadth, source, fetched_at}
"""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

BREADTH_CACHE_FILE = os.path.join(CACHE_DIR, "market_breadth.json")
BREADTH_CACHE_TTL_HOURS = 0.25  # 15 分钟


def _get_json(url: str, timeout: int = 10):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def _from_fenbu() -> dict:
    """东财全市场涨跌幅分布：fenbu 为 {涨跌幅档(取整%): 家数}，含北交所。"""
    params = {
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "dpt": "wz.ztzt",
        "Pageindex": "0",
        "pagesize": "-1",
        "sort": "fbt:asc",
        "date": datetime.now().strftime("%Y%m%d"),
    }
    url = "https://push2ex.eastmoney.com/getTopicZDFenBu?" + urllib.parse.urlencode(params)
    data = _get_json(url)
    fenbu = ((data or {}).get("data") or {}).get("fenbu") or []
    up = down = flat = 0
    for item in fenbu:
        if not isinstance(item, dict):
            continue
        for k, v in item.items():
            try:
                bucket = int(k)
            except (TypeError, ValueError):
                continue
            if bucket > 0:
                up += v
            elif bucket < 0:
                down += v
            else:
                flat += v
    total = up + down + flat
    if total == 0:
        raise ValueError("全市场涨跌分布为空")
    return {"up": up, "down": down, "flat": flat, "total": total, "source": "market"}


def _from_ulist() -> dict:
    """兜底：上证综指 + 深证成指的涨跌家数之和（不含北交所）。"""
    params = {
        "secids": "1.000001,0.399001",
        "fields": "f104,f105,f106",
    }
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get?" + urllib.parse.urlencode(params)
    data = _get_json(url)
    diff = ((data or {}).get("data") or {}).get("diff") or []
    up = sum(int(d.get("f104") or 0) for d in diff)
    down = sum(int(d.get("f105") or 0) for d in diff)
    flat = sum(int(d.get("f106") or 0) for d in diff)
    total = up + down + flat
    if total == 0:
        raise ValueError("ulist 涨跌家数为空")
    return {"up": up, "down": down, "flat": flat, "total": total, "source": "market"}


def _read_cache() -> dict:
    if not os.path.exists(BREADTH_CACHE_FILE):
        return {}
    try:
        with open(BREADTH_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_cache(data: dict):
    try:
        with open(BREADTH_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def fetch_market_breadth(use_cache: bool = True) -> dict:
    """拉取全市场涨跌家数。成功缓存 15 分钟；失败回退过期缓存并标 stale。"""
    cached = _read_cache() if use_cache else {}
    if cached and isinstance(cached, dict) and "total" in cached:
        age_h = (time.time() - cached.get("_ts", 0)) / 3600.0
        if age_h < BREADTH_CACHE_TTL_HOURS:
            return dict(cached, source=cached.get("source", "market"))
    errors = []
    for fn in (_from_fenbu, _from_ulist):
        try:
            out = fn()
            out["_ts"] = time.time()
            out["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            out.pop("_ts", None)
            out["stale"] = False
            _write_cache({**out, "_ts": time.time()})
            return out
        except Exception as e:
            errors.append(str(e))
    # 全部失败：回退过期缓存（标注 stale，前端可提示）
    if cached and isinstance(cached, dict) and cached.get("total"):
        age_min = round((time.time() - cached.get("_ts", 0)) / 60.0)
        return dict(cached, stale=True, stale_age_min=age_min,
                    fallback_error="; ".join(errors))
    return {"up": 0, "down": 0, "flat": 0, "total": 0,
            "source": "unavailable", "stale": True,
            "fallback_error": "; ".join(errors)}
