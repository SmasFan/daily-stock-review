#!/usr/bin/env python3
"""生成产业跟踪页面数据 data/industry_track.json（染料化工 / 钻石·超硬材料）。

两个板块的成分股、产业逻辑、催化跟踪点都在本文件 GROUPS 里维护（单一真源）。
行情/技术因子/估值自动拉取：腾讯行情 + 同花顺估值 + src/analyzer 技术分析。

用法:
  python3 build_industry.py            # 实时拉取
  python3 build_industry.py --offline  # 只用本地 K 线缓存（秒出）
"""
import json
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
sys.path.insert(0, BASE_DIR)

from src import analyzer as az  # noqa: E402
from src import data_provider as dp  # noqa: E402

# ---------------------------------------------------------------- 板块配置
GROUPS = [
    {
        "key": "dye",
        "name": "染料化工",
        "icon": "flask",
        "tag": "中间体供给刚性收缩 → 染料多轮提价",
        "thesis": [
            "中间体占染料成本 60%-80%，还原物 2.5万→12万元/吨（+380%），H酸 4万→15万元/吨（+275%），合规产能仅 3 家 / 有效产能不足 6 万吨",
            "染料年内逐级提价：分散黑 ECT 300% 17→33 元/kg，活性艳蓝 20万→30万元/吨；9/1 龙盛再上调 3000-4000 元/吨，8/26 活性染料单次提价最高 1 万元/吨",
            "格局：分散染料 CR3 60.34%、CR5 77.59%，活性染料 CR5 75.91%；环保/安全门槛抬高，产能 2020 年以来零新增",
            "需求：坯布库存历史低位，贸易商与印染厂原料储备不足 15 天，金九银十补库；染料占成衣成本仅 1%-3%，涨价传导顺畅",
            "业绩：闰土 H1 净利 +318.68%，龙盛染料营收 41.18 亿（+13.38%）毛利率 36.71%、中间体毛利率 39.56%",
        ],
        "catalysts": [
            {"item": "还原物", "value": "12 万元/吨", "note": "2025 年末 2.5 万，涨幅约 380%，仅 3 家可稳定生产"},
            {"item": "H酸", "value": "15 万元/吨", "note": "年初 4 万，涨幅 275%，有效供应缺口 10%+"},
            {"item": "分散黑 ECT 300%", "value": "3.3 万元/吨", "note": "9/1 龙盛上调 3000 元/吨，深蓝系列最高 +4000"},
            {"item": "活性艳蓝", "value": "30 万元/吨", "note": "7 月约 20 万，9/10 新报价体系执行"},
            {"item": "分散蓝 359 滤饼", "value": "约 30 万元/吨", "note": "高端蒽醌染料年内翻倍，亚邦 8/31 提价"},
        ],
        "watch": [
            "还原物/H酸 报价是否滞涨（涨价逻辑的唯一硬锚，一旦停涨行情即尾声）",
            "印染开工率与坯布库存：补库结束 = 需求端证伪",
            "龙盛/闰土能否放量站回 MA20（技术面确认第二波）",
            "中小产能复产（合规新增供给会直接打掉定价权）",
        ],
        "stocks": [
            {"code": "600352", "name": "浙江龙盛", "role": "全球染料第一，还原物 2 万吨自给，中间体+染料+助剂一体化"},
            {"code": "002440", "name": "闰土股份", "role": "染料产能 23.8 万吨居前二，H1 净利 +318.7%，估值最低"},
            {"code": "603188", "name": "亚邦股份", "role": "蒽醌类分散/还原染料，8/31 提价（分散蓝60滤饼 +6万/吨）"},
            {"code": "603980", "name": "吉华集团", "role": "分散染料产能第三（CR3 成员）"},
            {"code": "300798", "name": "锦鸡股份", "role": "活性染料，年内相对强势"},
            {"code": "300067", "name": "安诺其", "role": "分散染料 + 数码印花"},
            {"code": "300758", "name": "七彩化学", "role": "有机颜料/中间体"},
            {"code": "300107", "name": "建新股份", "role": "染料中间体"},
            {"code": "002054", "name": "德美化工", "role": "纺织助剂"},
            {"code": "002010", "name": "传化智联", "role": "印染助剂 + 物流"},
        ],
    },
    {
        "key": "diamond",
        "name": "钻石 · 超硬材料",
        "icon": "gem",
        "tag": "工业金刚石涨价 + AI 芯片金刚石散热 0→1",
        "thesis": [
            "散热叙事：单晶金刚石热导率 2000-2500 W/(m·K)，约为铜的 5 倍、硅的 15 倍；英伟达下一代架构采用金刚石铜复合散热，机构中性测算 2026 全球 AI 芯片金刚石散热 87 亿元 → 2030 年 592 亿元",
            "现实约束：散热产品仅小批量送样/试产，规模化盈利需 2-3 年；东吴证券明确『业绩修复由工业金刚石涨价驱动，而非散热放量』，中兵红箭亦称『和散热概念关联并不大』",
            "基本盘回暖：2026 行业上调价格（惠丰 5/1 起工业金刚石提价 8%-12%），培育钻石毛坯价格止跌企稳；力量钻石 H1 净利 +247.61%（大颗粒金刚石收入 +141.93%）、四方达 +46.90%、中兵红箭扭亏",
            "新赛道卡位：四方达 CVD 散热片小批量供货 + 20 亿定增投金刚石钻针；力量钻石 10.28 亿募投转金刚石功能材料；沃尔德声学振膜已量产装车；惠丰钻石包头 CVD 项目一期规划 500 台 MPCVD",
            "风险：培育钻石消费价仍在低位（1 克拉零售约 3500 元），中兵红箭 9/3 明确『价格较 5 月没有上涨』；黄河旋风仍有历史包袱亏损",
        ],
        "catalysts": [
            {"item": "AI 芯片散热市场", "value": "87 亿→592 亿元", "note": "2026→2030 中性测算，CAGR 50%+"},
            {"item": "金刚石热导率", "value": "2000-2500 W/(m·K)", "note": "铜的 5 倍、硅的 15 倍，已逼近铜基散热物理极限"},
            {"item": "工业金刚石价格", "value": "上调 8%-12%", "note": "惠丰 5/1 起结构性提价，同业跟进"},
            {"item": "培育钻石毛坯", "value": "企稳回升", "note": "2025Q4 起止跌；但中兵 9/3 称『较 5 月没有上涨』"},
            {"item": "金刚石钻针", "value": "渗透率低", "note": "PCB 高阶化驱动，四方达拟定增 17.5 亿投产业化"},
        ],
        "watch": [
            "散热订单是否从送样转向批量采购价（题材转业绩的唯一验证点）",
            "培育钻石/工业金刚石报价月度变化（基本盘）",
            "板块估值：四方达 PE 178、国机精工 PE 243，题材证伪则杀估值",
            "RSI 高位 + 距 60 日高点大幅回撤的『第二波』结构，追高易套",
        ],
        "stocks": [
            {"code": "000519", "name": "中兵红箭", "role": "中南钻石，工业金刚石全球市占率第一，HPHT+CVD 双线"},
            {"code": "301071", "name": "力量钻石", "role": "培育钻石龙头，H1 净利 +247.6%，10.28 亿投金刚石功能材料"},
            {"code": "300179", "name": "四方达", "role": "CVD 散热片小批量供货 + 金刚石钻针 20 亿定增"},
            {"code": "600172", "name": "黄河旋风", "role": "金刚石热沉片/单晶 CVD 研发，历史包袱仍亏损"},
            {"code": "002046", "name": "国机精工", "role": "8 英寸多晶金刚石晶圆/热沉量产线，H1 净利 -71.9%"},
            {"code": "688028", "name": "沃尔德", "role": "金刚石声学振膜已量产装车 + 超硬刀具"},
            {"code": "920725", "name": "惠丰钻石", "role": "金刚石微粉 + 包头 CVD 项目（一期 500 台 MPCVD）"},
            {"code": "300861", "name": "美畅股份", "role": "金刚线龙头（光伏链条，与本轮散热主题分化）"},
            {"code": "002943", "name": "宇晶股份", "role": "金刚线切片设备（光伏链条）"},
            {"code": "605580", "name": "恒盛能源", "role": "热电 + 培育钻石业务（跨界标的）"},
        ],
    },
]


def fetch_group(group, klines, quotes, vals):
    """单板块：拼装成分股行情 + 技术因子 + 估值。"""
    rows = []
    for st in group["stocks"]:
        code = st["code"]
        k = klines.get(code)
        q = quotes.get(code) or {}
        v = vals.get(code) or {}
        row = {
            "code": code,
            "name": st["name"],
            "role": st["role"],
            "price": q.get("price"),
            "change_pct": q.get("change"),
            "amount": q.get("amount"),
            "pe_ttm": v.get("pe_ttm"),
            "pb_mrq": v.get("pb_mrq"),
        }
        if k and len(k["closes"]) >= 30:
            closes = k["closes"]
            a = az.analyze_stock(st["name"], k["dates"], k["opens"], closes,
                                 k["highs"], k["lows"], k["volumes"], code=code)
            last = closes[-1]
            row["price"] = last if not row["price"] else row["price"]
            if row["change_pct"] is None and len(closes) >= 2:
                row["change_pct"] = round((last / closes[-2] - 1) * 100, 2)
            for n in (5, 20, 60):
                row[f"change_{n}d"] = round((last / closes[-n - 1] - 1) * 100, 2) if len(closes) > n else None
            hi60 = max(k["highs"][-60:]) if len(k["highs"]) >= 60 else max(k["highs"])
            row["from_60d_high"] = round((last / hi60 - 1) * 100, 2)
            if a is not None:
                row.update({
                    "ma5": a.ma5, "ma20": a.ma20, "ma60": a.ma60,
                    "trend_status": a.trend_status, "trend_strength": a.trend_strength,
                    "rsi6": a.rsi6, "rsi12": a.rsi12, "rsi_status": a.rsi_status,
                    "volume_ratio": a.volume_ratio, "volume_status": a.volume_status,
                    "macd_status": a.macd_status, "boll_pos": a.boll_pos,
                    "score": a.score, "signal_key": a.signal_key, "signal": a.signal,
                    "ideal_buy": a.ideal_buy, "secondary_buy": a.secondary_buy,
                    "stop_loss": a.stop_loss, "take_profit": a.take_profit,
                    "ma_state": _ma_state(last, a.ma5, a.ma20, a.ma60),
                })
        rows.append(row)
    return rows


def _ma_state(p, m5, m20, m60):
    if None in (p, m5, m20, m60):
        return None
    if p > m5 > m20 > m60:
        return "多头"
    if p < m5 < m20 < m60:
        return "空头"
    return "纠缠"


def summarize(rows):
    """板块 KPI：今日均涨、上涨家数、各周期均涨、中位 PE。"""
    def avg(key):
        vs = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(vs) / len(vs), 2) if vs else None
    pes = sorted(r["pe_ttm"] for r in rows if r.get("pe_ttm") and r["pe_ttm"] > 0)
    return {
        "count": len(rows),
        "up_count": sum(1 for r in rows if (r.get("change_pct") or 0) > 0),
        "down_count": sum(1 for r in rows if (r.get("change_pct") or 0) < 0),
        "avg_change": avg("change_pct"),
        "avg_5d": avg("change_5d"),
        "avg_20d": avg("change_20d"),
        "avg_60d": avg("change_60d"),
        "median_pe": round(pes[len(pes) // 2], 1) if pes else None,
        "bull_count": sum(1 for r in rows if r.get("ma_state") == "多头"),
    }


def main():
    offline = "--offline" in sys.argv
    codes = [s["code"] for g in GROUPS for s in g["stocks"]]
    quotes = {} if offline else dp.fetch_quotes(codes)
    klines = dp.fetch_daily_kline_batch(codes, count=320)
    try:
        from src import ths_api
        vals = ths_api.fetch_valuations(codes) if ths_api.available() else {}
    except Exception as e:
        print(f"   [warn] 估值获取失败: {e}")
        vals = {}

    out_groups = []
    for g in GROUPS:
        rows = fetch_group(g, klines, quotes, vals)
        out_groups.append({
            "key": g["key"], "name": g["name"], "icon": g["icon"], "tag": g["tag"],
            "thesis": g["thesis"], "catalysts": g["catalysts"], "watch": g["watch"],
            "stats": summarize(rows),
            "stocks": rows,
        })

    out = {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "groups": out_groups,
    }
    path = os.path.join(DATA_DIR, "industry_track.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, path)
    print(f"写入 {path}")
    for g in out_groups:
        s = g["stats"]
        print(f"  {g['name']}: {s['count']} 只 · 今日均涨 {s['avg_change']}% · 5日 {s['avg_5d']}% · 20日 {s['avg_20d']}% · 多头 {s['bull_count']}")


if __name__ == "__main__":
    main()
