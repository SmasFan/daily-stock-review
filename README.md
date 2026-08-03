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

- [工作台首页](index.html) —— 三合一入口
- [每日复盘](review.html) —— 按板块分组、点击展开股票；信号一览、趋势评分、买卖点位、大盘温度计
- [每日推荐](recommend.html) —— 自选 TopN + 大盘 Top5 + 板块推荐（估值+动量）
- [网格回测](backtest.html) —— 均衡偏低均值线 + 不对称网格回测

## 架构

```
run_review.py            # 统一入口（拉数→分析→复盘→推荐→回测）
src/
├── indicators.py        # MA/MACD/RSI/布林带/乖离率/量比（纯Python）
├── analyzer.py          # 趋势七档 + 百分制评分 + 五档信号 + 买卖点位
├── screener.py          # 多因子选股（动量/价值/流动性/活跃度/稳定性）
├── backtest.py          # 旧信号回测引擎（保留，不再默认使用）
├── grid_backtest.py     # 网格均值回归回测引擎（默认）：
│                        #   均值线(PE40分位/PB60分位/ROE60分位加权几何平均，
│                        #   无估值→价格锚) + 不对称网格(0.5%档,涨抛1%/跌买1.04%)
│                        #   + 半永久锁仓(偏离≤-5%锁定,≥+5%清仓) + 单边成本0.05%
├── data_provider.py     # 腾讯行情快照 + 日K线(支持长历史翻页) + 本地缓存
├── stock_pool.py        # 自选池/大盘池/回测标的 + code→板块映射
└── report.py            # 汇总生成前端 JSON
assets/
├── css/common.css       # 统一设计语言（含板块分组/展开样式）
└── js/common.js         # RV.API 数据层 + RV.fmt 格式化 + RV.ui 组件
data/
├── review_data.json     # 复盘数据
├── recommend_data.json  # 推荐数据
├── backtest_data.json   # 网格回测数据
└── cache/               # K线/估值缓存（不入库）
index.html / review.html / recommend.html / backtest.html   # 数据驱动页面
```

## 分析逻辑口径

- **趋势**：MA5>MA10>MA20 为多头，分七档（强势多头→强势空头）
- **评分**：百分制 = 趋势30 + 乖离率20 + 量能15 + 支撑10 + MACD15 + RSI10
- **信号**：80+强烈买入 / 60+买入 / 40+观望 / 20+减仓 / <20卖出（含趋势过滤）
- **买卖点位**：理想买点=MA5、次优=MA10、止损=MA20、止盈=前高
- **交易纪律**：乖离>5% 严禁追高；只做多头排列

## 网格回测策略口径（移植自 dividend_grid_strategy）

1. **均值线（均衡偏低估值锚）**：近 3 年（750 交易日）滚动窗口分位锚
   - PE-TTM 取 40 分位、股息率/ROE 取 60 分位，各自折算成公允价格锚
   - 复合均值线 = 加权几何平均（PE 0.5 / DY 0.3 / ROE 0.2，缺因子自动归一）
   - 偏离度 `dev = close / anchor - 1`；窗口样本 < 500 天视为不可用
   - 个股用东财 PE/PB 历史做估值锚；无估值数据的 ETF 退化为价格均值线
2. **不对称网格仓位**：`pos = 0.70 − 斜率×dev`
   - 每上涨 0.5% 抛总资产 1%（斜率 2.0）；每下跌 0.5% 买 1.04%（斜率 2.08）
   - 仓位下限 10%、上限 100%（不融资）
3. **半永久锁仓**：偏离 ≤ −5% 后的加仓锁仓，反弹不回吐；偏离 ≥ +5% 超涨清仓
4. **回测口径**：交易触发 = dev 每跨越一个 grid_step 边界才调仓；收益 = 价格 + 日股息；
   单边成本 0.05%；基准 = 满仓买入持有（含股息）；指标含年化/波动/夏普/最大回撤/卡玛/超额

## 旧版页面

保留的旧版静态页：
[完整回测报告](report.html) · [自选股](watchlist.html) · [工作台](dashboard.html)

## 免责声明

本项目仅供学习和研究使用，不构成任何投资建议。股市有风险，投资需谨慎。
