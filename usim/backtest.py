# -*- coding: utf-8 -*-
"""回测：东财日线，信号次日开盘成交，与实盘共用 strategy 规则。"""
import time

from . import config, market, strategy


def _apply(overrides):
    for k, v in (overrides or {}).items():
        setattr(config, k, v)


def run(days=320, verbose=True, overrides=None):
    _apply(overrides)
    t0 = time.time()
    bars_map = {}
    for s in list(config.UNIVERSE) + [config.BENCH]:
        try:
            bars_map[s] = market.daily(s, limit=days)
        except Exception as e:
            print("取数失败 %s: %s" % (s, str(e)[:60]))
    syms = [s for s in config.UNIVERSE if bars_map.get(s)]
    bench = bars_map.get(config.BENCH) or []
    bdates = [b["date"] for b in bench]
    b_idx = {d: i for i, d in enumerate(bdates)}

    dates = sorted({b["date"] for s in syms for b in bars_map[s]})
    idx = {s: {b["date"]: i for i, b in enumerate(bars_map[s])} for s in syms}
    warm = config.SMA_SLOW + 10

    cash, pos, curve, trades = config.CAPITAL, {}, [], []
    total = max(1, len(dates) - warm - 1)

    for n, d in enumerate(dates[warm:-1]):
        if verbose and n % 25 == 0:
            print("[progress] %d/%d %s" % (n, total, d), flush=True)
        close = {s: bars_map[s][idx[s][d]]["close"] for s in syms if d in idx[s]}

        # 持仓管理（与实盘同一套 manage）
        for s in list(pos):
            px = close.get(s)
            if not px:
                continue
            p = pos[s]
            p["hold"] += 1
            reason, frac = strategy.manage(p, px)
            if reason and p["hold"] >= 1:
                qty = p["qty"] if frac >= 1 else round(p["qty"] * frac, config.QTY_DECIMALS)
                cash += qty * px
                p["qty"] -= qty
                trades.append([d, s, "sell", qty, px, (px - p["avg"]) * qty, reason])
                if frac >= 1:
                    pos.pop(s)

        # 开仓
        bi = b_idx.get(d)
        bench_bars = bench[:bi + 1] if bi is not None else []
        if strategy.regime_ok(bench_bars) and len(pos) < config.MAX_POS:
            equity = cash + sum(p["qty"] * (close.get(s) or p["avg"]) for s, p in pos.items())
            cands = []
            for s in syms:
                i = idx[s].get(d)
                if s in pos or i is None or i < warm or i + 1 >= len(bars_map[s]):
                    continue
                sig = strategy.evaluate(s, bars_map[s][:i + 1])
                if sig:
                    cands.append(sig)
            cands.sort(key=lambda x: x["score"], reverse=True)
            for sig in cands:
                if len(pos) >= config.MAX_POS:
                    break
                s = sig["symbol"]
                nxt = bars_map[s][idx[s][d] + 1]
                px = nxt["open"] or nxt["close"]
                sig2 = dict(sig, price=px, stop=px - config.STOP_ATR * sig["atr"])
                qty = strategy.size(equity, cash, sig2)
                if qty <= 0 or qty * px > cash:
                    continue
                cash -= qty * px
                pos[s] = {"qty": qty, "avg": px, "stop": sig2["stop"], "stop0": sig2["stop"],
                          "peak": px, "atr": sig["atr"], "took_half": False, "hold": 0}
                trades.append([nxt["date"], s, "buy", qty, px, None, "突破"])

        curve.append((d, cash + sum(p["qty"] * (close.get(s) or p["avg"]) for s, p in pos.items())))

    eq = curve[-1][1] if curve else config.CAPITAL
    peak, mdd = 0.0, 0.0
    for _, v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    sells = [t for t in trades if t[2] == "sell"]
    wins = [t for t in sells if t[5] > 0]
    gw = sum(t[5] for t in wins)
    gl = -sum(t[5] for t in sells if t[5] <= 0)
    spy = (bench[-1]["close"] / bench[warm]["close"] - 1) * 100 if len(bench) > warm else 0.0
    res = {
        "start": curve[0][0] if curve else "", "end": curve[-1][0] if curve else "",
        "days": len(curve), "final": eq,
        "ret_pct": (eq / config.CAPITAL - 1) * 100, "spy_pct": spy,
        "max_dd": mdd * 100, "trades": len(trades), "closed": len(sells),
        "win_rate": (len(wins) / len(sells) * 100) if sells else 0.0,
        "pf": (gw / gl) if gl > 0 else 0.0,
        "elapsed": time.time() - t0, "curve": curve, "trades": trades,
    }
    if verbose:
        from collections import Counter
        print("\n" + "=" * 58)
        print(" 回测 %s → %s  (%d 个交易日, %.1fs)" % (res["start"], res["end"], res["days"], res["elapsed"]))
        print("=" * 58)
        print(" 策略收益  %+7.2f%%   $%.2f → $%.2f" % (res["ret_pct"], config.CAPITAL, eq))
        print(" SPY 基准  %+7.2f%%   超额 %+.2f%%" % (spy, res["ret_pct"] - spy))
        print(" 最大回撤  %7.2f%%" % res["max_dd"])
        print(" 交易 %d 笔（平仓 %d），胜率 %.0f%%，盈亏比 %.2f" % (
            len(trades), len(sells), res["win_rate"], res["pf"]))
        print(" 离场原因  %s" % dict(Counter(t[6].split(" ")[0] for t in sells)))
    return res
