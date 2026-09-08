#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sim_live 双池 · 盘中空转守卫（随 run_intraday.sh 每5分钟调用）

问题背景：收盘 auto_run 顺序 bug 曾导致"次日计划被误标过期"→ 次日盘中
无任何待触发单 = 空转（资金闲、不交易）。本守卫在交易时段巡检：
  1. 统计双池每账户 wait 待触发单数、是否有"可买空间"(现金够+未满仓)
  2. 若有账户 可买空间>0 但 该账户 wait==0 → 疑似空转
  3. 空转原因分类：
     a. 计划过期/缺失（asof 旧 or plan 空）→ 用最新 review_data 重建计划
     b. review_data 本身陈旧(非当日/上一交易日) → 先跑 run_review 刷新再重建
     c. 数据新鲜且计划已重建仍无单 → 今日无达标股（合法观望日，不算故障）
  4. 修复动作全部记录 data/sim_guard.log + 留时间戳供人工核对
无空转时仅更新时间戳（轻量，不刷屏）。
"""
import datetime
import json
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
STATE = os.path.join(DATA, "sim_live.json")
REVIEW = os.path.join(DATA, "review_data.json")
GUARD_LOG = os.path.join(DATA, "sim_guard.log")
STAMP = os.path.join(DATA, "sim_guard.stamp")
LOCK_STATE = os.path.join(DATA, ".sim_guard_cooldown")  # review 刷新冷却

REAL_ACCOUNTS = ["aggressive", "balanced", "disciplined"]
# 空转只看激进/稳健（门槛低，池有信号必出单）；纪律门槛76天然常空仓，不算空转
WATCH_KEYS = ["aggressive", "balanced"]
POOLS = ("six", "all")
# 单笔最低预算（激进0.22/稳健0.18/纪律0.14 × 5万）
MIN_CASH = {"aggressive": 4000, "balanced": 4000, "disciplined": 3000}
MAX_POS = {"aggressive": 5, "balanced": 5, "disciplined": 4}
# 冷却：review 刷新太慢，30 分钟内只自动刷一次
REVIEW_COOLDOWN = 1800


def log(msg, level="INFO"):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s][%s] %s" % (ts, level, msg)
    print(line)
    with open(GUARD_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def touch():
    with open(STAMP, "w") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S"))


def load(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def trading_now():
    """A股交易时段（含集合竞价后 9:30-11:30 / 13:00-15:00）。守卫只在盘中修。"""
    t = time.localtime()
    hhmm = t.tm_hour * 100 + t.tm_min
    wd = t.tm_wday
    if wd >= 5:
        return False
    return (930 <= hhmm <= 1130) or (1300 <= hhmm <= 1500)


def review_fresh(review):
    """review_data 是否为当日或上一交易日（盘中数据可能滞后1天）。"""
    gen = (review or {}).get("generatedAt", "")[:10]
    if not gen:
        return False
    today = time.strftime("%Y-%m-%d")
    if gen == today:
        return True
    # 上一交易日近似：昨天（跳过周末）
    d = datetime.date.today() - datetime.timedelta(days=1)
    for _ in range(3):
        if d.weekday() < 5:
            return gen == d.strftime("%Y-%m-%d")
        d -= datetime.timedelta(days=1)
    return False


def run(cmd, timeout=240):
    try:
        r = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or "")[-300:] + (r.stderr or "")[-200:]
    except Exception as e:
        return False, str(e)


def main():
    if not trading_now():
        touch()
        return  # 非交易时段不干预
    touch()
    st = load(STATE)
    review = load(REVIEW)
    if not st or not review:
        return
    # 统计空转
    idle_accounts = []
    for pool in POOLS:
        accs = (st.get("pools", {}).get(pool, {}) or {}).get("accounts", {})
        for key in WATCH_KEYS:
            a = accs.get(key) or {}
            cash = a.get("cash") or 0
            pos_n = len(a.get("positions") or [])
            wait = [p for p in (a.get("plan") or []) if p.get("status") == "wait"]
            can_buy = cash > MIN_CASH[key] and pos_n < MAX_POS[key]
            if can_buy and len(wait) == 0:
                idle_accounts.append("%s/%s" % (POOL_LABEL.get(pool, pool), key))
    if not idle_accounts:
        return  # 无空转
    gen = (review.get("generatedAt") or "")[:10]
    log("空转检测: 可买账户无待触发单 = %s ; review_data=%s" % (",".join(idle_accounts), gen), "WARN")
    if not review_fresh(review):
        # review 陈旧 → 冷却内先重建旧信号计划兜底，超冷却则刷新 review
        ctime = 0
        try:
            ctime = os.path.getmtime(LOCK_STATE)
        except Exception:
            pass
        if time.time() - ctime > REVIEW_COOLDOWN:
            log("review_data 陈旧(%s)，自动刷新 review 后重建计划..." % gen, "FIX")
            ok, out = run([sys.executable, "run_review.py", "--mode", "review", "--no-backtest"],
                          timeout=400)
            log("run_review: %s %s" % ("OK" if ok else "FAIL", out[-120:]), "INFO" if ok else "ERROR")
            try:
                with open(LOCK_STATE, "w") as f:
                    f.write("1")
            except Exception:
                pass
    log("自动重建双池计划(--plan no-llm)...", "FIX")
    ok, out = run([sys.executable, "sim_live.py", "--plan", "--no-llm"], timeout=300)
    if not ok:
        log("计划重建失败: %s" % out[-200:], "ERROR")
        return
    # 复核
    st2 = load(STATE)
    still = []
    if st2:
        for pool in POOLS:
            accs = (st2.get("pools", {}).get(pool, {}) or {}).get("accounts", {})
            for key in WATCH_KEYS:
                a = accs.get(key) or {}
                wait = [p for p in (a.get("plan") or []) if p.get("status") == "wait"]
                if (a.get("cash") or 0) > MIN_CASH[key] and len(a.get("positions") or []) < MAX_POS[key] \
                        and len(wait) == 0:
                    still.append("%s/%s" % (POOL_LABEL.get(pool, pool), key))
    if still:
        # 数据新鲜仍无单 = 今日无达标候选（闸门/分数不够），合法防守，不算故障
        log("重建后仍无待触发 %s → 今日无达标候选(闸门block或分数不足)，合法观望" % ",".join(still), "INFO")
    else:
        log("空转已修复：双池计划重建完成", "OK")


POOL_LABEL = {"six": "6股精选", "all": "全池"}


if __name__ == "__main__":
    main()
