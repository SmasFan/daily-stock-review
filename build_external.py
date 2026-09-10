#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部市场因子（隔夜/实时）→ A股板块偏好打分

为什么需要：sim_live 的大盘闸门此前只看国内宽度 + LLM 宏观文字，
对「美债收益率飙升 / 油价破百 / 纳指期指杀跌 / 黄金崩」这类外力完全瞎。
本模块把外盘行情量化成 **板块偏好分**，供 sim_live 选股/买点排序使用。

数据源（全部免费实时，无需 key）：
  腾讯 qt.gtimg.cn   : 美股指数（usDJI/usIXIC/usINX/usNDX）、美债ETF代理（usTLT/usIEF）
  新浪 hq.sinajs.cn  : 外盘期货（WTI原油/COMEX金/银/铜/天然气、三大期指）、美元指数(DINIW)、VIX(znb_VIX)
  本地 metals_data  : 国内期货（沪铜/沪金/沪银/原油/焦煤…），与 metals 页共用

产出 data/external_data.json：
  {generatedAt, quotes:{...}, factors:{name:{value,chg,level,note}},
   sector_bias:{板块: 分}, link_bias:{期货关键词: 分}, summary:[...]}

用法：
  python3 build_external.py            # 抓取并写盘（约 3~6 秒）
  python3 build_external.py --dry      # 只打印不写盘
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUT = os.path.join(DATA_DIR, "external_data.json")
METALS = os.path.join(DATA_DIR, "metals_data.json")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
SINA_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Referer": "https://finance.sina.com.cn"}


def _get(url, headers=UA, enc="utf-8", timeout=12):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(enc, errors="ignore")


# ---------------- 抓取 ----------------
TENCENT_SYMS = {
    "DJI": "usDJI", "IXIC": "usIXIC", "INX": "usINX", "NDX": "usNDX",
    "TLT": "usTLT", "IEF": "usIEF",
}


def _parse_tencent(text, code):
    """腾讯行情：v_usXXX="200~名称~代码~现价~昨收~今开~...~时间~涨跌~涨跌%~最高~最低~..." """
    m = re.search(r'v_%s="([^"]*)"' % re.escape(code), text)
    if not m:
        return None
    p = m.group(1).split("~")
    if len(p) < 33:
        return None
    def f(i):
        try:
            return float(p[i])
        except (ValueError, IndexError):
            return None
    price, prev = f(3), f(4)
    if not price or not prev:
        return None
    return {"name": p[1], "code": p[2], "price": price, "prevClose": prev,
            "open": f(5), "high": f(33), "low": f(34),
            "change_pct": round((price / prev - 1) * 100, 2),
            "ts": p[30] if len(p) > 30 else ""}


def fetch_tencent(syms):
    url = "https://qt.gtimg.cn/q=" + ",".join(syms)
    try:
        raw = _get(url, enc="gbk")
    except Exception as e:
        print("  [warn] 腾讯行情失败:", str(e)[:80])
        return {}
    out = {}
    for key, code in TENCENT_SYMS.items():
        if code in syms:
            q = _parse_tencent(raw, code)
            if q:
                out[key] = q
    return out


# 新浪外盘：逗号分隔 [现价,?,买,卖,高,低,时间,昨收,开,...]
SINA_SYMS = {
    "WTI": "hf_CL", "BRENT_X": "hf_OIL", "GOLD": "hf_GC", "GOLD_SPOT": "hf_XAU",
    "SILVER": "hf_SI", "COPPER": "hf_HG", "NGAS": "hf_NG",
    "ES": "hf_ES", "NQ": "hf_NQ", "YM": "hf_YM",
    "VIX": "znb_VIX", "DXY": "DINIW",
}


def _parse_sina_field(field):
    """hf_* 格式：现价,,买,卖,高,低,时间,昨收,开,..."""
    p = field.split(",")
    if len(p) < 8:
        return None
    def f(i):
        try:
            return float(p[i])
        except (ValueError, IndexError):
            return None
    price, prev = f(0), f(7)
    if not price or not prev:
        return None
    return {"price": price, "prevClose": prev, "open": f(8),
            "high": f(4), "low": f(5), "ts": p[6],
            "change_pct": round((price / prev - 1) * 100, 2)}


def _parse_sina_special(key, field):
    """DINIW(美元指数) / znb_*(VIX等) 格式与 hf_* 不同，单独解析。

    DINIW : 时间,现价,现价,昨收,?,今开,高,低,现价,名称,日期
    znb_* : 名称,现价,涨跌,涨跌%,,,日期,时间,昨收,开,高,低
    """
    p = field.split(",")
    def f(i):
        try:
            return float(p[i])
        except (ValueError, IndexError):
            return None
    if key == "DXY":
        price, prev = f(1), f(3)
        if not price or not prev:
            return None
        return {"price": price, "prevClose": prev, "open": f(5), "high": f(6), "low": f(7),
                "ts": (p[10] + " " + p[0]) if len(p) > 10 else p[0],
                "change_pct": round((price / prev - 1) * 100, 2)}
    if key == "VIX":
        price = f(1)
        chg_pct = f(3)
        if not price or chg_pct is None:
            return None
        return {"price": price, "prevClose": f(8), "open": f(9), "high": f(10), "low": f(11),
                "ts": ((p[6] + " " + p[7]) if len(p) > 7 else ""),
                "change_pct": round(chg_pct, 2)}
    return None


def fetch_sina():
    url = "https://hq.sinajs.cn/list=" + ",".join(SINA_SYMS.values())
    try:
        raw = _get(url, headers=SINA_UA, enc="gbk")
    except Exception as e:
        print("  [warn] 新浪外盘失败:", str(e)[:80])
        return {}
    out = {}
    for key, sym in SINA_SYMS.items():
        m = re.search(r'hq_str_%s="([^"]*)"' % re.escape(sym), raw)
        if not m or not m.group(1).strip():
            continue
        q = _parse_sina_special(key, m.group(1)) or _parse_sina_field(m.group(1))
        if q:
            out[key] = q
    return out


def fetch_domestic_futures():
    """国内期货（复用 metals_data，含原油/沪金/沪银/沪铜/焦煤等）。"""
    try:
        with open(METALS, encoding="utf-8") as f:
            d = json.load(f)
        return {x["name"]: {"price": x.get("price"), "change_pct": x.get("change"),
                            "category": x.get("category")} for x in (d.get("items") or [])}
    except Exception:
        return {}


# ---------------- 因子判定 ----------------
# 阈值：涨跌幅绝对值超过阈值才算"有效信号"，避免噪音触发偏好
TH = 0.8


def _lvl(chg, th=TH):
    if chg is None:
        return 0
    if chg >= th * 2:
        return 2
    if chg >= th:
        return 1
    if chg <= -th * 2:
        return -2
    if chg <= -th:
        return -1
    return 0


# 因子 → 板块偏好。value 为正表示"该板块受益"，负表示受损。
# 板块名须与 review_data.items[].sector 一致（见下方 SECTORS 校验）
FACTOR_RULES = {
    "oil":     {"label": "原油", "bull": {"石油石化": 1.0, "煤炭": 0.6, "油服": 0.8, "新能源电力": 0.2},
                "bear": {"汽车零部件": 0.5, "交通运输": 0.8, "基础化工": 0.5, "汽车": 0.5}},
    "gold":    {"label": "黄金", "bull": {"黄金": 1.2, "贵金属": 1.2}, "bear": {}},
    "silver":  {"label": "白银", "bull": {"贵金属": 1.0}, "bear": {}},
    "copper":  {"label": "铜", "bull": {"周期资源": 0.8}, "bear": {}},
    # 美债收益率↑（TLT/IEF 价格↓）→ 贴现率↑，压制高估值成长；红利/公用防御受益
    "yield":   {"label": "美债收益率", "bull": {"红利银行": 0.8, "红利金融": 0.8, "红利非银": 0.6,
                                               "公用事业": 0.6, "红利": 0.6},
                "bear": {"AI算力": 1.2, "CPO/光模块": 1.2, "半导体": 1.0,
                         "科技-通信电子": 1.0, "科技-互联网传媒": 0.8, "机器人": 0.8, "军工": 0.5}},
    "dxy":     {"label": "美元指数", "bull": {}, "bear": {"周期资源": 0.6, "贵金属": 0.8}},
    "nasdaq":  {"label": "纳指(隔夜/期指)", "bull": {"AI算力": 1.0, "半导体": 0.9, "CPO/光模块": 1.0,
                                                 "科技-通信电子": 0.9, "机器人": 0.6},
                "bear": {"红利银行": 0.6, "公用事业": 0.5, "大消费": 0.4}},
    "vix":     {"label": "VIX恐慌", "bull": {"红利银行": 0.5, "公用事业": 0.5},
                "bear": {"AI算力": 0.8, "半导体": 0.7, "机器人": 0.7, "军工": 0.5}},
    "sp500":   {"label": "标普期指", "bull": {"AI算力": 0.6, "半导体": 0.5, "周期资源": 0.4},
                "bear": {"红利银行": 0.4, "公用事业": 0.4}},
    # 补覆盖：利率/风险偏好敏感但方向不同于主规则
    # PCB/覆铜板：电子成长（利率敏感），但自身有涨价产业逻辑 → 权重低于半导体
    "yield2":  {"label": "美债收益率(电子)", "bull": {},
                "bear": {"PCB/覆铜板": 0.5, "宽基跨境": 0.4, "房地产": 0.5}},
    # 医药医疗/基建交通：防御属性，risk-off 时相对受益
    "vix2":    {"label": "VIX恐慌(防御)", "bull": {"医药医疗": 0.4, "基建交通": 0.4},
                "bear": {}},
}


def _chg_of(quotes, sina, domestic, key):
    """取某因子的涨跌幅（优先实时外盘，其次国内期货）。返回 (chg, note)"""
    if key == "oil":
        q = sina.get("WTI")
        if q:
            return q["change_pct"], "WTI %.2f (%.2f%%)" % (q["price"], q["change_pct"])
        d = domestic.get("原油")
        if d:
            return d["change_pct"], "国内原油 %.2f%%" % d["change_pct"]
    if key == "gold":
        q = sina.get("GOLD") or sina.get("GOLD_SPOT")
        if q:
            return q["change_pct"], "COMEX金 %.2f (%.2f%%)" % (q["price"], q["change_pct"])
        d = domestic.get("沪金")
        if d:
            return d["change_pct"], "沪金 %.2f%%" % d["change_pct"]
    if key == "silver":
        q = sina.get("SILVER")
        if q:
            return q["change_pct"], "COMEX银 %.2f (%.2f%%)" % (q["price"], q["change_pct"])
        d = domestic.get("沪银")
        if d:
            return d["change_pct"], "沪银 %.2f%%" % d["change_pct"]
    if key == "copper":
        q = sina.get("COPPER")
        if q:
            return q["change_pct"], "COMEX铜 %.2f (%.2f%%)" % (q["price"], q["change_pct"])
        d = domestic.get("沪铜")
        if d:
            return d["change_pct"], "沪铜 %.2f%%" % d["change_pct"]
    if key in ("yield", "yield2"):
        # 债券 ETF 价格跌 = 收益率涨 → 取反
        q = quotes.get("TLT") or quotes.get("IEF")
        if q:
            return -q["change_pct"], "TLT %.2f (%+.2f%%) → 收益率%s" % (
                q["price"], q["change_pct"], "上行" if q["change_pct"] < 0 else "下行")
    if key == "dxy":
        q = sina.get("DXY")
        if q:
            return q["change_pct"], "美元指数 %.3f (%+.2f%%)" % (q["price"], q["change_pct"])
    if key == "nasdaq":
        q = sina.get("NQ")
        if q:
            return q["change_pct"], "纳指期指 %.0f (%+.2f%%)" % (q["price"], q["change_pct"])
        q = quotes.get("IXIC") or quotes.get("NDX")
        if q:
            return q["change_pct"], "纳指 %.0f (%+.2f%%)" % (q["price"], q["change_pct"])
    if key == "sp500":
        q = sina.get("ES")
        if q:
            return q["change_pct"], "标普期指 %.0f (%+.2f%%)" % (q["price"], q["change_pct"])
        q = quotes.get("INX")
        if q:
            return q["change_pct"], "标普 %.0f (%+.2f%%)" % (q["price"], q["change_pct"])
    if key in ("vix", "vix2"):
        q = sina.get("VIX")
        if q:
            # VIX 用绝对水平+相对变化综合：>20 算恐慌，涨幅大也算
            lvl_v = q["price"]
            return q["change_pct"], "VIX %.2f (%+.2f%%)" % (lvl_v, q["change_pct"])
    return None, "无数据"


# 面向"市场"的描述文案（区别于"对某板块利好/利空"，避免 VIX↑ 被读成利好）
def _effect_text(key, chg, lvl, sina):
    up = chg > 0
    if key == "oil":
        return "能源涨价 / 中下游成本升" if up else "能源跌价 / 中下游成本降"
    if key == "gold":
        return "黄金上涨 / 避险需求强" if up else "黄金下跌 / 避险降温"
    if key == "silver":
        return "白银上涨" if up else "白银下跌"
    if key == "copper":
        return "铜价上涨 / 工业需求旺" if up else "铜价下跌 / 工业需求弱"
    if key in ("yield", "yield2"):
        return "收益率上行 / 压制高估值" if up else "收益率下行 / 利好成长"
    if key == "dxy":
        return "美元走强 / 压制商品" if up else "美元走弱 / 利好商品"
    if key == "nasdaq":
        return "外盘科技走强" if up else "外盘科技走弱"
    if key == "sp500":
        return "外盘风险偏好升" if up else "外盘风险偏好降"
    if key in ("vix", "vix2"):
        v = (sina.get("VIX") or {}).get("price")
        base = "波动率升 / 避险升温" if up else "波动率降 / 风险偏好升"
        return base + ("（VIX %.1f 高位）" % v if v and v >= 20 else "")
    return ""


def build_bias(quotes, sina, domestic):
    """产出 factors / sector_bias / link_bias / summary。"""
    factors, bias, details, summary = {}, {}, {}, []

    for key, rule in FACTOR_RULES.items():
        chg, note = _chg_of(quotes, sina, domestic, key)
        if chg is None:
            factors[key] = {"label": rule["label"], "value": None, "chg": None,
                            "level": 0, "note": "无数据"}
            continue
        lvl = _lvl(chg)
        # VIX 特殊：绝对水平高也加惩罚
        if key in ("vix", "vix2"):
            # 统一语义下 lvl>0 = 恐慌升温（红利受益、成长受损）。
            # 绝对水平高也代表恐慌，即使当日没涨
            v = (sina.get("VIX") or {}).get("price")
            if v:
                if v >= 25:
                    lvl = max(lvl, 2)
                elif v >= 20:
                    lvl = max(lvl, 1)
        factors[key] = {"label": rule["label"], "chg": round(chg, 2), "level": lvl, "note": note,
                        "market_effect": _effect_text(key, chg, lvl, sina)}
        if lvl == 0:
            continue
        sign = "+" if lvl > 0 else "-"
        mag = abs(lvl)
        if lvl > 0:
            # 因子涨：bull 板块加分，bear 板块减分
            for sec, w in rule["bull"].items():
                bias[sec] = round(bias.get(sec, 0) + w * mag, 2)
            for sec, w in rule["bear"].items():
                bias[sec] = round(bias.get(sec, 0) - w * mag, 2)
            bull_hits, bear_hits = list(rule["bull"]), list(rule["bear"])
        else:
            # 因子跌：bull 板块减分（如金价跌→黄金股利空），bear 板块加分（如油价跌→航空利好）
            for sec, w in rule["bull"].items():
                bias[sec] = round(bias.get(sec, 0) - w * mag, 2)
            for sec, w in rule["bear"].items():
                bias[sec] = round(bias.get(sec, 0) + w * mag, 2)
            bull_hits, bear_hits = list(rule["bear"]), list(rule["bull"])
        _msg = "%s%s %s" % (rule["label"], "↑" if lvl > 0 else "↓", note)
        if bull_hits:
            _msg += " → 利好 " + "/".join(bull_hits)
        if bear_hits:
            _msg += "；利空 " + "/".join(bear_hits)
        summary.append(_msg)
        if key in ("oil", "gold", "silver", "copper"):
            details[key] = {"bull": rule["bull"] if lvl > 0 else rule["bear"],
                            "bear": rule["bear"] if lvl > 0 else rule["bull"]}

    # ---- 个股级偏好：用 metals_data 的「个股→期货 link」精确映射 ----
    # 池子里中国石化/中国石油被归为"周期资源"，板块分吃不到油价利好；
    # 这里按名称关键词二次匹配，保证油价/金价/铜价能落到具体股票
    kw_rules = [
        ("oil", r"石化|石油|海油|油气|油服|能源|煤"),          # 油价↑受益（能源类）
        ("gold", r"黄金|金矿|贵金属"),                        # 金价↑受益
        ("silver", r"白银|银泰|盛达"),                        # 银价↑受益
        ("copper", r"铜|有色|铝|稀土|矿业"),                   # 铜价↑受益
    ]
    for key, pat in kw_rules:
        lvl = (factors.get(key) or {}).get("level") or 0
        if lvl == 0:
            continue
        w = {"oil": 1.0, "gold": 1.2, "silver": 1.0, "copper": 0.8}[key]
        sc = w * lvl
        # 注意：板块分已在主循环算过，这里只生成个股名关键词分，勿重复累加
        # keyword → 供 sim_live 按股票名匹配（板块映射不到的石油/矿业股靠这个）
        if lvl > 0:
            details.setdefault("kw_bull", {})[pat] = round(sc, 2)
        else:
            details.setdefault("kw_bear", {})[pat] = round(-sc, 2)

    # 整体风险偏好（标普/纳指/VX 综合）→ 供闸门参考
    risk = 0
    for k, w in (("sp500", 1.0), ("nasdaq", 1.0), ("vix", -1.0)):
        # sp500/nasdaq 上涨=risk-on；vix 上涨=risk-off（取负）
        lv = (factors.get(k) or {}).get("level") or 0
        risk += lv * w
    return factors, bias, details, summary, risk


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只打印不写盘")
    args = ap.parse_args()

    t0 = time.time()
    quotes = fetch_tencent(list(TENCENT_SYMS.values()))
    sina = fetch_sina()
    domestic = fetch_domestic_futures()
    print("抓取: 腾讯 %d 项 / 新浪 %d 项 / 国内期货 %d 项 (%.1fs)" % (
        len(quotes), len(sina), len(domestic), time.time() - t0))
    if not quotes and not sina:
        print("外盘全部失败，放弃写盘（保留上次数据）")
        return 1

    factors, bias, details, summary, risk = build_bias(quotes, sina, domestic)

    # 外部风险偏好（-6~+6）：负=避险
    if risk <= -3:
        tone, tone_txt = "risk_off", "外部避险（外盘杀跌+波动率升）"
    elif risk >= 3:
        tone, tone_txt = "risk_on", "外部risk-on（外盘走强+波动率低）"
    else:
        tone, tone_txt = "neutral", "外部中性"

    out = {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "quotes": {"us_index": {k: v for k, v in quotes.items() if k in ("DJI", "IXIC", "INX", "NDX")},
                   "us_bond_proxy": {k: v for k, v in quotes.items() if k in ("TLT", "IEF")},
                   "global": sina},
        "domestic_futures": domestic,
        "factors": factors,
        "sector_bias": dict(sorted(bias.items(), key=lambda x: -abs(x[1]))),
        # 个股名关键词 → 期货因子分（sim_live 对 sector 无法覆盖的股票用这个）
        "keyword_bias": {**details.get("kw_bull", {}), **{k: -v for k, v in details.get("kw_bear", {}).items()}},
        "risk_pref": {"score": risk, "tone": tone, "note": tone_txt},
        "summary": summary,
        "note": "外部因子→A股板块偏好分（正=受益/负=受损）；供 sim_live 选股与买点排序使用",
    }
    if not args.dry:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)

    print("\n=== 因子 ===")
    for k, v in factors.items():
        print("  %-10s %-22s level=%+d" % (k, (v.get("note") or "")[:22], v.get("level") or 0))
    print("\n=== 板块偏好（|分|降序）===")
    for sec, sc in list(out["sector_bias"].items())[:18]:
        print("  %-14s %+5.2f  %s" % (sec, sc, "利好" if sc > 0 else "利空"))
    print("\n=== 风险偏好 === %s (score %+d) %s" % (tone, risk, tone_txt))
    print("\n=== 摘要 ===")
    for s in summary:
        print("  ·", s)
    if not args.dry:
        print("\n→ %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
