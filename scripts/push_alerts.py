#!/usr/bin/env python3
"""微信推送（Server酱）：回测操作提醒 / 复盘总结 / 资金动向 / 盘中推荐。

用法：
    python3 scripts/push_alerts.py backtest   # 回测操作提醒（长江电力/中远海控/中证红利）
    python3 scripts/push_alerts.py review     # 复盘总结
    python3 scripts/push_alerts.py fund       # 资金动向总结
    python3 scripts/push_alerts.py recommend  # 盘中推荐 Top
    python3 scripts/push_alerts.py all        # 全部

Server酱 Key 从 .env 的 SERVERCHAN_KEY 读取（不写入代码/git）。
"""
import json
import os
import sys

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")


def serverchan_key():
    env_path = os.path.join(BASE_DIR, ".env")
    key = os.environ.get("SERVERCHAN_KEY", "")
    if not key:
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("SERVERCHAN_KEY="):
                        key = line.split("=", 1)[1].strip()
                        break
        except OSError:
            pass
    return key


def send(title, content):
    key = serverchan_key()
    if not key:
        print("[push] 未配置 SERVERCHAN_KEY，跳过推送")
        return
    url = "https://sctapi.ftqq.com/%s.send" % key
    try:
        resp = requests.post(url, data={"title": title[:200], "desp": content[:32768]}, timeout=20)
        result = resp.json()
        if result.get("code") == 0:
            print("[push] OK:", title)
        else:
            print("[push] ERROR:", result)
    except Exception as e:
        print("[push] FAIL:", e)


def load(name):
    try:
        with open(os.path.join(DATA_DIR, name), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def pct(v, nd=2):
    """格式化小数比例（0.118 → +11.80%）。"""
    if v is None:
        return "--"
    try:
        return ("%+." + str(nd) + "f%%") % (float(v) * 100)
    except (TypeError, ValueError):
        return str(v)


def pct_num(v, nd=2):
    """格式化已是百分比的数值（-1.78 → -1.78%），recommend/institution 的 change_pct 用这个。"""
    if v is None:
        return "--"
    try:
        return ("%+." + str(nd) + "f%%") % float(v)
    except (TypeError, ValueError):
        return str(v)


def money(v):
    """格式化金额：亿/万。"""
    if v is None:
        return "--"
    try:
        a = abs(float(v))
        neg = float(v) < 0
        s = (a / 1e8 if a >= 1e8 else a / 1e4)
        u = "亿" if a >= 1e8 else "万"
        return ("-" if neg else "") + ("%.1f" % s) + u
    except (TypeError, ValueError):
        return str(v)


def red(t):
    # 微信 Server酱 不渲染 HTML，红涨用 emoji 标注
    return "🔴 " + str(t)


def green(t):
    return "🟢 " + str(t)


def dark(t):
    # Markdown 加粗（Server酱/微信支持），不用 HTML 颜色
    return "**" + str(t) + "**"


def updown(v, text=None):
    """涨跌标注：正红负绿（A股习惯，emoji），非数值返回 --。"""
    if v is None:
        return "--"
    try:
        if float(v) > 0:
            return red(text if text is not None else str(v))
        if float(v) < 0:
            return green(text if text is not None else str(v))
        return text if text is not None else "0"
    except (TypeError, ValueError):
        return text if text is not None else str(v)


def push_backtest():
    """回测操作提醒：长江电力 / 中远海控 / 中证红利。"""
    bi = load("backtest_index.json")
    if not bi:
        print("[backtest] 无数据")
        return
    stocks = bi.get("stocks", {})
    targets = ["长江电力", "中远海控", "中证红利ETF招商"]
    lines = ["## 网格回测 · 操作提醒", ""]
    for name in targets:
        s = stocks.get(name)
        if not s:
            lines.append("**%s**：回测无数据" % name)
            continue
        sm = s.get("summary", {})
        lines.append("### %s" % name)
        lines.append("- 年化 **%s** · 夏普 %s · 最大回撤 %s" % (
            pct(sm.get("annual_return")), sm.get("sharpe"), pct(sm.get("max_drawdown"))))
        lines.append("- 卡玛 %s · 超额 %s · 交易 %s 笔" % (
            sm.get("calmar"), pct(sm.get("excess_return")), sm.get("trade_count")))
        lines.append("")
    # 持仓网格信号
    hd = load("holdings_data.json")
    if hd:
        lines.append("### 持仓网格信号")
        for h in hd.get("items", []):
            g = h.get("grid", {})
            if not g:
                continue
            act = g.get("action", "--")
            dev = g.get("dev")
            pos = g.get("position")
            lines.append("- %s：**%s** · 偏离均值线 %s · 目标仓位 %s" % (
                h.get("name"), act,
                ("%+.1f%%" % (dev * 100)) if dev is not None else "--",
                ("%.0f%%" % (pos * 100)) if pos is not None else "--"))
            if g.get("reason"):
                lines.append("  %s" % g["reason"])
    send("回测操作提醒 · 长江电力/中远海控/中证红利", "\n".join(lines))


def push_review():
    """复盘总结。"""
    d = load("review_data.json")
    if not d:
        print("[review] 无数据")
        return
    t = d.get("temperature", {})
    st = d.get("stats", {})
    lines = ["## 每日复盘总结", ""]
    lines.append("**市场温度：%s（%s）**" % (t.get("score", "--"), t.get("label", "--")))
    lines.append("市场广度：%s%% 上涨 · 平均涨跌 %s" % (
        t.get("breadth", "--"), pct(t.get("avg_change")) if isinstance(t.get("avg_change"), (int, float)) else "--"))
    if t.get("market_total"):
        lines.append("全市场：涨 %s / 跌 %s / 平 %s" % (
            t.get("market_up", "--"), t.get("market_down", "--"), t.get("market_flat", "--")))
    lines.append("自选池：涨 %s / 跌 %s（共 %s）" % (
        st.get("up", "--"), st.get("down", "--"), st.get("total", "--")))
    lines.append("")
    if st.get("strongest"):
        lines.append("- 最强：**%s**" % st["strongest"])
    if st.get("weakest"):
        lines.append("- 最弱：**%s**" % st["weakest"])
    # 大盘指数
    for ix in (d.get("indices") or [])[:5]:
        if ix.get("name") and ix.get("change_pct") is not None:
            lines.append("- %s：%s" % (ix["name"], pct(ix["change_pct"])))
    lines.append("")
    lines.append("完整复盘见页面 review.html")
    send("每日复盘总结 · %s" % t.get("label", ""), "\n".join(lines))


def push_fund():
    """资金动向总结。"""
    d = load("institution_data.json")
    if not d:
        print("[fund] 无数据")
        return
    o = d.get("overview", {})
    lines = ["## 资金动向总结", ""]
    main_net = o.get("main_net")
    lines.append("**两市主力净流入：%s**" % (
        "%.0f亿" % (main_net / 1e8) if isinstance(main_net, (int, float)) else "--"))
    lines.append("净流入板块 %s / 净流出板块 %s" % (
        o.get("inflow_sectors", "--"), o.get("outflow_sectors", "--")))
    # 板块资金
    sec = d.get("sector", {})
    ind = sec.get("industry", {})
    inf = (ind.get("inflow") or [])[:3]
    outf = (ind.get("outflow") or [])[:3]
    if inf:
        lines.append("")
        lines.append("### 主力净流入板块")
        for x in inf:
            lines.append("- %s：%s" % (x.get("sector") or x.get("name"), pct(x.get("change_pct")) if x.get("change_pct") else ("%.1f亿" % (x.get("main_net", 0) / 1e8))))
    if outf:
        lines.append("")
        lines.append("### 主力净流出板块")
        for x in outf:
            lines.append("- %s：%s" % (x.get("sector") or x.get("name"), pct(x.get("change_pct")) if x.get("change_pct") else ("%.1f亿" % (x.get("main_net", 0) / 1e8))))
    # 同花顺特色
    ths = d.get("ths", {})
    if ths:
        dt = ths.get("dragon_tiger", {})
        hot = ths.get("hot_list", [])
        if dt.get("items"):
            lines.append("")
            lines.append("### 龙虎榜（%s）" % (dt.get("date") or ""))
            for x in (dt["items"][:5]):
                lines.append("- %s：净买 %s" % (x.get("name"), ("%.1f亿" % (x.get("net_value", 0) / 1e8)) if isinstance(x.get("net_value"), (int, float)) else "--"))
        if hot:
            lines.append("")
            lines.append("### 热股榜 Top5")
            for x in hot[:5]:
                lines.append("- %s：%s" % (x.get("name"), pct(x.get("change_pct")) if x.get("change_pct") else ""))
    lines.append("")
    lines.append("完整资金页见 institution.html")
    send("资金动向总结", "\n".join(lines))


def push_recommend():
    """盘中推荐 Top（约每小时一次）。"""
    d = load("recommend_data.json")
    if not d:
        print("[recommend] 无数据")
        return
    picks = d.get("picks", [])[:8]
    lines = ["## 盘中推荐 Top%d" % len(picks), ""]
    for i, p in enumerate(picks, 1):
        lines.append("%d. **%s**（%s）%s → %s · %s分" % (
            i, p.get("name"), p.get("sector", "--"),
            p.get("signal", "--"), p.get("rating", "--"), p.get("total_score", "--")))
        if p.get("change_pct") is not None:
            lines.append("   现价 %s · %s" % (p.get("price"), pct(p.get("change_pct"))))
        if p.get("reasons"):
            lines.append("   %s" % p["reasons"][:80])
    lines.append("")
    lines.append("完整推荐页见 recommend.html")
    send("盘中推荐 · %s" % (d.get("generatedAt") or ""), "\n".join(lines))


def _summary_lines(*parts):
    """合并多个推送内容为单条（控制 Server酱 免费版每日 5 条限额）。"""
    out = []
    for title, content in parts:
        if content:
            out.append(content.strip())
    return "\n\n---\n\n".join(out)


def push_intraday():
    """盘中合并推送（1 条）：回测操作提醒 + 推荐 Top + 资金跟踪（10:00/12:00/14:00）。"""
    parts = []
    # 回测操作提醒（长江电力/中远海控/中证红利）
    bi = load("backtest_index.json")
    if bi:
        stocks = bi.get("stocks", {})
        targets = ["长江电力", "中远海控", "中证红利ETF招商"]
        lines = [dark("回测操作提醒"), ""]
        for name in targets:
            s = stocks.get(name)
            if not s:
                lines.append("**%s**：回测无数据" % name)
                continue
            sm = s.get("summary", {})
            lines.append("**%s**" % name)
            lines.append("年化 %s · 夏普 %s · 回撤 %s" % (
                red(pct(sm.get("annual_return"))) if (sm.get("annual_return") or 0) >= 0 else green(pct(sm.get("annual_return"))),
                sm.get("sharpe"), pct(sm.get("max_drawdown"))))
            lines.append("卡玛 %s · 超额 %s · 交易 %s 笔" % (
                sm.get("calmar"), pct(sm.get("excess_return")), sm.get("trade_count")))
            lines.append("")
        hd = load("holdings_data.json")
        if hd:
            lines.append("**持仓网格信号**")
            for h in hd.get("items", []):
                g = h.get("grid", {})
                if not g:
                    continue
                dev = g.get("dev")
                pos = g.get("position")
                dev_s = updown(dev, ("%+.1f%%" % (dev * 100)) if dev is not None else "--")
                lines.append("%s：**%s** · 偏离 %s · 仓位 %s" % (
                    h.get("name"), g.get("action", "--"), dev_s,
                    ("%.0f%%" % (pos * 100)) if pos is not None else "--"))
        parts.append(("回测", "\n".join(lines)))
    # 推荐 Top
    d = load("recommend_data.json")
    picks = (d.get("picks") or [])[:6] if d else []
    if picks:
        lines = [dark("盘中推荐 Top%d" % len(picks)), ""]
        for i, p in enumerate(picks, 1):
            chg = pct_num(p.get("change_pct")) if p.get("change_pct") is not None else "--"
            lines.append("%d. **%s**（%s）%s → %s · %s分" % (
                i, p.get("name"), p.get("sector", "--"),
                p.get("signal", "--"), p.get("rating", "--"), p.get("total_score", "--")))
            lines.append("   现价 %s · %s" % (p.get("price"), updown(p.get("change_pct"), chg)))
            if p.get("reasons"):
                lines.append("   %s" % p["reasons"][:60])
        parts.append(("推荐", "\n".join(lines)))
    # 资金跟踪
    fi = load("institution_data.json")
    if fi:
        o = fi.get("overview", {})
        main_net = o.get("main_net")
        lines = [dark("资金跟踪"), ""]
        lines.append("两市主力净流入：**%s**" % (
            updown(main_net, money(main_net)) if isinstance(main_net, (int, float)) else "--"))
        lines.append("净流入板块 %s / 净流出板块 %s" % (
            o.get("inflow_sectors", "--"), o.get("outflow_sectors", "--")))
        ths = fi.get("ths", {})
        if ths.get("hot_list"):
            lines.append("")
            lines.append("热股榜：" + "、".join(x.get("name", "") for x in ths["hot_list"][:5]))
        parts.append(("资金", "\n".join(lines)))
    if not parts:
        print("[intraday] 无数据")
        return
    send("盘中播报 · %s" % ((d or {}).get("generatedAt", "") or ""), _summary_lines(*parts))


def push_close():
    """盘后合并推送（1 条）：复盘总结。"""
    parts = []
    d = load("review_data.json")
    if d:
        t = d.get("temperature", {})
        st = d.get("stats", {})
        lines = [dark("每日复盘总结"), ""]
        tmp = t.get("score", "--")
        # 温度：高温红/低温绿（emoji），不加粗（避免嵌套 Markdown 错乱）
        tmp_s = red(str(tmp)) if isinstance(tmp, (int, float)) and tmp >= 60 else (
            green(str(tmp)) if isinstance(tmp, (int, float)) and tmp <= 40 else str(tmp))
        lines.append("市场温度：**%s（%s）**" % (tmp_s, t.get("label", "--")))
        breadth = t.get("breadth")
        avg = t.get("avg_change")
        avg_s = updown(avg, pct_num(avg)) if isinstance(avg, (int, float)) else "--"
        lines.append("市场广度：%s%% 上涨 · 平均涨跌 %s" % (breadth if breadth is not None else "--", avg_s))
        if t.get("market_total"):
            lines.append("全市场：涨 %s / 跌 %s / 平 %s" % (
                red(t.get("market_up", "--")), green(t.get("market_down", "--")), t.get("market_flat", "--")))
        lines.append("自选池：涨 %s / 跌 %s（共 %s）" % (
            red(st.get("up", "--")), green(st.get("down", "--")), st.get("total", "--")))
        if st.get("strongest"):
            lines.append("- 最强：**%s**" % st["strongest"])
        if st.get("weakest"):
            lines.append("- 最弱：**%s**" % st["weakest"])
        for ix in (d.get("indices") or [])[:5]:
            if ix.get("name") and ix.get("change_pct") is not None:
                lines.append("- %s：%s" % (ix["name"], updown(ix["change_pct"], pct_num(ix["change_pct"]))))
        parts.append(("复盘", "\n".join(lines)))
    if not parts:
        print("[close] 无数据")
        return
    send("收盘复盘 · %s" % ((d or {}).get("generatedAt", "") or ""), _summary_lines(*parts))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode == "intraday":
        push_intraday()
    elif mode == "close":
        push_close()
    elif mode in ("backtest", "all"):
        push_backtest()
    elif mode == "review":
        push_review()
    elif mode == "fund":
        push_fund()
    elif mode == "recommend":
        push_recommend()


if __name__ == "__main__":
    main()
