# GitHub Actions 自动复盘使用文档

本工作流让 A 股复盘全流程（拉取数据 → 技术分析 → 复盘/推荐 → 网格回测 → 部署页面）在 GitHub 云端自动运行，无需本地定时任务。

## 1. 工作流文件

- `.github/workflows/daily-review.yml` —— 每日自动复盘 + 手动触发（主工作流，schedule 已停用）
- `.github/workflows/external-factors.yml` —— 外部市场因子，**24 小时每 3 小时**抓取（仅此一个云端定时任务）
- `.github/workflows/deploy-pages.yml` —— push 到 `main` 时部署 GitHub Pages（已有）

## 2. 触发方式

### 2.1 定时自动运行（schedule）

| 时间（北京时间） | 运行内容 | 说明 |
|---|---|---|
| 周一至周五 09:05 | `recommend` | 开盘前生成推荐，含当日买入原因 |
| 周一至周五 15:40 | `review` | 盘后复盘 + 网格回测 + 板块估值更新 |

> `daily-review.yml` 的 schedule 已停用（云端跑会覆盖本地盘中数据），定时任务由本地 cron 负责。
> 唯一保留的云端定时任务是 `external-factors.yml`。

### 2.1.1 外部市场因子（每 3 小时）

| 时间（北京时间） | 运行内容 | 说明 |
|---|---|---|
| 每天 0/3/6/9/12/15/18/21 点 | `build_external.py` | 原油/黄金/白银/铜/美债/美元指数/纳指/VIX → 板块偏好分 |

- 与 A 股交易日**无关**：外盘 7×24 在动，周末与夜里同样要抓（原先只在本地工作日 09:05/15:40 顺带跑，周末空窗 60+ 小时）
- 只提交 `data/external_data.json` 一个文件，不会覆盖本地盘中产生的其它数据
- 抓取失败则沿用上次数据，绝不写坏文件
- 时间分段：`0/3/6/9/12/15/18/21` 点各一段，每段只抓一次

**本地侧（两条路，任选或都装，不会重复抓）**

| 方式 | 装法 | 说明 |
|---|---|---|
| A. 挂靠 `start_all.sh`（推荐，无需改 crontab） | 已在代码里 —— `scripts/start_all.sh` 每次运行都会调用 `scripts/external_cron.sh` | `start_all.sh` 的 cron 是 `*/30`（24 小时在跑），`external_cron.sh` 自己判断「本 3 小时段是否已抓过」，已抓过就静默退出（几乎零开销）→ 夜里/周末自动补上 |
| B. crontab 直排 | `crontab -e` 加一行 `7 */3 * * * /mnt/c/Users/z7280/daily-stock-review/scripts/external_cron.sh` | 与 A 等价；同装也不冲突（同一把 `flock` + 同一套分段判定） |

- 手工强制刷新（跳过分段判定）：`EXTERNAL_FORCE=1 bash scripts/external_cron.sh`
- 本地 `external_cron.sh` 还会顺手补推「已提交但未推送」的积压：在 Windows 侧改代码提交后（那边没有 GitHub 私钥，推不上去），WSL 侧会在下一段抓取时把提交一起推上去，避免云端 Pages 长期落后于本地。

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
