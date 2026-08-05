#!/bin/bash
H=$(date +%H%M)
if { [ "$H" -ge 930 ] && [ "$H" -le 1130 ]; } || { [ "$H" -ge 1300 ] && [ "$H" -le 1500 ]; }; then
  /bin/bash /mnt/c/Users/z7280/daily-stock-review/scripts/auto_run.sh recommend >> /mnt/c/Users/z7280/daily-stock-review/data/cron.log 2>&1
fi
