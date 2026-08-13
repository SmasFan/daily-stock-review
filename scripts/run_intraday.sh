#!/bin/bash
# 盘中任务：每 5 分钟生成本地推荐数据（crontab */5 触发）；
# 每 10 分钟（分钟数 % 10 == 0）提交并推送 GitHub（触发 Pages 部署，线上约 10 分钟级更新）。
# 盘中推送频率 10 分钟/次：避免每 5 分钟 push 导致的部署风暴与 Actions 配额耗尽。
H=$(date +%H%M)
if { [ "$H" -ge 930 ] && [ "$H" -le 1130 ]; } || { [ "$H" -ge 1300 ] && [ "$H" -le 1500 ]; }; then
  cd /mnt/c/Users/z7280/daily-stock-review
  python3 run_review.py --mode recommend --top 10 >> data/auto_run.log 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中推荐生成失败" >> data/auto_run.log

  # 每 10 分钟推送一次
  MM=$((10#$(date +%M)))
  if [ $((MM % 10)) -eq 0 ]; then
    git add -A . ':!data/cache' ':!*.log' 2>/dev/null || true
    if git diff --cached --quiet; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中无数据变更，跳过推送" >> data/auto_run.log
    else
      git commit -m "intraday update $(date '+%Y-%m-%d %H:%M:%S')" >> data/auto_run.log 2>&1 || echo "commit 跳过" >> data/auto_run.log
      for i in 1 2 3; do
        git push >> data/auto_run.log 2>&1 && { echo "[$(date '+%Y-%m-%d %H:%M:%S')] intraday push 成功" >> data/auto_run.log; break; }
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] intraday push 第${i}次失败，重试..." >> data/auto_run.log
        sleep 5
      done
    fi
  fi
fi
