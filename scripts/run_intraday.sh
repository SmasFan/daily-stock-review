#!/bin/bash
# 盘中任务（crontab */5 触发）：
# - 每 5 分钟：生成本地推荐+趋势数据
# - 每 10 分钟（MM%10==0）：跟踪数据 + 提交推送 GitHub
# - 每 30 分钟（MM%30==0）：资金（institution）+ 回测（含当天）
# - flock 防重叠：上次任务未完成时跳过本轮
# CACHE_MAX_AGE_HOURS=2：盘中 K 线缓存 2 小时过期，保证当天数据进分析。
export CACHE_MAX_AGE_HOURS=2
exec 9>/tmp/run_intraday.lock
flock -n 9 || { echo "[$(date '+%Y-%m-%d %H:%M:%S')] 上次盘中任务未完成，跳过本轮" >> /mnt/c/Users/z7280/daily-stock-review/data/auto_run.log; exit 0; }

H=$(date +%H%M)
MM=$((10#$(date +%M)))
if { [ "$H" -ge 930 ] && [ "$H" -le 1130 ]; } || { [ "$H" -ge 1300 ] && [ "$H" -le 1500 ]; }; then
  cd /mnt/c/Users/z7280/daily-stock-review
  python3 run_review.py --mode recommend --top 10 >> data/auto_run.log 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中推荐生成失败" >> data/auto_run.log

  # 趋势模块（上升趋势页面数据）：盘中同步更新到当天
  python3 build_uptrend.py >> data/auto_run.log 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中趋势数据生成失败" >> data/auto_run.log

  # 每 30 分钟：资金数据 + 回测 + 期货（含当天 K 线）
  if [ $((MM % 30)) -eq 0 ]; then
    python3 run_review.py --mode institution >> data/auto_run.log 2>&1 \
      || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中资金数据生成失败" >> data/auto_run.log
    python3 scripts/build_backtest.py >> data/auto_run.log 2>&1 \
      || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中回测生成失败" >> data/auto_run.log
    python3 run_review.py --mode metals >> data/auto_run.log 2>&1 \
      || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中期货数据生成失败" >> data/auto_run.log
  fi

  # 每 60 分钟（整点）：复盘分析（含当天盘中数据；回测由 build_backtest.py 单独跑）
  if [ $((MM % 60)) -eq 0 ]; then
    python3 run_review.py --mode review --no-backtest >> data/auto_run.log 2>&1 \
      || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中复盘生成失败" >> data/auto_run.log
  fi

  # 每 10 分钟推送一次（推送前先更新跟踪数据：当天快照+走势）
  if [ $((MM % 10)) -eq 0 ]; then
    python3 run_review.py --mode tracking >> data/auto_run.log 2>&1 \
      || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中跟踪数据生成失败" >> data/auto_run.log
    # 盘中不提交 tracking.db（二进制状态库，避免仓库膨胀；盘后 auto_run 统一提交）
    git add -A . ':!data/cache' ':!*.log' ':!data/tracking.db' 2>/dev/null || true
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
