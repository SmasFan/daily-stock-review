# GitHub Actions 自动复盘使用文档

本工作流让 A 股复盘全流程（拉取数据 → 技术分析 → 复盘/推荐 → 网格回测 → 部署页面）在 GitHub 云端自动运行，无需本地定时任务。

## 1. 工作流文件

- `.github/workflows/daily-review.yml` —— 每日自动复盘 + 手动触发（主工作流）
- `.github/workflows/deploy-pages.yml` —— push 到 `main` 时部署 GitHub Pages（已有）

## 2. 触发方式

### 2.1 定时自动运行（schedule）

| 时间（北京时间） | 运行内容 | 说明 |
|---|---|---|
| 周一至周五 09:05 | `recommend` | 开盘前生成推荐，含当日买入原因 |
| 周一至周五 15:40 | `review` | 盘后复盘 + 网格回测 + 板块估值更新 |

> GitHub Actions 的 `schedule` 使用 UTC 时间，本工作流已在 cron 中换算（09:05 北京 = 01:05 UTC，15:40 北京 = 07:40 UTC）。
> 实际触发时间可能延迟数分钟（Actions 队列繁忙时），且仓库超过 60 天无活动时定时任务会暂停。

### 2.2 手动触发（workflow_dispatch）

1. 打开仓库 → **Actions** 页
2. 左侧选择 **Daily Stock Review**
3. 点击 **Run workflow**（绿色按钮）
4. 填写可选参数后运行：
   - **mode**：`all`（默认）/ `review` / `recommend`
   - **top**：推荐数量 TopN（默认 10）

![手动触发入口](https://docs.github.com/assets/cb-41342/images/help/actions/run-workflow-button.png)

## 3. 执行流程

每次运行，工作流依次执行：

1. **Checkout** 拉取仓库代码（含历史）
2. **Setup Python** 配置 Python 3.11（项目全部使用标准库，无需安装依赖）
3. **更新板块估值**：运行 `scripts/update_sector_valuation.py` 生成 `sector_valuation_data.js`（失败不阻断）
4. **运行复盘/推荐**：`python3 run_review.py --mode <mode> --top <n>` 生成
   - `data/review_data.json` —— 每日复盘
   - `data/recommend_data.json` —— 每日推荐
   - `data/backtest_data.json` —— 网格回测（`review` 模式）
5. **提交并推送**：用 `github-actions[bot]` 提交数据变更到 `main`（`data/cache` 与 `*.log` 不入库），带 3 次重试
6. **部署 GitHub Pages**：将全量页面 + 数据直接部署（GitHub 限制：bot 的 push 不会再次触发 `push` 工作流，所以这里自行部署）

## 4. 前置要求

### 4.1 一次性配置（首次）

**开启 GitHub Pages（Actions 方式）：**

1. 仓库 → **Settings** → **Pages**
2. **Source** 选择 **GitHub Actions**（不要选分支部署）
3. 保存

> 若选择"分支部署"（Deploy from a branch），本工作流的 `deploy-pages` 步骤会因环境未绑定而失败，此时只需让 `daily-review.yml` 完成步骤 1–5，部署仍由 `deploy-pages.yml` 在 push 后完成。

### 4.2 权限

工作流已在文件内声明所需权限（`contents: write`、`pages: write`、`id-token: write`），无需额外设置。默认 `GITHUB_TOKEN` 的 push 即可触发 `deploy-pages.yml` 的 Pages 部署与 `daily-review.yml` 的页面发布。

## 5. 查看结果

- **运行日志**：仓库 → **Actions** → **Daily Stock Review** → 点击某次运行查看各步骤输出
- **在线页面**：`https://<用户名>.github.io/daily-stock-review/`
  - 工作台首页 `index.html`（三合一入口）
  - 每日复盘 `review.html` · 每日推荐 `recommend.html` · 网格回测 `backtest.html`
- **本地预览**：`python -m http.server 8000` 后访问 `http://localhost:8000/`

## 6. 常见问题

### 6.1 定时任务没有运行？

- 检查仓库最近 60 天是否有活动（无活动 Actions 定时任务会暂停）
- 手动触发一次确认工作流正常，之后再观察 schedule

### 6.2 部署失败 / 页面 404？

- 确认 **Settings → Pages → Source = GitHub Actions**
- 若使用分支部署，删除 `daily-review.yml` 中的 `deploy-pages` 步骤，改为依赖 `deploy-pages.yml`
- 检查工作流日志中 `deploy-pages` 步骤的错误信息

### 6.3 数据源在 GitHub 拉取失败？

腾讯/东方财富接口在 GitHub 全球节点可用性通常稳定，但偶发限流。`run_review.py` 自带重试与缓存，板块估值失败不阻断主流程。若整轮失败，可手动触发重跑。

### 6.4 push 失败？

工作流内置 3 次重试。若仍失败，检查仓库分支保护规则是否阻止 `github-actions[bot]` 推送（需将 bot 加入白名单）。

## 7. 调整建议

- **改定时时间**：编辑 `daily-review.yml` 中 `schedule.cron`（UTC 时间 = 北京时间 - 8）
- **换 Python 版本**：修改 `actions/setup-python` 的 `python-version`
- **改推荐数量**：手动触发时填 `top`，或修改工作流中 `--top` 默认值

## 8. 免责声明

本项目仅供学习和研究使用，自动生成的内容不构成任何投资建议。股市有风险，投资需谨慎。
