#!/bin/bash
# -*- coding: utf-8 -*-
# 每日复盘定时执行脚本：运行分析 -> 提交 -> 推送 GitHub
# 用法: scripts/auto_run.sh [review|recommend]
set -e
cd "$(dirname "$0")/.."
MODE="${1:-all}"
LOG="data/auto_run.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始 mode=$MODE" >> "$LOG"

# 运行分析（股票分析需在交易时段；盘后复盘建议 15:30 后）
python3 run_review.py --mode "$MODE" --top 10 >> "$LOG" 2>&1 || {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 分析失败，跳过提交" >> "$LOG"
  exit 1
}

# 提交并推送（数据文件 + 页面）
git add data/*.json index.html review.html recommend.html backtest.html \
        assets/ src/ run_review.py README.md 2>/dev/null || true
git add -A data/ assets/ src/ run_review.py 2>/dev/null || true
if git diff --cached --quiet; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 无数据变更" >> "$LOG"
else
  git commit -m "auto update $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG" 2>&1 || echo "commit 跳过" >> "$LOG"
  git push >> "$LOG" 2>&1 || echo "push 失败（稍后重试）" >> "$LOG"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 完成" >> "$LOG"
