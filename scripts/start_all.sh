#!/bin/bash
# WSL 启动自愈脚本（幂等，可重复执行）
# 用途: cron @reboot 开机拉起 + */5 分钟兜底 + 手动修复
# 模式: all(默认: serve+watchdog) | serve_only | watchdog_only
# 日志: data/startup.log
BASE_DSR=/mnt/c/Users/z7280/daily-stock-review
BASE_BN=/mnt/c/Users/z7280/binance-llm-bot
LOG=$BASE_DSR/data/startup.log
MODE="${1:-all}"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

# ---------- 1) 每日复盘静态服务 :8000 ----------
start_serve() {
  if ss -tln 2>/dev/null | grep -qE ':(8000)\s'; then
    return 0   # 已监听
  fi
  # 清残留进程（精确匹配脚本路径，排除自身）
  ps -eo pid,cmd | grep "daily-stock-review/scripts/serve\.py" | grep -v grep | awk '{print $1}' | xargs -r kill 2>/dev/null
  sleep 0.5
  cd "$BASE_DSR" && setsid nohup python3 scripts/serve.py >> http8000.log 2>&1 < /dev/null &
  sleep 1.5
  if ss -tln 2>/dev/null | grep -qE ':(8000)\s'; then
    log "serve.py :8000 拉起成功"
  else
    log "[warn] serve.py :8000 拉起失败"
  fi
}

# ---------- 2) binance watchdog（子进程全家由它拉起，keepalive.sh 每分钟亦兜底） ----------
start_watchdog() {
  if pgrep -f "binance-llm-bot/watchdog\.py" > /dev/null; then
    return 0
  fi
  # 锁文件持有者已死则清锁
  if [ -f "$BASE_BN/watchdog.lock" ]; then
    LPID=$(cat "$BASE_BN/watchdog.lock" 2>/dev/null)
    kill -0 "$LPID" 2>/dev/null || rm -f "$BASE_BN/watchdog.lock"
  fi
  cd "$BASE_BN" && setsid nohup python3 watchdog.py >> watchdog_stdout.log 2>&1 < /dev/null &
  sleep 1
  pgrep -f "binance-llm-bot/watchdog\.py" > /dev/null && log "watchdog 拉起成功" || log "[warn] watchdog 拉起失败"
}

case "$MODE" in
  serve_only)   start_serve ;;
  watchdog_only) start_watchdog ;;
  *)            start_serve; start_watchdog ;;
esac
