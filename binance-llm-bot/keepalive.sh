#!/bin/bash
# cron 每分钟执行: 保 watchdog 存活 (单实例)
BASE=/mnt/c/Users/z7280/daily-stock-review/binance-llm-bot
LOCK="$BASE/watchdog.lock"

# watchdog 活着? (精确匹配, 排除自己)
if ! pgrep -f "$BASE/watchdog\.py" > /dev/null && ! pgrep -f "watchdog\.py" > /dev/null; then
    # 双保险: 检查锁文件进程是否真活着
    if [ -f "$LOCK" ]; then
        LPID=$(cat "$LOCK" 2>/dev/null)
        if kill -0 "$LPID" 2>/dev/null; then
            exit 0   # 锁持有者还活着但 pgrep 没找到(罕见), 不重复起
        fi
        rm -f "$LOCK"
    fi
    echo "$(date '+%F %T') watchdog 不在, 重启" >> "$BASE/watchdog_restart.log"
    cd "$BASE" && setsid nohup python3 "$BASE/watchdog.py" >> "$BASE/watchdog_stdout.log" 2>&1 < /dev/null &
fi
