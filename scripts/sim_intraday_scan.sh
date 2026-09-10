#!/bin/bash
# 实时模拟盘 · 盘中触发巡检（独立锁 + 独立日志，cron */2，交易时段）
#
# 为什么独立：run_intraday.sh 每 30 分钟要跑 review/资金/回测/期货/主线重块（10~25 分钟），
# 巡检排在它后面会被同一把锁挡掉（日志里的「锁忙(>90s)，跳过本轮」）→ 实际采样间隔 5~20 分钟，
# 盘中瞬时砸破买点容易漏。本脚本独立 flock + timeout，采样间隔回到 ~2 分钟。
#
# - sim_live.py --intraday ：双池巡检，现价 ≤ 买点×(1+0.2%容差) 即买，破止损/到止盈即卖，漏单留痕
# - sim_sprint.py --scan  ：一周冲刺盘扫止盈/止损
export CACHE_MAX_AGE_HOURS=2
BASE=/mnt/c/Users/z7280/daily-stock-review
LOCK=/tmp/sim_intraday.lock
LOG=$BASE/data/sim_intraday.log

# 交易时段：周一~周五 09:30-11:30 / 13:00-15:00
WD=$(date +%u); HHMM=$(date +%H%M)
[ "$WD" -gt 5 ] && exit 0
if ! { [ "$HHMM" -ge 0930 ] && [ "$HHMM" -le 1130 ]; } \
   && ! { [ "$HHMM" -ge 1300 ] && [ "$HHMM" -le 1500 ]; }; then
  exit 0
fi

exec 9>"$LOCK"
# flock 是内核锁，进程死了自动释放；这里只防同一时刻重入（最多等 20 秒）
flock -w 20 9 || { echo "[$(date '+%Y-%m-%d %H:%M:%S')] 巡检锁忙(>20s)，跳过本轮" >> "$LOG"; exit 0; }
cd "$BASE" || exit 0

# 单轮硬超时，防网络挂死；给子进程关掉 fd9，避免孤儿进程一直握着锁
timeout 240 python3 sim_live.py --intraday 9>&- >> "$LOG" 2>&1 \
  || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 盘中巡检失败/超时" >> "$LOG"
timeout 120 python3 sim_sprint.py --scan 9>&- >> "$LOG" 2>&1 \
  || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 冲刺盘巡检失败/超时" >> "$LOG"
