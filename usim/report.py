# -*- coding: utf-8 -*-
"""报告：状态、绩效统计、Markdown 日报。"""
import time

from . import broker, config, market

SPARK = "▁▂▃▄▅▆▇█"


def equity_now(st):
    for s in reversed(st.get("equity") or []):
        return float(s["equity"])
    return float(st.get("capital") or config.CAPITAL)


def stats(st):
    cap = float(st.get("capital") or config.CAPITAL)
    eq = equity_now(st)
    curve = [float(x["equity"]) for x in st.get("equity") or []]
    peak, mdd = 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        if peak:
            mdd = min(mdd, v / peak - 1.0)
    sells = [t for t in st.get("trades") or [] if t["side"] == "sell"]
    wins = [t for t in sells if (t.get("pnl") or 0) > 0]
    losses = [t for t in sells if (t.get("pnl") or 0) <= 0]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = -sum(t["pnl"] for t in losses)
    pf = (gross_w / gross_l) if gross_l > 0 else (float("inf") if gross_w > 0 else 0.0)
    return {
        "capital": cap,
        "equity": eq,
        "ret_pct": (eq / cap - 1) * 100 if cap else 0.0,
        "realized": float(st.get("realized") or 0.0),
        "cash": float(st.get("cash") or 0.0),
        "positions": len(st.get("positions") or {}),
        "trades": len(st.get("trades") or []),
        "closed": len(sells),
        "win_rate": (len(wins) / len(sells) * 100) if sells else 0.0,
        "avg_win": (gross_w / len(wins)) if wins else 0.0,
        "avg_loss": (gross_l / len(losses)) if losses else 0.0,
        "profit_factor": pf,
        "max_dd": mdd * 100,
        "curve": curve,
    }


def sparkline(vals, width=48):
    if not vals:
        return ""
    if len(vals) > width:
        step = len(vals) / float(width)
        vals = [vals[int(i * step)] for i in range(width)]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return SPARK[3] * len(vals)
    return "".join(SPARK[int((v - lo) / (hi - lo) * (len(SPARK) - 1))] for v in vals)


def status_text(st=None):
    st = st or broker.load()
    s = stats(st)
    lines = []
    lines.append("=" * 62)
    lines.append(" 美股模拟盘 · 起始 $%.2f" % s["capital"])
    lines.append("=" * 62)
    lines.append(" 权益      $%8.2f   (%+.2f%%)" % (s["equity"], s["ret_pct"]))
    lines.append(" 现金      $%8.2f   持仓 %d 只 / 上限 %d" % (s["cash"], s["positions"], config.MAX_POS))
    lines.append(" 已实现盈亏 $%7.2f   胜率 %.0f%% (%d 笔平仓)" % (s["realized"], s["win_rate"], s["closed"]))
    if s["curve"]:
        lines.append(" 权益曲线  %s" % sparkline(s["curve"]))
    if s["positions"]:
        lines.append("-" * 62)
        lines.append(" %-6s %10s %10s %10s %8s %s" % ("代码", "股数", "成本", "现价", "盈亏%", "止损"))
        for sym, p in st["positions"].items():
            lines.append(" %-6s %10.6f %10.2f %10.2f %+7.2f%% %8.2f" % (
                sym, p["qty"], p["avg"], p.get("last") or p["avg"],
                p.get("upnl_pct") or 0.0, p.get("stop") or 0.0))
    return "\n".join(lines)


def write_report(st=None):
    st = st or broker.load()
    s = stats(st)
    et = market.now_et()
    md = []
    md.append("# 美股模拟盘日报 %s" % time.strftime("%Y-%m-%d %H:%M"))
    md.append("")
    md.append("> 美东时间 %s · 交易时段 %s" % (et.strftime("%Y-%m-%d %H:%M %Z"),
                                           "开市" if market.market_open(et) else "休市"))
    md.append("")
    md.append("| 指标 | 数值 |")
    md.append("|---|---|")
    md.append("| 起始资金 | $%.2f |" % s["capital"])
    md.append("| 当前权益 | **$%.2f** (%+.2f%%) |" % (s["equity"], s["ret_pct"]))
    md.append("| 现金 | $%.2f |" % s["cash"])
    md.append("| 已实现盈亏 | $%.2f |" % s["realized"])
    md.append("| 胜率 | %.0f%% (%d 笔) |" % (s["win_rate"], s["closed"]))
    md.append("| 盈亏比 | %.2f (均盈 $%.2f / 均亏 $%.2f) |" % (
        s["profit_factor"] if s["profit_factor"] != float("inf") else 99.0, s["avg_win"], s["avg_loss"]))
    md.append("| 最大回撤 | %.2f%% |" % s["max_dd"])
    md.append("")
    if s["curve"]:
        md.append("## 权益曲线")
        md.append("")
        md.append("```")
        md.append(sparkline(s["curve"], 60))
        md.append("```")
        md.append("")
    if st["positions"]:
        md.append("## 当前持仓")
        md.append("")
        md.append("| 代码 | 股数 | 成本 | 现价 | 浮盈 | 止损 | 建仓时间 |")
        md.append("|---|---|---|---|---|---|---|")
        for sym, p in st["positions"].items():
            md.append("| %s | %.6f | %.2f | %.2f | %+.2f%% | %.2f | %s |" % (
                sym, p["qty"], p["avg"], p.get("last") or p["avg"],
                p.get("upnl_pct") or 0.0, p.get("stop") or 0.0, p.get("opened", "")))
        md.append("")
    recent = [t for t in st["trades"] if t["side"] == "sell"][-10:]
    if recent:
        md.append("## 最近平仓")
        md.append("")
        md.append("| 时间 | 代码 | 股数 | 价格 | 盈亏 | 原因 |")
        md.append("|---|---|---|---|---|---|")
        for t in reversed(recent):
            md.append("| %s | %s | %.6f | %.2f | %+.3f | %s |" % (
                t["ts"], t["symbol"], t["qty"], t["price"], t.get("pnl") or 0, t["reason"]))
        md.append("")
    path = config.DATA_DIR + "/report.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    return path
