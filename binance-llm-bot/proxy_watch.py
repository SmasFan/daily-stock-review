#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""币安竞技场"假死"自动恢复监控。

背景（2026-11-19）：进程活着、watchdog 也不重启，但 ccxt 长连接缓存了中途失效的
Clash 节点，导致 demo-fapi 请求 502/503/451，行情接口连续失败熔断、决策失效、
状态页价格不更新。curl 单次测试节点是通的，但引擎持有的旧 session 已坏——重启即可恢复。

本脚本定时（LaunchAgent 每 3min）检查 arena.log 最近窗口：
  - 若连续失败 WARNING 达到阈值（说明引擎拿不到数据）→ 判定"假死"
  - 恢复动作：①通过 Clash unix socket 轮换到一个实测可用的节点 ②kill arena_engine
    让 watchdog 拉起新进程（重建 ccxt session）③写事件日志 + 可选推送
  - 冷却期（默认 10min）防止代理整体故障时反复切换/重启形成风暴

用法：
  python3 binance_arena_watch.py            # 检查一次，异常才动作
  python3 binance_arena_watch.py --dry       # 只检测+打印，不切节点/不重启
  python3 binance_arena_watch.py --status    # 打印当前诊断，不动作
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
ARENA_LOG = os.path.join(BASE, 'arena.log')
STATE = os.path.join(BASE, '.proxy_watch_state.json')
EVENT_LOG = os.path.join(BASE, 'proxy_watch.log')
CLASH_SOCK = os.environ.get('CLASH_SOCK', '/var/run/clash-verge-service/users/501/verge-mihomo.sock')
SELECTOR = os.environ.get('CLASH_SELECTOR', '一分机场')
PROXY_URL = os.environ.get('PROXY', 'http://127.0.0.1:7897')

# 检测阈值
# 关键：引擎有重试机制，偶发失败（502/503）会自愈，不能用失败条数判假死。
# 真正的"假死"= 长时间没有任何决策进展（[progress]/权益）且状态文件也不更新。
WINDOW_MIN = 15          # 观察最近 15 分钟
STALE_MIN = 20           # 日志无决策进展 且 状态文件 mtime 超过此值 → 假死
COOLDOWN_MIN = 10        # 两次恢复动作最小间隔

# 候选节点（按优先级；实测能过 demo-fapi klines 的）
CANDIDATES = [
    '🇯🇵AWS日本1号', '🇭🇰香港HKBN', '🇯🇵日本6号-0.1倍率',
    '🇯🇵日本专线6-0.1倍率', '🇺🇸美国凤凰城专线-0.1倍率',
    '🇸🇬新加坡专线', '🇭🇰香港家宽专线',
]


def log(msg):
    line = f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} {msg}'
    print(line)
    with open(EVENT_LOG, 'a') as f:
        f.write(line + '\n')


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    with open(STATE, 'w') as f:
        json.dump(s, f, ensure_ascii=False)


def curl_unix(path):
    try:
        r = subprocess.run(['curl', '-s', '--unix-socket', CLASH_SOCK,
                            'http://localhost' + path, '--max-time', '5'],
                           capture_output=True, text=True)
        return json.loads(r.stdout)
    except Exception:
        return {}


def get_current_node():
    d = curl_unix('/proxies/' + _q(SELECTOR))
    return d.get('now', '')


def set_node(name):
    import urllib.parse
    path = '/proxies/' + urllib.parse.quote(SELECTOR)
    try:
        subprocess.run(['curl', '-s', '-X', 'PUT', '--unix-socket', CLASH_SOCK,
                        'http://localhost' + path, '--data', json.dumps({'name': name}),
                        '--max-time', '5'], capture_output=True)
        return get_current_node() == name
    except Exception:
        return False


def _q(s):
    import urllib.parse
    return urllib.parse.quote(s)


def test_node(name):
    """切到节点并实测 demo-fapi 的 klines（ccxt 实际调用的接口）。"""
    if not set_node(name):
        return False
    time.sleep(1.2)
    url = 'https://demo-fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=1'
    try:
        r = subprocess.run(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
                            '-x', PROXY_URL, '--max-time', '12', url],
                           capture_output=True, text=True)
        return r.stdout.strip() == '200'
    except Exception:
        return False


def analyze_log():
    """扫描 arena.log 最近窗口，返回 (失败数, 决策进展是否新鲜, 最后活动, 距状态文件更新分钟)。

    判定核心看"决策进展"（[progress]/权益/平仓/开仓）而非失败条数：
    引擎有重试，偶发 502/503 会自愈；只有长时间完全没有进展且状态文件不更新才是假死。
    """
    if not os.path.exists(ARENA_LOG):
        return 0, True, None, 0
    cutoff = datetime.now() - timedelta(minutes=WINDOW_MIN)
    fails = 0
    last_progress = None
    last_fail = None
    ts_re = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
    with open(ARENA_LOG, 'r', errors='ignore') as f:
        for line in f:
            m = ts_re.match(line)
            if not m:
                continue
            try:
                t = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
            except Exception:
                continue
            if t < cutoff:
                continue
            if ('失败' in line or '熔断' in line or 'ERROR' in line or
                    'SSLError' in line or '451' in line):
                fails += 1
                last_fail = line.strip()[:160]
            # [progress] 行没有时间戳，用最近一条带时间戳的行近似
            if ('[progress]' in line or '权益' in line and '持仓' in line or
                    '平short' in line or '平long' in line or '开空' in line or '开多' in line):
                last_progress = t
    # 状态文件新鲜度
    status_file = os.path.join(os.path.dirname(BASE), 'data', 'binance_arena.json')
    status_age = 0
    try:
        status_age = (time.time() - os.path.getmtime(status_file)) / 60.0
    except Exception:
        pass
    fresh = (last_progress is not None and
             (datetime.now() - last_progress) < timedelta(minutes=STALE_MIN)) or status_age < STALE_MIN
    return fails, fresh, last_fail, status_age


def restart_engine():
    """kill arena_engine，watchdog 会在 1min 内拉起（重建 ccxt session）。
    成功 kill 返回 True；进程不存在返回 False（ watchdog 会处理）。"""
    try:
        r = subprocess.run(['pgrep', '-f', 'arena_engine.py'], capture_output=True, text=True)
        pids = [p for p in r.stdout.split() if p]
    except Exception:
        pids = []
    if not pids:
        log('  arena_engine 未运行，等待 watchdog 拉起')
        return False
    # 排除 watchdog 自身（它的 cmdline 含 arena_engine 字符串？不含脚本名，安全）
    for pid in pids:
        try:
            os.kill(int(pid), 15)
            log(f'  已 kill arena_engine PID {pid}（等待 watchdog 重建 session）')
        except Exception as e:
            log(f'  kill {pid} 失败: {e}')
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--status', action='store_true')
    args = ap.parse_args()

    fails, fresh, last_fail, status_age = analyze_log()
    cur = get_current_node()
    st = load_state()
    last_act = st.get('last_action')
    cooling = False
    if last_act:
        try:
            last_t = datetime.strptime(last_act, '%Y-%m-%d %H:%M:%S')
            cooling = (datetime.now() - last_t) < timedelta(minutes=COOLDOWN_MIN)
        except Exception:
            pass

    dead = not fresh  # 决策进展停止 + 状态文件陈旧 = 假死
    print(f'[诊断] 节点={cur} 窗口{WINDOW_MIN}min 失败={fails} 决策进展={"新鲜" if fresh else "停滞"} '
          f'状态文件{status_age:.0f}min前 判定={"假死" if dead else "正常"}')
    if last_fail:
        print(f'[最近失败] {last_fail}')
    if args.status:
        return 0
    if not dead:
        # 引擎已恢复正常 → 清零重启计数（下次假死从第 1 次重启开始）
        if st.get('restart_streak'):
            st['restart_streak'] = 0
            save_state(st)
        return 0
    if cooling:
        log(f'[跳过] 距上次恢复不足 {COOLDOWN_MIN}min，避免风暴')
        return 0
    if args.dry:
        log('[dry] 检测到假死，不动作')
        return 0

    log(f'[恢复] 检测到假死（失败{fails}条 状态文件{status_age:.0f}min未更新）')
    # 策略升级：先只重启引擎（长连接缓存是主因，节点本身通常没坏）。
    # 连续 N 次重启无效（restart_streak>=2）才轮换节点，避免把好节点切成坏的。
    streak = st.get('restart_streak', 0)
    good = None
    if streak >= 2:
        log('  已连续重启 %d 次无效，轮换节点' % streak)
        order = [n for n in CANDIDATES if n != cur] + [cur]
        for n in order:
            ok = test_node(n)
            print(f'  节点 {n}: {"OK" if ok else "FAIL"}')
            if ok:
                good = n
                break
        if good:
            log(f'  已切换节点 → {good}')
        else:
            log('  [警告] 所有候选节点都不可用，代理可能整体故障')
            if cur:
                set_node(cur)
        streak = 0  # 切节点后重置计数
    else:
        log(f'  第 {streak+1} 次重启引擎重建 ccxt session（节点 {cur} 保持不变）')
        streak += 1

    restart_engine()

    st['last_action'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    st['last_fails'] = fails
    st['last_node'] = good or cur
    st['restart_streak'] = streak
    save_state(st)
    log('[完成] 恢复动作已执行')
    return 0


if __name__ == '__main__':
    sys.exit(main())
