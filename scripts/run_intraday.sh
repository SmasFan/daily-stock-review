#!/bin/bash
# 盘中每 5 分钟：仅更新本地推荐数据，不提交/不推送。
# 原因：每 5 分钟 push 会触发 GitHub Pages 全站重新部署（部署窗口内页面/数据不可用，
#       且大量消耗 GitHub Actions 免费配额）。线上页面以盘前 09:05 / 盘后 15:40 快照为准。
H=$(date +%H%M)
if { [ "$H" -ge 930 ] && [ "$H" -le 1130 ]; } || { [ "$H" -ge 1300 ] && [ "$H" -le 1500 ]; }; then
  cd /mnt/c/Users/z7280/daily-stock-review
  python3 run_review.py --mode recommend --top 10 >> data/auto_run.log 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中推荐生成失败" >> data/auto_run.log
fi
