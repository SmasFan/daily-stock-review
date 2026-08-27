# -*- coding: utf-8 -*-
"""
资金流向与机构动向数据模块（2026-08 新增）。

数据源：
- 新浪资金流向（个股，含超大单/大单/中单/小单拆分）：
    https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs
- 东方财富（实时排行/板块/北向）：
    push2delay.eastmoney.com / push2.eastmoney.com / push2his.eastmoney.com
- 东方财富数据中心（龙虎榜机构席位、十大流通股东）：
    datacenter-web.eastmoney.com

说明：
- 主力 = 超大单(r0) + 大单(r1)；中单(r2)/小单(r3) 为散户口径
- 板块资金/排行接口带 0.5s 节流，失败自动回退备用域名
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0 Safari/537.36"}
REF_EM = {"User-Agent": UA["User-Agent"], "Referer": "https://data.eastmoney.com/"}
REF_SINA = {"User-Agent": UA["User-Agent"], "Referer": "https://finance.sina.com.cn/"}

EM_HOSTS = ["push2delay.eastmoney.com", "push2.eastmoney.com", "push2his.eastmoney.com"]
DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# 全 A 股（沪深主板+创业板+科创板，含北交所）
FS_STOCKS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
FS_INDUSTRY = "m:90+t:2"
FS_CONCEPT = "m:90+t:3"


def _get(url: str, headers: Dict, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _em_get(url: str, timeout: int = 15) -> str:
    """东财实时接口：请求 url 中的域名失败时依次换其他域名重试（WAF 会临时限流）。"""
    m = re.search(r"https://([a-z0-9.]+\.eastmoney\.com)/", url)
    base_host = m.group(1) if m else EM_HOSTS[0]
    last = ""
    for host in [base_host] + [h for h in EM_HOSTS if h != base_host]:
        u = url.replace(base_host, host) if host != base_host else url
        try:
            return _get(u, REF_EM, timeout=timeout)
        except Exception as e:
            last = str(e)
    raise RuntimeError(f"东财接口不可用: {last}")


def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, f"fflow_{key}.json")


def _cache_get(key: str, max_age_hours: float = 12):
    cp = _cache_path(key)
    if os.path.exists(cp):
        age = time.time() - os.path.getmtime(cp)
        if age < max_age_hours * 3600:
            try:
                with open(cp, "r", encoding="utf-8") as fp:
                    return json.load(fp)
            except Exception:
                pass
    return None


def _cache_set(key: str, data) -> None:
    try:
        with open(_cache_path(key), "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False)
    except Exception:
        pass


# ---------------- 个股资金流（东方财富 fflow kline） ----------------

def _sina_fflow_cumulative(code: str, days: int = 10) -> Optional[Dict]:
    """新浪资金流向（备用源）：返回近 N 日超大单(r0)累计净流入。

    新浪接口不含大单字段，5日/10日累计采用超大单口径（标注 cum_source=sina_super）。
    """
    sym = ("sh" if code.startswith(("6", "5", "9", "11")) else "sz") + code
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num={days}&sort=opendate&asc=0&daima={sym}")
    try:
        raw = _get(url, REF_SINA)
        rows = json.loads(raw)
    except Exception:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    r0 = [float(r.get("r0_net") or 0) for r in rows]
    return {"main_net_5d": round(sum(r0[:5]), 2), "main_net_10d": round(sum(r0[:10]), 2)}


def fetch_stock_fflow_history(code: str, days: int = 10) -> Optional[Dict]:
    """个股资金流向历史（东财 fflow/kline，主力=超大单+大单）。

    kline 每行: [date, 主力净流入, 小单净流入, 中单净流入, 大单净流入, 超大单净流入,
                主力净占比%, 小单%, 中单%, 大单%, 超大单%, 收盘价, 涨跌幅%, ...]
    返回 {today, main_net_5d, main_net_10d, cum_source, dates}。
    push2his 提供多日历史但易被限流，失败时仅保留当日数据（push2/push2delay）。
    """
    market = 1 if code.startswith(("6", "5", "9", "11")) else 0
    url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/kline/get"
           f"?lmt=0&klt=101&secid={market}.{code}"
           "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63")
    try:
        raw = _em_get(url)
        d = json.loads(raw)
        klines = ((d.get("data") or {}).get("klines") or [])
    except Exception:
        klines = []
    if not klines:
        return None
    items = []
    for line in klines:
        p = line.split(",")
        if len(p) < 2:
            continue
        def fv(i):
            try:
                return float(p[i])
            except (ValueError, IndexError):
                return 0.0
        items.append({
            "date": p[0],
            "main_net": fv(1),
            "small_net": fv(2),
            "mid_net": fv(3),
            "big_net": fv(4),
            "super_net": fv(5),
            "main_ratio": fv(6),
            "close": fv(11),
            "change_pct": fv(12),
        })
    if not items:
        return None
    items.sort(key=lambda x: x["date"], reverse=True)
    main_nets = [x["main_net"] for x in items]
    out = {
        "dates": [x["date"] for x in items],
        "today": items[0],
        "main_net_5d": round(sum(main_nets[:5]), 2) if len(items) >= 2 else None,
        "main_net_10d": round(sum(main_nets[:10]), 2) if len(items) >= 2 else None,
        "cum_source": "eastmoney",
    }
    if len(items) == 1:
        # push2his 被限流只有当日 → 用新浪超大单口径补累计（趋势参考）
        s = _sina_fflow_cumulative(code, days=10)
        if s:
            out["main_net_5d"], out["main_net_10d"] = s["main_net_5d"], s["main_net_10d"]
            out["cum_source"] = "sina_super"
    return out


def fetch_stock_fflow(code: str, use_cache: bool = True,
                      cache_max_age_hours: float = 8) -> Optional[Dict]:
    """个股资金流摘要（复盘用）：当日 + 近5日/近10日累计。带本地缓存。"""
    cp = _cache_path(code)
    if use_cache and os.path.exists(cp):
        age = time.time() - os.path.getmtime(cp)
        if age < cache_max_age_hours * 3600:
            try:
                with open(cp, "r", encoding="utf-8") as fp:
                    return json.load(fp)
            except Exception:
                pass
    h = fetch_stock_fflow_history(code, days=10)
    if not h or not h["today"]:
        return None
    t = h["today"]
    out = {
        "date": t["date"],
        "main_net": t["main_net"],
        "main_ratio": t["main_ratio"],
        "super_net": t["super_net"],
        "big_net": t["big_net"],
        "mid_net": t["mid_net"],
        "small_net": t["small_net"],
        "main_net_5d": h["main_net_5d"],
        "main_net_10d": h["main_net_10d"],
        "cum_source": h.get("cum_source"),
        "close": t["close"],
        "change_pct": t["change_pct"],
    }
    if use_cache:
        try:
            with open(cp, "w", encoding="utf-8") as fp:
                json.dump(out, fp, ensure_ascii=False)
        except Exception:
            pass
    return out


def fetch_fflow_batch(codes, sleep: float = 0.25) -> Dict[str, Dict]:
    """批量拉取个股资金流摘要，带限速。返回 {code: {...}}。"""
    out = {}
    for c in codes:
        try:
            f = fetch_stock_fflow(c)
            if f:
                out[c] = f
        except Exception:
            pass
        time.sleep(sleep)
    return out


# ---------------- 东方财富实时排行 ----------------

def _clist(url: str) -> Optional[List[Dict]]:
    raw = _em_get(url)
    d = json.loads(raw)
    return (d.get("data") or {}).get("diff") or []


def _sector_rank(fs: str, top: int = 10) -> Dict:
    """板块主力净流入排行（行业/概念）。f62=主力净流入, f184=主力净占比, f3=涨跌幅。"""
    url_in = ("https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz={pz}&po=1&np=1&fltt=2&invt=2"
              f"&fid=f62&fs={fs}&fields=f12,f14,f62,f184,f3").format(pz=top)
    url_out = url_in.replace("po=1", "po=0")

    def clean(rows):
        return [{"code": r.get("f12"), "name": r.get("f14"),
                 "main_net": round((r.get("f62") or 0), 2),
                 "main_ratio": round((r.get("f184") or 0), 2),
                 "change_pct": r.get("f3")} for r in rows]
    try:
        time.sleep(0.4)
        inflow = clean(_clist(url_in))
        time.sleep(0.4)
        outflow = clean(_clist(url_out))
    except Exception:
        return {"inflow": [], "outflow": []}
    return {"inflow": inflow, "outflow": outflow}


def fetch_sector_fflow(top: int = 10) -> Dict:
    """行业 + 概念板块主力资金排行。"""
    return {"industry": _sector_rank(FS_INDUSTRY, top),
            "concept": _sector_rank(FS_CONCEPT, top)}


def fetch_stock_fflow_rank(top: int = 20) -> Dict:
    """全市场个股主力净流入/净流出榜（f62 主力净流入, f184 净占比, f2 现价, f3 涨跌幅）。"""
    def clean(rows):
        return [{"code": r.get("f12"), "name": r.get("f14"),
                 "price": r.get("f2"), "change_pct": r.get("f3"),
                 "main_net": round((r.get("f62") or 0), 2),
                 "main_ratio": round((r.get("f184") or 0), 2)} for r in rows]
    base = ("https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz={pz}&po={po}&np=1&fltt=2&invt=2"
            f"&fid=f62&fs={FS_STOCKS}&fields=f12,f14,f2,f3,f62,f184")
    try:
        time.sleep(0.4)
        inflow = clean(_clist(base.format(pz=top, po=1)))
        time.sleep(0.4)
        outflow = clean(_clist(base.format(pz=top, po=0)))
    except Exception:
        return {"inflow": [], "outflow": []}
    return {"inflow": inflow, "outflow": outflow}


def fetch_market_fflow_overview() -> Optional[Dict]:
    """全市场主力资金概览：以行业板块（东方财富分类，覆盖全A）求和口径。

    行业板块数约 500 个，clist 单页上限 100，分页拉全后汇总：
    - main_net: 板块主力净流入之和 ≈ 全市场主力净流入
    - inflow/outflow: 净流入/净流出板块数
    """
    total = 0.0
    up = down = 0
    pn = 1
    got = 0
    while pn <= 8:
        url = ("https://push2delay.eastmoney.com/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2"
               f"&fid=f62&fs={FS_INDUSTRY}&fields=f12,f62").format(pn=pn)
        try:
            raw = _em_get(url)
            d = json.loads(raw)
            data = (d.get("data") or {})
            rows = data.get("diff") or []
        except Exception:
            return None
        if not rows:
            break
        for r in rows:
            v = r.get("f62") or 0
            total += v
            if v > 0:
                up += 1
            elif v < 0:
                down += 1
        got += len(rows)
        all_total = (d.get("data") or {}).get("total") or 0
        if got >= all_total or len(rows) < 100:
            break
        pn += 1
        time.sleep(0.4)
    if not got:
        return None
    return {"main_net": round(total, 2), "inflow_sectors": up,
            "outflow_sectors": down, "sectors": got, "basis": "行业板块求和"}


def fetch_north_money() -> Optional[Dict]:
    """沪深港通当日额度（北向实时数据已于 2024-08 停止披露，返回最近额度状态）。"""
    url = "https://push2delay.eastmoney.com/api/qt/kamt/get?fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55,f56"
    try:
        raw = _em_get(url)
        d = json.loads(raw)
        data = (d.get("data") or {})
    except Exception:
        return None
    if not data:
        return None
    out = {"date": None, "hk2sh": None, "sh2hk": None, "note": None}
    for key, name in (("hk2sh", "北向(沪股通)"), ("sh2hk", "北向(深股通)")):
        v = data.get(key) or {}
        if v.get("date2"):
            out["date"] = v["date2"]
        out[key] = {"name": name, "net": v.get("dayNetAmtIn"),
                    "threshold": v.get("dayAmtThreshold"), "status": v.get("status")}
    out["note"] = "北向资金实时净买入自 2024-08 起停止披露，以上为额度快照，仅供参考"
    return out


# ---------------- 龙虎榜机构席位（东财数据中心） ----------------

def fetch_billboard_institution(days_back: int = 10) -> Dict:
    """最近交易日的龙虎榜机构专用席位动向。

    聚合 RPT_BILLBOARD_DAILYDETAILSBUY（买入明细）与
    RPT_BILLBOARD_DAILYDETAILSSELL（卖出明细）中 OPERATEDEPT_NAME="机构专用" 的记录。
    返回最近有数据的一个交易日 {date, date_name, buys, sells, by_date: [...]}。
    """
    def query(report: str, size: int = 300) -> List[Dict]:
        url = (f"{DATACENTER}?reportName={report}&columns=ALL&pageSize={size}&pageNumber=1"
               "&sortColumns=TRADE_DATE&sortTypes=-1")
        raw = _get(url, REF_EM)
        d = json.loads(raw)
        return (d.get("result") or {}).get("data") or []

    try:
        buy_rows = query("RPT_BILLBOARD_DAILYDETAILSBUY")
        time.sleep(0.4)
        sell_rows = query("RPT_BILLBOARD_DAILYDETAILSSELL")
    except Exception:
        return {"date": None, "buys": [], "sells": []}

    def is_inst(r):
        return "机构专用" in (r.get("OPERATEDEPT_NAME") or "")

    # 按交易日分组
    from collections import defaultdict
    by_date = defaultdict(lambda: {"buys": [], "sells": []})
    for r in buy_rows:
        if is_inst(r):
            d = r.get("TRADE_DATE", "")[:10]
            by_date[d]["buys"].append({
                "code": r.get("SECURITY_CODE"), "name": r.get("SECURITY_NAME_ABBR", r.get("SECURITY_CODE")),
                "reason": r.get("EXPLANATION"), "buy": r.get("BUY"), "sell": r.get("SELL"),
                "net": r.get("NET"), "close": r.get("CLOSE_PRICE"), "change_pct": r.get("CHANGE_RATE"),
            })
    for r in sell_rows:
        if is_inst(r):
            d = r.get("TRADE_DATE", "")[:10]
            by_date[d]["sells"].append({
                "code": r.get("SECURITY_CODE"), "name": r.get("SECURITY_NAME_ABBR", r.get("SECURITY_CODE")),
                "reason": r.get("EXPLANATION"), "buy": r.get("BUY"), "sell": r.get("SELL"),
                "net": r.get("NET"), "close": r.get("CLOSE_PRICE"), "change_pct": r.get("CHANGE_RATE"),
            })

    if not by_date:
        return {"date": None, "buys": [], "sells": [], "by_date": []}

    latest = max(by_date.keys())
    # 合并买入+卖出为个股净额榜
    merged = {}
    for r in by_date[latest]["buys"]:
        merged.setdefault(r["code"], {"name": r["name"], "inst_buy": 0, "inst_sell": 0})
        merged[r["code"]]["inst_buy"] += (r["buy"] or 0)
    for r in by_date[latest]["sells"]:
        merged.setdefault(r["code"], {"name": r["name"], "inst_buy": 0, "inst_sell": 0})
        merged[r["code"]]["inst_sell"] += (r["sell"] or 0)
    net_list = []
    for code, v in merged.items():
        net_list.append({"code": code, "name": v["name"],
                         "inst_buy": round(v["inst_buy"], 2), "inst_sell": round(v["inst_sell"], 2),
                         "inst_net": round(v["inst_buy"] - v["inst_sell"], 2)})
    net_list.sort(key=lambda x: x["inst_net"], reverse=True)
    return {"date": latest,
            "buys": by_date[latest]["buys"], "sells": by_date[latest]["sells"],
            "net_list": net_list}


# ---------------- 国家队/机构持股扫描（东财数据中心 · 十大流通股东） ----------------

# 国家队/机构名称关键词 -> 归类
INST_KEYWORDS = [
    ("central_hj", "中央汇金", ("汇金",)),
    ("central_zj", "中国证金", ("证券金融",)),
    ("social_security", "社保基金", ("社保",)),
    ("pension", "养老金/基本养老", ("养老",)),
    ("insurance", "保险", ("保险", "人寿", "人保", "平安", "泰康", "新华")),
    ("fund", "公募基金", ("基金",)),
    ("qfii", "QFII/外资", ("QFII", "境外机构", "香港中央结算")),
    ("broker", "券商", ("证券", "中信", "国泰", "华泰", "广发")),
    ("company", "国家队/央企", ("中央", "国资", "国家")),
]


def classify_holder(name: str) -> Optional[str]:
    for key, label, kws in INST_KEYWORDS:
        if any(k in name for k in kws):
            return key
    return None


def fetch_top10_holders(code: str) -> List[Dict]:
    """十大流通股东（东财数据中心 RPT_F10_EH_FREEHOLDERS，最新一期）。"""
    url = (f"{DATACENTER}?reportName=RPT_F10_EH_FREEHOLDERS&columns=ALL&pageSize=10&pageNumber=1"
           f"&filter=(SECURITY_CODE%3D%22{code}%22)")
    try:
        raw = _get(url, REF_EM)
        d = json.loads(raw)
        rows = (d.get("result") or {}).get("data") or []
    except Exception:
        return []
    return [{
        "code": code,
        "name": r.get("SECURITY_NAME_ABBR", code),
        "holder": r.get("HOLDER_NAME"),
        "rank": r.get("HOLDER_RANK"),
        "hold_num": r.get("HOLD_NUM"),
        "hold_ratio": r.get("FREE_HOLDNUM_RATIO") or r.get("HOLD_RATIO"),
        "change": r.get("HOLD_NUM_CHANGE") or r.get("HOLD_CHANGE"),
        "change_ratio": r.get("CHANGE_RATIO"),
        "holder_type": r.get("HOLDER_TYPE"),
        "end_date": (r.get("END_DATE") or "")[:10],
        "report": r.get("REPORT_DATE_NAME"),
    } for r in rows]


def scan_institutional_holdings(codes: List[str], sleep: float = 0.3) -> Dict:
    """对股票池扫描十大流通股东，识别国家队/机构持仓与增减持。

    返回: {national: [持股明细], by_type: {类型: [明细]}, summary: {类型: 家数}}
    """
    out = []
    for code in codes:
        try:
            rows = fetch_top10_holders(code)
        except Exception:
            rows = []
        for r in rows:
            cat = classify_holder(r.get("holder") or "")
            if cat:
                r["cat"] = cat
                out.append(r)
        time.sleep(sleep)
    by_type: Dict[str, list] = {}
    national = []
    for r in out:
        by_type.setdefault(r["cat"], []).append(r)
        if r["cat"] in ("central_hj", "central_zj", "social_security", "pension"):
            national.append(r)
    # 按持仓市值排序
    for k in by_type:
        by_type[k].sort(key=lambda x: x.get("hold_ratio") or 0, reverse=True)
    national.sort(key=lambda x: x.get("hold_ratio") or 0, reverse=True)
    return {"national": national, "by_type": by_type,
            "summary": {k: len(v) for k, v in by_type.items()}}


def build_institution_data(codes: List[str]) -> Dict:
    """组装机构资金动向页数据（单次调用，含全部分区）。"""
    print("== 资金流与机构数据 ==")
    overview = fetch_market_fflow_overview()
    if overview:
        print(f"   全市场主力净流入 {overview['main_net'] / 1e8:.1f}亿，"
              f"流入 {overview['inflow_sectors']} / 流出 {overview['outflow_sectors']} 板块")
    else:
        print("   [warn] 全市场资金概览失败")
    time.sleep(0.5)
    sector = fetch_sector_fflow(top=10)
    time.sleep(0.5)
    stock_rank = fetch_stock_fflow_rank(top=20)
    billboard = fetch_billboard_institution()
    if billboard.get("date"):
        print(f"   龙虎榜机构席位({billboard['date']}): {len(billboard.get('net_list', []))} 只")
    # 龙虎榜接口不含股票名，用腾讯批量快照补名
    try:
        from . import data_provider as _dp
        b_codes = [x["code"] for x in (billboard.get("net_list") or [])
                   + (billboard.get("buys") or []) + (billboard.get("sells") or [])]
        b_codes = list(dict.fromkeys([c for c in b_codes if c]))
        if b_codes:
            q = _dp.fetch_quotes(b_codes)
            for lst in (billboard.get("net_list"), billboard.get("buys"), billboard.get("sells")):
                for x in lst or []:
                    nm = (q.get(x.get("code")) or {}).get("name")
                    if nm:
                        x["name"] = nm
    except Exception:
        pass
    north = fetch_north_money()
    inst = scan_institutional_holdings(codes)
    print(f"   机构持股扫描: 命中 {sum(inst['summary'].values())} 条")
    data = {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overview": overview,
        "sector": sector,
        "stock_rank": stock_rank,
        "billboard": billboard,
        "north": north,
        "institution": inst,
        "scanned_codes": len(codes),
    }
    # 同花顺官方特色数据（需 API Key，失败静默跳过；2026-08 新增）
    try:
        from . import ths_api
        if ths_api.available():
            ths_part = {}
            try:
                dt = ths_api.fetch_dragon_tiger_list("all")
                ths_part["dragon_tiger"] = {
                    "date": dt.get("trade_date"), "count": dt.get("count"),
                    "items": (dt.get("stock_items") or [])[:20],
                }
                print(f"   同花顺龙虎榜({dt.get('trade_date')}): {dt.get('count')} 条")
            except Exception as e:
                print(f"   [warn] 同花顺龙虎榜失败: {e}")
            time.sleep(0.3)
            try:
                lu = ths_api.fetch_limit_up_pool()
                ths_part["limit_up"] = {
                    "date": (lu[0].get("trade_date") if lu else None),
                    "items": lu[:30],
                }
                print(f"   同花顺涨停池: {len(lu)} 只")
            except Exception as e:
                print(f"   [warn] 同花顺涨停池失败: {e}")
            time.sleep(0.3)
            try:
                ths_part["hot_list"] = ths_api.fetch_hot_stock_list()
                print(f"   同花顺热股榜: {len(ths_part['hot_list'])} 只")
            except Exception as e:
                print(f"   [warn] 同花顺热榜失败: {e}")
            time.sleep(0.3)
            try:
                ths_part["skyrocket"] = ths_api.fetch_skyrocket_list()
                print(f"   同花顺飙升榜: {len(ths_part['skyrocket'])} 只")
            except Exception as e:
                print(f"   [warn] 同花顺飙升榜失败: {e}")
            time.sleep(0.3)
            try:
                ths_part["anomaly"] = ths_api.fetch_anomaly_list()[:30]
                print(f"   同花顺个股异动: {len(ths_part['anomaly'])} 条")
            except Exception as e:
                print(f"   [warn] 同花顺异动失败: {e}")
            data["ths"] = ths_part
    except Exception as e:
        print(f"   [warn] 同花顺特色数据整体失败: {e}")
    # 给全部股票列表补当日行情（现价/涨跌幅/成交额/换手）：龙虎榜/国家队/机构/同花顺列表
    try:
        from . import data_provider as _dp2
        _q_codes = []
        for _x in (billboard.get("net_list") or []) + (billboard.get("buys") or []) + (billboard.get("sells") or []):
            if _x.get("code"):
                _q_codes.append(_x["code"])
        for _x in inst.get("national") or []:
            if _x.get("code"):
                _q_codes.append(_x["code"])
        for _lst in (inst.get("by_type") or {}).values():
            for _x in _lst or []:
                if _x.get("code"):
                    _q_codes.append(_x["code"])
        for _lst in (data.get("ths") or {}).values():
            _items = _lst.get("items") if isinstance(_lst, dict) else _lst
            for _x in _items or []:
                _c = _x.get("code") or _x.get("ticker")
                if _c:
                    _q_codes.append(str(_c).zfill(6))
        _q_codes = list(dict.fromkeys([c for c in _q_codes if c and c.isdigit()]))
        if _q_codes:
            _q = _dp2.fetch_quotes(_q_codes)
            def _patch(_x):
                _c = str(_x.get("code") or _x.get("ticker") or "").zfill(6)
                _qq = _q.get(_c) or {}
                if not _qq:
                    return
                _x["price"] = _qq.get("price")
                _x["change_pct"] = _qq.get("change")
                _x["amount"] = _qq.get("amount")
                _x["turnover"] = _qq.get("turnover")
                if not _x.get("name") and _qq.get("name"):
                    _x["name"] = _qq["name"]
            for _x in (billboard.get("net_list") or []) + (billboard.get("buys") or []) + (billboard.get("sells") or []):
                _patch(_x)
            for _x in inst.get("national") or []:
                _patch(_x)
            for _lst in (inst.get("by_type") or {}).values():
                for _x in _lst or []:
                    _patch(_x)
            for _lst in (data.get("ths") or {}).values():
                _items = _lst.get("items") if isinstance(_lst, dict) else _lst
                for _x in _items or []:
                    _patch(_x)
            print(f"   股票列表补当日行情: {len(_q_codes)} 只")
    except Exception as _e:
        print(f"   [warn] 补当日行情失败: {_e}")
    return data


def save_institution_data(data: Dict) -> str:
    path = os.path.join(BASE_DIR, "data", "institution_data.json")
    # 紧凑序列化：线上加载更快
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return path


if __name__ == "__main__":
    import sys
    sys.path.insert(0, BASE_DIR)
    from src.stock_pool import WATCHLIST_CODES, MARKET_POOL_CODES
    d = build_institution_data(list(dict.fromkeys(WATCHLIST_CODES + MARKET_POOL_CODES)))
    print(save_institution_data(d))
