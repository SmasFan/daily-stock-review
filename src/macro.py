# -*- coding: utf-8 -*-
"""
宏观政策与新闻情绪模块（2026-08 新增）。

多数机构做法（词典法情感 + 政策主题/行业映射 + 注意力热度）：
1. 抓取东方财富财经新闻流（column=346 财经频道，含标题+摘要+发布时间）
2. 金融词典法打情感分；政策发布主体（央行/证监会/国务院等）加权
3. 主题关键词 → 自选板块 → 自选个股 三级映射
4. 聚合为利好/风险提醒 + 板块/个股影响分，输出 macro_data.json

数据下游：
- 宏观页 macro.html 直接展示
- 复盘页 review.html / 推荐页 recommend.html 读取后给个股/板块打标签（反馈）
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

NEWS_URL = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
NEWS_PAGES = 2        # 每页 50 条，共拉 ~100 条财经新闻
NEWS_CACHE_MAX_HOURS = 2.0

# ---------------- 政策发布主体（来源权重，最高优先） ----------------

POLICY_SOURCES = [
    ("国务院", 1.6), ("证监会", 1.6), ("央行", 1.5), ("人民银行", 1.5),
    ("发改委", 1.4), ("财政部", 1.4), ("金融监管总局", 1.4), ("国家金融监督管理总局", 1.4),
    ("国家统计局", 1.2), ("工信部", 1.2), ("商务部", 1.2), ("住建部", 1.2),
    ("国资委", 1.2), ("税务总局", 1.2), ("能源局", 1.2), ("网信办", 1.2),
    ("发改委", 1.4), ("中央经济工作会议", 1.6), ("政治局", 1.6),
    ("国常会", 1.6), ("国务院常务会议", 1.6), ("二十届三中全会", 1.6),
    ("中央金融工作会议", 1.6),
]

# ---------------- 金融词典法：利好 / 利空词（财经语境） ----------------

POSITIVE_WORDS = [
    "上涨", "增长", "创新高", "新高", "利好", "支持", "提振", "扩大", "回升", "回暖",
    "向好", "稳健", "宽松", "降准", "降息", "增持", "回购", "改善", "景气", "超预期",
    "上调", "减税", "补贴", "获批", "批复", "放开", "加大", "加快", "推动", "受益",
    "获准", "落地", "突破", "放量", "流入", "净流入", "大涨", "涨停", "领涨", "攀升",
    "提速", "加码", "积极", "鼓励", "加大力度", "中长期资金", "增量资金", "外资",
    "回暖", "企稳", "稳中有进", "平稳", "录得增长", "大幅增长", "好于预期", "首破",
    "中标", "签约", "订单", "投产", "量产", "首发", "问世", "获批上市", "免税", "让利",
]

NEGATIVE_WORDS = [
    "下跌", "下滑", "回落", "下降", "利空", "风险", "压力", "亏损", "减持", "流出",
    "净流出", "下调", "收紧", "加息", "制裁", "关税", "处罚", "调查", "退市", "暂停",
    "停产", "违约", "债务", "崩盘", "危机", "疲软", "低迷", "收缩", "低于预期", "恶化",
    "强制", "限制", "大跌", "跌停", "领跌", "跳水", "重挫", "承压", "放缓", "走弱",
    "走低", "遇冷", "寒冬", "裁撤", "下调评级", "预警", "警示", "约谈", "立案",
    "违约风险", "资金链", "爆雷", "大跌眼镜", "不及预期", "冲高回落", "出货",
]

# 风险事件关键词：命中即强风险（即使出现利好词也以风险优先）
RISK_WORDS = [
    "处罚", "罚款", "立案", "调查", "退市", "风险警示", "ST", "强制退市", "暂停上市",
    "终止上市", "违约", "逾期", "爆雷", "暴雷", "跑路", "失联", "被查", "被罚",
    "减持", "清仓", "质押", "冻结", "限售解禁", "解禁", "巨额亏损", "亏损扩大",
    "停产", "停工", "停产整顿", "裁员", "降薪", "下调", "下调评级", "评级下调",
    "制裁", "加征关税", "关税", "出口管制", "反倾销", "监管函", "警示函", "通报批评",
    "立案调查", "涉嫌", "违法违规", "虚增", "造假", "欺诈", "商誉减值", "计提减值",
    "诉讼", "仲裁", "债务危机", "到期兑付", "信用风险", "流动性风险",
]

# ---------------- 主题映射：主题 → 关键词 → 自选板块 ----------------

THEMES: Dict[str, Dict] = {
    "人工智能/算力": {
        "kws": ["人工智能", "大模型", "算力", "英伟达", "GPU", "AI应用", "智能体",
                "AI芯片", "推理", "训练", "数据中心", "智算"],
        "sectors": ["AI算力", "半导体", "科技-通信电子", "CPO/光模块", "科技-互联网传媒"],
    },
    "半导体/芯片": {
        "kws": ["半导体", "芯片", "晶圆", "集成电路", "光刻", "存储芯片", "国产替代",
                "先进制程", "封测", "功率半导体", "第三代半导体"],
        "sectors": ["半导体"],
    },
    "机器人/具身智能": {
        "kws": ["机器人", "具身智能", "人形机器人", "工业母机", "减速器", "伺服电机",
                "灵巧手", "机器狗"],
        "sectors": ["机器人", "AI算力"],
    },
    "新能源": {
        "kws": ["新能源", "光伏", "风电", "储能", "锂电池", "锂电", "充电桩", "绿电",
                "硅料", "硅片", "组件", "氢能", "固态电池", "碳酸锂", "电池"],
        "sectors": ["新能源电力", "汽车零部件"],
    },
    "电力/电网": {
        "kws": ["电力", "电网", "特高压", "虚拟电厂", "电价", "火电", "水电", "核电",
                "用电量", "绿证"],
        "sectors": ["新能源电力", "基建交通"],
    },
    "汽车": {
        "kws": ["汽车", "新能源汽车", "智能驾驶", "自动驾驶", "车企", "车市", "以旧换新",
                "整车", "零部件", "销量", "出口"],
        "sectors": ["汽车零部件", "大消费"],
    },
    "军工/国防": {
        "kws": ["军工", "国防", "军贸", "导弹", "战斗机", "航母", "无人机", "军费",
                "装备", "航天", "卫星"],
        "sectors": ["军工", "科技-通信电子"],
    },
    "医药/医疗": {
        "kws": ["医药", "医疗", "创新药", "集采", "医保", "疫苗", "CXO", "医疗器械",
                "中药", "生物制品", "医疗设备", "带量采购"],
        "sectors": ["医药医疗"],
    },
    "消费": {
        "kws": ["消费", "白酒", "零售", "家电", "旅游", "免税", "食品饮料", "餐饮",
                "首发经济", "消费券", "社零", "社会消费品"],
        "sectors": ["大消费"],
    },
    "房地产/基建": {
        "kws": ["房地产", "楼市", "房贷", "购房", "房价", "地产", "保交楼", "基建",
                "专项债", "基础设施", "重大项目", "水利", "城中村", "城市更新",
                "土拍", "商品房"],
        "sectors": ["房地产", "基建交通", "红利银行"],
    },
    "金融/货币政策": {
        "kws": ["降准", "降息", "LPR", "MLF", "逆回购", "货币政策", "流动性", "银行",
                "券商", "保险", "资本市场", "中长期资金", "险资", "存款利率", "利率",
                "汇金", "平准基金", "互换便利"],
        "sectors": ["红利银行", "红利非银", "红利金融"],
    },
    "周期资源": {
        "kws": ["煤炭", "有色", "黄金", "铜", "铝", "石油", "原油", "稀土", "钢铁",
                "大宗商品", "化工", "磷", "锂", "镍", "锡", "关键矿产"],
        "sectors": ["周期资源", "红利金融"],    },
    "通信/光模块": {
        "kws": ["5G", "6G", "通信", "光模块", "CPO", "卫星互联网", "北斗", "数据中心互联"],
        "sectors": ["科技-通信电子", "CPO/光模块"],
    },
    "互联网/传媒": {
        "kws": ["互联网", "传媒", "游戏", "短视频", "电商", "平台", "AIGC", "数字人",
                "内容", "IP", "出海"],
        "sectors": ["科技-互联网传媒", "AI算力"],
    },
    "PCB/覆铜板": {
        "kws": ["PCB", "覆铜板", "印制电路板", "HDI"],
        "sectors": ["PCB/覆铜板"],
    },
}

SENTI_SIGMA = 1.0  # 词典命中数 → 分数的灵敏度


def _get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


# ---------------- 新闻抓取 ----------------

def _news_cache_path() -> str:
    return os.path.join(CACHE_DIR, "macro_news.json")


def fetch_news(page_size: int = 50, use_cache: bool = True,
               cache_max_age_hours: float = NEWS_CACHE_MAX_HOURS) -> List[Dict]:
    """拉取东财财经新闻流（column=346），返回 [{code, title, summary, show_time, url}]。

    带本地缓存（2 小时），失败时降级用缓存，避免频繁请求被封。
    """
    cp = _news_cache_path()
    if use_cache and os.path.exists(cp):
        age = time.time() - os.path.getmtime(cp)
        if age < cache_max_age_hours * 3600:
            try:
                with open(cp, "r", encoding="utf-8") as fp:
                    return json.load(fp)
            except Exception:
                pass

    items: List[Dict] = []
    for page in range(1, NEWS_PAGES + 1):
        params = {
            "client": "web", "biz": "web_news_col", "column": "346", "order": "1",
            "needInteractData": "0", "page_index": str(page), "page_size": str(page_size),
            "req_trace": "1",
        }
        url = NEWS_URL + "?" + urllib.parse.urlencode(params)
        try:
            raw = _get(url)
            d = json.loads(raw)
            rows = ((d.get("data") or {}).get("list") or [])
        except Exception as e:
            print(f"   [warn] 新闻拉取失败(page {page}): {e}")
            break
        for r in rows:
            title = (r.get("title") or "").strip()
            if not title:
                continue
            items.append({
                "code": r.get("code", ""),
                "title": title,
                "summary": (r.get("summary") or "").strip(),
                "show_time": (r.get("showTime") or "").strip(),
                "url": r.get("url") or "",
            })
        if len(rows) < page_size:
            break
        time.sleep(0.4)

    # 去重（同标题只留最早一条）
    seen, out = set(), []
    for it in sorted(items, key=lambda x: x["show_time"], reverse=True):
        if it["title"] in seen:
            continue
        seen.add(it["title"])
        out.append(it)
    if not out and os.path.exists(cp):
        # 网络失败时降级用旧缓存
        try:
            with open(cp, "r", encoding="utf-8") as fp:
                out = json.load(fp)
        except Exception:
            pass
    if out:
        try:
            with open(cp, "w", encoding="utf-8") as fp:
                json.dump(out, fp, ensure_ascii=False)
        except Exception:
            pass
    return out


# ---------------- 词典法情感 + 政策加权 ----------------

def _hits(text: str, words: List[str]) -> List[str]:
    return [w for w in words if w in text]


def policy_source_of(text: str) -> Optional[float]:
    """命中政策发布主体 → 返回加权倍数（去重后取最高）。"""
    hits = [(name, w) for name, w in POLICY_SOURCES if name in text]
    if not hits:
        return None
    return max(w for _, w in hits)


def sentiment_of(text: str) -> Dict:
    """金融词典法情感打分：pos/neg 命中数 → 分数（约 [-10, 10]）。"""
    pos = len(_hits(text, POSITIVE_WORDS))
    neg = len(_hits(text, NEGATIVE_WORDS))
    raw = (pos - neg) / max(1, SENTI_SIGMA * (pos + neg)) * 10
    raw = max(-10.0, min(10.0, raw * 3))
    return {"pos": pos, "neg": neg, "score": round(raw, 1)}


def risk_words_hit(text: str) -> List[str]:
    return _hits(text, RISK_WORDS)


def theme_of(text: str) -> List[str]:
    """主题关键词命中（按 THEMES 顺序）。"""
    return [name for name, cfg in THEMES.items() if any(kw in text for kw in cfg["kws"])]


def _sectors_of(text: str) -> List[str]:
    out = []
    for name, cfg in THEMES.items():
        if any(kw in text for kw in cfg["kws"]):
            for s in cfg["sectors"]:
                if s not in out:
                    out.append(s)
    return out


# ---------------- 新闻分析（纯函数，便于离线测试） ----------------

def analyze_news(item: Dict) -> Dict:
    """单条新闻 → 结构化分析：情感 + 政策权重 + 主题 + 板块 + 提醒判定。"""
    text = item["title"] + " " + (item.get("summary") or "")
    senti = sentiment_of(text)
    src_w = policy_source_of(text) or 1.0
    risks = risk_words_hit(text)
    # 误报防护：统计口径的"调查"（如"调查显示"）不算风险事件
    if "调查" in risks and "调查显示" in text:
        risks = [w for w in risks if w != "调查"]
    # "关税退税"是利好，不判风险
    if "关税" in risks and "退税" in text:
        risks = [w for w in risks if w != "关税"]
    themes = theme_of(text)
    sectors = _sectors_of(text)

    # 风险事件优先：命中风险词直接判风险（分数取负）
    if risks:
        kind = "risk"
        score = -max(abs(senti["score"]), 5.0) * min(src_w, 1.6)
    else:
        score = senti["score"] * src_w
        kind = "bull" if score >= 2.0 else ("risk" if score <= -2.0 else "info")
    score = round(max(-10.0, min(10.0, score)), 1)

    return {
        "title": item["title"],
        "summary": item.get("summary", ""),
        "show_time": item.get("show_time", ""),
        "url": item.get("url", ""),
        "source_w": src_w,
        "pos": senti["pos"], "neg": senti["neg"],
        "score": score, "kind": kind,
        "risks": risks[:5],
        "themes": themes, "sectors": sectors,
    }


def build_alerts(analyzed: List[Dict], min_score: float = 2.0) -> Dict:
    """利好/风险提醒：|score| ≥ min_score 或命中风险词的新闻。"""
    bulls, risks = [], []
    for a in analyzed:
        if a["kind"] == "bull" and a["score"] >= min_score:
            bulls.append(a)
        elif a["kind"] == "risk":
            risks.append(a)
    bulls.sort(key=lambda x: (-x["score"], x["show_time"]))
    risks.sort(key=lambda x: (x["score"], x["show_time"]))
    return {"bulls": bulls, "risks": risks}


# ---------------- 板块 / 个股影响聚合 ----------------

def build_theme_scores(analyzed: List[Dict]) -> List[Dict]:
    """主题情绪榜：注意力热度（新闻条数）+ 净情绪分。"""
    agg = {}
    for a in analyzed:
        if a["kind"] == "info":
            continue
        for t in a["themes"]:
            d = agg.setdefault(t, {"name": t, "news": 0, "net": 0.0,
                                   "sectors": THEMES[t]["sectors"], "bulls": 0, "risks": 0})
            d["news"] += 1
            d["net"] += a["score"]
            if a["kind"] == "bull":
                d["bulls"] += 1
            else:
                d["risks"] += 1
    for d in agg.values():
        d["net"] = round(d["net"], 1)
    out = sorted(agg.values(), key=lambda x: (abs(x["net"]), x["news"]), reverse=True)
    return out


def build_sector_impact(alerts: Dict, analyzed: List[Dict]) -> List[Dict]:
    """自选板块影响：净情绪分 + 利好/风险条数 + 关键提醒。"""
    agg = {}
    for a in analyzed:
        if a["kind"] == "info":
            continue
        for s in a["sectors"]:
            d = agg.setdefault(s, {"sector": s, "net": 0.0, "bulls": 0, "risks": 0,
                                   "key": []})
            d["net"] += a["score"]
            if a["kind"] == "bull":
                d["bulls"] += 1
            else:
                d["risks"] += 1
            if len(d["key"]) < 3:
                d["key"].append({"title": a["title"], "kind": a["kind"], "score": a["score"]})
    for d in agg.values():
        d["net"] = round(d["net"], 1)
    return sorted(agg.values(), key=lambda x: (x["bulls"] + x["risks"], abs(x["net"])),
                  reverse=True)


def build_stock_impact(sector_impact: List[Dict]) -> List[Dict]:
    """个股影响：板块分 → 自选股（同一板块内股票共享板块宏观分）。"""
    from . import stock_pool as sp
    code_sector = sp.get_code_sector()
    code_name = sp.get_code_name()
    sector_map = {d["sector"]: d for d in sector_impact}
    by_code = {}
    for code, sec in code_sector.items():
        hit = sector_map.get(sec)
        if not hit or hit["net"] == 0:
            continue
        by_code[code] = {
            "code": code, "name": code_name.get(code, code), "sector": sec,
            "score": hit["net"], "bulls": hit["bulls"], "risks": hit["risks"],
            "key": hit["key"],
        }
    out = sorted(by_code.values(), key=lambda x: (-abs(x["score"]), x["code"]))
    return out


# ---------------- 组装 / 保存 ----------------

def build_macro_data(offline: bool = False) -> Dict:
    """组装宏观页数据（含给其他页面的反馈索引 stocks/sectors）。"""
    print("== 宏观政策与新闻情绪 ==")
    news = fetch_news(use_cache=not offline)
    print(f"   新闻 {len(news)} 条（东财财经频道）")
    analyzed = [analyze_news(n) for n in news]
    alerts = build_alerts(analyzed)
    themes = build_theme_scores(analyzed)
    sector_impact = build_sector_impact(alerts, analyzed)
    stock_impact = build_stock_impact(sector_impact)

    bulls, risks = alerts["bulls"], alerts["risks"]
    policy_count = sum(1 for a in analyzed if a["source_w"] > 1.0)
    net = round(sum(a["score"] for a in analyzed if a["kind"] != "info"), 1)
    print(f"   利好 {len(bulls)} 条 / 风险 {len(risks)} 条 · 主题 {len(themes)} 个 · "
          f"板块 {len(sector_impact)} 个 · 个股 {len(stock_impact)} 只")

    # 情绪温度：50 为中性，净情绪 ±45 封顶 → 温度 5~95，避免打满极端值
    temp_val = int(round(50 + max(-45.0, min(45.0, net)))) if news else None
    temp_label = ("偏多" if temp_val >= 65 else "偏空" if temp_val <= 35 else "中性") if temp_val is not None else "--"

    # 反馈索引：给其他页面按 code / 板块名 O(1) 查询
    stocks_idx = {s["code"]: s for s in stock_impact}
    sectors_idx = {s["sector"]: s for s in sector_impact}

    data = {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overview": {
            "news_total": len(news),
            "policy_news": policy_count,
            "bull_count": len(bulls),
            "risk_count": len(risks),
            "net_score": net,
            "temperature": temp_val,
            "temperature_label": temp_label,
            "theme_count": len(themes),
            "sector_count": len(sector_impact),
            "stock_count": len(stock_impact),
        },
        "alerts": {"bulls": bulls, "risks": risks},
        "themes": themes,
        "sectors": sector_impact,
        "stocks": stock_impact,
        "news": analyzed,
        "_index": {"stocks": stocks_idx, "sectors": sectors_idx},
    }
    return data


def save_macro_data(data: Dict) -> str:
    path = os.path.join(BASE_DIR, "data", "macro_data.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return path


if __name__ == "__main__":
    import sys
    sys.path.insert(0, BASE_DIR)
    d = build_macro_data()
    print(save_macro_data(d))
