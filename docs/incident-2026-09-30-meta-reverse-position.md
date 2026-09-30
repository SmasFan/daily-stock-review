# 事故复盘：短线池反向仓（META 空头 18.56 张）

- 时间：2026-09-24 08:01 起持续到 2026-09-30 12:01 清理
- 影响：短线池被做出 **-18.56 张 META 空单（名义 ~13,724 U）**，浮亏 -158 ~ -160 U；
  状态页「短线池净值」被拖到 -60.58 U（起始 100 U）
- 直接损失：**-159.10 U**（币安订单口径 `realizedPnl`，含手续费）

## 症状（用户可见）

1. `http://localhost:8000/binance.html` → 短线池净值 **-60.58 U**，浮盈 -160 U，与日志
   「名义~63」完全不符（实际名义 1.37 万 U）。
2. 持仓行显示异常：META `杠杆 -5.0x`、`名义 -13,724.7`、`盈亏 +1.18%`（空头被当多头）。
3. 「已实现 +0.00 U · 平仓 7 笔」——**平仓盈亏恒为 0**，池净值只看未实现盈亏，平仓后盈亏凭空消失。

## 根因

`trader100.py` / `trader_short.py` 的平仓路径把「持仓存在」等同于「持有多头」：

```python
for p in ex.fetch_positions([sym]):
    if float(p['contracts']) > 0:     # ← ccxt 对空头同样返回正数
        amt = float(p['contracts'])
...
ex.create_order(sym, 'market', 'sell', ex.amount_to_precision(sym, amt))   # 无 reduceOnly
```

- 9/24 08:01 多头平完后，仓位约等于 0，下一轮 `sync_from_real` 把空头 0.08 记成「持有中」；
- 信号是「跌破 SMA50 平仓」（hard 规则）→ 继续 `sell` → 空头 **每轮翻倍**
  （0.08 → 0.16 → 0.32 → … → 5.12），`sell_close` 无 `reduceOnly`，越卖越空。
- 期间只有 trader100 打过一行「存在空头 positionAmt=-18.72（bot 只做多）→ 不记入池」，
  但**只忽略、不纠正**，空头继续滚动。
- 平仓记录使用了 `o.get('average')`，market 单偶发为空 → `pnl = 0`（假 0），
  且 `status_page` 的池净值只累加未实现盈亏 → 收益账目整体失真。

## 修复

| 位置 | 改动 |
|---|---|
| `pos_guard.py`（新增） | `side_of()` 判方向（优先原始 `positionAmt`）；`flatten_unexpected()` 反向仓/超仓一律 `reduceOnly` 市价纠正并清池记账 |
| `trader_short.py` / `trader100.py` | `sync_from_real` 先过方向守卫；`sell_close` 只平真实多头、`qty = min(请求量, 真实多头)`、`reduceOnly=True`；平仓遇到空头直接拒绝卖出；`buy_open` 开仓前校验无反向仓 |
| `guard.py` | 同样只对多头止损；空头仓先市价平掉；`reduceOnly`；`algo_tools` 提到模块级导入 |
| `trade_log.py` | `pool` 支持 `'daily'` / `'short'` 标签（原来只接受数字，传字符串会 TypeError） |
| `trader100/trader_short/guard` | 平仓/止损记录带池标签；盈亏优先用交易所 `realizedPnl`，均价缺失时用 `info.avgPrice` / ticker 兜底 |
| `status_page.py` | 池净值 = 起始 + **已实现（按池）** + 未实现；方向感知显示（空头涨跌幅/名义/杠杆取正号）；交易记录加池标签、旧记录假 0 显示为 `—`；短线卡标注「含方向守卫事故」 |

## 数据清理

- 撤 META 全部 algo 条件单，市价买入 18.56 张 `reduceOnly` 平掉空头（成交价 739.42，`realizedPnl=-159.0978`）。
- `state_short.json`：删除 META / CRCL / AMZN 幽灵记账，清 `close_fail`，`bug_loss=-159.0978`，追加审计日志。
- `trades.jsonl`：补写一条 `close`（`pool='short'`, `pnl=-159.0978`）作为事故实现亏损。
- 备份：`state_short.json.bak-*`、`state_pool.json.bak-*`（未提交，本地留存）。

## 验证

- `python3 -m unittest tests.test_pos_guard` → 11 passed（含：空头仓绝不被卖出、超仓被减回、平仓量被真实多头封顶、guard 对空头买入平仓）
- `tests.test_profit_guard` 18 passed、`tests.test_llm_gate` 9 passed（无回归）
- 线上持仓已无空头；状态页账户权益 ~4,691 U，短线池空仓、净值 -59.12 U（含事故标注）

## 遗留 / 注意

- 短线池本金 100 U 被事故打穿，净值显示为负数属**真实记账**（Demo 账户两池共用保证金，没有单池爆仓保护）。
- 历史 `trades.jsonl`（135 条，`pool=None`）中平仓盈亏存的是假 0，金额无法回溯；笔数按标的集合归类显示。
- 建议后续：单池权益硬上限（净值归零即停机）、`guard` 的短线止损阈值改为跟随池配置（当前 guard 一律 -12%，短线实际 -3% 由 algo 单负责）。
