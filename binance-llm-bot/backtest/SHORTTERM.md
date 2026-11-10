# 币安日内混合策略回测（2026-11-10）

## 背景与目标

用户要求（原话）「这里只用关心币安的东西。你执行吧。然后因为美股要开23h小时交易了，我需要你策略上更短线一点。因为可以上杠杆。所以弹性大」。

改动范围只限 `binance-llm-bot/`（A 股复盘/DSR Bar 卡片/usim 一律不动）。目标：把原来
「20 根日线动量 > +2% 才开仓、每 1h 巡检、-8% 止损」的慢节奏，换成能吃到日内波动的
执行层，同时保留统计上真正有 edge 的方向判断；杠杆保持可配 5x（弹性来自日内波动被放大）。

## 数据与口径

- `backtest/data/*_1d.csv` / `*_1h.csv`：13 只美股 TradFi 永续（TSLA/COIN/PLTR/MSTR/HOOD
  + NVDA/META/AMZN/QQQ/SPY/GOOGL/INTC/CRCL），1h 各 5000 根，
  `2026-04-14 05:00Z → 2026-11-08 12:00Z`。列 `ts,open,high,low,close,volume`。
- 训练/样本外按时间 70/30 切分（不是随机切）；全期收益 `total`、最大回撤 `mdd`、
  Sharpe 按 `sqrt(252*24)` 年化。
- 成本：双边各 `FEE=0.0004`（0.04%），`LEV=5`，每标初始保证金 `100/13`。
- **必须过滤破产路径**：`total > -0.5 且 mdd < 0.95`。早期实验里出现过净值被打到 0 后
  从 0 起算、显示「样本外 +123.8%」的假阳性。
- 综合评分 `score = oos*1.2 + total*0.4 + oos_sh*0.02 - mdd*1.2 - max(0,train-oos)*0.3`，
  偏向样本外、惩罚回撤与过拟合。

## 三版实验

### v1：把日线动量规则原样搬到 1h（`shortterm_results.csv`，96 组 / 5 标）

规则：`|mom(n)| < th` 立刻平仓、反向即反手。结论：**不可用**。

- 每 20~30 根 K 线换手一次，5 标合计 737~1279 笔，双边 0.04% 费把收益吃光。
- 名义最优 `n=12/th=1%/sl=2%/PG_SHORT/short=True`：全期 +39.3%、样本外 **-12.5%**、
  MDD 62.8%、Sharpe 0.84。样本外几乎全负。

### v2：滞回平仓 + 1h SMA200 过滤（`shortterm.py`，216 组 / 13 标，14s）

`sim(rows, n, th, sl, pg, th_short, short_risk, sl_short, exit_ratio, trend_n, margin, cache=None)`：

- 持仓中动量衰减到 `exit_ratio*th` 才平（不再 `|mom|<th` 就平），换手大幅下降；
- `trend_n=200`：价在 1h SMA200 上方只做多、下方只做空；
- 空头独立阈值 `th_short ∈ {off, sym=1×th, cons=2×th}`、半仓 `short_risk=0.5`、
  `sl_short=sl*0.75`；
- `PG_SHORT = dict(lock=1.0, trail=0.6, trail_min=1.0, give_back=0.5,
  tp_scale=2.0, scale_ratio=0.5, tp_fast=3.5)`，`BE_BUF=0.0008`。

结论：**风险控住了，收益没了**。MDD 从 60%+ 降到 ~15%，但样本外 -2%~-9%。
最优组 `n=12/th=1%/sl=2%/exit=0/trend=200/short=sym` 全期 +2.1% / MDD 14.7% /
OOS +0.9%；去掉 trend 过滤全期 +26.7% 但 OOS -3.3%（纯拟合训练段）。
维度边际：`trend_n=200` 明显优于 0。**纯 1h 动量噪声里没有稳定 edge。**

### v3：日线定方向 + 1h 择时（`shortterm_hybrid.py`，192 组 / 13 标，16s）

`daily_dir(rows_1d, dth, short_ok)` 用日线 20 根动量定方向：> `dth` 只多、
< `-dth` 只空（半仓）、中间观望。日线值用
`j = bisect.bisect_right(dts, ts[i]-86400000)-1` 取**当时已收**的日线，无未来函数。
1h 只做同向触发：`dir>0 且 mom_h>th` 开多；`dir<0 且 mom_h<-th` 开空；
再叠 `trend_n=200` 的 SMA200 过滤。离场：日线方向翻转、1h 动量衰减到
`exit_ratio*th`、2% 止损、或 `PG_SHORT` 触发。

**最优组（也是最终上线参数）**
`dth=0.02, short=1, n=12, th=0.01, sl=0.02, exit_ratio=0.0, trend_n=200`：

| 全期 | MDD | Sharpe | 训练 | 样本外 | OOS Sharpe | 成交笔数 |
|---|---|---|---|---|---|---|
| **+22.0%** | **11.5%** | **1.42** | +18.6% | **+2.9%** | 0.82 | 1653 |

前 4 名构成稳健平台（`sl=2%/4%`、`exit_ratio=0/0.4` 任意组合）：
全期 +22~24%、MDD 11.4~13.1%、样本外 +0.8~+3.8%。不是单点尖峰。

维度边际：`dth=0.02 > 0.005`、`n=12 > 6`、`th=0.01` 最优、`trend=200 > 0`、
`exit=0.0` 略优、`short=1 > 0`。**`short=0`（纯多）最好样本外只有 -6.2%~-6.9%**——
样本外是下跌行情，双向做空是转正的关键，所以上线必须开 `BN_ALLOW_SHORT=1`。

## 上线参数（`.env` / `intraday_signal.py`）

| 参数 | 值 | 作用 |
|---|---|---|
| `DAILY_MOM_N` / `DAILY_MOM_TH` | 20 / 0.02 | 日线 20 根动量 ±2% 定方向 |
| `BN_SHORT_MOM_TH` | 0.02 | 空头方向阈值（旧值 0.08，太迟） |
| `BN_EXEC_TF` / `BN_EXEC_N` / `BN_EXEC_TH` | 1h / 12 / 0.01 | 1h 12 根动量 ±1% 择时 |
| `BN_TREND_N` | 200 | 1h SMA200 过滤（上只多、下只空） |
| `BN_EXIT_RATIO` | 0.0 | 滞回：动量衰减到 0 才离场 |
| `SL_PCT` / `SHORT_SL_PCT` / `BN_SHORT_SL` | 0.02 | 多空同口径 2% 止损（旧 8%/3%/6%） |
| `BN_SHORT_RISK` | 0.5 | 空头半仓 |
| `LEV` / `SHORT_LEV` | 5 | 杠杆；只线性缩放收益与回撤，不改变信号质量 |
| `LOOP_MIN` / `GUARD_SEC` | 15 / 30 | 巡检 15min、本地止损守护 30s（旧 1h / 60s） |
| `SHORT_ENABLED` | 1 | 短线池重新允许开仓（旧 1h SMA 负期望时关掉） |

利润保护改用 `profit_guard` 的 `intraday` profile（`1/0.6/2.0/3.5`），
即回测里的 `PG_SHORT`；旧的 `daily`（4/7/12）和 `short`（2/3/5）阈值在日内波动下
几乎永远触发不了，浮盈会原样还回去。

LLM 成交前闸门（`llm_gate.py`）同步改成 1h 语境：`d7/d30` 解释为近 7/30 **小时**、
SMA50 是 50 小时均线，avoid 门槛改成 `d7<=-4%`、`d7<=-10%`、偏离 ±8%，
并在 SYS_PROMPT 里写明「别否决统计信号：方向来自日线，1h 只是入场时机」，
否则闸门会继续用日线尺度把日内信号全否掉。

## 复现

```bash
cd /Users/smas/code/daily-stock-review/binance-llm-bot
python3 backtest/shortterm.py --syms 13          # v2，输出 shortterm_v2_results.csv
python3 backtest/shortterm.py --syms 13 --full   # v2 细网格（含 n=3 的 15m 代理）
python3 backtest/shortterm_hybrid.py             # v3，输出 shortterm_v3_results.csv
```

实盘对应实现：`intraday_signal.py`（纯函数 `daily_dir` / `exec_signal` / `target`）、
`trader100.py::hybrid_signal` 与 `trader_short.py::hybrid_signal`；
单测 `tests/test_intraday_signal.py` 用假交易所验证两个 bot 都真的取了 1d + 1h 两组 K 线。
