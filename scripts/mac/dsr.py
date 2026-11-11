#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""macOS 定时任务入口（由 launchd 调用）。

替代原来的 cron + scripts/*.sh 组合：那些脚本依赖 Linux 才有的
flock / timeout / setsid / `stat -c` / `ss`，在 macOS 上跑不起来。
这里用 Python 复刻同样的任务序列：
  - 互斥锁   fcntl.flock（进程退出自动释放，不会留死锁）
  - 单步超时 subprocess.run(timeout=)，子进程独立 session，超时整组 kill
  - 幂等     外部因子按 3 小时段判断，重复调用不重复抓
  - 不推送   默认只生成本地数据；需要同步到 GitHub 时加 --push

用法：
    python3 scripts/mac/dsr.py recommend      # 09:05 盘前
    python3 scripts/mac/dsr.py review         # 15:40 盘后
    python3 scripts/mac/dsr.py intraday       # 交易时段每 5 分钟
    python3 scripts/mac/dsr.py simscan        # 交易时段每 2 分钟
    python3 scripts/mac/dsr.py external       # 每 3 小时外部因子
    python3 scripts/mac/dsr.py serve          # :8000 常驻静态服务
    python3 scripts/mac/dsr.py status         # 查看 launchd 与数据新鲜度
"""
import datetime
import fcntl
import os
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 统一 LLM 行为（2026-11-08 起按决策路由，见 config/llm_routing.json）：
#   宏观 → 云端 ds4.1 + 联网简报；短 JSON 决策 → 纯本地 group/local-code
#   agent 队列只做人工接管，不空等；付费 commandcode 云端默认关闭
DEFAULT_ENV = {
    "DSR_LLM_MODEL": os.environ.get("DSR_LLM_MODEL", "qwen3-coder:30b"),
    "DSR_LOCAL_MODEL": os.environ.get("DSR_LOCAL_MODEL", "qwen3-coder:30b"),
    "DSR_LLM_MAXTOK": os.environ.get("DSR_LLM_MAXTOK", "800"),
    "DSR_AGENT_WAIT": os.environ.get("DSR_AGENT_WAIT", "8"),
    "AGENT_LLM_WAIT": os.environ.get("AGENT_LLM_WAIT", "8"),
    "DSR_MACRO_AGENT": os.environ.get("DSR_MACRO_AGENT", "0"),
}
for _k, _v in DEFAULT_ENV.items():
    os.environ.setdefault(_k, _v)

LOG = os.path.join(ROOT, "data", "auto_run.log")
PY = sys.executable or "/usr/bin/python3"
LOCK_DIR = "/tmp"


# ---------------------------------------------------------------- 基础设施
def log(msg, echo=True):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    if echo:
        print(line, flush=True)


def acquire(name, wait=30):
    """非阻塞 flock 抢锁；最多等 wait 秒，抢不到返回 None（本轮跳过）。"""
    path = os.path.join(LOCK_DIR, "dsr_%s.lock" % name)
    fh = open(path, "w")
    deadline = time.time() + wait
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.write(str(os.getpid()))
            fh.flush()
            return fh
        except BlockingIOError:
            if time.time() >= deadline:
                fh.close()
                return None
            time.sleep(1)


def run(step, args, timeout, optional=False):
    """跑一条 python3 命令；返回 True/False。超时整组 kill，不拖垮后续步骤。"""
    cmd = [PY] + args
    started = time.time()
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=open(LOG, "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        start_new_session=True,  # 自成进程组，超时时能连子进程一起收掉
        env=dict(os.environ, PYTHONUNBUFFERED="1"),
    )
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            time.sleep(3)
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        log("  [超时 %ds] %s 已终止" % (timeout, step))
        code = -9
    except KeyboardInterrupt:
        proc.terminate()
        raise
    cost = time.time() - started
    if code == 0:
        log("  ✓ %s (%.0fs)" % (step, cost))
        return True
    level = "warn" if optional else "错误"
    log("  %s %s (exit=%s, %.0fs)" % ("[%s]" % level, step, code, cost))
    return False


def git_push(note):
    """把数据改动同步到 GitHub（可选，默认不执行）。"""
    cmds = [
        ["git", "add", "-A", ".", ":!data/cache", ":!*.log", ":!data/tracking.db"],
        ["git", "commit", "-m", "%s %s" % (note, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))],
        ["git", "push"],
    ]
    for c in cmds:
        r = subprocess.run(c, cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0 and c[1] != "commit":
            log("  [warn] %s 失败: %s" % (" ".join(c[:2]), (r.stderr or "").strip()[:200]))


def etf_cache_stale():
    """ETF 成分表 7 天刷新一次（原 auto_run.sh 用 stat -c %Y，这里用 mtime）。"""
    for name in ("data/etf_components.json",):
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            return True
        return time.time() - os.path.getmtime(path) > 7 * 86400
    return True


def now():
    return datetime.datetime.now()


def in_trading(dt):
    """A 股连续竞价：周一~周五 09:30-11:30 / 13:00-15:00。"""
    if dt.weekday() > 4:
        return False
    hm = dt.hour * 100 + dt.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


# ---------------------------------------------------------------- 任务集
def task_recommend(push=False):
    log("=== 盘前推荐 ===")
    run("行业估值", ["scripts/update_sector_valuation.py"], 600, optional=True)
    if etf_cache_stale():
        run("ETF 成分", ["scripts/etf_components.py"], 900, optional=True)
    run("推荐", ["run_review.py", "--mode", "recommend", "--top", "10"], 1800)
    run("宏观", ["run_review.py", "--mode", "macro"], 900, optional=True)
    run("外部因子", ["build_external.py"], 180)
    run("宏观LLM", ["macro_llm.py"], 600, optional=True)
    if push:
        git_push("recommend")


def task_review(push=False):
    log("=== 盘后复盘 ===")
    run("行业估值", ["scripts/update_sector_valuation.py"], 900, optional=True)
    if etf_cache_stale():
        run("ETF 成分", ["scripts/etf_components.py"], 900, optional=True)
    run("复盘", ["run_review.py", "--mode", "review", "--top", "10"], 3600)
    run("宏观", ["run_review.py", "--mode", "macro"], 900, optional=True)
    run("外部因子", ["build_external.py"], 180)
    run("宏观LLM", ["macro_llm.py"], 900, optional=True)
    run("跟踪", ["run_review.py", "--mode", "tracking"], 900)
    run("趋势页", ["build_uptrend.py"], 600, optional=True)
    run("热力图", ["build_heatmap.py"], 300, optional=True)
    run("微信推送", ["scripts/push_alerts.py", "close"], 300, optional=True)
    run("低估值", ["run_review.py", "--mode", "lowval"], 1200, optional=True)
    run("模拟盘计划", ["sim_live.py", "--plan"], 900, optional=True)
    run("K线导出", ["build_kline_export.py"], 900, optional=True)
    today = now().strftime("%Y-%m-%d")
    run("模拟盘复盘", ["sim_live.py", "--review", "--date", today], 600, optional=True)
    run("冲刺盘自动", ["sim_sprint.py", "--auto"], 600, optional=True)
    run("量化痕迹", ["scripts/quant_filter.py", "--pool"], 900, optional=True)
    run("外盘影子", ["scripts/ext_shadow.py"], 600, optional=True)
    run("冲刺盘复盘", ["sim_sprint.py", "--review", "--date", today], 600, optional=True)
    if push:
        git_push("daily review")


def task_intraday(push=False):
    dt = now()
    hm = dt.hour * 100 + dt.minute
    mm = dt.minute

    # 12:00 午间：刷一遍复盘 + 推「午间大盘分析」
    if hm == 1200:
        log("=== 午间大盘 ===")
        run("午间复盘", ["run_review.py", "--mode", "review", "--no-backtest"], 1800)
        run("午间推送", ["scripts/push_alerts.py", "market"], 300, optional=True)
        return

    if not in_trading(dt):
        return

    log("=== 盘中 %02d:%02d ===" % (dt.hour, dt.minute))
    run("盘中推荐", ["run_review.py", "--mode", "recommend", "--top", "10"], 1200)
    run("趋势页", ["build_uptrend.py"], 600, optional=True)
    run("模拟盘守卫", ["scripts/sim_live_guard.py"], 300, optional=True)

    if mm % 30 == 0:
        run("盘中复盘", ["run_review.py", "--mode", "review", "--no-backtest"], 1800)
        qf = os.path.join(ROOT, "data/quant_filter.json")
        if not os.path.exists(qf) or datetime.date.fromtimestamp(os.path.getmtime(qf)) != dt.date():
            run("量化痕迹补算", ["scripts/quant_filter.py", "--pool"], 400, optional=True)
        run("外部因子", ["build_external.py"], 90, optional=True)
        run("宏观LLM", ["macro_llm.py"], 240, optional=True)
        run("模拟盘重建", ["sim_live.py", "--intraday-plan"], 600, optional=True)
        run("资金", ["run_review.py", "--mode", "institution"], 900, optional=True)
        run("回测", ["scripts/build_backtest.py"], 900, optional=True)
        run("期货", ["run_review.py", "--mode", "metals"], 600, optional=True)
        run("主线", ["build_mainline.py"], 600, optional=True)
        run("产业", ["build_industry.py"], 600, optional=True)

    if mm == 0 and dt.hour in (10, 14):
        run("盘中播报", ["scripts/push_alerts.py", "intraday"], 300, optional=True)

    if mm % 10 == 0:
        run("跟踪", ["run_review.py", "--mode", "tracking"], 600)
        if push:
            git_push("intraday update")


def task_simscan():
    if not in_trading(now()):
        return
    run("模拟盘巡检", ["sim_live.py", "--intraday"], 240, optional=True)
    run("红利池巡检", ["sim_dividend.py", "--intraday"], 240, optional=True)
    run("冲刺盘巡检", ["sim_sprint.py", "--scan"], 120, optional=True)


def task_external(force=False):
    """幂等：当前 3 小时段内已抓过就直接退出。"""
    path = os.path.join(ROOT, "data/external_data.json")
    if not force and os.path.exists(path):
        dt = now()
        seg_start = dt.replace(hour=dt.hour // 3 * 3, minute=0, second=0, microsecond=0)
        if os.path.getmtime(path) >= seg_start.timestamp():
            return
    lock = acquire("external", wait=10)
    if lock is None:
        log("外部因子：锁忙，跳过")
        return
    try:
        if not force and os.path.exists(path):
            dt = now()
            seg_start = dt.replace(hour=dt.hour // 3 * 3, minute=0, second=0, microsecond=0)
            if os.path.getmtime(path) >= seg_start.timestamp():
                return
        run("外部因子", ["build_external.py"], 120, optional=True)
    finally:
        lock.close()


def task_ussim():
    """更新美股模拟盘账本和网页快照；交易信号由策略硬规则决定。"""
    run("美股模拟盘", ["scripts/us_sim.py"], 180)


def task_serve():
    os.execv(PY, [PY, os.path.join(ROOT, "scripts", "serve.py"), "8000"])


def task_status():
    print("项目: %s" % ROOT)
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    rows = [l for l in r.stdout.splitlines() if "dsr." in l]
    print("\nlaunchd 任务: %s" % (len(rows) or "无"))
    for l in rows:
        print("  " + " ".join(l.split()[:3]))
    print("\n数据新鲜度:")
    for f in ("review_data.json", "recommend_data.json", "tracking_data.json",
              "external_data.json", "sim_live.json", "us-sim.json"):
        p = os.path.join(ROOT, "data", f)
        if os.path.exists(p):
            age = (time.time() - os.path.getmtime(p)) / 3600
            print("  %-22s %s  (%.1f 小时前)" % (
                f, datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M"), age))
        else:
            print("  %-22s 缺失" % f)


# ---------------------------------------------------------------- 入口
def main():
    args = [a for a in sys.argv[1:]]
    action = args[0] if args else "status"
    push = "--push" in args

    if action == "status":
        return task_status()
    if action == "serve":
        return task_serve()

    lock = acquire(action, wait=90)
    if lock is None:
        log("%s：上一轮仍在运行，跳过本轮" % action)
        return
    try:
        if action == "recommend":
            task_recommend(push)
        elif action == "review":
            task_review(push)
        elif action == "intraday":
            task_intraday(push)
        elif action == "simscan":
            task_simscan()
        elif action == "external":
            lock.close()  # external 自带细粒度锁
            task_external(force="--force" in args)
            return
        elif action == "ussim":
            task_ussim()
        else:
            log("未知任务: %s" % action)
            return
    finally:
        try:
            lock.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
