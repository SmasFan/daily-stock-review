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

# 股票跟踪（推荐 Top10 持久化 + 收益/稳定榜，依赖当日 recommend 快照；盘后跑，失败不阻断主流程）
if [ "$MODE" = "all" ] || [ "$MODE" = "review" ]; then
  python3 run_review.py --mode tracking >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 跟踪数据生成失败，沿用上次数据" >> "$LOG"
fi

# 上升趋势页面数据（扫描自选池多头/强势多头；复用当日缓存，秒出）
if [ "$MODE" = "all" ] || [ "$MODE" = "review" ]; then
  python3 build_uptrend.py >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 上升趋势数据生成失败，沿用上次数据" >> "$LOG"
fi

# 市场温度 & 走势联动数据（温度历史 + 个股/板块走势）
if [ "$MODE" = "all" ] || [ "$MODE" = "review" ]; then
  python3 build_heatmap.py >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 市场温度数据生成失败，沿用上次数据" >> "$LOG"
fi

# 微信推送（Server酱）：收盘播报（复盘+资金+回测合并为 1 条，盘后；失败不阻断）
if [ "$MODE" = "all" ] || [ "$MODE" = "review" ]; then
  python3 scripts/push_alerts.py close >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 收盘播报推送失败" >> "$LOG"
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
