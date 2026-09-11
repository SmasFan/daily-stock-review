#!/bin/bash
# -*- coding: utf-8 -*-
# 每日复盘定时执行脚本：运行分析 -> 更新板块估值 -> 提交 -> 推送 GitHub（触发 Pages 部署）
# 用法: scripts/auto_run.sh [review|recommend|all]
set -e
cd "$(dirname "$0")/.."
MODE="${1:-all}"
LOG="data/auto_run.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始 mode=$MODE" >> "$LOG"

# 更新板块估值（涨跌幅/PE/PB，页面展示用；失败不阻断主流程）
if [ "$MODE" = "all" ] || [ "$MODE" = "review" ]; then
  python3 scripts/update_sector_valuation.py >> "$LOG" 2>&1 || echo "[warn] 板块估值更新失败" >> "$LOG"
fi

# ETF 成分刷新（自选ETF前5重仓; 季报口径 周频足够; 供复盘把成分并入全池）——须在 run_review 前
if [ "$MODE" = "all" ] || [ "$MODE" = "review" ]; then
  ETF_CACHE=data/etf_components.json
  if [ ! -f "$ETF_CACHE" ] || [ $(( $(date +%s) - $(stat -c %Y "$ETF_CACHE") )) -gt 604800 ]; then
    python3 scripts/etf_components.py >> "$LOG" 2>&1 \
      || echo "[warn] ETF成分刷新失败，沿用上次缓存" >> "$LOG"
  fi
fi

# 运行分析（股票分析需在交易时段；盘后复盘建议 15:30 后）
python3 run_review.py --mode "$MODE" --top 10 >> "$LOG" 2>&1 || {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 分析失败，跳过提交" >> "$LOG"
  exit 1
}

# 宏观政策与新闻情绪（利好/风险提醒，反馈到复盘/推荐页；失败不阻断主流程）
if [ "$MODE" = "all" ] || [ "$MODE" = "review" ] || [ "$MODE" = "recommend" ]; then
  python3 run_review.py --mode macro >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 宏观数据生成失败，沿用上次数据" >> "$LOG"
fi

# 外部市场因子（build_external.py）：原油/黄金/白银/铜/美债收益率/美元/纳指/VIX → 板块偏好
# 供 sim_live 选股加分与门槛调整；必须在 sim_live --plan 之前
if [ "$MODE" = "all" ] || [ "$MODE" = "review" ] || [ "$MODE" = "recommend" ]; then
  timeout 90 python3 build_external.py >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 外部因子生成失败，沿用上次数据" >> "$LOG"
fi

# 宏观 LLM 消息面（macro_llm.py）：新闻 → LLM 多空判断 + 6股池逐股消息面
# 供 sim_live --plan 的宏观闸门（空头/防御→block）与个股回避；必须在 sim_live --plan 之前
if [ "$MODE" = "all" ] || [ "$MODE" = "review" ] || [ "$MODE" = "recommend" ]; then
  timeout 240 python3 macro_llm.py >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 宏观LLM消息面生成失败，沿用上次数据" >> "$LOG"
fi

# 股票跟踪（推荐 Top10 持久化 + 收益/稳定榜，依赖当日 recommend 快照；盘后跑，失败不阻断主流程）
if [ "$MODE" = "all" ] || [ "$MODE" = "review" ]; then
  python3 run_review.py --mode tracking >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 跟踪数据生成失败，沿用上次数据" >> "$LOG"
fi

# 上升趋势页面数据（扫描自选池多头/强势多头；复用当日缓存，秒出）
if [ "$MODE" = "all" ] || [ "$MODE" = "review" ]; then
  python3 build_uptrend.py >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 上升趋势数据生成失败，沿用上次数据" >> "$LOG"
fi

# 市场温度 & 走势联动数据（温度历史 + 个股/板块走势）
if [ "$MODE" = "all" ] || [ "$MODE" = "review" ]; then
  python3 build_heatmap.py >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 市场温度数据生成失败，沿用上次数据" >> "$LOG"
fi

# 微信推送（Server酱）：收盘播报（复盘+资金+回测合并为 1 条，盘后；失败不阻断）
if [ "$MODE" = "all" ] || [ "$MODE" = "review" ]; then
  python3 scripts/push_alerts.py close >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 收盘播报推送失败" >> "$LOG"
fi

# 低估值选股（好公司+低估值+横盘，全市场；约 5-10 分钟；失败不阻断主流程）
if [ "$MODE" = "all" ] || [ "$MODE" = "review" ]; then
  python3 run_review.py --mode lowval >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 低估值选股生成失败" >> "$LOG"
fi

# 实时模拟盘（v4 双池三账户盘中触发）：收盘后重建计划 + 导出个股K线 + 收盘复盘
# （盘中巡检由 scripts/sim_intraday_scan.sh 每2分钟独立锁执行）
if [ "$MODE" = "all" ] || [ "$MODE" = "review" ]; then
  python3 sim_live.py --plan >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 实时模拟盘计划失败" >> "$LOG"
  python3 build_kline_export.py >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 模拟盘K线导出失败" >> "$LOG"
  python3 sim_live.py --review --date $(date +%F) >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 实时模拟盘复盘失败" >> "$LOG"
  # 一周冲刺盘：自动选股建仓（此前只有人工 --buy，平仓后永久空转）
  # 过大盘闸门 + 消息面回避 + 外部强利空过滤 + LLM 成交前复核
  python3 sim_sprint.py --auto >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 冲刺盘自动建仓失败" >> "$LOG"
  # 量化痕迹识别（quant_filter.py）：60日窗口，每天算一次
  # 供 screen_pool 硬过滤（量化度≥75 剔除）+ sim_live 选股参考；
  # 必须早于任何使用 quant_filter.json 的环节
  timeout 400 python3 scripts/quant_filter.py --pool >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 量化痕迹识别失败，沿用上次数据" >> "$LOG"
  # 外部因子有效性统计（外部分档 vs 实际收益；供页面卡片）
  python3 scripts/ext_shadow.py >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 外部因子统计失败" >> "$LOG"
  python3 sim_sprint.py --review --date $(date +%F) >> "$LOG" 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 冲刺盘收盘失败" >> "$LOG"
fi

# 提交并推送（数据 + 页面 + 资源 + workflow）
git add -A . ':!data/cache' ':!*.log' 2>/dev/null || true
if git diff --cached --quiet; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 无数据变更" >> "$LOG"
else
  git commit -m "auto update $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG" 2>&1 || echo "commit 跳过" >> "$LOG"
  # push 带重试（TLS/网络抖动）
  for i in 1 2 3; do
    git push -u origin main >> "$LOG" 2>&1 && { echo "[$(date '+%Y-%m-%d %H:%M:%S')] push 成功" >> "$LOG"; break; }
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] push 第${i}次失败，重试..." >> "$LOG"
    sleep 5
  done
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 完成" >> "$LOG"
