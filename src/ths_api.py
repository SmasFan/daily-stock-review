"""同花顺金融数据服务客户端（官方 REST API）。

数据源：https://fuyao.aicubes.cn （同花顺官方）
认证：X-api-key 请求头（API Key 从 .env / 环境变量读取，不写入代码或 git）

能力：
- 行情快照（单只/批量）
- 历史 K 线（日线，前/后复权）
- 估值快照（PE/PB/PS/PCF）
- 财务指标
- 涨跌停池 / 炸板池 / 连板天梯
- 龙虎榜（全部/机构/游资）
- 热股榜 / 飙升榜 / 个股异动

约定：所有函数失败时返回 None / {}，不抛异常，由调用方回退到腾讯源。
"""
import datetime
import json
import os
import time
import urllib.parse

import requests

BASE_URL = "https://fuyao.aicubes.cn"
TIMEOUT = 15


def api_key():
    """读取 API Key：环境变量 > 项目 .env 文件。"""
    key = os.environ.get("HITHINK_FINANCE_API_KEY", "")
    if key:
        return key.strip()
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("HITHINK_FINANCE_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _get(path, params=None):
    """GET 请求，返回 data 字段；失败/无 key 返回 None。"""
    key = api_key()
    if not key:
        return None
    url = BASE_URL + path
    if params:
        qs = urllib.parse.urlencode(params)
        url = url + "?" + qs
    try:
        resp = requests.get(url, headers={"X-api-key": key}, timeout=TIMEOUT)
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("code") != 0:
        return None
    return data.get("data")


def _code_to_thscode(code):
    """6 位代码转 thscode（600xxx.SH / 000xxx.SZ / 30xxxx.SZ / 68xxxx.SH 等）。"""
    code = str(code).strip()
    if "." in code:
        return code
    if len(code) != 6 or not code.isdigit():
        return None
    if code.startswith(("6", "9", "5")):
        return code + ".SH"
    return code + ".SZ"


def fetch_snapshot(codes):
    """批量行情快照，返回 {code: quote_dict}（key 用原始 6 位代码）。"""
    ths = [_code_to_thscode(c) for c in codes]
    ths = [t for t in ths if t]
    if not ths:
        return {}
    data = _get("/api/a-share/prices/snapshot", {"thscodes": ",".join(ths)})
    if not data:
        return {}
    out = {}
    for it in data.get("item") or []:
        code6 = (it.get("thscode") or "")[:6]
        out[code6] = {
            "name": it.get("name", ""),
            "code": code6,
            "price": it.get("last_price"),
            "prevClose": it.get("prev_price"),
            "open": it.get("open_price"),
            "high": it.get("high_price"),
            "low": it.get("low_price"),
            "change": it.get("price_change_ratio_pct"),
            "volume": it.get("volume"),
            "amount": it.get("turnover"),
            "source": "ths",
        }
    return out


def fetch_ticker_list(asset_type="a-share", limit=10000):
    """拉取标的代码表。返回 [{thscode, ticker, name, exchange, asset_type}]。"""
    data = _get("/api/meta/tickers/list", {"asset_type": asset_type, "limit": limit, "offset": 0})
    if not data:
        return []
    return data.get("item") or []


def fetch_snapshot_paged(limit=100, offset=0):
    """全市场行情快照分页。返回 {code6: quote_dict} + total。"""
    data = _get("/api/a-share/prices/snapshot", {"limit": limit, "offset": offset})
    if not data:
        return {}, 0
    total = data.get("total", 0)
    out = {}
    for it in data.get("item") or []:
        code6 = (it.get("thscode") or "")[:6]
        out[code6] = {
            "name": it.get("name", ""),
            "code": code6,
            "price": it.get("last_price"),
            "change": it.get("price_change_ratio_pct"),
            "amount": it.get("turnover"),
            "volume": it.get("volume"),
            "source": "ths",
        }
    return out, total


def fetch_daily_kline(code, days=320):
    """历史日 K（前复权），返回 {dates, opens, highs, lows, closes, volumes} 升序。
    days 超过 10 年会被截断，按 320 默认即可。"""
    ths = _code_to_thscode(code)
    if not ths:
        return None
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    data = _get("/api/a-share/prices/historical", {
        "thscode": ths, "interval": "1d",
        "start": start_ms, "end": end_ms, "adjust": "forward",
    })
    if not data or not data.get("item"):
        return None
    items = sorted(data["item"], key=lambda x: x["date_ms"])
    out = {
        "dates": [],
        "opens": [], "highs": [], "lows": [], "closes": [], "volumes": [],
    }
    for it in items:
        dt = datetime.datetime.fromtimestamp(it["date_ms"] / 1000)
        out["dates"].append(dt.strftime("%Y-%m-%d"))
        out["opens"].append(it.get("open_price"))
        out["highs"].append(it.get("high_price"))
        out["lows"].append(it.get("low_price"))
        out["closes"].append(it.get("close_price"))
        out["volumes"].append(it.get("volume") or 0)
    return out


def fetch_valuations(codes):
    """批量估值快照，返回 {code6: {pe_ttm, pe_mrq, pb_mrq, ps_ttm, pcf_ttm}}。"""
    ths = [_code_to_thscode(c) for c in codes]
    ths = [t for t in ths if t]
    if not ths:
        return {}
    out = {}
    # 大批量分批（单次 100 只以内，避免 URL 过长）
    for i in range(0, len(ths), 100):
        chunk = ths[i:i + 100]
        data = _get("/api/a-share/valuations/snapshot", {"thscodes": ",".join(chunk)})
        if not data:
            continue
        for it in data.get("item") or []:
            code6 = (it.get("thscode") or "")[:6]
            out[code6] = {
                "pe_ttm": it.get("pe_ttm"), "pe_mrq": it.get("pe_mrq"),
                "pb_mrq": it.get("pb_mrq"), "ps_ttm": it.get("ps_ttm"),
                "pcf_ttm": it.get("pcf_ttm"),
            }
    return out


def extract_financial_summary(fi: dict) -> dict:
    """把 fetch_financial_indicators 的原始分组结果映射为通用摘要字段。

    返回 {revenue_yoy, profit_yoy, roe, gross_margin, net_margin, debt_ratio}（缺省 None）。
    供 run_review 推荐财务栏、build_lowval 质量分等共用，避免多处重复 key 映射。
    """
    if not fi:
        return {}
    g = fi.get("growth", {})
    p = fi.get("profitability", {})
    s = fi.get("solvency", {})
    return {
        "revenue_yoy": g.get("calculate_operating_income_yoy_growth_ratio"),
        "profit_yoy": g.get("calculate_parent_holder_net_profit_yoy_growth_ratio"),
        "roe": p.get("index_weighted_avg_roe"),
        "gross_margin": p.get("sale_gross_margin"),
        "net_margin": p.get("sale_net_interest_ratio"),
        "debt_ratio": s.get("asset_liability_ratio"),
    }


def fetch_financial_indicators(code, report=None):
    """单只股票财务指标（按能力块分组）。

    report: 报告期如 '2026-1'（一季报）/ '2025-4'（年报）。缺省用最近一季。
    返回 {ability: {index_id: value}}，如 {'growth': {'revenue_yoy': '12.3'}, ...}
    """
    ths = _code_to_thscode(code)
    if not ths:
        return {}
    if not report:
        # 财报披露滞后：一季报4月底、中报8月底、三季报10月底、年报次年4月底
        # 5~8月→Q1(今年)、9~10月→Q2(今年)、11~12月→Q3(今年)、1~4月→Q4(去年)
        now = datetime.date.today()
        m = now.month
        if 5 <= m <= 8:
            report = "%d-1" % now.year
        elif 9 <= m <= 10:
            report = "%d-2" % now.year
        elif 11 <= m <= 12:
            report = "%d-3" % now.year
        else:  # 1~4 月：年报(去年 Q4)已披露
            report = "%d-4" % (now.year - 1)
    data = _get("/api/a-share/financials/indicators", {"thscode": ths, "report": report})
    if not data:
        return {}
    out = {}
    for ab in data.get("abilities") or []:
        out[ab.get("ability", "")] = {
            (ind.get("index_id") or ""): ind.get("value")
            for ind in (ab.get("indicators") or [])
        }
    return out


def fetch_limit_up_pool(date=None):
    """涨停股票池。date: yyyy-MM-dd，缺省最近交易日。"""
    params = {"date": date} if date else {}
    data = _get("/api/a-share/special-data/limit-up-pool", params)
    if not data:
        return []
    return data.get("stock_items") or []


def fetch_limit_down_pool(date=None):
    """跌停股票池。"""
    params = {"date": date} if date else {}
    data = _get("/api/a-share/special-data/limit-down-pool", params)
    if not data:
        return []
    return data.get("stock_items") or []


def fetch_limit_break_pool(date=None):
    """炸板股票池。"""
    params = {"date": date} if date else {}
    data = _get("/api/a-share/special-data/limit-break-pool", params)
    if not data:
        return []
    return data.get("stock_items") or []


def fetch_limit_up_ladder():
    """连板天梯（近 30 交易日连板梯队矩阵）。"""
    data = _get("/api/a-share/special-data/limit-up-ladder")
    if not data:
        return {}
    return data


def fetch_dragon_tiger_list(board_type="all", date=None):
    """龙虎榜。board_type: all/org/hot_money。"""
    params = {"board_type": board_type}
    if date:
        params["date"] = date
    data = _get("/api/a-share/special-data/dragon-tiger-list", params)
    if not data:
        return {}
    return data


def fetch_hot_stock_list(period_type="1d"):
    """A股热股榜 Top30。period_type: 1d / intraday。"""
    data = _get("/api/a-share/special-data/hot-stock-list", {"period_type": period_type})
    if not data:
        return []
    return data.get("item") or []


def fetch_skyrocket_list(period_type="1d"):
    """飙升榜 Top30。period_type: 1d / intraday。"""
    data = _get("/api/a-share/special-data/skyrocket-list", {"period_type": period_type})
    if not data:
        return []
    return data.get("item") or []


def fetch_anomaly_list(tag_codes=None):
    """当日个股异动原因列表。tag_codes: LIMIT_UP/LIMIT_DOWN/SHARP_RISE/... 逗号分隔。"""
    params = {"tag_codes": tag_codes} if tag_codes else {}
    data = _get("/api/a-share/special-data/anomaly-analysis-list", params)
    if not data:
        return []
    return data.get("item") or []


def fetch_anomaly_stock(codes):
    """按代码批量查询当日个股异动原因。"""
    ths = [_code_to_thscode(c) for c in codes]
    ths = [t for t in ths if t]
    if not ths:
        return {}
    data = _get("/api/a-share/special-data/anomaly-analysis-stock", {"thscodes": ",".join(ths)})
    if not data:
        return {}
    return data


def available():
    """API key 是否已配置且可用（轻量探测）。"""
    key = api_key()
    return bool(key)
