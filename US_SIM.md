# 美股模拟盘（已并入每日复盘工作台）

- 页面：<http://127.0.0.1:8000/us-sim.html>
- 网页快照：`data/us-sim.json`
- 独立账本：`data/us-sim/state.json`
- 策略与交易包：`usim/`
- 手动更新入口：`scripts/us_sim.py`
- macOS 定时任务：`com.smas.dsr.ussim`，每 300 秒执行一轮

> 仅用于模拟和研究，不连接券商，不使用真实资金。初始资金 $100，支持碎股；默认零佣金、5bp 滑点。

## 使用

```bash
cd /Users/smas/code/daily-stock-review

# 获取行情、执行一轮策略并更新网页
python3 scripts/us_sim.py

# 只重新导出网页数据，不执行交易轮次
python3 scripts/us_sim.py --export-only

# 命令行查看账户
python3 -m usim.cli status

# 历史回测（会访问行情接口）
python3 -m usim.cli backtest --days 320
```

网页每 30 秒重新读取 JSON；后台 launchd 每 5 分钟更新一次 JSON。静态页面不会直接触发交易，避免刷新浏览器导致重复下单。

## 页面内容

- 总权益、累计收益、现金、持仓市值、浮动/已实现盈亏
- 权益曲线、仓位、最大回撤、平仓胜率、盈亏比
- 当前持仓的成本、现价、仓位、止损和浮动盈亏
- 12 只美股的实时价格、SMA50、RSI14、45 日突破距离和信号
- 最近 100 笔模拟交易
- 历史回测参考及完整策略说明

## 策略规则

1. **大盘过滤**：SPY 在 SMA50 上方才允许开新仓。
2. **突破入场**：收盘突破前 45 日最高收盘，位于 SMA50 上方，RSI14 < 78。
3. **仓位**：单笔风险为权益 3%，单票市值上限 40%，最多 6 只，保留 3% 现金。
4. **止损**：初始止损为入场价 − 1.75×ATR；浮盈达到 2R 后，启用峰值 − 2.5×ATR 移动止损。
5. **交易时段**：默认只在周一至周五美东 09:30–16:00 开仓和平仓，休市时只观察。未单独处理美股节假日。

## 自动任务管理

```bash
# 查看任务
launchctl list | grep com.smas.dsr.ussim
python3 scripts/mac/dsr.py status

# 手动触发
launchctl kickstart -k "gui/$UID/com.smas.dsr.ussim"

# 停止 / 恢复
launchctl bootout "gui/$UID/com.smas.dsr.ussim"
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.smas.dsr.ussim.plist"
```

统一任务安装脚本 `scripts/mac/install_launchd.sh install` 也已包含 `ussim`。日志位于 `data/logs/ussim.log` 和 `data/auto_run.log`。

## 测试

```bash
python3 -m unittest tests.test_us_sim -v
python3 -m unittest discover -s tests
```

`tests/test_us_sim.py` 全部使用注入行情，不访问网络，覆盖空账户、持仓按最新价估值、缺报价降级和 JSON 落盘。
