#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 宏观消息面观察（ollama 本地；云端欠费回退）。

用法:
  python3 macro_llm.py                # 宏观(财经全流) + 个股新闻(按 sim_live 池模式) → LLM 判断
  python3 macro_llm.py --pool six     # 强制 6股精选池个股新闻
  python3 macro_llm.py --pool all     # 全池（不抓个股新闻，只宏观）
  python3 macro_llm.py --date 2026-09-04   # 指定日期

输出 data/macro_llm_data.json:
  {generatedAt, date, llm: {sentiment/score/title/summary/drivers/risks/sectors/stance/backend},
   stocks: [{code,name,sentiment,score,note}]   # 6股池逐股消息面评审
  }
下游:
  sim_live.py --plan 读取: sentiment∈{空头,防御} → 计划闸门 block
  sim_live.py --plan 读取 stocks: verdict=avoid 的个股不入计划
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
# LLM 后端（2026-09 多通道）:
#   1) commandcode /provider/v1 (deepseek-v4-flash) — 快, UA 须 OpenAI/Python 才过 CF
#   2) ollama 本地 qwen3-vl:32b — 免费离线降级
# key 复用 binance-llm-bot/.env 的 COMMAND_CODE_API_KEY（已验证可用）
OLLAMA = "http://localhost:11434"
MODEL = "qwen3-vl:32b"                 # ollama 本地模型
CLOUD_MODEL = "deepseek/deepseek-v4-flash"
CC_URL = "https://api.commandcode.ai/provider/v1/chat/completions"
CC_UA = "OpenAI/Python 1.99.0"


def _cc_key():
    import glob as _g
    envs = _g.glob("/mnt/c/Users/z7280/binance-llm-bot/.env")
    for envf in envs:
        try:
            for line in open(envf):
                if line.startswith("COMMAND_CODE_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return ""


def _call_cc(system, user, timeout=120):
    import urllib.request
    key = _cc_key()
    if not key:
        raise RuntimeError("无 commandcode key")
    body = json.dumps({
        "model": CLOUD_MODEL, "stream": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.2, "max_tokens": 2400,
    }).encode()
    req = urllib.request.Request(CC_URL, data=body, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json",
        "User-Agent": CC_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    return (d["choices"][0]["message"]["content"] or "").strip()


def _call_ollama(system, user, timeout=240):
    import urllib.request
    body = json.dumps({
        "model": MODEL, "stream": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "format": "json",
        "options": {"num_predict": 4096, "temperature": 0.2, "num_ctx": 16384},
    }).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    content = (d.get("message") or {}).get("content") or ""
    if not content.strip():
        raise RuntimeError("ollama 返回空 content")
    return content


def call_llm(system, user, timeout=240, expect_json=False):
    """返回 (content, backend)。commandcode 优先 → ollama 降级。

    expect_json=True 时校验返回可解析为 JSON，失败视为无效并重试
    （云端通道偶发非法 JSON：字符串未转义/缺逗号，只看"非空"会直接炸到调用方）。
    """
    def _ok(c):
        if not c or not c.strip():
            return False, "空返回"
        if not expect_json:
            return True, ""
        try:
            _json_of(c)          # 注意：_json_of 已返回解析后的对象，不要再 json.loads 一次
            return True, ""
        except Exception as e:
            return False, "JSON非法(%s)" % str(e)[:60]

    errs = []
    for attempt in range(2):
        try:
            c = _call_cc(system, user, timeout=min(timeout, 60))
            good, why = _ok(c)
            if good:
                return c, "commandcode"
            errs.append("cc %s(第%d次)" % (why, attempt + 1))
        except Exception as e:
            errs.append("cc: %s" % str(e)[:80])
        import time as _t
        _t.sleep(1.5 * (attempt + 1))
    try:
        c = _call_ollama(system, user, timeout=timeout)
        good, why = _ok(c)
        if good:
            return c, "ollama"
        errs.append("ollama %s" % why)
    except Exception as e:
        errs.append("ollama: %s" % str(e)[:80])
    raise RuntimeError("LLM 全部失败: " + "; ".join(errs))


def _json_of(text):
    """从 LLM 返回里抠出 JSON 对象（容错 ```json 包裹 / 前后废话 / 多对象）。

    直接 json.loads 在云端模型偶发非纯 JSON 时会炸（实测个股评审因此长期失败）。
    """
    import re as _re
    if not text or not text.strip():
        raise ValueError("空返回")
    t = text.strip()
    if "```" in t:
        m = _re.search(r"```(?:json)?\s*([\s\S]*?)```", t)
        if m:
            t = m.group(1).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("无 JSON")
    return json.loads(t[i:j + 1])


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



# ---------------- 个股相关新闻（新浪个股资讯页，6股池用） ----------------
SINA_STOCK = "https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllNewsStock/symbol/{sym}.phtml"
SIX_POOL = {
    "601138": ("工业富联", "sh601138"), "600900": ("长江电力", "sh600900"),
    "601899": ("紫金矿业", "sh601899"), "600309": ("万华化学", "sh600309"),
    "002142": ("宁波银行", "sz002142"), "600177": ("雅戈尔", "sh600177"),
}
SINA_SYM = {"6": "sh", "0": "sz", "3": "sz"}


def _sina_get(url):
    import re as _re
    import urllib.request as _ur
    req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = _ur.urlopen(req, timeout=15).read().decode("gbk", "ignore")
    return html


def fetch_stock_news(code, date=None, limit=12):
    """抓单只个股新浪资讯页新闻 → [{title, time, url}]（升序）。"""
    import re
    sym = SIX_POOL.get(code, (None, None))[1]
    if not sym:
        sym = (SINA_SYM.get(code[0], "sh") + code)
    try:
        html = _sina_get(SINA_STOCK.format(sym=sym))
    except Exception as e:
        return []
    out = []
    # 条目: <a target='_blank' href='URL'>标题</a> ... 时间在 <br>后
    for m in re.finditer(r"<a target='_blank' href='([^']+)'>([^<]{6,120})</a>\s*<br>[^0-9]*(\d{4}-\d{2}-\d{2})\s*(\d{2}:\d{2})?", html):
        url, title, d, t = m.groups()
        if date and d != date:
            continue
        out.append({"title": title.strip(), "time": (d + " " + t if t else d),
                    "url": url})
        if len(out) >= limit:
            break
    return out


STOCK_SYSTEM = """你是A股消息面分析师。输入宏观背景 + 某只股票的相关新闻标题（个股/行业/政策），判断该股消息面：
输出JSON: {"stocks":[{"code":"600900","sentiment":"利好|中性|利空","score":0-100,"note":"<=30字依据"}]}
要点：政策/业绩/行业景气利好=高分；监管/减持/行业利空=低分；纯行业新闻不给极端分。"""


def llm_review_stocks(macro_txt, stock_news, timeout=300):
    """逐股消息面 LLM 评审。stock_news: {code: [news...]}"""
    import json as _json
    lines = []
    for code, news in stock_news.items():
        name = SIX_POOL.get(code, (code,))[0]
        titles = "；".join(n["title"][:60] for n in news[:8]) or "（无个股新闻）"
        lines.append("%s %s: %s" % (code, name, titles))
    if not lines:
        return {}
    user = "【宏观】%s\n【个股新闻】\n%s\n逐只给出消息面 sentiment/score/note。" % (macro_txt, "\n".join(lines))
    try:
        content, _bk1 = call_llm(STOCK_SYSTEM, user, timeout=timeout, expect_json=True)
        j = _json_of(content)
        out = {}
        for r in j.get("stocks", []):
            code = str(r.get("code", "")).strip()
            if code:
                out[code] = {"sentiment": r.get("sentiment", "中性"),
                             "score": r.get("score", 50),
                             "note": (r.get("note") or "")[:40]}
        return out
    except Exception as e:
        print("  [llm] 个股新闻评审失败: %s" % e)
        return {}


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
    ap.add_argument("--pool", default=None, help="six=6股精选(抓个股新闻) / all=全池(仅宏观)")
    args = ap.parse_args()

    if args.offline and os.path.exists(OUT):
        print("离线模式: 沿用 %s" % OUT)
        return
    # 池模式：--pool > 默认 six（v4 双池后 meta.pool_mode 已废弃，恒为 all 导致
    # 个股消息面评审从未生效；six 池固定 6 只、成本低，默认就抓）
    pmode = args.pool or "six"
    if pmode not in ("six", "all"):
        pmode = "six"
    print("池模式:", "6股精选(含个股消息面)" if pmode == "six" else "全池(仅宏观)")
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
        content, _bk = call_llm(SYSTEM, build_user(news, date), expect_json=True)
        j = _json_of(content)
        j["_backend"] = _bk
    except Exception as e:
        print("LLM 失败: %s（回退词典法温度）" % e)
        # 回退前先看旧数据：LLM 偶发失败不该把之前一份有效判断冲成"中性50"
        # （否则闸门会在无声无息中从"多头/防御"退化成永远中性）
        try:
            old = json.load(open(OUT, encoding="utf-8"))
            old_ll = (old.get("llm") or {})
            if old_ll.get("sentiment") and not old_ll.get("_fallback"):
                print("→ 保留上次有效宏观判断（%s %s，%s），本次不写盘" % (
                    old_ll.get("sentiment"), old_ll.get("score"), old.get("date")))
                return
        except Exception:
            pass
        j = {"sentiment": "中性", "score": 50, "title": "LLM不可用,回退中性",
             "summary": str(e)[:100], "drivers": [], "risks": [], "sectors": [],
             "stance": "均衡", "_fallback": True}
    out = {"generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
           "date": date, "news_count": len(news), "pool_mode": pmode,
           "llm": {"model": MODEL, "backend": j.get("_backend", MODEL), **{k2: v2 for k2, v2 in j.items() if k2 != "_backend"}}}
    ll = out["llm"]
    # ---- 6股池：抓个股新闻 + 逐股消息面评审 ----
    if pmode == "six":
        macro_txt = "%s score=%s | %s | 利好:%s | 风险:%s" % (
            ll.get("sentiment"), ll.get("score"), ll.get("summary") or "",
            "、".join(ll.get("drivers") or []) or "-", "、".join(ll.get("risks") or []) or "-")
        stock_news = {}
        for code, (name, _sym) in SIX_POOL.items():
            sn = fetch_stock_news(code, date=date)
            stock_news[code] = sn
            print("  %s %s: %d 条新闻" % (code, name, len(sn)))
        srev = llm_review_stocks(macro_txt, stock_news)
        out["stocks"] = [{"code": c, "name": SIX_POOL[c][0], **r}
                         for c, r in srev.items()]
        for s in out["stocks"]:
            print("  [股] %s %s %s score=%s %s" % (s["code"], s["name"], s["sentiment"],
                                                  s["score"], s.get("note", "")))
    print("\n[%s][%s] %s  %s  score=%s  stance=%s" % (date, ll.get("backend", "?"),
          ll["sentiment"], ll["title"], ll["score"], ll["stance"]))
    print("逻辑:", ll.get("summary"))
    if ll.get("drivers"):
        print("驱动:", "；".join(ll["drivers"]))
    if ll.get("risks"):
        print("风险:", "；".join(ll["risks"]))
    if ll.get("sectors"):
        print("板块:", "、".join(ll["sectors"]))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("→ %s" % OUT)


if __name__ == "__main__":
    main()
