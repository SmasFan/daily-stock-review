#!/usr/bin/env python3
"""微信推送（Server酱）：回测操作提醒 / 复盘总结 / 资金动向 / 盘中推荐。

用法：
    python3 scripts/push_alerts.py intraday   # 盘中播报（回测+推荐+资金，10/12/14点）
    python3 scripts/push_alerts.py close      # 收盘复盘
    python3 scripts/push_alerts.py backtest / review / fund / recommend / all

格式说明：微信 Server酱 会把 Markdown `**加粗**` 渲染成蓝色链接样式（看不清），
因此全部改用纯文本 + Unicode 符号分隔（◇ ▍▶ · 等），颜色只用 emoji（🔴红涨 🟢绿跌）。

Server酱 Key 从 .env 的 SERVERCHAN_KEY 读取（不写入代码/git）。
"""
import json
import os
import sys
import time

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
    """格式化已是百分比的数值（-1.78 → -1.78%）。"""
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
    return "🔴 " + str(t)


def green(t):
    return "🟢 " + str(t)


def head(t):
    """区块标题：纯文本符号前缀（不用 Markdown 加粗，避免微信渲染成蓝色）。"""
    return "▍" + str(t)


def name(t):
    """股票名：用 ◇ 前缀区分（纯文本）。"""
    return "◇ " + str(t)


def updown(v, text=None):
    """涨跌标注：正红负绿（A股习惯，emoji）。"""
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
    lines = [head("回测操作提醒"), ""]
    for name in targets:
        s = stocks.get(name)
        if not s:
            lines.append("%s：回测无数据" % name)
            continue
        sm = s.get("summary", {})
        lines.append(name)
        lines.append("年化 %s · 夏普 %s · 最大回撤 %s" % (
            pct(sm.get("annual_return")), sm.get("sharpe"), pct(sm.get("max_drawdown"))))
        lines.append("卡玛 %s · 超额 %s · 交易 %s 笔" % (
            sm.get("calmar"), pct(sm.get("excess_return")), sm.get("trade_count")))
        lines.append("")
    # 持仓网格信号
    hd = load("holdings_data.json")
    if hd:
        lines.append("持仓网格信号")
        for h in hd.get("items", []):
            g = h.get("grid", {})
            if not g:
                continue
            act = g.get("action", "--")
            dev = g.get("dev")
            pos = g.get("position")
            lines.append("%s：%s · 偏离均值线 %s · 目标仓位 %s" % (
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
    lines = [head("每日复盘总结"), ""]
    lines.append("市场温度：%s（%s）" % (t.get("score", "--"), t.get("label", "--")))
    lines.append("市场广度：%s%% 上涨 · 平均涨跌 %s" % (
        t.get("breadth", "--"), pct(t.get("avg_change")) if isinstance(t.get("avg_change"), (int, float)) else "--"))
    if t.get("market_total"):
        lines.append("全市场：涨 %s / 跌 %s / 平 %s" % (
            t.get("market_up", "--"), t.get("market_down", "--"), t.get("market_flat", "--")))
    lines.append("自选池：涨 %s / 跌 %s（共 %s）" % (
        st.get("up", "--"), st.get("down", "--"), st.get("total", "--")))
    lines.append("")
    if st.get("strongest"):
        lines.append("最强：%s" % st["strongest"])
    if st.get("weakest"):
        lines.append("最弱：%s" % st["weakest"])
    # 大盘指数
    for ix in (d.get("indices") or [])[:5]:
        if ix.get("name") and ix.get("change_pct") is not None:
            lines.append("%s：%s" % (ix["name"], pct(ix["change_pct"])))
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
    lines = [head("资金动向总结"), ""]
    main_net = o.get("main_net")
    lines.append("两市主力净流入：%s" % (
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
        lines.append("主力净流入板块")
        for x in inf:
            lines.append("%s：%s" % (x.get("sector") or x.get("name"), pct(x.get("change_pct")) if x.get("change_pct") else ("%.1f亿" % (x.get("main_net", 0) / 1e8))))
    if outf:
        lines.append("")
        lines.append("主力净流出板块")
        for x in outf:
            lines.append("%s：%s" % (x.get("sector") or x.get("name"), pct(x.get("change_pct")) if x.get("change_pct") else ("%.1f亿" % (x.get("main_net", 0) / 1e8))))
    # 同花顺特色
    ths = d.get("ths", {})
    if ths:
        dt = ths.get("dragon_tiger", {})
        hot = ths.get("hot_list", [])
        if dt.get("items"):
            lines.append("")
            lines.append("龙虎榜（%s）" % (dt.get("date") or ""))
            for x in (dt["items"][:5]):
                lines.append("%s：净买 %s" % (x.get("name"), ("%.1f亿" % (x.get("net_value", 0) / 1e8)) if isinstance(x.get("net_value"), (int, float)) else "--"))
        if hot:
            lines.append("")
            lines.append("热股榜 Top5")
            for x in hot[:5]:
                lines.append("%s：%s" % (x.get("name"), pct(x.get("change_pct")) if x.get("change_pct") else ""))
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
    lines = [head("盘中推荐 Top%d" % len(picks)), ""]
    for i, p in enumerate(picks, 1):
        lines.append("%d. %s（%s）%s → %s · %s分" % (
            i, p.get("name"), p.get("sector", "--"),
            p.get("signal", "--"), p.get("rating", "--"), p.get("total_score", "--")))
        if p.get("change_pct") is not None:
            lines.append("   现价 %s · %s" % (p.get("price"), pct(p.get("change_pct"))))
        if p.get("reasons"):
            lines.append("   %s" % p["reasons"])
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


def push_market():
    """午间大盘综合分析（12:00 推送）：大盘指数 / 自选信号 / 资金动向 / 宏观温度。
    数据驱动：读 review_data / institution_data / macro_data 当日数据，不重复抓取。
    """
    today = time.strftime("%Y-%m-%d")
    d = load("review_data.json")
    if not d:
        print("[market] 无复盘数据")
        return
    gen = d.get("generatedAt", "")
    if not gen.startswith(today):
        print("[market] 数据非当日（%s），跳过推送" % gen)
        return
    lines = [head("午间大盘分析 · %s" % gen[:16]), ""]
    # 1) A股大盘指数（上证/深成/创业板/沪深300）
    a_codes = {"sh000001", "sz399001", "sz399006", "sh000300"}
    a_ix = [x for x in (d.get("indices") or []) if x.get("code") in a_codes]
    if a_ix:
        lines.append("▎大盘指数")
        for ix in a_ix:
            f = ix.get("factors") or {}
            name_map = {"sh000001": "上证", "sz399001": "深成", "sz399006": "创业板", "sh000300": "沪深300"}
            chg = ix.get("change_pct")
            lines.append("%s %s · %s%s" % (
                name_map.get(ix.get("code"), ix.get("name", "")),
                updown(chg, pct_num(chg)) if chg is not None else "--",
                f.get("trend_status", "--"),
                " · %s" % ix.get("signal", "") if ix.get("signal") else ""))
        lines.append("")
    # 2) 市场温度 + 广度 + 过热闸门
    t = d.get("temperature", {})
    st = d.get("stats", {})
    lines.append("▎市场温度：%s（%s）· 广度 %s%%" % (
        t.get("score", "--"), t.get("label", "--"),
        t.get("breadth", "--") if t.get("breadth") is not None else "--"))
    if t.get("market_total"):
        lines.append("全市场 涨%s/跌%s · 自选池 涨%s/跌%s" % (
            t.get("market_up", "--"), t.get("market_down", "--"),
            st.get("up", "--"), st.get("down", "--")))
    rg = d.get("market_regime") or {}
    if rg.get("overheat"):
        lines.append("⚠️ 普涨过热：%s 只买入信号降为观望（上涨占比 %s%%），警惕次日回踩" % (
            rg.get("downgraded_count", "--"),
            round((rg.get("breadth_up_ratio") or 0) * 100)))
    lines.append("")
    # 3) 自选池信号分布 + 强势股
    items = d.get("items") or []
    buy = [x for x in items if x.get("signal_key") in ("strong_buy", "buy")]
    sell_n = sum(1 for x in items if x.get("signal_key") == "sell")
    watch_n = sum(1 for x in items if x.get("signal_key") == "watch")
    strong_n = sum(1 for x in items if x.get("signal_key") == "strong_buy")
    lines.append("▎自选（%s 只）：强烈买入%s · 买入%s · 观望%s · 卖出%s" % (
        len(items), strong_n, len(buy) - strong_n, watch_n, sell_n))
    if buy:
        buy_sorted = sorted(buy, key=lambda x: x.get("score") or 0, reverse=True)[:6]
        lines.append("强势：" + "、".join(
            "%s%s分" % (x.get("name", ""), x.get("score", "")) for x in buy_sorted))
        lines.append("")
    # 4) 资金动向
    fi = load("institution_data.json")
    if fi:
        o = fi.get("overview", {})
        mn = o.get("main_net")
        lines.append("▎资金：主力净流入 %s（板块 流入%s/流出%s）" % (
            updown(mn, money(mn)) if isinstance(mn, (int, float)) else "--",
            o.get("inflow_sectors", "--"), o.get("outflow_sectors", "--")))
        sec = fi.get("sector", {}).get("industry", {})
        for x in (sec.get("inflow") or [])[:2]:
            lines.append("流入 %s：%s · %s" % (x.get("name"),
                ("%.1f亿" % (x.get("main_net", 0) / 1e8)),
                pct_num(x.get("change_pct")) if x.get("change_pct") is not None else ""))
        for x in (sec.get("outflow") or [])[:2]:
            lines.append("流出 %s：%s" % (x.get("name"),
                ("%.1f亿" % (-x.get("main_net", 0) / 1e8))))
        lines.append("")
    # 5) 宏观温度
    md = load("macro_data.json")
    if md:
        mo = md.get("overview", {})
        lines.append("▎宏观舆情：%s · 利好 %s/风险 %s · 净分 %s" % (
            mo.get("temperature_label", "--"), mo.get("bull_count", "--"),
            mo.get("risk_count", "--"), mo.get("net_score", "--")))
    lines.append("")
    lines.append("完整复盘见 review.html · 推荐见 recommend.html")
    send("午间大盘分析 · %s" % gen[:16], "\n".join(lines))


def push_intraday():
    """盘中合并推送（1 条）：回测操作提醒 + 推荐 Top + 资金跟踪（10:00/12:00/14:00）。"""
    parts = []
    # 回测操作提醒（长江电力/中远海控/中证红利）
    bi = load("backtest_index.json")
    if bi:
        stocks = bi.get("stocks", {})
        targets = ["长江电力", "中远海控", "中证红利ETF招商"]
        lines = [head("回测操作提醒"), ""]
        for name in targets:
            s = stocks.get(name)
            if not s:
                lines.append("%s：回测无数据" % name)
                continue
            sm = s.get("summary", {})
            lines.append(name)
            lines.append("年化 %s · 夏普 %s · 回撤 %s" % (
                red(pct(sm.get("annual_return"))) if (sm.get("annual_return") or 0) >= 0 else green(pct(sm.get("annual_return"))),
                sm.get("sharpe"), pct(sm.get("max_drawdown"))))
            lines.append("卡玛 %s · 超额 %s · 交易 %s 笔" % (
                sm.get("calmar"), pct(sm.get("excess_return")), sm.get("trade_count")))
            lines.append("")
        hd = load("holdings_data.json")
        if hd:
            lines.append("持仓网格信号")
            for h in hd.get("items", []):
                g = h.get("grid", {})
                if not g:
                    continue
                dev = g.get("dev")
                pos = g.get("position")
                dev_s = updown(dev, ("%+.1f%%" % (dev * 100)) if dev is not None else "--")
                lines.append("%s：%s · 偏离 %s · 仓位 %s" % (
                    h.get("name"), g.get("action", "--"), dev_s,
                    ("%.0f%%" % (pos * 100)) if pos is not None else "--"))
        parts.append(("回测", "\n".join(lines)))
    # 推荐 Top
    d = load("recommend_data.json")
    picks = (d.get("picks") or [])[:6] if d else []
    if picks:
        lines = [head("盘中推荐 Top%d" % len(picks)), ""]
        for i, p in enumerate(picks, 1):
            chg = pct_num(p.get("change_pct")) if p.get("change_pct") is not None else "--"
            lines.append("%d. %s（%s）%s → %s · %s分" % (
                i, p.get("name"), p.get("sector", "--"),
                p.get("signal", "--"), p.get("rating", "--"), p.get("total_score", "--")))
            lines.append("   现价 %s · %s" % (p.get("price"), updown(p.get("change_pct"), chg)))
            if p.get("reasons"):
                lines.append("   %s" % p["reasons"])
        parts.append(("推荐", "\n".join(lines)))
    # 资金跟踪
    fi = load("institution_data.json")
    if fi:
        o = fi.get("overview", {})
        main_net = o.get("main_net")
        lines = [head("资金跟踪"), ""]
        lines.append("两市主力净流入：%s" % (
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
        lines = [head("每日复盘总结"), ""]
        tmp = t.get("score", "--")
        # 温度：高温红/低温绿（emoji）
        tmp_s = red(str(tmp)) if isinstance(tmp, (int, float)) and tmp >= 60 else (
            green(str(tmp)) if isinstance(tmp, (int, float)) and tmp <= 40 else str(tmp))
        lines.append("市场温度：%s（%s）" % (tmp_s, t.get("label", "--")))
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
            lines.append("最强：%s" % st["strongest"])
        if st.get("weakest"):
            lines.append("最弱：%s" % st["weakest"])
        for ix in (d.get("indices") or [])[:5]:
            if ix.get("name") and ix.get("change_pct") is not None:
                lines.append("%s：%s" % (ix["name"], updown(ix["change_pct"], pct_num(ix["change_pct"]))))
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
    elif mode == "market":
        push_market()
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
