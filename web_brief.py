#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宏观联网简报（给宏观 LLM 的实时外部上下文）。

用户要求「宏观要记得联网」。模型本身不联网，所以由本模块把联网抓到的数据
拼成带时间戳的【联网简报】注入宏观 prompt：

  1. data/external_data.json —— build_external.py 每 3 小时抓的
     美股/纳指/VIX/原油/黄金/铜 + 板块偏好 + 风险偏好（launchd com.smas.dsr.external）
  2. src.macro.fetch_news_international() —— 新浪国际财经，实时抓
  3. src.macro.fetch_news() —— 东方财富财经快讯（主新闻列表已有，这里只补条数）

超过 24h 的外部因子会标注「可能过期」，避免模型拿旧数据当实时。
抓不到就跳过该段，绝不阻塞宏观判断（模型还有新闻列表可依据）。

    python3 web_brief.py            # 打印简报（人工检查）
    python3 web_brief.py --json     # 输出结构化 JSON
"""
import json
import os
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

EXTERNAL = os.path.join(BASE_DIR, "data", "external_data.json")
MAX_CHARS = 4200
STALE_HOURS = 24


def _age_hours(ts):
    try:
        return (time.time() - datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").timestamp()) / 3600.0
    except Exception:
        return None


def external_block():
    """把定时抓取的外部因子压成短文本；返回 (text, meta)。"""
    try:
        with open(EXTERNAL, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        return "", {"ok": False, "why": str(e)[:80]}
    ts = d.get("generatedAt") or ""
    age = _age_hours(ts)
    stale = age is not None and age > STALE_HOURS
    lines = []
    if ts:
        lines.append("外部因子抓取时间 %s%s" % (ts, "（超过24h，可能过期）" if stale else ""))
    for s in (d.get("summary") or [])[:6]:
        lines.append("· " + str(s)[:180])
    q = (d.get("quotes") or {}).get("us_index") or {}
    if isinstance(q, dict):
        for k in ("DJI", "IXIC", "INX", "NDX"):
            v = q.get(k)
            if isinstance(v, dict) and v.get("price") is not None:
                pct = float(v.get("change_pct") or v.get("chg") or 0)
                lines.append("· %s %s (%+.2f%%) %s" % (v.get("name") or k, v.get("price"), pct,
                                                       ("截至" + str(v.get("ts") or "")[:16]) if v.get("ts") else ""))
    sb = d.get("sector_bias") or {}
    if sb:
        pos = [k for k, v in sorted(sb.items(), key=lambda x: -float(x[1] or 0)) if float(v or 0) > 0][:5]
        neg = [k for k, v in sorted(sb.items(), key=lambda x: float(x[1] or 0)) if float(v or 0) < 0][:5]
        if pos:
            lines.append("· 板块偏好(正): " + "、".join(pos))
        if neg:
            lines.append("· 板块偏好(负): " + "、".join(neg))
    rp = d.get("risk_pref") or {}
    if rp:
        lines.append("· 风险偏好: %s %s" % (rp.get("score"), rp.get("note") or rp.get("tone") or ""))
    meta = {"ok": True, "generatedAt": ts, "age_hours": round(age, 1) if age is not None else None,
            "stale": stale, "n_summary": len(d.get("summary") or [])}
    return "\n".join(lines), meta


def international_block(limit=16, timeout=12):
    """实时抓国际财经新闻；失败返回空，不阻塞。"""
    try:
        from src import macro as mc
        news = mc.fetch_news_international(page_size=30) or []
    except Exception as e:
        return "", {"ok": False, "why": str(e)[:80], "n": 0}
    lines, seen = [], set()
    for n in news:
        t = (n.get("title") or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        s = (n.get("summary") or "").strip()
        lines.append("· %s%s" % (t[:110], ("｜" + s[:90]) if s else ""))
        if len(lines) >= limit:
            break
    return "\n".join(lines), {"ok": True, "n": len(lines)}


def build_web_context(max_chars=MAX_CHARS):
    """返回 (简报文本, meta)。简报文本可直接拼进宏观 user prompt。"""
    ext, em = external_block()
    intl, im = international_block()
    if not ext and not intl:
        return "", {"ok": False, "why": "外部因子与国际新闻均不可用", "external": em, "international": im}
    parts = ["【联网简报｜实时抓取，优先据此判断，勿凭记忆臆造】"]
    if ext:
        parts.append("■ 外部行情/因子（每3小时定时抓取）\n" + ext)
    if intl:
        parts.append("■ 国际财经要闻（实时抓取）\n" + intl)
    parts.append("（以上为联网数据；若与新闻列表冲突，以时间戳更近者为准，并在 summary 里点明关键数据。）")
    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n（简报截断）"
    meta = {"ok": True, "external": em, "international": im,
            "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "chars": len(text)}
    return text, meta


if __name__ == "__main__":
    text, meta = build_web_context()
    if "--json" in sys.argv:
        print(json.dumps({"web": text, "meta": meta}, ensure_ascii=False, indent=2))
    else:
        print(text)
        print("\n--- meta ---")
        print(json.dumps(meta, ensure_ascii=False, indent=2))
