#!/usr/bin/env python3
"""supervisor: 监控 arena_engine + guard + status_page, 挂了拉起
用锁文件防多实例; 每次拉起记日志"""
import os, subprocess, sys, time

BASE = os.path.dirname(os.path.abspath(__file__))
LOCK = os.path.join(BASE, 'watchdog.lock')
LOG = os.path.join(BASE, 'watchdog.log')

PROCS = [
    ('arena_engine', ['python3', os.path.join(BASE, 'arena_engine.py')],
     os.path.join(BASE, 'arena.log')),
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
    """按 cmdline 匹配进程。macOS 自带 pgrep；万一没有则退回 ps -axo pid,command。"""
    try:
        r = subprocess.run(['pgrep', '-f', cmd], capture_output=True, text=True)
        return [p for p in r.stdout.split() if p]
    except FileNotFoundError:
        try:
            out = subprocess.check_output(['ps', '-axo', 'pid,command'],
                                          stderr=subprocess.DEVNULL).decode()
        except Exception:
            return []
        pids = []
        for line in out.splitlines()[1:]:
            if cmd not in line:
                continue
            try:
                pids.append(line.strip().split(None, 1)[0])
            except (ValueError, IndexError):
                continue
        return pids


def cmdline(pid):
    """取进程完整命令行：Linux 读 /proc，macOS/BSD 用 ps -p -o command=。"""
    if sys.platform.startswith('linux'):
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                return f.read().decode(errors='ignore')
        except Exception:
            return ''
    try:
        return subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='],
                                       stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ''


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
                cl = cmdline(pid)
                if name + '.py' in cl and 'watchdog' not in cl:
                    alive = True
                    break
            if not alive:
                log(f'[{name}] 未运行, 重启...')
                # 杀残留同类进程 (防重复)
                for pid in pids:
                    cl = cmdline(pid)
                    if name + '.py' in cl and 'watchdog' not in cl and pid != str(os.getpid()):
                        try:
                            os.kill(int(pid), 15)
                            log(f'[{name}] 杀残留 PID {pid}')
                        except (ProcessLookupError, PermissionError):
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
