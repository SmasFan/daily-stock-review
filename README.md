# 每日股票复盘工作台

A股每日复盘系统：技术分析、多因子选股推荐、板块轮动、网格均值回归回测。

> 本版本已基于股票分析项目（daily_stock_analysis）的分析逻辑全面重构：
> 趋势七档判断、百分制评分、五档信号、买卖点位、多因子选股；
> 回测策略移植自 dividend_grid_strategy 的「均衡偏低均值线 + 不对称网格」。

## 快速开始

```bash
# 一键全流程：拉取数据 -> 技术分析 -> 复盘 -> 推荐 -> 回测
python run_review.py

# 可选参数
python run_review.py --top 15          # 推荐输出前15
python run_review.py --no-backtest     # 跳过回测（更快）
python run_review.py --offline         # 仅用缓存数据
  python run_review.py --mode metals     # 仅生成有色金属期货页面

# 本地查看
python -m http.server 8000
# 打开 http://localhost:8000/
```

## 自动化（GitHub Actions）

云端自动执行每日复盘/推荐并部署 GitHub Pages，无需本地定时任务：

- 周一至周五 **09:05**（北京）自动生成开盘推荐
- 周一至周五 **15:40**（北京）自动生成盘后复盘 + 网格回测 + 板块估值
- 也支持在 **Actions → Daily Stock Review → Run workflow** 手动触发

完整说明见 [docs/github-actions.md](docs/github-actions.md)。

## 页面入口（新版）

- [工作台首页](index.html) —— 四合一入口
- [每日复盘](review.html) —— 按板块分组、点击展开股票；信号一览、趋势评分、买卖点位、大盘温度计
- [每日推荐](recommend.html) —— 自选 TopN + 大盘 Top5 + 板块推荐（估值+动量）+ 网格策略操作提醒
- [持仓跟踪](holdings.html) —— 盘中实时跟踪 + 盘后复盘 + 网格策略操作提醒（配置见 holdings.json）
- [网格回测](backtest.html) —— 均衡偏低均值线 + 不对称网格回测
- [有色金属期货](metals.html) —— SHFE 主力连续合约（铜/铝/锌/铅/镍/锡）行情、走势与技术因子，点击展开各品种

## 持仓配置（holdings.json）

在项目根目录 `holdings.json` 维护持仓（代码/名称/成本价/股数），运行
`python run_review.py --mode holdings` 生成 `data/holdings_data.json`：

```json
{
  "holdings": [
    {"name": "长江电力", "code": "600900", "cost": 28.5, "shares": 1000},
    {"name": "中证A500ETF景顺", "code": "159353", "cost": 1.15, "shares": 10000},
    {"name": "中概互联网ETF易方达", "code": "513050", "cost": 1.2, "shares": 10000}
  ]
}
```

页面展示总市值/总成本/总盈亏、各持仓现价涨跌与盈亏、以及基于均值线偏离度的网格操作提醒。

## 架构

```
run_review.py            # 统一入口（拉数→分析→复盘→推荐→回测→持仓）
src/
├── indicators.py        # MA/MACD/RSI/布林带/乖离率/量比/ATR/ADX（纯Python）
├── analyzer.py          # 趋势七档 + 百分制评分 + 五档信号 + 买卖点位（含ATR止损）
├── screener.py          # 多因子选股（动量/价值[板块内分位]/流动性/活跃度/稳定性）
├── backtest.py          # 旧信号回测引擎（保留，不再默认使用）
├── grid_backtest.py     # 网格均值回归回测引擎（默认）：
│                        #   均值线(PE40分位/PB60分位/ROE60分位加权几何平均，
│                        #   无估值→价格锚) + 不对称网格(ATR动态步长1%~3%，
│                        #   涨抛1%/跌买1.04%) + 半永久锁仓(偏离≤-5%锁定下限,
│                        #   ≥+5%解锁；ADX闸门为默认关闭的实验开关) + 单边成本0.05%
├── grid_signal.py       # 网格策略操作信号（均值线偏离 → 加仓/减仓/清仓/持有）
├── holdings.py          # 持仓分析（实时行情 + 盈亏 + 网格提醒）
├── data_provider.py     # 腾讯行情快照 + 日K线(支持长历史翻页) + 本地缓存
├── stock_pool.py        # 自选池/大盘池/回测标的 + code→板块映射
├── futures.py           # 有色金属期货数据（新浪期货日线 SHFE 主力连续）+ 趋势/技术因子
└── report.py            # 汇总生成前端 JSON
assets/
├── css/common.css       # 统一设计语言（含板块分组/展开样式）
└── js/common.js         # RV.API 数据层 + RV.fmt 格式化 + RV.ui 组件
data/
├── review_data.json     # 复盘数据
├── recommend_data.json  # 推荐数据
├── backtest_data.json   # 网格回测数据
├── holdings_data.json   # 持仓数据
├── metals_data.json     # 有色金属期货行情/技术因子数据
└── cache/               # K线/估值缓存（不入库）
index.html / review.html / recommend.html / holdings.html / backtest.html / metals.html   # 数据驱动页面
```

## 分析逻辑口径

- **趋势**：MA5>MA10>MA20 为多头，分七档（强势多头→强势空头）
- **评分**：百分制 = 趋势30 + 乖离率20 + 量能15 + 支撑10 + MACD15 + RSI10
- **信号**：80+强烈买入 / 60+买入 / 40+观望 / 20+减仓 / <20卖出（含趋势过滤）
- **买卖点位**：理想买点=MA5、次优=MA10、止损=MA20、止盈=前高；另提供 **ATR止损**（收盘价−2×ATR14，波动自适应）
- **交易纪律**：乖离>5% 严禁追高；只做多头排列
- **RSI 口径（2026-08 修正）**：奖励强势（10分），超买给中性分，超卖不再高分——与趋势跟随系统一致

## 网格回测策略口径（2026-08 优化版，移植自 dividend_grid_strategy）

1. **均值线（均衡偏低估值锚）**：近 3 年（750 交易日）滚动窗口分位锚
   - PE-TTM 取 40 分位、股息率/ROE 取 60 分位，各自折算成公允价格锚
   - 复合均值线 = 加权几何平均（PE 0.5 / DY 0.3 / ROE 0.2，缺因子自动归一）
   - 偏离度 `dev = close / anchor - 1`；窗口样本 < 500 天视为不可用
   - 个股用东财 PE/PB 历史做估值锚（缓存带 TTL：空结果 6h 自动重试，
     不再因一次网络失败永久退化为价格锚）；无估值数据的 ETF 退化为价格均值线
2. **不对称网格仓位**：`pos = 0.70 − 斜率×dev`
   - 每上涨一格抛总资产 1%；每下跌一格买 1.04%（斜率不对称偏防守）
   - **ATR 动态步长（默认）**：步长 = 1.2×ATR14/收盘价（近 250 日中位数，
     夹在 1%~3%），替代固定 0.5%，大幅减少过度交易
   - 仓位下限 10%、上限 100%（不融资）
3. **半永久锁仓（真正生效）**：偏离 ≤ −5% 后的加仓累入锁仓下限，
   反弹时仓位不低于下限（不回吐）；偏离 ≥ +5% 解锁
4. **ADX 趋势闸门（实验开关，默认关闭）**：开启后 ADX>25 视为趋势市冻结调仓；
   回测显示在低波 ETF 上过度冻结拖累收益，故默认关闭
5. **回测口径**：交易触发 = dev 每跨越一个 grid_step 边界才调仓；收益 = 价格 + 日股息；
   单边成本 0.05%；基准 = 满仓买入持有（含股息）；指标含年化/波动/夏普/最大回撤/卡玛/超额
6. **优化前后回测对比 + 近半年窗口校验**：见 [docs/project-review-2026-08-06.md](docs/project-review-2026-08-06.md)

## 项目审查与优化报告

2026-08-06 对全项目做了代码审查与优化（网格策略、回测引擎、选股因子、复盘跟踪等），
含优化前后完整回测对比和近半年窗口校验结论：
[查看报告](docs/project-review-2026-08-06.md)（docs.html 策略说明页也有入口）

## 免责声明

本项目仅供学习和研究使用，不构成任何投资建议。股市有风险，投资需谨慎。
