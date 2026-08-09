---
description: 运行 run_review.py 生成数据。用法：/run review|recommend|institution|holdings|metals|all [--no-backtest]
agent: daily-review-dev
---

运行数据生成入口：

```
python3 run_review.py --mode $ARGUMENTS
```

mode 可选：review / recommend / institution / holdings / metals / tracking / all。
- review 较慢（含个股资金流 + 可选回测），可加 `--no-backtest`、`--no-fundflow` 加速。
- institution 会拉取全市场资金排行 + 龙虎榜 + 股东扫描（约 2-5 分钟）。
- 生成的数据文件（data/*.json）完成后按项目规范提交并推送 GitHub。
执行后汇报关键输出（温度/资金净流入/命中数等），并确认本地 http://127.0.0.1:8000 页面可访问。
