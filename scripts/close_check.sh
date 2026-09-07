#!/bin/bash
# 收盘结算复核（15:46 自动跑，结果写 sprint_review_report.txt）
sleep 2850   # 等到 ~15:46（当前 14:59 启动）
cd /mnt/c/Users/z7280/daily-stock-review
R=data/sprint_review_report.txt
{
  echo "== 收盘复核 $(date '+%F %T') =="
  echo "--- auto_run.log 尾 25:"
  tail -25 data/auto_run.log
  echo ""
  echo "--- 冲刺盘状态:"
  python3 sim_sprint.py --status 2>&1 | head -10
  echo ""
  echo "--- 冲刺净值曲线:"
  python3 -c "
import json
d=json.load(open('data/sim_sprint.json'))
for x in d.get('equity_curve',[]): print(' ',x.get('date'),'净值',x.get('equity'),'当日%',x.get('daily_return'),'持仓',x.get('pos'))
" 2>&1
  echo ""
  echo "--- 双池 sim_live 净值:"
  python3 -c "
import json
d=json.load(open('data/sim_live.json'))
for p in ('six','all'):
    print('['+p+']')
    for k,a in d['pools'][p]['accounts'].items():
        ec=a['equity_curve']
        if ec: print(' ',k,'净值',ec[-1]['equity'],'当日%',ec[-1].get('daily_return'),'持仓',len(a['positions']),'成交',len(a['trades']))
" 2>&1
} > "$R" 2>&1
echo "复核完成 → $R ($(date '+%T'))"
