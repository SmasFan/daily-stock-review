# -*- coding: utf-8 -*-
"""生成美股模拟盘网页快照。

页面只读取 data/us-sim.json；账本仍由 broker 独占写入 data/us-sim/state.json。
定时任务先执行一轮 engine.tick，再调用本模块导出，前端无需后端 API。
"""
import argparse
import copy
import json
import os
import time

from . import broker, config, engine, market, report, strategy
from .indicators import closes_of, rsi, sma


def _write_json(path, data):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
    os.replace(tmp, path)


def _finite(value):
    return value if isinstance(value, (int, float)) and abs(value) != float("inf") else None


def build_snapshot(quotes=None, bars_map=None, bench_bars=None):
    """构建纯 JSON 数据；可选注入行情，便于离线测试和失败降级。"""
    st = copy.deepcopy(broker.load())
    symbols = list(dict.fromkeys(config.UNIVERSE + [config.BENCH] + list(st["positions"])))
    errors = []
    if quotes is None:
        try:
            quotes = market.quotes(symbols)
        except Exception as exc:
            quotes = {}
            errors.append("实时行情：%s" % exc)
    if bars_map is None:
        bars_map = {}
        for sym in config.UNIVERSE:
            try:
                bars_map[sym] = market.daily(sym)
            except Exception as exc:
                errors.append("%s 日线：%s" % (sym, exc))
    if bench_bars is None:
        try:
            bench_bars = market.daily(config.BENCH)
        except Exception as exc:
            bench_bars = []
            errors.append("基准日线：%s" % exc)

    positions = []
    market_value = 0.0
    unrealized = 0.0
    for sym, original in st.get("positions", {}).items():
        p = copy.deepcopy(original)
        px = float((quotes.get(sym) or {}).get("price") or p.get("last") or p["avg"])
        p["last"] = round(px, 4)
        p["mv"] = round(px * p["qty"], 4)
        p["upnl"] = round((px - p["avg"]) * p["qty"], 4)
        p["upnl_pct"] = round((px / p["avg"] - 1) * 100, 4) if p["avg"] else 0.0
        p["weight_pct"] = 0.0
        p["quote_name"] = (quotes.get(sym) or {}).get("name") or sym
        positions.append(p)
        market_value += p["mv"]
        unrealized += p["upnl"]
    positions.sort(key=lambda p: p["mv"], reverse=True)

    cash = float(st.get("cash") or 0.0)
    equity = cash + market_value
    capital = float(st.get("capital") or config.CAPITAL)
    for p in positions:
        p["weight_pct"] = round(p["mv"] / equity * 100, 4) if equity else 0.0
    old_stats = report.stats(st)
    curve = list(st.get("equity") or [])
    if not curve or abs(float(curve[-1]["equity"]) - equity) > 1e-8:
        curve.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "equity": round(equity, 4),
                      "cash": round(cash, 4), "position_value": round(market_value, 4),
                      "positions": len(positions)})
    curve = curve[-600:]

    signals = strategy.candidates(config.UNIVERSE, bars_map)
    signal_map = {s["symbol"]: s for s in signals}
    pool = []
    for sym in config.UNIVERSE:
        bars = bars_map.get(sym) or []
        closes = closes_of(bars)
        ma = sma(closes, config.SMA_SLOW) if closes else None
        rr = rsi(closes, config.RSI_N) if closes else None
        hi = max(closes[-config.BREAKOUT_N - 1:-1]) if len(closes) > config.BREAKOUT_N else None
        sig = signal_map.get(sym)
        q = quotes.get(sym) or {}
        price = q.get("price") or (closes[-1] if closes else None)
        pool.append({
            "symbol": sym,
            "name": q.get("name") or sym,
            "price": price,
            "chg_pct": q.get("chg_pct"),
            "quote_ts": q.get("ts"),
            "source": q.get("src"),
            "close": closes[-1] if closes else None,
            "sma50": round(ma, 4) if ma else None,
            "rsi14": round(rr, 2) if rr is not None else None,
            "breakout45": round(hi, 4) if hi else None,
            "breakout_distance_pct": round((closes[-1] / hi - 1) * 100, 3) if closes and hi else None,
            "above_sma50": bool(ma and closes and closes[-1] > ma),
            "signal": sig,
        })

    et = market.now_et()
    bench_closes = closes_of(bench_bars or [])
    bench_ma = sma(bench_closes, config.SMA_SLOW) if bench_closes else None
    bench_price = (quotes.get(config.BENCH) or {}).get("price") or (bench_closes[-1] if bench_closes else None)
    account = {
        "capital": capital,
        "equity": round(equity, 4),
        "cash": round(cash, 4),
        "market_value": round(market_value, 4),
        "ret_pct": round((equity / capital - 1) * 100, 4) if capital else 0.0,
        "unrealized": round(unrealized, 4),
        "realized": float(st.get("realized") or 0.0),
        "position_count": len(positions),
        "max_positions": config.MAX_POS,
        "exposure_pct": round(market_value / equity * 100, 4) if equity else 0.0,
        "trade_count": len(st.get("trades") or []),
        "closed_count": old_stats["closed"],
        "win_rate": round(old_stats["win_rate"], 2),
        "profit_factor": _finite(old_stats["profit_factor"]),
        "max_dd": round(old_stats["max_dd"], 4),
    }
    backtest = None
    if os.path.exists(config.BACKTEST_FILE):
        try:
            with open(config.BACKTEST_FILE, encoding="utf-8") as f:
                backtest = json.load(f)
        except Exception as exc:
            errors.append("回测摘要：%s" % exc)
    return {
        "meta": {
            "title": "美股模拟盘",
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": "1.0.0",
            "mode": "paper",
            "currency": "USD",
            "fractional": True,
        },
        "market": {
            "is_open": market.market_open(et),
            "et_time": et.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "benchmark": config.BENCH,
            "benchmark_price": bench_price,
            "benchmark_sma50": round(bench_ma, 4) if bench_ma else None,
            "regime_ok": strategy.regime_ok(bench_bars),
        },
        "account": account,
        "positions": positions,
        "candidates": signals,
        "pool": pool,
        "trades": list(reversed((st.get("trades") or [])[-100:])),
        "equity_curve": curve,
        "backtest": backtest,
        "strategy": {
            "name": "45日突破 + 趋势过滤 + ATR风控",
            "breakout_days": config.BREAKOUT_N,
            "sma_days": config.SMA_SLOW,
            "rsi_max": config.RSI_MAX,
            "stop_atr": config.STOP_ATR,
            "trail_r": config.TRAIL_R,
            "trail_atr": config.TRAIL_ATR,
            "risk_pct": config.RISK_PCT * 100,
            "max_weight_pct": config.MAX_WEIGHT * 100,
            "slippage_bps": config.SLIP_BPS,
        },
        "errors": errors,
    }


def export_snapshot(**kwargs):
    snapshot = build_snapshot(**kwargs)
    _write_json(config.WEB_JSON, snapshot)
    return snapshot


def run_once(force=False):
    """先执行交易轮次，再导出网页；行情失败也尽量保留可读页面。"""
    tick_error = None
    try:
        engine.tick(force_trade=True if force else None, verbose=True)
        report.write_report()
    except Exception as exc:
        tick_error = str(exc)
    snapshot = export_snapshot()
    if tick_error:
        snapshot["errors"].insert(0, "本轮更新：" + tick_error)
        _write_json(config.WEB_JSON, snapshot)
    print("[progress] 1/1 美股模拟盘快照已更新")
    print("权益 $%.2f · 持仓 %d · %s" % (
        snapshot["account"]["equity"], snapshot["account"]["position_count"],
        "交易中" if snapshot["market"]["is_open"] else "休市"))
    return snapshot


def main():
    parser = argparse.ArgumentParser(description="更新美股模拟盘网页数据")
    parser.add_argument("--export-only", action="store_true", help="只导出，不执行交易轮次")
    parser.add_argument("--force", action="store_true", help="休市时也允许模拟成交（仅测试）")
    args = parser.parse_args()
    if args.export_only:
        export_snapshot()
        print(config.WEB_JSON)
    else:
        run_once(force=args.force)


if __name__ == "__main__":
    main()
