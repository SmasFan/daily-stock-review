#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 宏观消息面观察（deepseek-v4-flash = pi 当前同款模型）。

用法:
  python3 macro_llm.py            # 拉当日新闻 → LLM 判断 → 写 data/macro_llm_data.json
  python3 macro_llm.py --date 2026-09-04   # 指定日期（用缓存新闻）

输出结构:
  {generatedAt, date, llm: {model, sentiment: 多头/中性/空头/防御, score:0-100,
    title, summary, drivers:[], risks:[], sectors:[], stance: 进攻/均衡/防守},
   news_count, source: 'deepseek'}
下游:
  sim_live.py --plan 读取: sentiment∈{空头,防御} → 计划闸门 block（不建新仓）
  复盘/推荐页 可展示 "LLM 宏观观察" 卡
"""
import argparse
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

from src import macro as mc

OUT = os.path.join(BASE_DIR, "data", "macro_llm_data.json")
# LLM 后端：优先本地 ollama（免费离线，与 pi 同机）；云端 deepseek/commandcode 需余额。
# 若配了可用 key，把 OLLAMA 置空即可走 deepseek。
OLLAMA = "http://localhost:11434"
MODEL = "qwen3-vl:32b"          # ollama 本地
CLOUD_MODEL = "deepseek-v4-flash"
API = "https://api.deepseek.com/chat/completions"


def call_llm(system, user, timeout=240):
    import urllib.request
    body = json.dumps({
        "model": MODEL, "stream": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "format": "json",
        "options": {"num_predict": 2400, "temperature": 0.2, "num_ctx": 16384},
    }).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    content = (d.get("message") or {}).get("content") or ""
    if not content.strip():
        # qwen3 思考型：content 空则取 thinking 兜底（一般不会）
        raise RuntimeError("ollama 返回空 content")
    return content


SYSTEM = """你是A股宏观策略分析师。输入一批当日财经新闻标题+摘要（含政策/央行动向/经济数据/产业/外围）。
输出JSON(仅对象,无其他文字):
{
 "sentiment": "多头|中性|空头|防御",   // 防御=风险事件主导建议降仓防守
 "score": 0-100,                       // 0极度利空 100极度利多, 50中性
 "title": "一句话总纲(≤30字)",
 "summary": "150字内逻辑链: 主要驱动+对A股影响",
 "drivers": ["利多驱动1", "利多驱动2"],
 "risks": ["风险点1", "风险点2"],
 "sectors": ["受益板块1","受益板块2"],
 "stance": "进攻|均衡|防守"             // 对应建议: 满仓/正常/空仓等回踩
}
要求: 依据事实新闻判断, 不臆造; 若新闻不足以判断给中性50均衡。"""


def build_user(news, date):
    lines = []
    for n in news[:60]:
        t = (n.get("title") or "").strip()
        s = (n.get("summary") or "").strip()
        lines.append("• %s%s" % (t, ("｜" + s[:120]) if s else ""))
    return ("日期 %s。以下为当日财经新闻列表，请给出宏观多空判断：\n\n%s"
            % (date, "\n".join(lines)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    if args.offline and os.path.exists(OUT):
        print("离线模式: 沿用 %s" % OUT)
        return
    news = mc.fetch_news(use_cache=not args.no_cache)
    try:
        intl = mc.fetch_news_international(page_size=50)
        news = news + intl
    except Exception as e:
        print("  [warn] 国际新闻失败 %s" % e)
    if args.date:
        news = [n for n in news if (n.get("show_time") or "")[:10] == args.date]
        if not news:
            # 兜底: 用最新
            news = mc.fetch_news(use_cache=True)
    # 去重
    seen, uniq = set(), []
    for n in news:
        t = n.get("title", "")
        if t in seen:
            continue
        seen.add(t)
        uniq.append(n)
    news = uniq
    date = args.date or (news[0].get("show_time") or time.strftime("%Y-%m-%d"))[:10]
    print("新闻 %d 条（截至 %s），调用 %s ..." % (len(news), date, MODEL))
    try:
        content = call_llm(SYSTEM, build_user(news, date))
        j = json.loads(content)
        j["_backend"] = "ollama " + MODEL
    except Exception as e:
        print("LLM 失败: %s（回退词典法温度）" % e)
        # 回退: macro 词典净分 → 近似
        j = {"sentiment": "中性", "score": 50, "title": "LLM不可用,回退中性",
             "summary": str(e)[:100], "drivers": [], "risks": [], "sectors": [],
             "stance": "均衡", "_fallback": True}
    out = {"generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
           "date": date, "news_count": len(news),
           "llm": {"model": MODEL, "backend": j.get("_backend", MODEL), **{k2: v2 for k2, v2 in j.items() if k2 != "_backend"}}}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    ll = out["llm"]
    print("\n[%s][%s] %s  %s  score=%s  stance=%s" % (date, ll.get("backend", "?"),
          ll["sentiment"], ll["title"], ll["score"], ll["stance"]))
    print("逻辑:", ll.get("summary"))
    if ll.get("drivers"):
        print("驱动:", "；".join(ll["drivers"]))
    if ll.get("risks"):
        print("风险:", "；".join(ll["risks"]))
    if ll.get("sectors"):
        print("板块:", "、".join(ll["sectors"]))
    print("→ %s" % OUT)


if __name__ == "__main__":
    main()
