#!/bin/bash
# 实时模拟盘每日维护（v4 双池并行）
# 收盘后运行（auto_run.sh 已含，此脚本可手动补跑）：
#   --plan 重建双池（6股+全池）各账户回踩买点计划
#   build_kline_export.py 导出个股K线（页面弹层绘图）
#   --review 收盘市值+复盘（双池）
# 盘中触发由 run_intraday.sh 每5分钟自动 --intraday（双池并行）。
set -u
cd "$(dirname "$0")/.."
LOG="data/auto_run.log"
D=$(date +%F)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] sim_live 收盘维护开始 $D" >> "$LOG"
python3 sim_live.py --plan >> "$LOG" 2>&1
python3 build_kline_export.py >> "$LOG" 2>&1
python3 sim_live.py --review --date "$D" >> "$LOG" 2>&1
git add data/sim_live.json data/kline 2>/dev/null
if git diff --cached --quiet; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] sim_live 无变更" >> "$LOG"
else
  git commit -m "sim live $D" >> "$LOG" 2>&1
  for i in 1 2 3; do git push origin main >> "$LOG" 2>&1 && break; sleep 5; done
fi
echo "[$(date '+%Y-%m-%d %H:%M:%S')] sim_live 完成" >> "$LOG"
