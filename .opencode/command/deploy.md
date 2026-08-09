---
description: 提交并推送全部改动到 GitHub（项目强制规范）。用法：/deploy [提交信息]
agent: daily-review-dev
---

按项目规范提交并推送：

1. `git status --short` 检查改动范围，确认没有意外文件（缓存/日志应被 .gitignore 排除）。
2. `git add -A`（数据文件 data/*.json 一并提交）。
3. `git commit -m "<提交信息>"`；信息用中文、简洁、描述改动要点，参照历史风格。
   若未提供提交信息，根据 git diff 自动生成。
4. `git push origin main`，确认推送成功（输出 "main -> main"）。
5. 汇报：提交 hash、改动文件数、线上 GitHub Pages 约 1-2 分钟后自动部署。
