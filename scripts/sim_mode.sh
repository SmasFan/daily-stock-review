#!/bin/bash
# 实时模拟手动重建计划: ./sim_mode.sh
# 2026-11-10 起只有全池（all）；原 6 股精选池（six）已下线
cd "$(dirname "$0")/.."
MODE="${1:-all}"
if [ "$MODE" != "all" ]; then
  echo "用法: sim_mode.sh（只有全池；six 已停用）"; exit 1
fi
python3 sim_live.py --plan --no-llm >> data/auto_run.log 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 实时模拟重建计划 → 全池" >> data/auto_run.log
tail -5 data/auto_run.log
