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
- 红利池扩池（2026-11-12）：`sim_dividend.py` 的 `POOL` 共 10 只，核心 6 只权重 1.0、扩池 4 只（601988 中国银行 / 601000 唐山港 / 601398 工商银行 / 601225 陕西煤业）权重 0.5；`unit_budget(code)` 决定每标总预算（核心 16,667 / 扩池 6,250），`refresh_plan()` 幂等迁移老账本 `meta.pool`。**不要改成等权扩池**（25 只全等权回测只剩 +10.6%，基准 6 只 +39.2%）；筛选与回测见 `backtest/DIVIDEND_EXPAND.md`。
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

## 前端适配层（2026-11-12，参照 smas·fx 手机状态台）
- `assets/css/smasfx.css` 是**覆盖层**：在 `common.css` 之后引入，改 token/背景/卡片描边/顶栏，
  手机（<720px）显示 `assets/js/smasfx-nav.js` 注入的底部 5 视图条 + 更多面板，桌面（≥720px）隐藏。
  整文件删掉即可回退旧外观。断点：`@media (max-width:719px)` / `(min-width:720px)` / `(min-width:1100px)`。
- `assets/js/smasfx-nav.js` **独立、不依赖 RV / common.js**：自带内联 SVG 图标（有 RV 时用 RV.ui.icon），
  还在 head 加载的页面靠 DOMContentLoaded / setTimeout / load 三次兜底注入；`dsrbar_card.html`
  （macOS 菜单栏 popover）明确排除，不要给它加导航。
- 21 个根目录 HTML 全部接入：18 个共享层页面（common.css + common.js）与 3 个自包含页面
  （`binance.html` / `perp.html` / `watchlist.html`，link/script 插在 `</head>` 前以覆盖其自带 style）。
  自包含页由脚本加 `body.sfx-has-nav`，用于底部留白、长标题缩进与状态条换行。
- `index.html` 首屏 4 格状态条读 `review.json` / `recommend.json` / `sim_live.json`（`pools.all.accounts`）/
  `us-sim.json`，入口卡片手机 2 列、720px 3 列、1100px 4 列。
- 验证脚本（临时）：`/tmp/dsr_shot.py`（CDP 真视口 390×844 与 1440×900，检查横向溢出、
  tabbar 显隐、JS 错误），截图在 `/tmp/dsr-shots/`。改前端后建议跑一遍：18 页全绿。
