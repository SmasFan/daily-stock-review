# -*- coding: utf-8 -*-
"""引擎：一轮 = 取价 → 管持仓 → 找候选 → 开仓 → 记快照。规则与回测共用 strategy。"""
import time

from . import broker, config, market, strategy


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(config.LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _equity(st, prices):
    return st["cash"] + sum(p["qty"] * (prices.get(s) or p["avg"]) for s, p in st["positions"].items())


def tick(force_trade=None, verbose=True):
    st = broker.load()
    syms = list(dict.fromkeys(list(st["positions"].keys()) + config.UNIVERSE + [config.BENCH]))
    try:
        q = market.quotes(syms)
    except Exception as e:
        log("报价失败: %s" % str(e)[:100])
        return None
    prices = {k: v["price"] for k, v in q.items()}
    if verbose:
        miss = [s for s in syms if s not in prices]
        if miss:
            log("缺报价: %s" % ",".join(miss))

    is_open = market.market_open() if force_trade is None else force_trade
    acted = []

    # 1) 持仓管理
    for s in list(st["positions"]):
        px = prices.get(s)
        if not px:
            continue
        pos = st["positions"][s]
        reason, frac = strategy.manage(pos, px)
        if reason:
            qty = pos["qty"] if frac >= 1 else round(pos["qty"] * frac, config.QTY_DECIMALS)
            if broker.sell(st, s, qty, px, reason):
                acted.append("SELL %-5s %10.6f @ %-8.2f %s" % (s, qty, px, reason))

    # 2) 开仓
    bench = market.daily(config.BENCH) if is_open else []
    equity = _equity(st, prices)
    if is_open and len(st["positions"]) < config.MAX_POS and strategy.regime_ok(bench):
        bars_map = {}
        for s in config.UNIVERSE:
            if s in st["positions"]:
                continue
            try:
                bars_map[s] = market.daily(s)
            except Exception as e:
                if verbose:
                    log("日线失败 %s: %s" % (s, str(e)[:60]))
        for sig in strategy.candidates(list(bars_map), bars_map):
            if len(st["positions"]) >= config.MAX_POS:
                break
            px = prices.get(sig["symbol"]) or sig["price"]
            sig2 = dict(sig, price=px, stop=px - config.STOP_ATR * sig["atr"])
            qty = strategy.size(equity, st["cash"], sig2)
            if qty <= 0:
                continue
            filled = broker.buy(st, sig["symbol"], qty, px,
                                "突破前%d日高 收%.2f" % (config.BREAKOUT_N, sig["breakout"]),
                                atr=sig["atr"])
            if filled:
                acted.append("BUY  %-5s %10.6f @ %-8.2f 止损 %.2f (rsi %.0f)" % (
                    sig["symbol"], qty, filled, sig2["stop"], sig["rsi"]))
                equity = _equity(st, prices)
    elif is_open and not strategy.regime_ok(bench) and verbose:
        log("大盘环境不佳（SPY 在 50 日线下），本轮不开新仓")

    eq, pv = broker.mark(st, prices)
    broker.save(st)
    if verbose:
        log("交易时段: %s | 权益 %.2f (现金 %.2f 持仓 %.2f) | 持仓 %d/%d" % (
            "开市" if is_open else "休市(只观察)", eq, st["cash"], pv,
            len(st["positions"]), config.MAX_POS))
        for a in acted:
            log(a)
        if not acted and is_open:
            log("本轮无动作")
    return {"equity": eq, "acted": acted, "positions": len(st["positions"])}
