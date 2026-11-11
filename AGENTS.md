# 项目指令

## 推送规则（重要）
- **GitHub 只放代码与业务逻辑，不放实际数据**（用户 2026-11-11 明确要求）：`data/`、`holdings.json`、`state*.json`、`*.log`、回测 CSV、分享 zip、`reviews/` 一律不入库，`.gitignore` 已覆盖。
- 上传方式：`~/.local/bin/dsr-publish "提交信息"`。工作区 `/Users/smas/code/daily-stock-review` 不是 git 仓库，镜像在 `~/code/daily-stock-review-git`；脚本会自动排除密钥/数据、做敏感扫描、commit 并 push。
- 提交信息风格：中文、简洁，参照现有历史（如「docs/index/watchlist 导航补全 + 普涨过热日闸门」）。

## 项目约定
- 运行入口：`python3 run_review.py --mode all`（复盘/推荐/机构/策略/持仓/期货）
- 测试：`python3 -m unittest discover -s tests`（改完代码必须跑）
- 页面数据由 Python 生成 JSON 到 `data/`，前端页面读 JSON 渲染（禁止缓存，改动即时生效）
- 数据源：腾讯行情（K线/快照）、东方财富（资金流/龙虎榜/股东，push2delay 域名防 WAF 限流）、新浪（备用）
- 红利池当日涨幅：`data/sim_dividend.json` 的 `positions[].last_chg` / `plan[].chg` 是涨跌幅 %，来自 `src/data_provider.fetch_quotes()['change']`；`sim_dividend.py --plan` / `--review` 必须传实时 quotes，否则恒为旧值 / `None`。
- `sim_dividend.py --intraday` 在非交易时段（`in_session()` 09:15–11:35 / 12:55–15:05）只刷行情不交易；`scripts/mac/dsr.py task_intraday()` 已挂「红利池巡检」，交易时段每 5 分钟自动更新。

## 币安竞技场（2026-11-10 起，替代日线池/短线池两套 bot）
- 引擎：`binance-llm-bot/arena_engine.py`（`watchdog.py` 守护，替换 trader100/trader_short）。
- 6 个策略共用 **同一 100U**、**同一批 23 只 TradFi 永续**，symbol 级仲裁
  （空仓按胜率优先级 S1>S5>S2>S6>S4 选策略开仓，平仓只由开仓策略决定）。
  权重 60/20/8/7/5，S3 双均线上线权重 0（回测 -25%）。
- 为什么不按策略分池：S1 在 13 标上 +26.8%，换成 5 只就 -28.4% —— 分池比的是标的分配，不是策略。
- 记录：`state_arena.json`（每笔带 strategy/sl_pct）、`data/binance_arena.json`（排名）、
  状态页「策略竞技场」板块、日志 `arena.log`；`guard.py` 改读 state_arena 按每笔 sl_pct 兜底。
- 回测与结论：`binance-llm-bot/backtest/arena.py`、`ARENA.md`、`arena_results.csv`。
- 已知差异：第一版不接 profit_guard（无分批止盈/移动止盈）。
