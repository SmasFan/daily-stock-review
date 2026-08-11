#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拉取 A 股主要宽基指数与行业板块估值（PE/PB），生成 sector_valuation_data.js。
数据源优先级：
  1. 腾讯财经实时行情接口（价格、涨跌幅、PE-TTM）
  2. 东方财富行情接口（PE、PB 补全）
  3. 本地静态兜底表（部分指数在线接口无 PB 或停牌时的近似值）
"""
import json
import os
import re
import ssl
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(BASE_DIR, "..", "sector_valuation_data.js")

SECTORS = [
    # 宽基指数
    {"name": "上证指数", "code": "000001", "symbol": "sh000001", "category": "宽基指数", "secid": "1.000001"},
    {"name": "深证成指", "code": "399001", "symbol": "sz399001", "category": "宽基指数", "secid": "0.399001"},
    {"name": "创业板指", "code": "399006", "symbol": "sz399006", "category": "宽基指数", "secid": "0.399006"},
    {"name": "科创50", "code": "000688", "symbol": "sh000688", "category": "宽基指数", "secid": "1.000688"},
    {"name": "上证50", "code": "000016", "symbol": "sh000016", "category": "宽基指数", "secid": "1.000016"},
    {"name": "沪深300", "code": "000300", "symbol": "sh000300", "category": "宽基指数", "secid": "1.000300"},
    {"name": "中证500", "code": "000905", "symbol": "sh000905", "category": "宽基指数", "secid": "1.000905"},
    {"name": "中证1000", "code": "000852", "symbol": "sh000852", "category": "宽基指数", "secid": "1.000852"},
    # 行业板块（重点：非银金融）
    {"name": "非银金融", "code": "399966", "symbol": "sz399966", "category": "金融地产", "secid": "0.399966", "highlight": True},
    {"name": "证券公司", "code": "399975", "symbol": "sz399975", "category": "金融地产", "secid": "0.399975"},
    {"name": "中证银行", "code": "399986", "symbol": "sz399986", "category": "金融地产", "secid": "0.399986"},
    {"name": "中证医药", "code": "399933", "symbol": "sz399933", "category": "医药消费", "secid": "0.399933"},
    {"name": "中证医疗", "code": "399989", "symbol": "sz399989", "category": "医药消费", "secid": "0.399989"},
    {"name": "中证白酒", "code": "399997", "symbol": "sz399997", "category": "医药消费", "secid": "0.399997"},
    {"name": "中证新能", "code": "399808", "symbol": "sz399808", "category": "科技制造", "secid": "0.399808"},
    {"name": "中证军工", "code": "399967", "symbol": "sz399967", "category": "科技制造", "secid": "0.399967"},
    {"name": "CSSW电子", "code": "399811", "symbol": "sz399811", "category": "科技制造", "secid": "0.399811"},
    # 2026-08-11：300基建(399950)/内地地产(399948) 数据源已冻结（腾讯恒返回 09:00 开盘前快照、无 PE），
    # 替换为活跃指数：基建工程(399995)、国证地产(399393)
    {"name": "国证地产", "code": "399393", "symbol": "sz399393", "category": "金融地产", "secid": "0.399393"},
    {"name": "基建工程", "code": "399995", "symbol": "sz399995", "category": "周期基建", "secid": "0.399995"},
    {"name": "养老产业", "code": "399812", "symbol": "sz399812", "category": "医药消费", "secid": "0.399812"},
    {"name": "一带一路", "code": "399991", "symbol": "sz399991", "category": "周期基建", "secid": "0.399991"},
]

# 静态兜底估值：用于在线接口无数据或停牌时的近似展示，定期可手动校正。
FALLBACK_VALUATION = {
    "000001": {"pe": 17.5, "pb": 1.40},
    "399001": {"pe": 44.0, "pb": 3.00},
    "399006": {"pe": 58.0, "pb": 5.00},
    "000688": {"pe": 200.0, "pb": 5.50},
    "000016": {"pe": 11.5, "pb": 1.20},
    "000300": {"pe": 14.0, "pb": 1.38},
    "000905": {"pe": 35.0, "pb": 1.80},
    "000852": {"pe": 42.0, "pb": 2.20},
    "399966": {"pe": 10.0, "pb": 1.45},
    "399975": {"pe": 15.0, "pb": 1.35},
    "399986": {"pe": 7.0, "pb": 0.65},
    "399933": {"pe": 32.0, "pb": 3.50},
    "399989": {"pe": 30.0, "pb": 4.00},
    "399997": {"pe": 20.0, "pb": 6.00},
    "399808": {"pe": 34.0, "pb": 2.50},
    "399967": {"pe": 61.0, "pb": 3.00},
    "399811": {"pe": 64.0, "pb": 3.50},
    "399393": {"pe": 20.0, "pb": 1.00},
    "399995": {"pe": 10.0, "pb": 1.20},
    "399812": {"pe": 11.5, "pb": 1.30},
    "399991": {"pe": 17.5, "pb": 1.20},
}


def fetch_url(url, encoding="utf-8", timeout=15):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    # push2.eastmoney.com 需要 Referer
    if "eastmoney.com" in url:
        headers["Referer"] = "https://quote.eastmoney.com/"
    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read().decode(encoding, errors="ignore")
    except Exception as e:
        print(f"  请求失败 {url[:80]}...: {e}")
        return ""


def fetch_tencent_quotes(symbols):
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    return fetch_url(url, encoding="gbk", timeout=30)


def parse_tencent(raw):
    records = {}
    for sector in SECTORS:
        sym = sector["symbol"]
        m = re.search(rf'v_{sym}="([^"]*)";', raw)
        if not m:
            continue
        parts = m.group(1).split("~")
        if len(parts) < 45:
            continue
        try:
            price = float(parts[3]) if parts[3] else 0
            prev = float(parts[4]) if parts[4] else 0
            change = float(parts[32]) if parts[32] else 0
            pe = float(parts[39]) if parts[39] else None
        except ValueError:
            continue
        records[sector["code"]] = {
            "price": round(price, 2),
            "prevClose": round(prev, 2),
            "change": round(change, 2),
            "pe": round(pe, 2) if pe else None,
        }
    return records


# 东财实时接口域名（push2delay 优先防 WAF 限流，与 src/fund_flow.py 同策略）
EM_HOSTS = ["push2delay.eastmoney.com", "push2.eastmoney.com", "push2his.eastmoney.com"]


def fetch_eastmoney_valuation(secid):
    """从东方财富接口取 PE/PB。f162=动态PE, f163=PB, f167=TTM PE。

    2026-08-11 修正：push2 主域名被 WAF 限流（Remote end closed connection），
    依次换 push2delay/push2his 重试（与 src/fund_flow.py 同策略）。
    """
    base = (
        "https://push2.eastmoney.com/api/qt/stock/get"
        "?ut=bd1d9ddb04089700cf9c27f6f7426281"
        "&fltt=2&invt=2&volt=2"
        "&fields=f43,f162,f163,f167"
        f"&secid={secid}"
    )
    text = ""
    for host in EM_HOSTS:
        u = base.replace("https://push2.eastmoney.com", f"https://{host}")
        text = fetch_url(u, timeout=10)
        if text:
            break
    if not text:
        return None, None
    try:
        data = json.loads(text)
        item = data.get("data", {}) or {}
        # fltt=2 时价格类字段已做精度处理
        pe = item.get("f167") or item.get("f162")
        pb = item.get("f163")
        # 过滤掉 0、-1、空字符串等无效值
        pe_f = float(pe) if pe not in (None, "", "-", "0", 0, -1) else None
        pb_f = float(pb) if pb not in (None, "", "-", "0", 0, -1) else None
        return pe_f, pb_f
    except Exception as e:
        print(f"  解析东财数据失败 {secid}: {e}")
        return None, None


def valuation_level(pe):
    """根据 PE 给出简单估值分档。"""
    if pe is None or pe <= 0:
        # PE<=0：亏损行业（如地产），PE 无意义，不参与估值分档
        return "—"
    if pe < 15:
        return "低估"
    if pe < 25:
        return "合理"
    if pe < 40:
        return "偏高"
    return "高估"


def main():
    print("==> 开始拉取板块估值数据...")

    # 1. 腾讯接口：价格、涨跌幅、PE
    symbols = [s["symbol"] for s in SECTORS]
    raw = fetch_tencent_quotes(symbols)
    tencent = parse_tencent(raw) if raw else {}
    print(f"  腾讯接口返回 {len(tencent)} 条")

    # 2. 东财接口补全 PB / 缺失 PE
    eastmoney_ok = 0
    for sector in SECTORS:
        code = sector["code"]
        secid = sector.get("secid")
        if not secid:
            continue
        t_pe = (tencent.get(code) or {}).get("pe")
        need_pb = True
        need_pe = t_pe is None
        if not need_pb and not need_pe:
            continue
        em_pe, em_pb = fetch_eastmoney_valuation(secid)
        if em_pe is not None or em_pb is not None:
            eastmoney_ok += 1
        if code not in tencent:
            tencent[code] = {"price": 0, "prevClose": 0, "change": 0, "pe": None}
        rec = tencent[code]
        if need_pe and em_pe is not None:
            rec["pe"] = round(em_pe, 2)
        if em_pb is not None:
            rec["pb"] = round(em_pb, 2)
    print(f"  东财接口补全 {eastmoney_ok} 条")

    # 3. 静态兜底 + 组装输出
    records = []
    for sector in SECTORS:
        code = sector["code"]
        rec = tencent.get(code, {"price": 0, "prevClose": 0, "change": 0, "pe": None})
        fallback = FALLBACK_VALUATION.get(code, {})

        pe = rec.get("pe") if rec.get("pe") is not None else fallback.get("pe")
        pb = rec.get("pb") if rec.get("pb") is not None else fallback.get("pb")

        records.append({
            "name": sector["name"],
            "code": code,
            "category": sector["category"],
            "highlight": sector.get("highlight", False),
            "price": rec.get("price", 0),
            "prevClose": rec.get("prevClose", 0),
            "change": rec.get("change", 0),
            "pe": round(pe, 2) if pe is not None else None,
            "pb": round(pb, 2) if pb is not None else None,
            "level": valuation_level(pe),
        })

    data = {
        "sectors": records,
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dataSource": "腾讯行情 + 东方财富（PE/PB 补全）",
    }

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write("// Auto-generated by scripts/update_sector_valuation.py\n")
        f.write("window.sectorValuationData = ")
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write(";\n")

    print(f"==> 已生成 {OUTPUT}，共 {len(records)} 条板块估值记录。")
    # 打印重点板块
    for r in records:
        if r["highlight"] or r["pe"] is None or r["pb"] is None:
            print(f"  {r['name']:8s} PE={r['pe'] if r['pe'] is not None else '—':>6} PB={r['pb'] if r['pb'] is not None else '—':>6} 涨跌={r['change']:+.2f}%")


if __name__ == "__main__":
    main()
