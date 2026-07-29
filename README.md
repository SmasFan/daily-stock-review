# 每日股票复盘页面

本仓库自动存放 A股 / 美股 每日复盘结果页面。

## 页面入口

- [A股盘后复盘](review_a_share.html)
- [美股盘前复盘](review_us_pre.html)
- [美股盘后复盘](review_us_post.html)
- [完整回测报告](report.html)

## 本地查看

```bash
python post_market_review.py --market a-share --type post
python post_market_review.py --market us --type pre
python post_market_review.py --market us --type post
node serve.js
```

然后打开 http://localhost:7584/review_a_share.html 等地址。

## 说明

- 复盘脚本 `post_market_review.py` 基于日线数据计算 MA20、布林带、RSI 等指标。
- 页面为纯静态 HTML，可直接通过 GitHub Pages 部署。
- 数据更新依赖 `report_data.json`，本地运行脚本前请确保数据已更新。
