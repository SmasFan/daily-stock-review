#!/bin/bash
# 实时模拟 池模式一键切换: ./sim_mode.sh six|all
# six = 6股精选(富联/长电/紫金/万华/宁波银行/雅戈尔)  all = 全池
cd "$(dirname "$0")/.."
MODE="${1:-all}"
if [ "$MODE" != "six" ] && [ "$MODE" != "all" ]; then
  echo "用法: sim_mode.sh six|all"; exit 1
fi
python3 sim_live.py --pool "$MODE" >> data/auto_run.log 2>&1
# 切换后立即按新池重建计划（收盘信号 → 次日盘中触发）
python3 sim_live.py --plan >> data/auto_run.log 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 实时模拟池模式 → $MODE" >> data/auto_run.log
tail -3 data/auto_run.log
