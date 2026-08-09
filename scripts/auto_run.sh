#!/bin/bash
# -*- coding: utf-8 -*-
# 每日复盘定时执行脚本：运行分析 -> 更新板块估值 -> 提交 -> 推送 GitHub（触发 Pages 部署）
# 用法: scripts/auto_run.sh [review|recommend|all]
set -e
cd "$(dirname "$0")/.."
MODE="${1:-all}"
LOG="data/auto_run.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始 mode=$MODE" >> "$LOG"

# 更新板块估值（涨跌幅/PE/PB，页面展示用；失败不阻断主流程）
if [ "$MODE" = "all" ] || [ "$MODE" = "review" ]; then
  python3 scripts/update_sector_valuation.py >> "$LOG" 2>&1 || echo "[warn] 板块估值更新失败" >> "$LOG"
fi

# 运行分析（股票分析需在交易时段；盘后复盘建议 15:30 后）
python3 run_review.py --mode "$MODE" --top 10 >> "$LOG" 2>&1 || {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 分析失败，跳过提交" >> "$LOG"
  exit 1
}

# 宏观政策与新闻情绪（利好/风险提醒，反馈到复盘/推荐页；失败不阻断主流程）
if [ "$MODE" = "all" ] || [ "$MODE" = "review" ] || [ "$MODE" = "recommend" ]; then
  python3 run_review.py --mode macro >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 宏观数据生成失败，沿用上次数据" >> "$LOG"
fi

# 提交并推送（数据 + 页面 + 资源 + workflow）
git add -A . ':!data/cache' ':!*.log' 2>/dev/null || true
if git diff --cached --quiet; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 无数据变更" >> "$LOG"
else
  git commit -m "auto update $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG" 2>&1 || echo "commit 跳过" >> "$LOG"
  # push 带重试（TLS/网络抖动）
  for i in 1 2 3; do
    git push -u origin main >> "$LOG" 2>&1 && { echo "[$(date '+%Y-%m-%d %H:%M:%S')] push 成功" >> "$LOG"; break; }
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] push 第${i}次失败，重试..." >> "$LOG"
    sleep 5
  done
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 完成" >> "$LOG"
