#!/bin/bash
# 启动自愈脚本（幂等，可重复执行；WSL 与 macOS 均可跑）
# 用途: WSL cron / macOS launchd 兜底 + 手动修复
# 模式: all(默认: serve+watchdog+外部因子) | serve_only | watchdog_only | external_only
# 日志: data/startup.log（外部因子另有 data/external_cron.log）
#
# 第三个职责「外部市场因子」是 24/7 的：外盘不分工作日，靠本脚本每 30 分钟
# 碰一次 external_cron.sh（它自己按 3 小时分段判重）来保证夜里/周末也不断档。
BASE_DSR="$(cd "$(dirname "$0")/.." && pwd)"
BASE_BN="$BASE_DSR/binance-llm-bot"
# 脱离会话: Linux 用 setsid，macOS 无 setsid 就直接 nohup
if command -v setsid >/dev/null 2>&1; then SETSID="setsid"; else SETSID=""; fi
port_listen() {  # 端口是否已监听（Linux ss，macOS 用 python 探测）
  if command -v ss >/dev/null 2>&1; then
    ss -tln 2>/dev/null | grep -qE ":($1)\s"
  else
    python3 - "$1" <<'PY'
import socket, sys
s = socket.socket()
s.settimeout(1)
try:
    s.connect(('127.0.0.1', int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
  fi
}
LOG=$BASE_DSR/data/startup.log
MODE="${1:-all}"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

# ---------- 1) 每日复盘静态服务 :8000 ----------
start_serve() {
  if port_listen 8000; then
    return 0   # 已监听
  fi
  # 清残留进程（精确匹配脚本路径，排除自身）；不用 GNU 专有的 ps -eo pid,cmd / xargs -r
  local pids
  pids=$(ps -axo pid,command | grep "daily-stock-review/scripts/serve\.py" | grep -v grep | awk '{print $1}')
  [ -n "$pids" ] && kill $pids 2>/dev/null
  sleep 0.5
  cd "$BASE_DSR" && $SETSID nohup python3 scripts/serve.py >> http8000.log 2>&1 < /dev/null &
  sleep 1.5
  if port_listen 8000; then
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
  cd "$BASE_BN" && $SETSID nohup python3 "$BASE_BN/watchdog.py" >> watchdog_stdout.log 2>&1 < /dev/null &
  sleep 1
  pgrep -f "binance-llm-bot/watchdog\.py" > /dev/null && log "watchdog 拉起成功" || log "[warn] watchdog 拉起失败"
}

# ---------- 3) 外部市场因子（24/7，每 3 小时一段） ----------
# 外盘（原油/黄金/美债/美元/纳指/VIX）不分工作日，周末与夜里照样在动。
# 本函数把 external_cron.sh 挂到「已经在跑的 cron */30」上，实现全天候兜底：
#   external_cron.sh 自己是幂等的（先判断本 3 小时段是否已抓过），
#   所以每 30 分钟调一次几乎零开销，只有到段边界才真正抓。
# 等价于把 `7 */3 * * *` 写进 crontab，但不需要改 crontab（两者同装也不重复抓）。
refresh_external() {
  local cron="$BASE_DSR/scripts/external_cron.sh"
  [ -f "$cron" ] || return 0
  # 脱离会话 + 关 stdin：抓取要几秒，绝不能让 cron 那一轮卡住（macOS 无 setsid，直接 nohup）
  $SETSID nohup bash "$cron" >> "$BASE_DSR/data/external_cron.log" 2>&1 < /dev/null &
}

case "$MODE" in
  serve_only)    start_serve ;;
  watchdog_only) start_watchdog ;;
  external_only) refresh_external ;;
  *)             start_serve; start_watchdog; refresh_external ;;
esac
