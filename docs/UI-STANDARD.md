# daily-stock-review 前端 UI 规范（全模块细分 · 永久生效）

> 版本: v1 · 适用于仓库所有 .html 页面（现状 + 未来新增）
> 原则: 数据引擎生成 JSON → 页面读 JSON → **复用共享组件库**（common.js / rvui.js）→ 每个 UI 元素都有唯一规范归属，不复制粘贴、不另起风格。

## 0. 依赖与加载顺序（每个页面固定）

```html
<link rel="stylesheet" href="assets/css/common.css">
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<script src="assets/js/common.js"></script>   <!-- RV: fmt / ui / API / 主题组件 -->
<script src="assets/js/rvui.js"></script>    <!-- RVX: kpi / section / table / stockCard / popup … -->
```

## 1. 全局语义与色彩（不可改动）

| 语义 | 颜色 | CSS 变量 | 用法 |
|---|---|---|---|
| 上涨/买入/多头 | 红 | `--up:#dc2626` | class `up` |
| 下跌/卖出/空头 | 绿 | `--down:#16a34a` | class `down` |
| 中性 | 灰 | `--neutral` | class `neutral` |
| 观望/警告 | 橙 | `--watch` | class `watch` |
| 强调 | 金褐 | `--accent:#c78d5a` | 图标/重点 |
| 页面背景/卡片 | 浅灰/白 | `--bg/--surface/--surface-2` | — |
| 红涨绿跌 | — | — | **A股习惯，全站唯一** |

所有涨幅数字一律经 `RV.fmt.pct(v)`（v 已是百分数值）或 `RVX.p(v)`；带颜色用 `RV.fmt.cls(v)` → `up/down/neutral`。
**严禁** 页面内手写十六进制红绿蓝（除图表 series 专用色由 `#dc2626/#16a34a` 常量给出）。

## 2. 页面级骨架（layer 0）

每个页面 = 以下部分，顺序固定：

```
┌ header.top       RV.ui.header(title, sub, activeKey, genTime)  → 标题+副标题+全站导航+活跃高亮
├ div.loading      数据加载中…
├ div#content      全部内容（渲染函数写入）
└ footer           数据由 python X.py 生成 · 仅供学习 · 不构成投资建议
```

- header 必须最先渲染（失败分支也要有 header + 空态错误提示，见各页 catch）
- 每个页面在 common.js navs 有唯一 key；header 第3参传同 key

## 3. 组件目录（细分所有 UI 元素）

### 3.1 布局容器
| 名称 | 实现 | 说明 |
|---|---|---|
| 区块 section | `RVX.section(title, icon, body, chips)` | 白卡+标题行+右侧 chip；页面所有大块 |
| KPI 行 | `RVX.kpis([{label,val,cls,sub,icon}])` | 顶部门户数字卡组 |
| 折叠组 | `.month-group` + `.mg-head/.mg-body`（RVX.groupedTable） | 按月份/键分组，默认只开最新组，点击展开 |
| 双栏/多栏 | CSS grid `repeat(auto-fit,minmax(…,1fr))` | 卡片墙 |

### 3.2 文本/徽章
| 名称 | 实现 | 说明 |
|---|---|---|
| 涨跌百分 | `RVX.colored(v)` / `RV.fmt.pct` | 自动 +号 + up/down 色 |
| 信号章 | `RV.ui.signalBadge(key,label)` | 强烈买入/买入/观望/减仓/卖出 |
| 评级 | `RV.ui.ratingBadge(r)` | A/B/C |
| 徽章 chip | `.sec-chip` / `.date-tag` | 小标签：板块/日期/说明 |
| 图标 | `RV.ui.icon('name','fa-xs')` | FontAwesome，统一前缀 |

### 3.3 表格（数据主力）
| 名称 | 实现 | 说明 |
|---|---|---|
| 普通表 | `RVX.table(cols, rows, {emptyText})` | cols 支持 render(rowVal,row) 自定义、cls 着色列 |
| 折叠分组表 | `RVX.groupedTable(cols, rows, {groupKey/groupOf})` | 长流水（成交流水/跟踪）按月折叠 |
| 横向滚动 | `.table-wrap` 内自动 | 列多时横向滑 |
| 列规范 | 首列日期 `date-tag`、末列可理由/操作；数字列右对齐可着色 | 见各页 |

### 3.4 个股明细卡 stockCard（**六层标准结构**）
`RVX.stockCard(item, opts)` 返回完整卡：
1. 头行：名称/代码/板块 chip/评级/信号章
2. 价位行：现价大字 + 涨跌色 + MA20 + 止损(绿) + 目标(红)
3. 指标 chip 行：趋势/MACD/RSI/量能/60日
4. 评分条：0-100 色条（≥68红/≥40橙/<40绿）
5. 理由：reasons 截断
6. 资金行：主力净流入/5日（红正绿负，亿/万格式化）
- 复用方：review/recommend/uptrend/lowval 全部股票类呈现
- opts: `click`(指针)+ `codeAttr`（绑定点击→K线弹层）

### 3.5 图表（ECharts 统一约定）
| 元素 | 约定 |
|---|---|
| 线图 | 策略主色实线加粗；基准灰虚线；`animation:false` |
| 柱图 | 日收益柱 红涨绿跌 `v>=0?'#dc2626':'#16a34a'` |
| K线 | 标准 OHLC，红涨绿跌 |
| 缩放 | `dataZoom:[inside,slider]`（日/净值类必须） |
| 交互 | 点击 `chart.on('click',p=>…)` → `RVX.popupCard` 弹当日卡 |
| 副图 | 仓位/收益柱可 yAxisIndex 分轴 |
| 复用 | 同页多图统一 init/dispose；window resize 重绘 |

### 3.6 弹层 / 模态
| 名称 | 说明 |
|---|---|
| 图上浮层卡 | `RVX.popupCard(hostSel, bodyHTML, headerHTML, color)` 点击图弹出，默认**全展开**，× 关闭 |
| 宽基/宏观 modal | common.js 内 idx-modal（overlay+居中卡+Esc 关闭） |
| 个股 K线 弹层 | 各页 modal → 内嵌 echarts 走势 |

### 3.7 状态元素
| 名称 | 实现 |
|---|---|
| 加载 | `.loading` div（每页初始） |
| 空态 | `RVX.empty('先运行 python X.py')` |
| 错误 | catch → header + `loading` 显示错误消息 |
| hover | 表格行/卡片 `background:var(--surface-2)` |

## 4. 数据流规范
1. 计算全在 Python：`run_review.py` / `build_*.py` / `sim_live.py` 写 `data/*.json`
2. 页面 `RV.API.fetch('x.json')`（自动 ?t 防缓存、重试）
3. 前端**不联网**（除 cdn echarts/fa）、不计算复杂指标、不硬编码数据
4. JSON 字段：涨跌幅直接存百分数（3.21 而非 0.0321）；金额存元；日期 `YYYY-MM-DD`

## 5. 现有页面归属清单（哪些页面用哪些组件）

| 页面 | header key | 核心组件组合 |
|---|---|---|
| review.html | review | kpis + sectorSection + stockCard + 温度 |
| recommend.html | recommend | 同上 + 理由/评分 |
| uptrend.html | uptrend | 筛选条 + trendChip + 榜单表 |
| mainline.html | mainline | 主线卡 + 龙头表 |
| lowval.html | lowval | 横盘选股表 + 质量/估值条 |
| institution.html | institution | 资金概览 kpi + rankTable + stockLink(m→modal) |
| macro.html | macro | 情绪 kpi + alertCard + 主题榜 |
| holdings.html | holdings | 网格提醒 + 持仓表 |
| backtest.html | backtest | 侧栏 + 绩效表 + 调仓折叠 + 净值图 |
| tracking.html | tracking | 稳定榜 + 历史折叠组 |
| metals.html | metals | 分组行情卡 + 期货图 |
| sim.html | sim | 窗口切换 + 策略卡 + 净值/收益图 + 交易折叠 + 弹层 |
| sim_live.html | simlive | kpi + 净值图(买卖点) + 持仓/流水 + 日志/复盘 + 弹层 |
| watchlist.html | (旧独立) | 板块汇总 + 板块卡 + 个股 modal（历史遗留，新代码勿模仿） |
| index.html | — | entry-card 网格 + hero |

## 6. 新增/修改页面 checklist（含 §7 必选）
- [ ] 骨架照 layer 0；navs 已注册；active key 对
- [ ] 组件一律调 RV/RVX，不手写重复 HTML
- [ ] 股票信息用 stockCard 六层
- [ ] 长列表用 groupedTable 折叠
- [ ] 图表点击 → popupCard 弹当日卡（勿跳页）
- [ ] 红涨绿跌正确；百分数直接 pct 不加转换
- [ ] 空态/错误分支有 header + 提示
- [ ] footer 风险提示
- [ ] node serve.js 本地验证后 git 提交（含数据 json）

## 7. 必选增强规范（2026-09 起，新/改页面必须）

| 项 | 规范 |
|---|---|
| 侧边栏 | 内容区块 ≥2 个的页面必须给主要 section 加 `data-nav="标题" data-nav-icon="图标"`，渲染完调 `RV.ui.initSidebar()`（大屏右侧导航 / 小屏抽屉）。动态视图切换后重新 `setTimeout(()=>RV.ui.initSidebar(),30)` |
| 个股可点 | **所有涉及个股名称处**（持仓/成交/计划/榜单/推荐）都要可点击弹「个股卡」：class `sname` + `onclick="window.__stock('code')"`；页面定义 `window.__stock` 拉 review_data 当日快照 → `RVX.popupCard` 展示 价/涨跌/趋势/信号/指标/点位/资金 |
| 个股卡内嵌图表 | 弹卡内嵌专业行情图：引 `assets/js/stockchart.js` → `SC.fromKline(dom, code, name, klineJSON, {type})`；周期按钮 日/周/月；K线数据用 `data/kline/{code}.json`（`build_kline_export.py` 导出） |
| 现代行情图 | 新行情/K线一律用 stockchart.js（K线+MA+成交量+MACD 三格、十字光标、红涨绿跌、缩放）；不复用裸 echarts 简单折线当行情图 |
| 移动端 | viewport 已配；卡片 `grid repeat(auto-fit,minmax(280px,1fr))`；表格包 `.table-wrap` 横滚；640px 下缩图表高度/单列 |
| 图表点击弹日卡 | 净值/收益图 `chart.on(click)` → `RVX.popupCard`（图下浮层，默认全展开） |

## 8. 反模式（禁止）
- ❌ 复制 review.html 的 HTML 到新页再改 —— 用 RVX.section/table
- ❌ 页面写 `fetch('https://…')` 行情 —— 引擎层统一
- ❌ 新起一套 CSS 变量/主题 —— 复用 common.css；页面专属样式须 `.pg-` 前缀
- ❌ 涨跌色反用 / 手写 #c0392b 等（watchlist 旧色）—— 用变量
- ❌ 把整个表 innerHTML 一次塞几千行不折叠 —— groupedTable
