"""宽基指数模块：沪深300/中证500/中证A500/科创50/恒生科技/恒生创新药/中概互联/纳指/标普500/医药。

- 行情/日K：腾讯行情（qt.gtimg.cn），与 data_provider 复用
- 估值：蛋卷指数估值接口（danjuanfunds.com/djapi/index_eva/dj），按代码匹配 PE/PB/PE分位
- 技术因子：复用 analyzer.analyze_stock（趋势/MA/MACD/RSI/买卖点）
输出: data/wide_index_data.json
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from . import data_provider as dp
from . import analyzer as az

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_PATH = os.path.join(DATA_DIR, "wide_index_pe_cache.json")

# 宽基清单：code = 腾讯行情代码（含前缀），name 展示名，kind 分组，pe_keys 蛋卷匹配关键词
WIDE_INDEXES = [
    {"code": "sh000300", "name": "沪深300",    "kind": "A股宽基", "pe_keys": ["SH000300"]},
    {"code": "sh000905", "name": "中证500",    "kind": "A股宽基", "pe_keys": ["SH000905"]},
    {"code": "sh000510", "name": "中证A500",   "kind": "A股宽基", "pe_keys": []},
    {"code": "sh000688", "name": "科创50",     "kind": "A股宽基", "pe_keys": ["SH000688"]},
    {"code": "hkHSTECH", "name": "恒生科技",   "kind": "港股",   "pe_keys": ["HKHSTECH"]},
    {"code": "sh513120", "name": "港股创新药",  "kind": "港股",   "pe_keys": []},
    {"code": "sh513050", "name": "中概互联",    "kind": "海外中概", "pe_keys": ["CSIH30533"]},
    {"code": "usIXIC",   "name": "纳斯达克",    "kind": "美股",   "pe_keys": ["NDX"]},
    {"code": "usINX",    "name": "标普500",    "kind": "美股",   "pe_keys": ["SP500"]},
    {"code": "sh512010", "name": "医药",       "kind": "行业",   "pe_keys": ["SH000991", "SH000978", "SZ399989"]},
]


def fetch_pe_map(use_cache=True):
    """蛋卷指数估值 -> {code: {pe, pb, pe_percentile}}。

    返回的 key 为蛋卷 index_code 大写（如 SH000300 / HKHSTECH / NDX / SP500 / CSIH30533）。
    """
    import urllib.request
    url = "https://danjuanfunds.com/djapi/index_eva/dj"
    if use_cache and os.path.exists(CACHE_PATH):
        age = os.path.getmtime(CACHE_PATH)
        if (datetime.now().timestamp() - age) < 6 * 3600:
            try:
                with open(CACHE_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                  "Referer": "https://danjuanfunds.com/"})
        raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
        d = json.loads(raw)
        items = (d.get("data") or {}).get("items") or []
    except Exception:
        items = []
    out = {}
    for it in items:
        code = str(it.get("index_code") or "").upper()
        pe = it.get("pe")
        pb = it.get("pb")
        pct = it.get("pe_percentile")
        if not code:
            continue
        try:
            pe = float(pe) if pe else None
        except (TypeError, ValueError):
            pe = None
        try:
            pb = float(pb) if pb else None
        except (TypeError, ValueError):
            pb = None
        try:
            pct = float(pct) if pct else None
        except (TypeError, ValueError):
            pct = None
        out[code] = {"pe": pe, "pb": pb, "pe_percentile": pct,
                     "pe_date": it.get("date") or it.get("pe_date") or ""}
    if out and use_cache:
        try:
            with open(CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
        except Exception:
            pass
    return out


def _match_pe(pe_map, keys):
    """按 pe_keys 列表逐个在蛋卷 map 中找，返回第一个命中估值 dict 或 None。"""
    for k in keys:
        v = pe_map.get(k.upper())
        if v and v.get("pe"):
            return v
    return None


def build_wide_index_data() -> dict:
    """生成宽基指数数据。行情失败单条跳过，不阻断整体。"""
    pe_map = fetch_pe_map()
    symbols = [x["code"] for x in WIDE_INDEXES]
    quotes = dp.fetch_index_quotes(symbols)

    items = []
    for w in WIDE_INDEXES:
        sym = w["code"]
        q = quotes.get(sym)
        if not q:
            continue
        row = {
            "code": sym, "name": w["name"], "kind": w["kind"],
            "close": q.get("price"), "change_pct": q.get("change"),
            "prev_close": q.get("prevClose"),
        }
        # 技术因子
        try:
            k = dp.fetch_index_kline(sym, count=320, use_cache=True)
            if k and len(k["closes"]) >= 30:
                a = az.analyze_stock(w["name"], k["dates"], k["opens"], k["closes"],
                                     k["highs"], k["lows"], k["volumes"], code=sym)
                if a is not None:
                    row["factors"] = {
                        "trend_status": a.trend_status, "trend_strength": a.trend_strength,
                        "ma5": a.ma5, "ma20": a.ma20, "ma60": a.ma60,
                        "bias_ma5": a.bias_ma5, "bias_ma20": a.bias_ma20,
                        "volume_ratio": a.volume_ratio, "volume_status": a.volume_status,
                        "macd_status": a.macd_status,
                        "macd_dif": a.macd_dif, "macd_dea": a.macd_dea, "macd_bar": a.macd_bar,
                        "rsi6": a.rsi6, "rsi12": a.rsi12, "rsi24": a.rsi24, "rsi_status": a.rsi_status,
                        "score": a.score, "signal_key": a.signal_key, "signal": a.signal,
                        "ideal_buy": a.ideal_buy, "secondary_buy": a.secondary_buy,
                        "stop_loss": a.stop_loss, "take_profit": a.take_profit,
                        "support": a.support, "resistance": a.resistance,
                        "high20": a.high20, "low20": a.low20, "change_60d": a.change_60d,
                        "boll_pos": a.boll_pos,
                    }
                    row["price_ma20"] = a.ma20
        except Exception:
            pass
        # 估值：蛋卷优先，腾讯快照 PE 兜底（部分新指数如中证A500 蛋卷暂无）
        pev = _match_pe(pe_map, w["pe_keys"])
        if pev:
            row["pe"] = pev.get("pe")
            row["pb"] = pev.get("pb")
            row["pe_percentile"] = pev.get("pe_percentile")
            row["pe_date"] = pev.get("pe_date") or ""
            row["pe_source"] = "蛋卷"
        else:
            qpe = q.get("pe")
            if qpe:
                try:
                    row["pe"] = float(qpe)
                    row["pe_source"] = "腾讯"
                except (TypeError, ValueError):
                    pass
        items.append(row)

    return {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": items,
        "source_note": "行情: 腾讯 · 估值PE/PB分位: 蛋卷基金指数估值（缺省用腾讯PE） · 技术因子: 同源分析器",
    }


def save_wide_index_data(data: dict) -> str:
    path = os.path.join(DATA_DIR, "wide_index_data.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


if __name__ == "__main__":
    d = build_wide_index_data()
    p = save_wide_index_data(d)
    print(f"保存: {p}")
    for it in d["items"]:
        print(f"  {it['name']:8s} {it['close']:>10.2f} {it.get('change_pct', 0):+6.2f}%  "
              f"PE {it.get('pe') or '--':>8} 分位 {it.get('pe_percentile')}  {it.get('trend_status', '') if 'trend_status' in it else ''}"
              .replace("None", "--"))
