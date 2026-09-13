#!/bin/bash
# 外部市场因子 · 24 小时每 3 小时抓取（独立锁 + 独立日志）
#
# 为什么独立：原来只在 auto_run.sh（工作日 09:05 / 15:40）与 run_intraday.sh
# （A 股交易时段）里顺带跑。周五 15:40 之后到周一 09:05 之间是 60+ 小时空窗，
# 而美股/汇率/原油/黄金/VIX 周末和夜里照样在动（美股周五收盘后还有期货盘、
# 中东事件常在周末发酵）→ 页面长期挂着「外部因子已过期」，选股也确实没采用。
#
# 抓取时刻：全天候，按「0/3/6/9/12/15/18/21 点」分 8 段，每段抓一次，与 A 股交易日无关。
#
# 本脚本是【幂等】的 —— 会先判断「当前 3 小时段是否已经抓过」：
#   已抓过 → 静默退出（几乎零开销）
#   未抓过 → 抓取 + 提交推送
# 因此它可以安全地被高频调用，两种装法都行（二选一或都装，重复调用不重复抓）：
#   A. crontab 直排：  7 */3 * * * /mnt/c/Users/z7280/daily-stock-review/scripts/external_cron.sh
#   B. 挂靠 start_all.sh（cron */30 已在跑，24/7 生效，无需改 crontab）—— 推荐
#      start_all.sh 每次调用本脚本，由本脚本自己判断该不该抓。
# 另有云端兜底 .github/workflows/external-factors.yml（同样每 3 小时），本机关机时接管。
# 两侧都只提交 data/external_data.json 且带 push 重试，同时跑不会冲突。
#
# 环境变量：
#   EXTERNAL_FORCE=1   跳过「本段已抓」判定，强制抓一次（手工刷新用）
#
# 幂等 & 安全：
#   - flock 防重叠（等 10 秒，抓不完就跳过本轮）
#   - timeout 120 防网络挂死
#   - 抓取失败沿用上次数据，不写坏文件
#   - 仅当 external_data.json 真的变了才提交；且只提交这一个文件，不碰别人的暂存区
export CACHE_MAX_AGE_HOURS=2
BASE=/mnt/c/Users/z7280/daily-stock-review
LOCK=/tmp/build_external.lock
LOG=$BASE/data/external_cron.log
DATA=$BASE/data/external_data.json
SEG_HOURS=3

# ---------- 1) 本 3 小时段是否已抓过（幂等判定，须在加锁前做，避免高频调用互相排队） ----------
if [ "${EXTERNAL_FORCE:-0}" != "1" ]; then
  H=$(date '+%H'); H=${H#0}; [ -z "$H" ] && H=0
  SEG_START=$(date -d "$(date '+%Y-%m-%d') $((H / SEG_HOURS * SEG_HOURS)):00:00" +%s 2>/dev/null || echo 0)
  MTIME=$(stat -c %Y "$DATA" 2>/dev/null || echo 0)
  if [ "$SEG_START" -gt 0 ] && [ "$MTIME" -ge "$SEG_START" ]; then
    exit 0          # 本段已抓过 → 静默跳过
  fi
fi

exec 9>"$LOCK"
flock -w 10 9 || { echo "[$(date '+%Y-%m-%d %H:%M:%S')] 锁忙(>10s)，跳过本轮" >> "$LOG"; exit 0; }

# 拿到锁后复查一次：可能刚被并发的那一轮抓过（双保险）
if [ "${EXTERNAL_FORCE:-0}" != "1" ]; then
  MTIME=$(stat -c %Y "$DATA" 2>/dev/null || echo 0)
  [ "$SEG_START" -gt 0 ] && [ "$MTIME" -ge "$SEG_START" ] && exit 0
fi

cd "$BASE" || exit 0

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始抓取外部因子" >> "$LOG"
timeout 120 python3 build_external.py >> "$LOG" 2>&1 \
  || { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [warn] 外部因子抓取失败，沿用上次数据" >> "$LOG"; exit 0; }

# ---------- 2) 顺手补推「本地已提交但没推上去」的积压 ----------
# 场景：在 Windows 侧（WorkBuddy）改过代码并提交，但 Windows 侧没有 GitHub 私钥，
# push 会被 Host key verification 挡下 → 提交一直躺在本地。WSL 侧有 key，
# 这里每天 8 次地检查并补推，保证云端 workflow 与 Pages 不会长期落后于本地。
push_backlog() {
  git fetch origin main --quiet 2>/dev/null || true
  local n
  n=$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)
  [ "${n:-0}" -gt 0 ] || return 0
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 发现 $n 个未推送提交，补推" >> "$LOG"
  for i in 1 2 3; do
    git push origin main >> "$LOG" 2>&1 && { echo "[$(date '+%Y-%m-%d %H:%M:%S')] 补推成功" >> "$LOG"; break; }
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 补推第${i}次失败，重试..." >> "$LOG"
    sleep 5
  done
}

# ---------- 3) 只在有实质变化时提交（页面数据来自 GitHub Pages，不推则远程看不到） ----------
git add data/external_data.json 2>/dev/null || true
if git diff --cached --quiet -- data/external_data.json; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 外部因子无变化" >> "$LOG"
  push_backlog
  exit 0
fi

# --only + 路径限定：只提交这一个文件，不把盘中脚本暂存的其它改动一起带走
for i in 1 2 3; do
  if git commit --only -m "external update $(date '+%Y-%m-%d %H:%M:%S')" -- data/external_data.json >> "$LOG" 2>&1; then
    break
  fi
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] commit 第${i}次失败(可能index.lock冲突)，重试..." >> "$LOG"
  sleep 3
done

for i in 1 2 3; do
  # 先对齐远端再推（盘中/云端脚本可能刚推过），失败不阻断，交给 push 重试
  git pull --rebase origin main >> "$LOG" 2>&1 || true
  git push -u origin main >> "$LOG" 2>&1 && { echo "[$(date '+%Y-%m-%d %H:%M:%S')] push 成功" >> "$LOG"; break; }
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] push 第${i}次失败，重试..." >> "$LOG"
  sleep 5
done
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 完成" >> "$LOG"
