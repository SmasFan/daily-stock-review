#!/bin/bash
# 实时模拟 单池手动重建计划: ./sim_mode.sh six|all|both
# six = 6股精选(富联/长电/紫金/万华/宁波银行/雅戈尔)  all = 全池  both = 双池并行(默认)
cd "$(dirname "$0")/.."
MODE="${1:-both}"
case "$MODE" in
  six|all) POOL_ARG="--pool $MODE"; LABEL="$MODE";;
  both) POOL_ARG=""; LABEL="双池(six+all)";;
  *) echo "用法: sim_mode.sh six|all|both"; exit 1;;
esac
python3 sim_live.py --plan --no-llm $POOL_ARG >> data/auto_run.log 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 实时模拟重建计划 → $LABEL" >> data/auto_run.log
tail -5 data/auto_run.log
