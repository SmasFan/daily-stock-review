#!/bin/bash
# 盘中任务（crontab */5 触发）：
# - 每 5 分钟：生成本地推荐+趋势数据
# - 每 10 分钟（MM%10==0）：跟踪数据 + 提交推送 GitHub
# - 每 30 分钟（MM%30==0）：资金（institution）+ 回测（含当天）
# - 每 60 分钟（MM%60==0 / 整点）：复盘分析；10/14 点推盘中播报
# - 12:00（午休）：只跑复盘 + 推「午间大盘分析」（大盘/自选/资金/宏观综合，单条）
# - flock 防重叠：上次任务未完成时跳过本轮
# CACHE_MAX_AGE_HOURS=2：盘中 K 线缓存 2 小时过期，保证当天数据进分析。
export CACHE_MAX_AGE_HOURS=2
exec 9>/tmp/run_intraday.lock
flock -n 9 || { echo "[$(date '+%Y-%m-%d %H:%M:%S')] 上次盘中任务未完成，跳过本轮" >> /mnt/c/Users/z7280/daily-stock-review/data/auto_run.log; exit 0; }

cd /mnt/c/Users/z7280/daily-stock-review
H=$(date +%H%M)
MM=$((10#$(date +%M)))
HOUR=$((10#$(date +%H)))

IS_NOON=$([ "$H" = "1200" ] && echo 1 || echo 0)
IN_TRADING=0
if { [ "$H" -ge 930 ] && [ "$H" -le 1130 ]; } || { [ "$H" -ge 1300 ] && [ "$H" -le 1500 ]; }; then
  IN_TRADING=1
fi

# ============ 午间 12:00 大盘分析推送（午休，仅整点一次） ============
if [ "$IS_NOON" = "1" ]; then
  # 复盘数据 11:00 已是最新盘中值；12:00 再刷一次拿到完整上午走势
  python3 run_review.py --mode review --no-backtest >> data/auto_run.log 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 午间复盘生成失败" >> data/auto_run.log
  python3 scripts/push_alerts.py market >> data/auto_run.log 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 午间大盘分析推送失败" >> data/auto_run.log
  exit 0
fi

# ============ 交易时段盘中任务 ============
if [ "$IN_TRADING" = "1" ]; then
  python3 run_review.py --mode recommend --top 10 >> data/auto_run.log 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中推荐生成失败" >> data/auto_run.log

  # 趋势模块（上升趋势页面数据）：盘中同步更新到当天
  python3 build_uptrend.py >> data/auto_run.log 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中趋势数据生成失败" >> data/auto_run.log

  # 实时模拟盘盘中巡检：现价触发买点/止损即成交（每5分钟，幂等）
  python3 sim_live.py --intraday >> data/auto_run.log 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 模拟盘盘中巡检失败" >> data/auto_run.log

  # 每 30 分钟：资金数据 + 回测 + 期货（含当天 K 线）
  if [ $((MM % 30)) -eq 0 ]; then
    python3 run_review.py --mode institution >> data/auto_run.log 2>&1 \
      || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中资金数据生成失败" >> data/auto_run.log
    python3 scripts/build_backtest.py >> data/auto_run.log 2>&1 \
      || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中回测生成失败" >> data/auto_run.log
    python3 run_review.py --mode metals >> data/auto_run.log 2>&1 \
      || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中期货数据生成失败" >> data/auto_run.log
    python3 build_mainline.py >> data/auto_run.log 2>&1 \
      || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中主线数据生成失败" >> data/auto_run.log
  fi

  # 每 60 分钟（整点）：复盘分析（含当天盘中数据；回测由 build_backtest.py 单独跑）
  if [ $((MM % 60)) -eq 0 ]; then
    python3 run_review.py --mode review --no-backtest >> data/auto_run.log 2>&1 \
      || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中复盘生成失败" >> data/auto_run.log
    # 微信推送（Server酱）：盘中播报（回测+推荐+资金合并 1 条；10:00/14:00，
    # 12:00 走上方午间块，加盘后复盘 1 条 = 每日 4 条，在免费版 5 条限额内）
    if [ "$HOUR" = "10" ] || [ "$HOUR" = "14" ]; then
      python3 scripts/push_alerts.py intraday >> data/auto_run.log 2>&1 \
        || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中播报推送失败" >> data/auto_run.log
    fi
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
