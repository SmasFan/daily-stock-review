"""
网格均值回归回测引擎（移植自 dividend_grid_strategy，2026-08 优化版）。

策略口径：均衡偏低均值线 + 不对称网格 + 半永久锁仓 + ATR动态步长 + ADX趋势闸门。
- 均值线：近 3 年(750 交易日)滚动分位锚。有估值因子(PE-TTM/PB/ROE/股息率)
  时用「加权几何平均公允价格线」；无估值数据(ETF/个股)时退化为
  「收盘价相对 750 日价格均线的偏离度」。
- 仓位规则：pos = base_pos - slope*dev。上涨侧斜率 = sell_per_step/grid_step，
  下跌侧斜率 = buy_per_step/grid_step。默认涨抛 1%、跌买 1.04%。
- 动态网格步长（dynamic_step=True）：步长 = atr_mult × ATR(14)/收盘价
  （取近 250 日中位数，夹在 [min_step, max_step]），替代固定 0.5%，
  避免高波动标的上过度交易。
- ADX 趋势闸门（adx_gate，实验性，默认关闭）：开启时 ADX > adx_trend
  视为趋势市，冻结调仓（上行不减仓、下行不加仓）。回测显示该闸门在
  低波 ETF 上过度冻结拖累收益，默认关闭，留作参数实验。
- 半永久锁仓（use_lock=True，已真正生效）：dev <= dev_lock 后的加仓累入
  锁仓下限，反弹时仓位不低于该下限（不回吐）；dev >= dev_clear 解锁。
- 交易触发：dev 每跨越一个 grid_step 网格边界才调仓。
- 收益 = 价格收益 + 日股息(dy_daily，真实分红序列映射除息日，无则 0)，现金无利息。
- 成本：单边 cost_rate + slippage_rate（P2 滑点建模，默认各 5bps），
  调仓按 |目标仓-当前仓|*(cost_rate+slippage_rate) 扣除。
- 基准：满仓买入持有（价格收益 + 股息），归一化对比。
- A/B 对照：GridParams(dynamic_step=False, adx_gate=False, use_lock=False)
  即回退为旧版固定 0.5% 步长网格。
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import statistics
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict, replace
from typing import Dict, List, Optional, Tuple

from . import indicators as ind
from . import dividends as divs

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# 估值缓存 TTL（按文件 mtime）：成功结果 24h，失败(空)结果 6h——
# 旧版本无 TTL 且把网络失败永久缓存为空数组，导致估值锚静默退化为价格锚。
VALUE_CACHE_OK_HOURS = 24.0
VALUE_CACHE_EMPTY_HOURS = 6.0

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      "Referer": "https://data.eastmoney.com/"}


# ---------------- 参数 ----------------

@dataclass
class GridParams:
    """网格与仓位参数（对齐 dividend_grid_strategy/config.yaml，含 2026-08 优化项）。"""
    base_pos: float = 0.70        # 均衡仓位
    grid_step: float = 0.005      # 网格步长（dynamic_step=False 时的固定步长）
    sell_per_step: float = 0.010  # 每涨 0.5% 抛 1%
    buy_per_step: float = 0.0104  # 每跌 0.5% 买 1.04%
    dev_lock: float = -0.05       # 偏离<=-5% 后的加仓进入半永久锁仓
    dev_clear: float = 0.05       # 偏离>=+5% 解锁（超涨解锁）
    min_pos: float = 0.10
    max_pos: float = 1.00
    # ---- 2026-08 优化：ATR 动态步长 / ADX 趋势闸门 / 真锁仓 ----
    dynamic_step: bool = True     # 步长随 ATR 自适应（替代固定 0.5%）
    atr_mult: float = 1.2         # 步长 = atr_mult × ATR(14)/收盘价（近250日中位数）
    min_step: float = 0.01        # 步长下限 1%
    max_step: float = 0.03        # 步长上限 3%
    adx_gate: bool = False        # ADX 趋势闸门（实验性开关，默认关闭：回测显示
                                  # 在低波 ETF 上过度冻结反而拖累收益）
    adx_trend: float = 25.0       # ADX 阈值：之上为趋势市，冻结网格调仓
    use_lock: bool = True         # 半永久锁仓真正生效（反弹不回吐）

    @property
    def slope_up(self) -> float:
        return self.sell_per_step / self.grid_step

    @property
    def slope_down(self) -> float:
        return self.buy_per_step / self.grid_step


@dataclass
class AnchorParams:
    """均值线参数。"""
    lookback_days: int = 750
    fair_pct: float = 0.40
    min_periods: int = 500
    weights: Dict[str, float] = field(
        default_factory=lambda: {"pe": 0.5, "dy": 0.3, "roe": 0.2})


@dataclass
class BacktestConfig:
    cost_rate: float = 0.0005      # 单边佣金+印花税/过户费（按成交额比例）
    slippage_rate: float = 0.0005  # 滑点（P2：默认 5bps 单边，按成交额比例）
    cash_rate: float = 0.0
    rf: float = 0.0


# ---------------- 数据：个股估值历史（东财 RPT_VALUEANALYSIS_DET） ----------------

def _get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def _read_value_cache(cache: str):
    """读估值缓存。返回 (状态, 数据)：状态 fresh/stale/miss。按文件 mtime 判活：
    非空结果 24h 有效，空结果（历史拉取失败）仅 6h 有效，到期重试。"""
    if not os.path.exists(cache):
        return "miss", None
    try:
        with open(cache, "r", encoding="utf-8") as f:
            rows = json.load(f)
    except Exception:
        return "miss", None
    if not isinstance(rows, list):
        return "miss", None
    age_h = (time.time() - os.path.getmtime(cache)) / 3600.0
    ttl = VALUE_CACHE_OK_HOURS if rows else VALUE_CACHE_EMPTY_HOURS
    return ("fresh" if age_h < ttl else "stale"), rows


def fetch_stock_value_em(code: str) -> Optional[List[Dict]]:
    """东财每日估值分析历史（PE-TTM/PB/股息率等），返回升序列表或 None。

    数据源等价 akshare.stock_value_em，但仅用标准库实现，避免 akshare 依赖。
    缓存带 TTL：空结果 6h 后自动重试，避免一次网络失败导致估值锚永久退化。
    """
    cache = os.path.join(CACHE_DIR, f"value_{code}.json")
    state, cached = _read_value_cache(cache)
    if state == "fresh":
        return cached or None
    params = {
        "sortColumns": "TRADE_DATE",
        "sortTypes": "-1",
        "pageSize": "5000",
        "pageNumber": "1",
        "reportName": "RPT_VALUEANALYSIS_DET",
        "columns": "ALL",
        "quoteColumns": "",
        "source": "WEB",
        "client": "WEB",
        "filter": f'(SECURITY_CODE="{code}")',
    }
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params)
    try:
        # 东财接口在本机网络下常不可达，缩短超时并快速退化，避免拖慢整体流程
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(8)
        try:
            data = _get_json(url, timeout=8)
        finally:
            socket.setdefaulttimeout(old_timeout)
        rows = (data.get("result") or {}).get("data") or []
        if not rows:
            # 缓存空结果，避免同一代码反复重试拖慢流程
            try:
                with open(cache, "w", encoding="utf-8") as f:
                    json.dump([], f)
            except Exception:
                pass
            return None
        # 数据源为降序（最新在前），转为升序
        rows = sorted(rows, key=lambda r: r.get("TRADE_DATE") or "")
        try:
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
        except Exception:
            pass
        return rows
    except Exception:
        # 网络失败：缓存空结果（6h TTL，避免反复重试拖慢流程）；
        # 若手头有过期但非空的旧数据，优先回退使用旧数据
        try:
            with open(cache, "w", encoding="utf-8") as f:
                json.dump([], f)
        except Exception:
            pass
        if state == "stale" and cached:
            return cached
        return None


def _build_stock_panel(code: str, dates, closes, highs, lows, volumes) -> Optional[dict]:
    """把腾讯日K + 东财估值合并成回测面板。

    dates/opens/closes/... 与 fetch_daily_kline 返回一致（升序，前复权）。
    返回 {date, close, pe_ttm, pb, roe, dy_daily} 对齐字典，估值列可能缺失。

    前复权早期（高分红股除权）可能出现负价/畸形跳变，回测前截断到
    最后一个价格连续有效区段，避免污染均值线窗口。
    """
    n = len(closes)
    if n < 30:
        return None

    # 定位最后一个价格>0 且相邻比值合理(0.2~5)的连续有效段起点
    start = 0
    for i in range(n - 1, -1, -1):
        if closes[i] > 0 and closes[i - 1] > 0:
            r = closes[i] / closes[i - 1]
            if 0.2 <= r <= 5.0:
                continue
        start = i + 1
        break
    if start == n:
        start = 0
    # 至少保留最近 300 根
    start = max(start, n - 3200)

    dates = dates[start:]
    closes = closes[start:]
    highs = highs[start:]
    lows = lows[start:]
    volumes = volumes[start:]
    n = len(closes)
    px = {d: c for d, c in zip(dates, closes)}
    pe_hist, pb_hist = [], []
    vrows = fetch_stock_value_em(code)
    if vrows:
        pe_map, pb_map = {}, {}
        for r in vrows:
            d = (r.get("TRADE_DATE") or "")[:10]
            pe = r.get("PE_TTM")
            pb = r.get("PB_MRQ")
            if pe not in (None, "-", "", 0):
                pe_map[d] = float(pe)
            if pb not in (None, "-", "", 0):
                pb_map[d] = float(pb)
        for d in dates:
            pe_hist.append(pe_map.get(d))
            pb_hist.append(pb_map.get(d))
    else:
        pe_hist = [None] * n
        pb_hist = [None] * n

    # 缺失估值前向填充
    last_pe = last_pb = None
    pe_f, pb_f = [], []
    for i in range(n):
        if pe_hist[i] is not None:
            last_pe = pe_hist[i]
        if pb_hist[i] is not None:
            last_pb = pb_hist[i]
        pe_f.append(last_pe)
        pb_f.append(last_pb)

    roe = [pb / pe if (pe and pb) else None for pe, pb in zip(pe_f, pb_f)]

    # 日股息（P1 优化）：真实分红序列映射到除息日单日股息率（每股分红/昨收），
    # 替代旧版恒为 0 的占位；无分红记录（ETF/次新股）时保持 0。
    try:
        dy_daily = divs.build_dy_daily(dates, closes, code)
    except Exception:
        dy_daily = [0.0] * n

    return {
        "date": dates,
        "close": closes,
        "high": highs,
        "low": lows,
        "volume": volumes,
        "pe_ttm": pe_f,
        "pb": pb_f,
        "roe": roe,
        "dy_daily": dy_daily,
    }


def build_panel(name: str, code: str, dates, opens, closes, highs, lows, volumes) -> Optional[dict]:
    """外部入口：用腾讯日K 构建回测面板。"""
    return _build_stock_panel(code, dates, closes, highs, lows, volumes)


# ---------------- 均值线与仓位规则 ----------------

def _rolling_quantile(values: List[Optional[float]], window: int, q: float,
                      min_periods: int) -> List[Optional[float]]:
    """滚动分位。返回与输入等长列表（前 min_periods-1 个为 None）。"""
    out: List[Optional[float]] = []
    valid = []
    for i, v in enumerate(values):
        if v is not None and v == v:  # 排除 NaN
            valid.append(v)
        if i >= window:
            old = values[i - window]
            if old is not None and old == old:
                valid.remove(old)
        if len(valid) >= min_periods:
            s = sorted(valid)
            idx = q * (len(s) - 1)
            lo = int(math.floor(idx))
            hi = int(math.ceil(idx))
            if lo == hi:
                out.append(s[lo])
            else:
                frac = idx - lo
                out.append(s[lo] * (1 - frac) + s[hi] * frac)
        else:
            out.append(None)
    return out


def compute_anchor(panel: dict, ap: AnchorParams) -> dict:
    """在面板上计算均值线与偏离度，返回带 anchor/dev/dev_pe/dev_pb/dev_roe 的副本。"""
    out = {k: list(v) for k, v in panel.items()}
    w, mp = ap.lookback_days, ap.min_periods
    n = len(out["close"])
    close = out["close"]

    has_pe = "pe_ttm" in out and out["pe_ttm"] and sum(1 for x in out["pe_ttm"] if x is not None) > mp
    has_pb = "pb" in out and out["pb"] and sum(1 for x in out["pb"] if x is not None) > mp
    has_dy = "dy_ttm" in out and out["dy_ttm"] and sum(1 for x in out["dy_ttm"] if x is not None) > mp
    has_roe = "roe" in out and out["roe"] and sum(1 for x in out["roe"] if x is not None) > mp

    anchor_pe = anchor_pb = anchor_dy = anchor_roe = [None] * n
    dev_pe = dev_pb = dev_dy = dev_roe = [None] * n

    if has_pe:
        fair_pe = _rolling_quantile(out["pe_ttm"], w, ap.fair_pct, mp)
        anchor_pe = [close[i] * fair_pe[i] / out["pe_ttm"][i] if (fair_pe[i] and out["pe_ttm"][i]) else None
                     for i in range(n)]
    if has_pb:
        fair_pb = _rolling_quantile(out["pb"], w, 1 - ap.fair_pct, mp)
        anchor_pb = [close[i] * out["pb"][i] / fair_pb[i] if (fair_pb[i] and out["pb"][i]) else None
                     for i in range(n)]
    if has_dy:
        fair_dy = _rolling_quantile(out["dy_ttm"], w, 1 - ap.fair_pct, mp)
        anchor_dy = [close[i] * out["dy_ttm"][i] / fair_dy[i] if (fair_dy[i] and out["dy_ttm"][i]) else None
                     for i in range(n)]
    if has_roe:
        fair_roe = _rolling_quantile(out["roe"], w, 1 - ap.fair_pct, mp)
        anchor_roe = [close[i] * out["roe"][i] / fair_roe[i] if (fair_roe[i] and out["roe"][i]) else None
                      for i in range(n)]

    # 加权几何平均复合均值线
    anchors = {}
    col_w = {}
    for key, wgt in ap.weights.items():
        arr = {"pe": anchor_pe, "dy": anchor_dy, "roe": anchor_roe}.get(key)
        if arr is not None and any(x is not None for x in arr):
            anchors[f"anchor_{key}"] = arr
            col_w[key] = wgt

    anchor = [None] * n
    for i in range(n):
        log_sum = 0.0
        w_sum = 0.0
        for key, wgt in col_w.items():
            v = anchors[f"anchor_{key}"][i]
            if v is not None and v > 0:
                log_sum += math.log(v) * wgt
                w_sum += wgt
        if w_sum > 0:
            anchor[i] = math.exp(log_sum / w_sum)

    # 无估值因子时退化为价格均值线（收盘价相对 750 日简单均线的偏离）
    price_only = not col_w
    if price_only:
        ma = []
        acc = 0.0
        for i in range(n):
            acc += close[i]
            if i >= w:
                acc -= close[i - w]
            ma.append(acc / w if i >= w - 1 else None)
        anchor = ma

    dev = [close[i] / anchor[i] - 1.0 if (anchor[i] and anchor[i] > 0) else None for i in range(n)]
    anchor_ok = [a is not None for a in anchor]

    for key, arr in (("pe", anchor_pe), ("pb", anchor_pb), ("dy", anchor_dy), ("roe", anchor_roe)):
        if arr is not None:
            out[f"dev_{key}"] = [close[i] / arr[i] - 1.0 if (arr[i] and arr[i] > 0) else None
                                 for i in range(n)]
    out["anchor"] = anchor
    out["dev"] = dev
    out["anchor_ok"] = anchor_ok
    out["price_only"] = price_only
    return out


def position_target(dev: float, p: GridParams) -> float:
    if dev is None or dev != dev:
        return math.nan
    slope = p.slope_up if dev >= 0 else p.slope_down
    pos = p.base_pos - slope * dev
    return float(max(p.min_pos, min(p.max_pos, pos)))


def locked_from_dev(dev: float, p: GridParams) -> float:
    if dev is None or dev != dev or dev >= p.dev_lock:
        return 0.0
    return float(max(0.0, min(p.max_pos, (p.dev_lock - dev) * p.slope_down)))


def grid_index(dev: float, p: GridParams) -> int:
    if dev is None or dev != dev:
        return 0
    return int(math.floor(dev / p.grid_step))


def resolve_grid_params(a: dict, sp: GridParams) -> GridParams:
    """根据标的实际波动解析有效网格参数（ATR 动态步长）。

    a 为 compute_anchor 输出（含 close/high/low）。步长 = atr_mult ×
    ATR(14)/收盘价 的近 250 日中位数，夹在 [min_step, max_step]。
    dynamic_step=False 或数据不足时返回原参数（固定步长）。
    """
    if not sp.dynamic_step:
        return sp
    close, high, low = a.get("close"), a.get("high"), a.get("low")
    if not close or not high or not low:
        return sp
    atrs = ind.atr(high, low, close, 14)
    pcts = [atrs[i] / close[i] for i in range(len(close))
            if atrs[i] and close[i] and close[i] > 0]
    recent = pcts[-250:] if len(pcts) > 250 else pcts
    if not recent:
        return sp
    step = min(sp.max_step, max(sp.min_step, sp.atr_mult * statistics.median(recent)))
    return replace(sp, grid_step=step)


# ---------------- 回测引擎 ----------------

@dataclass
class TradeRecord:
    date: str
    side: str          # buy / sell
    pos_before: float
    pos_after: float
    amount: float
    dev: float
    grid_idx: int

    def to_dict(self):
        return asdict(self)


@dataclass
class BacktestResult:
    name: str
    equity: List[float]
    benchmark: List[float]
    position: List[float]
    dev: List[float]
    anchor: List[float]
    locked: List[float]
    close: List[float]
    dates: List[str]
    trades: List[TradeRecord]
    start_date: str
    end_date: str
    price_only: bool
    grid_step: float = 0.005


def run_grid_backtest(name: str, panel: dict, sp: GridParams, ap: AnchorParams,
                      cfg: BacktestConfig) -> BacktestResult:
    """在单标的面板上运行网格均值回归策略。"""
    # 数据不足时自适应缩短均值线窗口，保证至少留出 ~1 年评估段
    n_all = len(panel["close"])
    eff_ap = ap
    if n_all < ap.lookback_days:
        eff_lb = max(ap.min_periods, n_all - 250)
        eff_lb = min(eff_lb, max(200, n_all // 2))
        eff_ap = AnchorParams(
            lookback_days=eff_lb,
            fair_pct=ap.fair_pct,
            min_periods=max(120, min(ap.min_periods, n_all // 3)),
            weights=ap.weights,
        )
    a = compute_anchor(panel, eff_ap)
    n = len(a["close"])
    valid = a["anchor_ok"]
    if sum(1 for v in valid if v) < 30:
        raise ValueError(f"{name}: 均值线可用样本不足")

    dates = a["date"]
    close = a["close"]
    dy_daily = a.get("dy_daily", [0.0] * n)
    dev = a["dev"]
    anchor = a["anchor"]

    # 有效参数：ATR 动态步长；ADX 序列用于趋势闸门
    sp2 = resolve_grid_params(a, sp)
    adx_vals = (ind.adx(a["high"], a["low"], a["close"], 14)
                if (sp.adx_gate and a.get("high") and a.get("low")) else [None] * n)

    cash_daily = (1.0 + cfg.cash_rate) ** (1.0 / 252.0) - 1.0
    nav = 1.0
    pos = math.nan
    lock = 0.0
    prev_grid = 0

    navs, poss, devs, anchors, locks = [], [], [], [], []
    trades: List[TradeRecord] = []

    for i in range(n):
        ok = valid[i]
        if not ok:
            navs.append(nav)
            poss.append(pos)
            devs.append(None)
            anchors.append(None)
            locks.append(lock)
            continue

        d = dev[i]
        if i == 0 or not valid[i - 1]:
            target = position_target(d, sp2)
            if target == target:
                pos = target
                lock = locked_from_dev(d, sp2) if sp2.use_lock else 0.0
            prev_grid = grid_index(d, sp2)
        else:
            r_idx = close[i] / close[i - 1] - 1.0
            r_div = dy_daily[i] or 0.0
            r_total = r_idx + r_div
            nav *= 1.0 + pos * r_total + cash_daily * (1.0 - pos)

            # 半永久锁仓：偏离<=dev_lock 后的加仓累入锁仓下限；dev>=dev_clear 解锁
            if sp2.use_lock:
                if d < sp2.dev_lock:
                    lock = max(lock, locked_from_dev(d, sp2))
                if d >= sp2.dev_clear:
                    lock = 0.0

            g = grid_index(d, sp2)
            if g != prev_grid and pos == pos:
                target = position_target(d, sp2)
                # 锁仓下限：反弹时仓位不低于锁定量（不回吐）
                if sp2.use_lock and lock > 0:
                    target = max(target, lock)
                # ADX 趋势闸门：趋势市冻结调仓（上行不减仓、下行不加仓）
                ax = adx_vals[i]
                if sp2.adx_gate and ax is not None and ax > sp2.adx_trend:
                    target = max(target, pos) if d > 0 else min(target, pos)
                target = max(sp2.min_pos, min(sp2.max_pos, target))
                amt = target - pos
                side = "sell" if amt < 0 else "buy"
                if abs(amt) > 1e-9:
                    # 成本 = 佣金/税费 + 滑点（P2：滑点按成交额比例计提，默认 5bps）
                    nav *= 1.0 - abs(amt) * (cfg.cost_rate + cfg.slippage_rate)
                    trades.append(TradeRecord(
                        date=dates[i], side=side,
                        pos_before=pos, pos_after=target,
                        amount=abs(amt), dev=round(d, 4), grid_idx=g))
                    pos = target
                prev_grid = g

        navs.append(nav)
        poss.append(pos)
        devs.append(d)
        anchors.append(anchor[i])
        locks.append(lock)

    # 首日：第一个有效锚点（避免前复权负价/预热段污染净值）
    first = 0
    for i in range(n):
        if valid[i]:
            first = i
            break

    # 基准：满仓买入持有（价格收益 + 股息），从回测起点开始归一化复利。
    # 前复权历史早期可能为负价（高分红股除权），故收益只从有效起点起算。
    bench = []
    acc = 1.0
    for i in range(first, n):
        if i == first:
            acc = 1.0
        else:
            prev = close[i - 1]
            if prev > 0:
                acc *= 1.0 + (close[i] / prev - 1.0) + (dy_daily[i] or 0.0)
        bench.append(acc)

    return BacktestResult(
        name=name,
        equity=navs[first:],
        benchmark=bench,
        position=poss[first:],
        dev=devs[first:],
        anchor=anchors[first:],
        locked=locks[first:],
        close=close[first:],
        dates=dates[first:],
        trades=trades,
        start_date=dates[first],
        end_date=dates[-1],
        price_only=a["price_only"],
        grid_step=sp2.grid_step,
    )


# ---------------- 绩效指标 ----------------

def _ret_series(equity: List[float]) -> List[float]:
    out = []
    for i in range(1, len(equity)):
        if equity[i - 1] > 0:
            out.append(equity[i] / equity[i - 1] - 1.0)
    return out


def max_drawdown(equity: List[float]) -> float:
    if not equity:
        return 0.0
    peak, mdd = equity[0], 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = v / peak - 1.0
        if dd < mdd:
            mdd = dd
    return mdd


def cagr(equity: List[float], days: float) -> float:
    if days <= 0 or equity[-1] <= 0:
        return 0.0
    return equity[-1] ** (1.0 / days) - 1.0


def annualized_vol(equity: List[float]) -> float:
    rets = _ret_series(equity)
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def sharpe(equity: List[float], rf: float = 0.0) -> float:
    rets = _ret_series(equity)
    if len(rets) < 2:
        return 0.0
    rf_daily = (1.0 + rf) ** (1.0 / 252.0) - 1.0
    ex = [r - rf_daily for r in rets]
    mean = sum(ex) / len(ex)
    var = sum((r - mean) ** 2 for r in ex) / (len(ex) - 1)
    if var == 0:
        return 0.0
    return mean / math.sqrt(var) * math.sqrt(252)


def summary_metrics(res: BacktestResult, rf: float = 0.0) -> Dict:
    """汇总指标（对齐 dividend_grid_strategy/src/metrics.py）。"""
    equity = res.equity
    bench = res.benchmark
    if not equity:
        return {"error": "无数据"}
    days = 0
    try:
        from datetime import datetime
        d0 = datetime.strptime(res.start_date, "%Y-%m-%d")
        d1 = datetime.strptime(res.end_date, "%Y-%m-%d")
        days = (d1 - d0).days / 365.25
    except Exception:
        days = len(equity) / 252.0
    if days <= 0:
        days = len(equity) / 252.0

    strat_total = equity[-1] - 1.0
    bench_total = bench[-1] - 1.0
    mdd = max_drawdown(equity)
    b_mdd = max_drawdown(bench)
    ann = cagr(equity, days)
    b_ann = cagr(bench, days)
    return {
        "total_return": round(strat_total, 4),
        "annual_return": round(ann, 4),
        "annual_vol": round(annualized_vol(equity), 4),
        "sharpe": round(sharpe(equity, rf), 2),
        "max_drawdown": round(mdd, 4),
        "calmar": round(ann / abs(mdd), 2) if mdd != 0 else None,
        "benchmark_return": round(bench_total, 4),
        "benchmark_annual": round(b_ann, 4),
        "benchmark_drawdown": round(b_mdd, 4),
        "excess_return": round(strat_total - bench_total, 4),
        "years": round(days, 2),
        "trade_count": len(res.trades),
        "avg_position": round(sum(res.position) / len(res.position), 4) if res.position else None,
        "price_only": res.price_only,
        "grid_step": res.grid_step,
    }


def _window_returns(res: BacktestResult) -> Dict[str, float]:
    """按最近交易日窗口计算策略/基准收益。窗口从短到长排列。"""
    from datetime import datetime
    dates = res.dates
    equity = res.equity
    bench = res.benchmark
    windows = [("1周", 5), ("1月", 21), ("半年", 126), ("1年", 252), ("3年", 756), ("5年", 1260)]
    out = {}
    n = len(dates)
    for label, w in windows:
        out[label] = {}
        # 用日期差值精确到自然日（不足窗口则返回 None）
        try:
            d_end = datetime.strptime(dates[-1], "%Y-%m-%d")
            target = None
            for i in range(n - 1, -1, -1):
                if (d_end - datetime.strptime(dates[i], "%Y-%m-%d")).days >= w * 7 / 5:
                    target = i
                    break
        except Exception:
            target = n - 1 - w if n - 1 - w >= 0 else None
        if target is None or target < 0:
            out[label] = {"strategy": None, "benchmark": None}
            continue
        out[label] = {
            "strategy": round(equity[n - 1] / equity[target] - 1.0, 4),
            "benchmark": round(bench[n - 1] / bench[target] - 1.0, 4),
        }
    return out


def build_backtest_stock(res: "BacktestResult", rf: float = 0.0) -> Dict:
    """单只股票完整回测数据。浮点 round 压缩，控制前端传输体积。"""
    date_close = {d: c for d, c in zip(res.dates, res.close)}
    trades = []
    for t in res.trades:
        td = t.to_dict()
        for key in ("pos_before", "pos_after", "amount"):
            if isinstance(td.get(key), float):
                td[key] = round(td[key], 4)
        td["price"] = round(date_close[t.date], 3) if t.date in date_close else None
        trades.append(td)
    return {
        "summary": summary_metrics(res, rf),
        "equity": [round(x, 4) for x in res.equity],
        "benchmark": [round(x, 4) for x in res.benchmark],
        "close": [round(x, 2) if x is not None else None for x in res.close],
        "dates": res.dates,
        "position": [round(x, 4) if x == x else None for x in res.position],
        "dev": [round(x, 4) if x is not None else None for x in res.dev],
        "anchor": [round(x, 4) if x is not None else None for x in res.anchor],
        "trades": trades,
        "window_returns": _window_returns(res),
        "price_only": res.price_only,
    }


def build_backtest_data(per_stock: Dict[str, "BacktestResult"], rf: float = 0.0) -> Dict:
    """兼容旧接口：单文件完整回测 JSON。"""
    import datetime as _dt
    stocks = {name: build_backtest_stock(res, rf) for name, res in per_stock.items()}
    return {
        "generatedAt": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overall": _overall(per_stock, rf),
        "stocks": stocks,
    }


def build_backtest_split(per_stock: Dict[str, "BacktestResult"], rf: float = 0.0):
    """拆分回测数据：小索引（摘要/区间收益）+ 每只股票独立文件，供前端按需加载。

    返回 (index_dict, {name: 单只完整数据})。
    """
    import datetime as _dt
    index = {
        "generatedAt": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overall": _overall(per_stock, rf),
        "stocks": {},
    }
    per_stock_files = {}
    for name, res in per_stock.items():
        full = build_backtest_stock(res, rf)
        per_stock_files[name] = full
        index["stocks"][name] = {
            "summary": full["summary"],
            "window_returns": full["window_returns"],
            "price_only": full["price_only"],
        }
    return index, per_stock_files


def _overall(per_stock: Dict[str, BacktestResult], rf: float = 0.0) -> Dict:
    metas = [summary_metrics(r, rf) for r in per_stock.values()]
    if not metas:
        return {"total": 0}
    import statistics
    anns = [m["annual_return"] for m in metas]
    sharps = [m["sharpe"] for m in metas if m["sharpe"] is not None]
    mdds = [m["max_drawdown"] for m in metas]
    return {
        "total": len(metas),
        "avg_annual_return": round(statistics.mean(anns), 4) if anns else None,
        "avg_sharpe": round(statistics.mean(sharps), 2) if sharps else None,
        "avg_max_drawdown": round(statistics.mean(mdds), 4) if mdds else None,
        "avg_excess_return": round(statistics.mean([m["excess_return"] for m in metas]), 4),
        "stocks": len(metas),
        "rf": rf,
    }
