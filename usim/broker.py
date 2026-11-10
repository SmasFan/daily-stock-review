# -*- coding: utf-8 -*-
"""模拟券商：碎股、零佣金、滑点、JSON 持久化。"""
import json
import os
import time

from . import config


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load():
    if not os.path.exists(config.STATE_FILE):
        return new_state()
    with open(config.STATE_FILE, encoding="utf-8") as f:
        st = json.load(f)
    st.setdefault("positions", {})
    st.setdefault("trades", [])
    st.setdefault("equity", [])
    return st


def new_state():
    return {
        "created": _now(),
        "capital": config.CAPITAL,
        "cash": config.CAPITAL,
        "positions": {},
        "trades": [],
        "equity": [],
        "realized": 0.0,
        "wins": 0,
        "losses": 0,
    }


def save(st):
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
    tmp = config.STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, config.STATE_FILE)


def _fill_price(price, side):
    """滑点：买入吃高、卖出吃低。"""
    s = config.SLIP_BPS / 10000.0
    return price * (1 + s) if side == "buy" else price * (1 - s)


def buy(st, symbol, qty, price, reason="", atr=None):
    qty = round(float(qty), config.QTY_DECIMALS)
    px = _fill_price(price, "buy")
    notional = qty * px
    if notional < config.MIN_NOTIONAL or notional > st["cash"] + 1e-9:
        return None
    fee = notional * config.FEE_BPS / 10000.0
    st["cash"] -= notional + fee
    pos = st["positions"].get(symbol)
    if pos:
        total = pos["qty"] + qty
        pos["avg"] = (pos["avg"] * pos["qty"] + px * qty) / total
        pos["qty"] = round(total, config.QTY_DECIMALS)
        pos["stop"] = max(pos["stop"], px - config.STOP_ATR * (atr or pos.get("atr") or 0) * 0 or pos["stop"])
    else:
        stop = px - config.STOP_ATR * atr if atr else px * 0.92
        st["positions"][symbol] = {
            "qty": qty, "avg": px, "stop": stop, "stop0": stop,
            "peak": px, "atr": atr, "opened": _now(),
        }
    st["trades"].append({
        "ts": _now(), "symbol": symbol, "side": "buy", "qty": qty,
        "price": round(px, 4), "notional": round(notional, 2),
        "fee": round(fee, 4), "reason": reason, "pnl": None,
    })
    return px


def sell(st, symbol, qty, price, reason=""):
    pos = st["positions"].get(symbol)
    if not pos:
        return None
    qty = round(min(float(qty), pos["qty"]), config.QTY_DECIMALS)
    if qty <= 0:
        return None
    px = _fill_price(price, "sell")
    notional = qty * px
    fee = notional * config.FEE_BPS / 10000.0
    pnl = (px - pos["avg"]) * qty - fee
    st["cash"] += notional - fee
    st["realized"] += pnl
    pos["qty"] = round(pos["qty"] - qty, config.QTY_DECIMALS)
    if pos["qty"] <= 1e-9:
        st["positions"].pop(symbol, None)
    if pnl >= 0:
        st["wins"] += 1
    else:
        st["losses"] += 1
    st["trades"].append({
        "ts": _now(), "symbol": symbol, "side": "sell", "qty": qty,
        "price": round(px, 4), "notional": round(notional, 2),
        "fee": round(fee, 4), "reason": reason, "pnl": round(pnl, 4),
    })
    return px


def mark(st, prices):
    """用最新价更新持仓市值与权益。"""
    pv = 0.0
    for sym, pos in st["positions"].items():
        px = prices.get(sym) or pos["avg"]
        pv += pos["qty"] * px
        pos["peak"] = max(pos.get("peak") or px, px)
        pos["last"] = px
        pos["mv"] = round(pos["qty"] * px, 2)
        pos["upnl"] = round((px - pos["avg"]) * pos["qty"], 2)
        pos["upnl_pct"] = round((px / pos["avg"] - 1) * 100, 2) if pos["avg"] else 0.0
    equity = st["cash"] + pv
    snap = {"ts": _now(), "equity": round(equity, 4), "cash": round(st["cash"], 4),
            "position_value": round(pv, 4), "positions": len(st["positions"])}
    st["equity"].append(snap)
    st["equity"] = st["equity"][-2000:]
    return equity, pv
