#!/bin/bash
# 盘中任务（crontab */5 触发）：
# - 每 5 分钟：生成本地推荐+趋势数据
# - 每 10 分钟（MM%10==0）：跟踪数据 + 提交推送 GitHub
# - 每 30 分钟（MM%30==0）：全量复盘(review 含ETF成分) + 模拟盘盘中重建 + 资金 + 回测
#   注：盘中触发巡检（sim_live --intraday / sim_sprint --scan）已独立到 sim_intraday_scan.sh（cron */2）
# - 12:00（午休）：只跑复盘 + 推「午间大盘分析」（大盘/自选/资金/宏观综合，单条）
# - flock 防重叠：上次任务未完成时跳过本轮
#   2026-09 加固：等锁最长 90 秒（避免整天跳过）；锁龄 >35 分钟视为死锁，强制接管
# CACHE_MAX_AGE_HOURS=2：盘中 K 线缓存 2 小时过期，保证当天数据进分析。
export CACHE_MAX_AGE_HOURS=2
LOCK=/tmp/run_intraday.lock
LOG=/mnt/c/Users/z7280/daily-stock-review/data/auto_run.log

# 死锁检测：锁龄 >35 分钟 → 持锁进程视为卡死，SIGTERM + 清锁（正常任务不会超过 35 分钟）
if [ -f "$LOCK" ]; then
  LOCK_AGE=$(( $(date +%s) - $(stat -c %Y "$LOCK") ))
  if [ "$LOCK_AGE" -gt 2100 ]; then
    HOLDPID=$(fuser "$LOCK" 2>/dev/null | awk '{print $1}')
    if [ -n "$HOLDPID" ] && kill -0 "$HOLDPID" 2>/dev/null; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 检测到卡死任务(pid=$HOLDPID 锁龄${LOCK_AGE}s)，强制终止" >> "$LOG"
      kill "$HOLDPID" 2>/dev/null
      sleep 2
      kill -9 "$HOLDPID" 2>/dev/null
    fi
    rm -f "$LOCK"
  fi
fi

exec 9>"$LOCK"
# 等锁最长 90 秒（任务接近完成时顺延等待，避免整日跳过）；超时则放弃本轮
flock -w 90 9 || { echo "[$(date '+%Y-%m-%d %H:%M:%S')] 锁忙(>90s)，跳过本轮" >> "$LOG"; exit 0; }

cd /mnt/c/Users/z7280/daily-stock-review

# 整体超时：任务超 25 分钟自动终止（正常每轮 <10 分钟；防网络挂死拖垮整天）
cleanup() { [ -n "${TIMER_PID:-}" ] && kill $TIMER_PID 2>/dev/null; rm -f "$LOCK"; exit 0; }
trap cleanup EXIT
# 定时器子进程必须关掉 fd9 继承，否则主 shell 被杀后孤儿 sleep 会一直握住锁 fd
( exec 9>&-; sleep 1500 && echo "[$(date '+%Y-%m-%d %H:%M:%S')] 本轮超时25分钟，强制终止" >> "$LOG" &&   HOLD=$(fuser "$LOCK" 2>/dev/null | awk '{print $1}') && [ -n "$HOLD" ] && kill "$HOLD" 2>/dev/null && sleep 1 && kill -9 "$HOLD" 2>/dev/null; rm -f "$LOCK" ) &
TIMER_PID=$!

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

  # 实时模拟盘盘中巡检 / 一周冲刺盘巡检已解耦到 scripts/sim_intraday_scan.sh
  # （独立锁 + cron */2 交易时段），不再排在本重块后面被锁挡掉。

  # 空转守卫：盘中双池无待触发单(计划过期/未生成) → 自动重建修复（每5分钟）
  python3 scripts/sim_live_guard.py >> data/auto_run.log 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 模拟盘空转守卫失败" >> data/auto_run.log

  # 每 30 分钟：全量复盘(成分版) → 模拟盘盘中重建买点(v3.3) → 资金/回测/期货/主线
  if [ $((MM % 30)) -eq 0 ]; then
    python3 run_review.py --mode review --no-backtest >> data/auto_run.log 2>&1 \
      || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中复盘生成失败" >> data/auto_run.log
    # 外部市场因子刷新（外盘盘中在变；--intraday-plan 会重算选股偏好，须用最新）
    timeout 90 python3 build_external.py >> data/auto_run.log 2>&1 \
      || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 盘中外部因子刷新失败，沿用上次" >> data/auto_run.log
    # 宏观 LLM 消息面刷新（盘中新闻在变；--intraday-plan 会重算闸门，须用最新判断）
    timeout 240 python3 macro_llm.py >> data/auto_run.log 2>&1 \
      || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 盘中宏观LLM刷新失败，沿用上次" >> data/auto_run.log
    # 盘中复盘重建：用刚生成的全量成分版复盘刷新双池待触发买点/补新信号
    python3 sim_live.py --intraday-plan >> data/auto_run.log 2>&1 \
      || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 模拟盘盘中重建失败" >> data/auto_run.log
    python3 run_review.py --mode institution >> data/auto_run.log 2>&1 \
      || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中资金数据生成失败" >> data/auto_run.log
    python3 scripts/build_backtest.py >> data/auto_run.log 2>&1 \
      || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中回测生成失败" >> data/auto_run.log
    python3 run_review.py --mode metals >> data/auto_run.log 2>&1 \
      || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中期货数据生成失败" >> data/auto_run.log
    python3 build_mainline.py >> data/auto_run.log 2>&1 \
      || echo "[$(date '+%Y-%m-%d %H:%M:%S')] 盘中主线数据生成失败" >> data/auto_run.log
  fi

  # 每 60 分钟（整点）：盘中播报推送（复盘数据由每 30 分钟刷新提供）
  if [ $((MM % 60)) -eq 0 ]; then
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
