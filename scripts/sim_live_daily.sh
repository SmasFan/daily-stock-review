#!/bin/bash
# 实时模拟盘每日维护（收盘后 15:45 由 cron 调用；也可手动）
# 1) 用当日 review_data.json 结算昨日意向（按今日开盘价，A股100股整数）
# 2) 按当前策略版本生成新买卖意向（大盘防守/过热闸门/跳空规则）
# 3) 每日收盘总结 + 平仓后自动自我复盘（亏损共性/改进建议）
# 用法: bash scripts/sim_live_daily.sh        # 日更（auto_run 收盘后已含，勿重复）
#       python3 sim_live.py --review          # 仅复盘总结
#       python3 sim_live.py --init            # 初始化账本（首次）
#       python3 sim_live.py --strategy-log "理由"  # 记录策略调整并升版本
set -u
cd "$(dirname "$0")/.."
LOG="data/auto_run.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] sim_live 日更开始" >> "$LOG"
python3 sim_live.py >> "$LOG" 2>&1 || echo "[$(date '+%Y-%m-%d %H:%M:%S')] sim_live 失败" >> "$LOG"
# 提交账本
git add data/sim_live.json 2>/dev/null
if git diff --cached --quiet; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] sim_live 无变更" >> "$LOG"
else
  git commit -m "sim live $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG" 2>&1
  for i in 1 2 3; do
    git push origin main >> "$LOG" 2>&1 && break
    sleep 5
  done
fi
echo "[$(date '+%Y-%m-%d %H:%M:%S')] sim_live 完成" >> "$LOG"
