#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 宏观消息面观察（2026-11-08 起按决策路由：宏观走云端 ds4.1 + 联网简报）。

用法:
  python3 macro_llm.py                # 宏观(财经全流 + 联网简报) + 重点个股消息面 → LLM 判断
  python3 macro_llm.py --date 2026-09-04   # 指定日期
  DSR_FOCUS_NEWS=0 python3 macro_llm.py   # 跳过个股新闻抓取

输出 data/macro_llm_data.json:
  {generatedAt, date, llm: {sentiment/score/title/summary/drivers/risks/sectors/stance/backend},
   stocks: [{code,name,sentiment,score,note}]   # 重点个股消息面评审（冲刺盘/页面用）
  }
下游:
  sim_live.py --plan 读取: sentiment∈{空头,防御} → 计划闸门 block
  sim_sprint.py _news_avoid() 读取 stocks: 利空/低分个股回避
  sim_live.html 展示 stocks 表格

注：2026-11-10 起「6股精选策略池(six)」已下线；这里的 6 只重点个股只是消息面名单，
与该策略池无关，供冲刺盘与页面使用。
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
import llm_router
import web_brief

OUT = os.path.join(BASE_DIR, "data", "macro_llm_data.json")
# LLM 后端（2026-11-08 起：统一走 llm_router 决策路由）
#   macro      → workbuddy/deepseek-v4.1-flash（云端 ds4.1，长上下文 + 联网简报更稳）
#   stock_news → group/local-code（纯本地，短 JSON 与云端结论一致）
#   路由失败  → 直连 ollama 本地初稿（显式标记 local-draft）
# 旧的 commandcode 付费通道保留为最后兜底，仍受 AGENT_LLM_ALLOW_CLOUD 控制（默认关）。
# config/llm_routing.json 可改模型；DSR_LLM_LOCAL_ONLY=1 强制全本地。
OLLAMA = os.environ.get("DSR_OLLAMA", "http://localhost:11434")
MODEL = os.environ.get("DSR_LLM_MODEL", "qwen3-coder:30b")  # ollama 本地模型（原默认 qwen3-vl:32b 本机不存在）
CLOUD_MODEL = "deepseek/deepseek-v4-flash"
# 输出长度上限：本地模型最大的浪费是话多，800 token 足够宏观看板字段
MAX_TOKENS = int(os.environ.get("DSR_LLM_MAXTOK", "800"))
# agent 文件队列的等待秒数（原默认 2100s = 35 分钟，本地跑绝对不能用）
AGENT_WAIT = int(os.environ.get("DSR_AGENT_WAIT", "8"))
CC_URL = "https://api.commandcode.ai/provider/v1/chat/completions"
CC_UA = "OpenAI/Python 1.99.0"


def _cc_key():
    import glob as _g
    import os
    # 先找本机项目目录下的 .env（macOS 路径），再兼容旧 WSL 机器的绝对路径
    here = os.path.dirname(os.path.abspath(__file__))
    envs = [os.path.join(here, "binance-llm-bot", ".env")]
    envs += _g.glob("/mnt/c/Users/z7280/daily-stock-review/binance-llm-bot/.env")
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
    stream = os.environ.get("DSR_LLM_STREAM", "0") in ("1", "true", "yes", "on")
    body = json.dumps({
        "model": MODEL, "stream": stream,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "format": "json",
        "options": {"num_predict": MAX_TOKENS, "temperature": 0.2, "num_ctx": 16384},
    }).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    if not stream:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        content = (d.get("message") or {}).get("content") or ""
        if not content.strip():
            raise RuntimeError("ollama 返回空 content")
        return content
    # 流式：逐 token 累计，每 ~0.8s 打一行 [progress] 给 pi-progress 画进度条
    parts = []
    ntok = 0
    t0 = time.time()
    last = t0
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "ignore").strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            parts.append((d.get("message") or {}).get("content") or "")
            ntok += 1
            now = time.time()
            if now - last >= 0.8:
                print("[progress] %d/%d  %.1f tok/s" % (ntok, MAX_TOKENS, ntok / max(now - t0, 1e-6)), flush=True)
                last = now
            if d.get("done"):
                break
    content = "".join(parts)
    print("[progress] %d/%d  %.1f tok/s  (完成)" % (ntok, MAX_TOKENS, ntok / max(time.time() - t0, 1e-6)), flush=True)
    if not content.strip():
        raise RuntimeError("ollama 返回空 content")
    return content


def _agent_ask(kind, system, user, expect_json, meta=None):
    """人工/WorkBuddy 接管通道（llm_agent 文件队列）。没等到答案返回 None。

    按 2026-11-08 结论，各类决策都有固定模型路由兜底，队列只做「人工接管」：
    仍登记问题、仍优先读 done/ 里预置的答案，但不等人（wait=0），避免卡住定时任务。
    宏观默认连登记都跳过（DSR_MACRO_AGENT=1 可恢复），免得队列堆积。
    """
    if kind == "macro" and os.environ.get("DSR_MACRO_AGENT", "0") not in ("1", "true", "yes", "on"):
        return None
    wait = 0
    try:
        import llm_agent
        a = llm_agent.ask(kind, system, user, expect_json=expect_json, wait=wait, meta=meta)
        if a is None:
            return None
        return a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)
    except Exception as e:
        print("  [agent-llm] 不可用(%s)，走降级" % str(e)[:80])
        return None


def call_llm(system, user, timeout=240, expect_json=False, kind="macro", meta=None):
    """返回 (content, backend)。

    通道优先级（2026-11-08 起：按决策类型路由，见 config/llm_routing.json）：
      1) agent   —— llm_agent 文件队列，人工/WorkBuddy 接管（有人答就优先）
      2) router  —— macro 走云端 ds4.1（长上下文+联网简报）；其余短 JSON 走纯本地
      3) commandcode —— 付费云端，仅 AGENT_LLM_ALLOW_CLOUD=1 时最后兜底

    expect_json=True 时校验返回可解析为 JSON，失败视为无效。
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
    c = _agent_ask(kind, system, user, expect_json, meta=meta)
    if c is not None:
        good, why = _ok(c)
        if good:
            return c, "agent"
        errs.append("agent %s" % why)
    r = llm_router.route_of(kind)
    try:
        c, backend = llm_router.chat(kind, system, user, expect_json=expect_json,
                                     timeout=min(timeout, r["timeout"]))
        good, why = _ok(c)
        if good:
            return c, backend
        errs.append("router %s" % why)
    except Exception as e:
        errs.append("router %s: %s" % (r["model"], str(e)[:100]))
    # 最后兜底：旧的 commandcode 付费通道（默认关闭），以及直连 ollama
    try:
        c = _call_ollama(system, user, timeout=timeout)
        good, why = _ok(c)
        if good:
            return c, "ollama-direct"
        errs.append("ollama-direct %s" % why)
    except Exception as e:
        errs.append("ollama-direct: %s" % str(e)[:80])
    if os.environ.get("AGENT_LLM_ALLOW_CLOUD", "0") in ("1", "true", "yes", "on"):
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
要求: 依据事实新闻与【联网简报】判断, 不臆造; 联网简报是实时抓取的行情/国际要闻,
须优先引用其中的具体数据(指数涨跌幅、VIX、PMI、板块偏好等)并在 summary 里点明;
若新闻与简报不足以判断给中性50均衡。
精简要求(必须遵守): summary 不超过80字; drivers/risks 各最多2条; sectors 最多3个;
每条不超过20字; 总输出不超过350字。宁可少写, 不要凑字数。"""



# ---------------- 重点个股消息面（新浪个股资讯页） ----------------
# 注意：这组 6 只是「重点关注个股」名单，供 sim_sprint._news_avoid() 与页面展示，
# 与已下线的「6股精选策略池（six）」无关，不要跟着删。
FOCUS_NEWS_ON = os.environ.get("DSR_FOCUS_NEWS", "1").lower() not in ("0", "false", "off", "no")
SINA_STOCK = "https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllNewsStock/symbol/{sym}.phtml"
FOCUS_STOCKS = {
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
    sym = FOCUS_STOCKS.get(code, (None, None))[1]
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
        name = FOCUS_STOCKS.get(code, (code,))[0]
        titles = "；".join(n["title"][:60] for n in news[:8]) or "（无个股新闻）"
        lines.append("%s %s: %s" % (code, name, titles))
    if not lines:
        return {}
    user = "【宏观】%s\n【个股新闻】\n%s\n逐只给出消息面 sentiment/score/note。" % (macro_txt, "\n".join(lines))
    try:
        content, _bk1 = call_llm(STOCK_SYSTEM, user, timeout=timeout, expect_json=True,
                                 kind="stock_news")
        j = _json_of(content)
        raw = j.get("stocks")
        # 模型偶发返回非 list 结构（如 {"0": {...}} 或 [123]）→ 归一化，逐条校验类型
        if isinstance(raw, dict):
            raw = list(raw.values())
        if not isinstance(raw, list):
            raw = []
        out = {}
        for r in raw:
            if not isinstance(r, dict):
                continue
            code = str(r.get("code") or "").strip()
            if not code:
                continue
            try:
                sc = int(r.get("score", 50))
            except (TypeError, ValueError):
                sc = 50
            out[code] = {"sentiment": r.get("sentiment") or "中性",
                         "score": sc,
                         "note": (r.get("note") or "")[:40]}
        return out or None
    except Exception as e:
        print("  [llm] 个股新闻评审失败: %s" % e)
        return None


def build_user(news, date, web=None):
    lines = []
    for n in news[:60]:
        t = (n.get("title") or "").strip()
        s = (n.get("summary") or "").strip()
        lines.append("• %s%s" % (t, ("｜" + s[:120]) if s else ""))
    head = "日期 %s。以下为当日财经新闻列表，请给出宏观多空判断：" % date
    if web:
        head = "日期 %s。\n\n%s\n\n以下是当日财经新闻列表，结合联网简报给出宏观多空判断：" % (date, web)
    return "%s\n\n%s" % (head, "\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--pool", default=None, help="已停用（6 股精选池已下线，恒为全池）")
    args = ap.parse_args()

    if args.offline and os.path.exists(OUT):
        print("离线模式: 沿用 %s" % OUT)
        return
    # 2026-11-10 起 6 股精选池已下线：恒为全池，不再抓 6 只个股新闻
    pmode = "all"
    if args.pool and args.pool != "all":
        print("[warn] --pool %s 已停用（6 股精选池已下线），按全池处理" % args.pool)
    print("池模式: 全池(仅宏观)")
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
    # 宏观必须联网：把定时抓的外部因子 + 实时国际要闻注入 prompt（模型本身不联网）
    web, web_meta = web_brief.build_web_context()
    r = llm_router.route_of("macro")
    print("新闻 %d 条（截至 %s）｜联网简报 %d 字｜宏观路由 %s ..."
          % (len(news), date, len(web), r["model"]))
    try:
        content, _bk = call_llm(SYSTEM, build_user(news, date, web=web), expect_json=True)
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
           "route": {"macro": r["model"], "decision": llm_router.route_of("gate")["model"],
                     "web": bool(web), "web_meta": web_meta},
           "llm": {"model": j.get("_backend", r["model"]), "backend": j.get("_backend", r["model"]),
                   **{k2: v2 for k2, v2 in j.items() if k2 != "_backend"}}}
    ll = out["llm"]
    # ---- 重点个股消息面（供 sim_sprint._news_avoid() 与页面展示；非 six 策略池） ----
    if FOCUS_NEWS_ON:
        macro_txt = "%s score=%s | %s | 利好:%s | 风险:%s" % (
            ll.get("sentiment"), ll.get("score"), ll.get("summary") or "",
            "、".join(ll.get("drivers") or []) or "-", "、".join(ll.get("risks") or []) or "-")
        stock_news = {}
        for code, (name, _sym) in FOCUS_STOCKS.items():
            sn = fetch_stock_news(code, date=date)
            stock_news[code] = sn
            print("  %s %s: %d 条新闻" % (code, name, len(sn)))
        srev = llm_review_stocks(macro_txt, stock_news)
        if srev:
            out["stocks"] = [{"code": c, "name": FOCUS_STOCKS[c][0], **r}
                             for c, r in srev.items()]
            for s in out["stocks"]:
                print("  [股] %s %s %s score=%s %s" % (s["code"], s["name"], s["sentiment"],
                                                      s["score"], s.get("note", "")))
        else:
            # 评审失败：沿用上次有效的个股消息面（否则会写空 → 冲刺盘静默失去个股回避能力）
            prev = []
            try:
                with open(OUT, encoding="utf-8") as f:
                    prev = (json.load(f).get("stocks") or [])
            except Exception:
                pass
            out["stocks"] = prev
            out["stocks_stale"] = True
            print("  [warn] 个股消息面评审失败 → 沿用上次 %d 只（标记 stocks_stale）" % len(prev))
    else:
        # ds4.1 审阅：显式关闭时也沿用上次数据，否则写出无 stocks 的 json
        # 会让 sim_sprint._news_avoid() 静默失去个股回避能力。
        prev = []
        try:
            with open(OUT, encoding="utf-8") as f:
                prev = (json.load(f).get("stocks") or [])
        except Exception:
            pass
        out["stocks"] = prev
        out["stocks_stale"] = True
        print("  [warn] DSR_FOCUS_NEWS=0 → 沿用上次 %d 只个股消息面（标记 stocks_stale）" % len(prev))
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
