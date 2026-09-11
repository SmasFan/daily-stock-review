#!/usr/bin/env python3
"""supervisor: 监控 trader100 + guard, 挂了拉起
用锁文件防多实例; 每次拉起记日志"""
import os, subprocess, sys, time

BASE = '/mnt/c/Users/z7280/daily-stock-review/binance-llm-bot'
LOCK = os.path.join(BASE, 'watchdog.lock')
LOG = os.path.join(BASE, 'watchdog.log')

PROCS = [
    ('trader100', ['python3', os.path.join(BASE, 'trader100.py')],
     os.path.join(BASE, 'run100.log')),
    ('trader_short', ['python3', os.path.join(BASE, 'trader_short.py')],
     os.path.join(BASE, 'run_short.log')),
    ('guard', ['python3', os.path.join(BASE, 'guard.py')],
     os.path.join(BASE, 'guard.log')),
    ('status_page', ['python3', os.path.join(BASE, 'status_page.py')],
     os.path.join(BASE, 'status_page_stdout.log')),
    ('review', ['python3', os.path.join(BASE, 'review.py'), 'auto'],
     os.path.join(BASE, 'review_stdout.log')),
]


def log(msg):
    line = f'{time.strftime("%Y-%m-%d %H:%M:%S")} {msg}\n'
    with open(LOG, 'a') as f:
        f.write(line)


def running(cmd):
    """按 cmdline 匹配进程"""
    r = subprocess.run(['pgrep', '-f', cmd], capture_output=True, text=True)
    pids = [p for p in r.stdout.split() if p]
    return pids


def main():
    # 锁: 防多个 watchdog 重复拉起
    if os.path.exists(LOCK):
        try:
            with open(LOCK) as f:
                old = int(f.read().strip())
            os.kill(old, 0)          # 存活则退出
            sys.exit(0)
        except (ProcessLookupError, ValueError):
            pass                      # 旧的死了, 接管
    with open(LOCK, 'w') as f:
        f.write(str(os.getpid()))

    while True:
        for name, cmd, outfile in PROCS:
            # 精确匹配脚本路径, 避免 pgrep 误匹配自己
            pat = f'{cmd[1]}'
            pids = running(pat)
            alive = False
            for pid in pids:
                try:
                    with open(f'/proc/{pid}/cmdline', 'rb') as f:
                        cl = f.read().decode(errors='ignore')
                    if name + '.py' in cl and 'watchdog' not in cl:
                        alive = True
                        break
                except Exception:
                    continue
            if not alive:
                log(f'[{name}] 未运行, 重启...')
                # 杀残留同类进程 (防重复)
                for pid in pids:
                    try:
                        with open(f'/proc/{pid}/cmdline', 'rb') as f:
                            cl = f.read().decode(errors='ignore')
                        if name + '.py' in cl and 'watchdog' not in cl and pid != str(os.getpid()):
                            os.kill(int(pid), 15)
                            log(f'[{name}] 杀残留 PID {pid}')
                    except Exception:
                        pass
                time.sleep(2)
                with open(outfile, 'a') as out:
                    subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT,
                                     cwd=BASE, start_new_session=True)
                log(f'[{name}] 已拉起')
            # else: log debug 太吵, 跳过
        time.sleep(60)


if __name__ == '__main__':
    main()
