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
- 红利池扩池（2026-11-12）：`sim_dividend.py` 的 `POOL` 共 10 只，核心 6 只权重 1.0、扩池 4 只（601988 中国银行 / 601985 中国核电 / 601398 工商银行 / 601225 陕西煤业）权重 0.5；`unit_budget(code)` 决定每标总预算（核心 16,667 / 扩池 6,250），`refresh_plan()` 幂等迁移老账本 `meta.pool`。**不要改成等权扩池**（25 只全等权回测只剩 +10.6%，基准 6 只 +39.2%）；筛选与回测见 `backtest/DIVIDEND_EXPAND.md`。
- `sim_dividend.py --intraday` 在非交易时段（`in_session()` 09:15–11:35 / 12:55–15:05）只刷行情不交易；`scripts/mac/dsr.py task_intraday()` 已挂「红利池巡检」，交易时段每 5 分钟自动更新。
- **顶栏卡片联动（用户 2026-11-12 明确规则）**：凡是改动涉及红利池 / 模拟盘 / 币安账户的数据口径，都必须同步检查并改 `dsrbar_card.html`（红利 tab / 全池 tab / 币安 tab），改完重启 `/Applications/DSR Bar.app`（只重启、不重编译，避免 TCC 失效）。例：红利池换标的要确认红利 tab 的待触发买点能看到新标的、全池 tab 的账户列表要含新策略。
- **全池红利账户**：`sim_live.py` 的 `dividend_account()` 从 `data/sim_dividend.json` 按 `50000/start_cash` 等比例派生 `pools.all.accounts.dividend`（5 万本金口径、只读、不进 `all_books()` 下单循环，收益率与红利池一致）；`sim_live.html` 的 `ORDER`/`META` 与 `dsrbar_card.html` 的 `keys`/`LABEL` 都必须含 `dividend`，否则前端不显示。

## 美股收盘后刷新指数（2026-11-17）
- **问题**：`recommend_data.json` / `wide_index_data.json` 的 `indices[]`/`items[]` 只在 A 股时段（recommend 09:05、review 15:40）生成，美股指数（usDJI/usIXIC/usINX）一直停在白天快照或上一交易日；市场页（smasfx `?view=market`）把「更新」统一显示成 `recAt`，看着像「全是 15 点更新」。
- **做法**：新增 `refresh_us_index.py`（拉三大美股指数行情+日K因子，原地 patch 两份 JSON 的美股项，写 `usAt`）；`scripts/mac/dsr.py task_usclose()`；LaunchAgent `com.smas.dsr.usclose.plist`（`install_launchd.sh` 的 `plist_usclose`）**周二~周六 05:30 北京**（美东 16:00 收盘 = 北京次日 05:00 冬令/04:00 夏令）。
- smasfx `market_data()` 增加 `usDate`（取美股因子日K最新日期，`2026-11-16`→`11-16` 显示）+ `usAt`；前端 `?view=market` 指数区标题改「A股 `<recAt>` · 美股 `<usDate>` 收盘」，不再用单一 recAt。
- **坑**：smasfx 无 git 仓库（`git status` 报 128），改动只落本地，重启用 `launchctl kickstart -k gui/$(id -u)/com.smas.smasfx`；数据源层验证用 headless Chrome CDP（`/Users/smas/code/smasfx/tools/mobile_shot.py` 的 `WS/Chrome/PORT`，自建 9224 端口 `--headless=new --remote-allow-origins=*`）。实测 usDJI 51,608(-1.41%) / usIXIC 24,509(-2.49%) / usINX 7,416(-1.92%) 均已刷新。

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
- **进程保活（2026-11-13）**：`~/Library/LaunchAgents/com.smas.binance-arena.plist` 跑 `watchdog.py`（RunAtLoad + KeepAlive），watchdog 再管 arena_engine / guard / status_page / `review.py auto` 四个子进程。**2026-11-13 11:50 系统重启导致币安栈全停 1 小时 10 分（此前无任何自启项，crontab 为空）**，加了这个 LaunchAgent 才不会再断。`guard.py` 的 `load_markets()` 会因 `demo-fapi.binance.com/fapi/v1/exchangeInfo` 偶发 SSLError 崩掉并被 watchdog 每分钟重启，已改为重试 3 次 + 失败不退出（循环里补加载），否则止损守护形同虚设。

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

## 模拟盘市场环境自适应（2026-11-14 v4.1）
- `sim_live.py` 新增 `market_regime()`（上证/创业板 20 日动量 + 创业板 ATR20 + 涨占比 → bull/chop/overheat/bear/panic，连续 2 天确认）；`market_gate()` 里 **panic 直接 block、bear 至多半仓**（环境优先于单日 LLM/过热等软因素）。
- 组合级风控 `account_regime()`：账户近 20 日回撤 ≥6% 降仓×0.6、≥12% 或连亏 5 笔暂停开仓；`vol_target_pos()` 按创业板 ATR 波动目标化仓位（ATR 4.41% → ×0.45）。回测结论（`scripts/backtest_sprint.py` 120 天）：大盘方向驱动反而跑输（-3.66% vs 稳健 +7.44%，chop 段削掉逆势选股 alpha），组合级 adaptive2 +2.36%/MDD 3.0% 更稳 → **只保留 panic/bear 极端保护 + 组合熔断 + 波动目标化**，不给三账户换参数集。
- `save()` 给每账户写 `regime_info`（market/portfolio），`sim_live.html` 账户卡与 `dsrbar_card.html` 全池 tab 显示环境标签（如「高波动急跌·熔断」）。
- 坑：`market_gate(review, sh=..., cyb=...)` 的 sh 参数是给 `market_regime` 的指标 dict（mom20/dev20/atr20），**不是** review 里的 factors（signal/score）；`mkt_bear` 必须读 `idx_sigs['sh000001'].factors`，两者混用会把 bear/panic 判定悄悄变 chop。
- **同族对冲互斥（2026-11-16）**：SOXL（半导体3x正向）与 SOXS（半导体3x反向）经济方向可同向（多SOXS=空SOXL净值），同持=重复押注同一主题。`arena_engine.py` 加 `PAIR_FAMILY={'SOXL':'SOX','SOXS':'SOX'}` + `family_conflicts()`（开仓互斥）+ `converge_families()`（decide 开头收敛：族内>1笔只留策略优先级高者，同级留先开），实测全池曾出现「SOXL空50U(S1)+SOXS多25U(S5)」同持 → 半导体主题名义188U=权益155% 的过度集中。symbol 仲裁只防单票冲突，不防相关票同向叠加——回测每策略独立记账看不出，实盘共享账户会集中。

## 币安竞技场统一多因素方向打分器（2026-11-17 方案B）
- 用户 m03163「要统一多因素自由判断多空，而不是单纯按纯多或纯空」→ S2/S4/S6 从「纯多策略」改为**统一打分器不同权重配置，都能双向做多做空**；S1(sig_hybrid)/S5(sig_breakout) 保持不动（回测 C 全打分器 +9.5% 反而输 B，S1 本身已是多因素双向雏形）。
- 模块 `binance-llm-bot/direction_score.py`：`direction_score(ctx,w,f5sign)` 5 因素各 −1/0/+1 加权归一（日线20根动量±2%、1h12根动量±1%、日线价vsSMA50、1h价vsSMA200、偏离SMA20超买超跌）。配置 `SCORE_CFG`：S2(1,0,2,2,0) sl3% 趋势型、S4(0,0,0,0,3) sl2.5% 反转型半仓、S6(1,1,1,1,1) sl2% 均衡型。`make_score_signal(sid)` 返回 (fn, sl)，fn(ctx,holding) 持仓时净分仍同向则持有、转弱则离场，空仓时超开仓阈值顺势开多/空。
- 回测（`backtest/unified_compare.py`，23标全池5x，权重60/20/8/7/5）：A现状 +11.2%/Sh0.79；**B混合 +13.2%/Sh0.84/OOS+15.6%**，胜率 S2 35%→70%/S6 38%→66%；C全打分器 +9.5%（最差）。教训：v1 无利润保护(PG)的全打分器换手暴增全亏，加 PG（trailing+分批止盈+保本）后才站住。
- 网络健壮性（同日修）：demo-fapi 偶发 SSL 断连曾导致整轮 decide 崩溃→write_status 不执行→状态滞后 40min。修：open_position 查仓位失败跳过该票、close_position 查持仓失败放弃本次平仓（绝不假平仓 pop 本地）、decide 逐票开仓包 try/except 单票失败不拖垮整轮。
- 实测生效（00:48）：打分器平掉 HOOD/NVDA/PAYP 存量多单，开空 CRCL/GOOGL/PLTR/XPT，持仓从 6空+5多 → 9空+3多，权益 122U(+22%)。
- 坑：代理节点「🇯🇵日本专线6-0.1倍率」偶发整体不可达（所有节点 demo-fapi HTTP 000），curl 轮换探测后切回该节点恢复；不要误判为代码问题。

## 量化健康监控（2026-11-17）
- `scripts/quant_monitor.py`：daily/weekly/report 三模式，纯标准库，扫 `binance-llm-bot/{arena,watchdog,guard}.log` + `state_arena.json` + `data/logs/{simscan,intraday,review,recommend,external,ussim}.log` + `data/llm_route_log.jsonl`，输出 `data/monitor/daily_*.txt|json` 与 `weekly_*.txt|json`，`--push` 走 Server酱（key 未配则跳过）。
- 检测：币安心跳中断（LOOP_MIN=15，>30min warn >45min bad）、watchdog 重启风暴（>10 warn >50 bad）、行情失败退出、决策轮异常、开仓跳过、A股巡检缺口（扣午休 11:35–12:55）、复盘缺失、LLM 路由失败率；无任何日志的日子判 NODATA 不误报。
- launchd：`com.smas.dsr.monitor` 每天 20:30 日报、`com.smas.dsr.monweekly` 周日 20:35 周报（`install_launchd.sh` 的 `plist_monitor`，dsr.py action=`monitor`/`monweekly`）。
- 坑：dsr.py `run()` 的 step 名不能含 `%` 格式符（`"健康%d"` 会把 mode 当数字）。
