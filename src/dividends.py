"""
真实分红序列模块（P1：补齐 dy_daily）。

数据源：东财分红送配明细 RPT_SHAREBONUS_DET（等价 akshare.stock_fhps_detail_em，
仅用标准库实现，保持零三方依赖）。
- 只取 ASSIGN_PROGRESS="实施分配" 且已过除权除息日的记录
- 返回 {ex_date: cash_per_share}（每股税前现金分红，元）
- 缓存 24h（成功）/ 6h（空），失败回退过期旧数据

在回测中 dy_daily[i] = 除息日每股分红 / 昨收（除息日单日股息率），
替代旧版恒为 0 的占位。
"""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import List

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}

DIV_CACHE_OK_HOURS = 24.0
DIV_CACHE_EMPTY_HOURS = 6.0


def _get_json(url: str, timeout: int = 10):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def _read_cache(cache: str):
    """返回 (状态, 数据)：fresh/stale/miss。空结果 TTL 短，避免永久空缓存。"""
    if not os.path.exists(cache):
        return "miss", None
    try:
        with open(cache, "r", encoding="utf-8") as f:
            rows = json.load(f)
    except Exception:
        return "miss", None
    if not isinstance(rows, dict):
        return "miss", None
    age_h = (time.time() - os.path.getmtime(cache)) / 3600.0
    ttl = DIV_CACHE_OK_HOURS if rows else DIV_CACHE_EMPTY_HOURS
    return ("fresh" if age_h < ttl else "stale"), rows


def fetch_dividends(code: str) -> dict:
    """拉取单只股票的真实现金分红序列，返回 {ex_date('YYYY-MM-DD'): 每股现金分红(元)}。

    仅记录已实施分配（除权除息日已确定）的分红；送转股不计入现金收益。
    无分红或数据源不可达时返回 {}（ETF 用前复权价近似，dy_daily 保持 0）。
    """
    cache = os.path.join(CACHE_DIR, f"div_{code}.json")
    state, cached = _read_cache(cache)
    if state == "fresh":
        return cached or {}
    params = {
        "sortColumns": "EX_DIVIDEND_DATE",
        "sortTypes": "-1",
        "pageSize": "500",
        "pageNumber": "1",
        "reportName": "RPT_SHAREBONUS_DET",
        "columns": "SECURITY_CODE,EX_DIVIDEND_DATE,PRETAX_BONUS_RMB,IMPL_PLAN_PROFILE,ASSIGN_PROGRESS",
        "quoteColumns": "",
        "filter": f'(SECURITY_CODE="{code}")',
    }
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params)
    try:
        data = _get_json(url)
        rows = (data.get("result") or {}).get("data") or []
        out = {}
        for r in rows:
            ex = (r.get("EX_DIVIDEND_DATE") or "")[:10]
            prog = r.get("ASSIGN_PROGRESS") or ""
            cash = r.get("PRETAX_BONUS_RMB")
            if not ex or "实施" not in prog:
                continue
            if cash in (None, "-", ""):
                continue
            per_share = float(cash) / 10.0  # 每10股派X元 -> 每股
            out[ex] = round(per_share, 6)
        try:
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
        except Exception:
            pass
        return out
    except Exception:
        # 网络失败：缓存空结果（6h TTL），回退过期旧数据
        try:
            with open(cache, "w", encoding="utf-8") as f:
                json.dump({}, f)
        except Exception:
            pass
        if state == "stale" and cached:
            return cached
        return {}


def build_dy_daily(dates: List[str], closes: List[float], code: str) -> List[float]:
    """把分红序列映射到日收益率：除息日 dy = 每股分红 / 昨收。

    除息日价格会跳空下调，持有者实际收益 = 价格收益 + 分红/昨收。
    返回与 dates 等长的日股息率列表（非除息日为 0）。
    """
    divs = fetch_dividends(code)
    n = len(dates)
    out = [0.0] * n
    if not divs:
        return out
    for i, d in enumerate(dates):
        cash = divs.get(d)
        if cash is None or cash <= 0:
            continue
        prev = closes[i - 1] if i > 0 else 0.0
        if prev and prev > 0:
            out[i] = round(cash / prev, 8)
    return out
