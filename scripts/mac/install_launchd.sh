#!/bin/bash
# 安装/卸载 macOS launchd 定时任务（替代 WSL 上的 cron）
#
#   bash scripts/mac/install_launchd.sh install     # 安装并加载全部任务
#   bash scripts/mac/install_launchd.sh uninstall   # 停止并删除
#   bash scripts/mac/install_launchd.sh status      # 查看状态
#
# 与 cron 的差别：
#   - 电脑睡眠时 cron 直接丢过任务；launchd 的 StartCalendarInterval 会在唤醒后补跑一次
#   - launchd 崩溃自动重启（serve 用 KeepAlive），日志自动落文件
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AGENT="$HOME/Library/LaunchAgents"
LOGDIR="$ROOT/data/logs"
PY=/usr/bin/python3
ENTRY="$ROOT/scripts/mac/dsr.py"
PREFIX=com.smas.dsr

mkdir -p "$AGENT" "$LOGDIR"

plist_calendar() {  # $1=name  $2=hour  $3=minute
  local name=$1 hour=$2 minute=$3
  cat > "$AGENT/$PREFIX.$name.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$PREFIX.$name</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string><string>$ENTRY</string><string>$name</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>$hour</integer><key>Minute</key><integer>$minute</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>$hour</integer><key>Minute</key><integer>$minute</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>$hour</integer><key>Minute</key><integer>$minute</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>$hour</integer><key>Minute</key><integer>$minute</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>$hour</integer><key>Minute</key><integer>$minute</integer></dict>
  </array>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$LOGDIR/$name.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/$name.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
EOF
}

plist_interval() {  # $1=name  $2=seconds
  local name=$1 secs=$2
  cat > "$AGENT/$PREFIX.$name.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$PREFIX.$name</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string><string>$ENTRY</string><string>$name</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>StartInterval</key><integer>$secs</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$LOGDIR/$name.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/$name.log</string>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
</dict>
</plist>
EOF
}

plist_awake() {
  cat > "$AGENT/$PREFIX.awake.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$PREFIX.awake</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string><string>-s</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOGDIR/awake.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/awake.log</string>
</dict>
</plist>
EOF
}

plist_serve() {
  cat > "$AGENT/$PREFIX.serve.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$PREFIX.serve</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string><string>$ENTRY</string><string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOGDIR/serve.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/serve.log</string>
</dict>
</plist>
EOF
}

load_one() {
  local label=$1 file="$AGENT/$PREFIX.$1.plist"
  launchctl bootout "gui/$UID/$label" 2>/dev/null
  if launchctl bootstrap "gui/$UID" "$file" 2>/dev/null; then
    echo "  ✓ 已加载 $label"
  elif launchctl load -w "$file" 2>/dev/null; then
    echo "  ✓ 已加载 $label (load 回退)"
  else
    echo "  ✗ 加载失败 $label"
  fi
}

case "${1:-install}" in
  install)
    echo "生成 launchd 任务（项目根: ${ROOT}）"
    plist_calendar recommend 9 5
    plist_calendar review 15 40
    plist_interval intraday 300
    plist_interval simscan 120
    plist_interval ussim 300
    plist_interval external 10800
    plist_awake
    plist_serve
    # 原来的裸 http.server 交给 launchd 托管
    pkill -f "http.server 8000" 2>/dev/null
    for n in awake recommend review intraday simscan ussim external serve; do load_one "$n"; done
    echo
    echo "完成。日志: $LOGDIR/   统一任务日志: $ROOT/data/auto_run.log"
    echo "页面: http://127.0.0.1:8000/"
    ;;
  uninstall)
    for n in awake recommend review intraday simscan ussim external serve; do
      launchctl bootout "gui/$UID/$PREFIX.$n" 2>/dev/null || launchctl unload -w "$AGENT/$PREFIX.$n.plist" 2>/dev/null
      rm -f "$AGENT/$PREFIX.$n.plist"
      echo "  ✓ 已卸载 $PREFIX.$n"
    done
    ;;
  status)
    launchctl list | grep "dsr\." || echo "没有加载中的 dsr 任务"
    echo
    $PY "$ENTRY" status
    ;;
  *)
    echo "用法: $0 {install|uninstall|status}"; exit 1;;
esac
