#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部因子「每 3 小时一段」调度闸门的回归测试。

背景：scripts/external_cron.sh 是幂等的 —— 它先判断「当前 3 小时段是否已经抓过」，
已抓过就静默退出。正因为有这个判定，才能把它挂到已有 cron（*/30 的 start_all.sh）上，
实现「24 小时每 3 小时一次」而不必改 crontab。

这个判定写错的代价很大，而且很难在线上发现：
  · 判定过松 → 每 30 分钟抓一次（高频打行情接口，可能被限流）
  · 判定过严 → 永不抓取，页面一直挂着「外部因子已过期」，选股也用不上

因此这里把脚本里的抓取/git 调用打成桩，只保留分段判定逻辑，验证 4 个场景。
判定用的是 `date -d` + `stat -c`（GNU），所以非 Windows 的 bash 上会跳过。
"""
import glob
import os
import shutil
import subprocess
import tempfile
import time
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "scripts", "external_cron.sh")


def _find_bash():
    """找一个「真的能跑脚本的 bash」。

    注意不能直接用 shutil.which('bash')：Windows 上它会命中 System32\\bash.exe，
    那是 WSL 的启动器（不是 bash 本身，且通常被安全策略禁用）。
    WSL/Linux 上 /bin/bash 即真实 bash；Windows 上退回 Git 自带的 bash。
    """
    cands = [os.environ.get("EXT_TEST_BASH"), "/bin/bash", "/usr/bin/bash"]
    if os.name == "nt":
        for pat in (r"C:\Program Files\Git\bin\bash.exe",
                    r"C:\Program Files (x86)\Git\bin\bash.exe"):
            cands.append(pat)
        home = os.path.expanduser("~")
        cands += glob.glob(os.path.join(home, ".workbuddy", "binaries", "PortableGit",
                                        "versions", "*", "bin", "bash.exe"))
    for c in cands:
        if not c:
            continue
        if os.path.isabs(c) and os.path.exists(c):
            return c
        w = shutil.which(c)
        if w and os.path.basename(w).lower() != "bash.exe":
            return w
        if w and os.path.dirname(w).rstrip("\\/").lower().endswith("git\\bin"):
            return w
    return None


BASH = _find_bash()


def _fwd(p):
    """把 Windows 路径转成 bash 能懂的 /c/... 形式（POSIX 上原样返回）。"""
    p = p.replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        p = "/" + p[0].lower() + p[2:]
    return p


@unittest.skipUnless(BASH, "无 bash")
class TestExternalScheduleGate(unittest.TestCase):
    """验证 external_cron.sh 的「本段已抓过就跳过」闸门。"""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(SRC):
            raise unittest.SkipTest("external_cron.sh 不存在")
        cls.tmp = tempfile.mkdtemp(prefix="extschedule_")
        s = open(SRC, encoding="utf-8").read()

        # ---- 打桩：路径指向临时目录，抓取/git 全部换成 echo ----
        rep = [
            ("BASE=/mnt/c/Users/z7280/daily-stock-review", "BASE=" + _fwd(cls.tmp)),
            ("LOCK=/tmp/build_external.lock", "LOCK=" + _fwd(cls.tmp) + "/lock"),
            ("data/external_cron.log", "external_cron.log"),
            ("timeout 120 python3 build_external.py", "echo '[stub] FETCH build_external.py'"),
        ]
        for a, b in rep:
            s = s.replace(a, b)
        # git 相关整段打桩
        s = s.replace("git add ", "stub_git add ")
        s = s.replace("if git diff --cached --quiet -- data/external_data.json; then",
                      "if false; then")
        s = s.replace("git commit --only", "stub_git commit #")
        s = s.replace("git pull --rebase", "stub_git pull #")
        s = s.replace("git push -u origin main", "stub_git push #")
        s = s.replace("git fetch origin main", "stub_git fetch #")
        s = s.replace("git rev-list --count origin/main..HEAD", "echo 0")
        s = s.replace("git push origin main", "stub_git push #")
        # Windows 的 bash 没有 flock（WSL 有）；此处只测分段判定，把它置空
        s = s.replace('flock -w 10 9 || { echo "[$(date \'+%Y-%m-%d %H:%M:%S\')] 锁忙(>10s)，'
                      '跳过本轮" >> "$LOG"; exit 0; }', "true")

        os.makedirs(os.path.join(cls.tmp, "data"), exist_ok=True)
        cls.stub = os.path.join(cls.tmp, "stub.sh")
        open(cls.stub, "w", encoding="utf-8", newline="\n").write(s)
        cls.data = os.path.join(cls.tmp, "data", "external_data.json")
        cls.log = os.path.join(cls.tmp, "external_cron.log")

        # 当前时间所属的 3 小时段起点
        lt = time.localtime()
        cls.seg_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                     lt.tm_hour // 3 * 3, 0, 0, 0, 0, -1))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, mtime, force=False):
        """把数据文件时间设为 mtime，跑一次脚本；返回是否真的抓取了。"""
        open(self.data, "w").close()
        os.utime(self.data, (mtime, mtime))
        if os.path.exists(self.log):
            os.remove(self.log)
        env = dict(os.environ)
        env.pop("EXTERNAL_FORCE", None)
        if force:
            env["EXTERNAL_FORCE"] = "1"
        # encoding/errors 显式给死：Windows 上 bash 的 stderr 可能是 GBK，
        # 默认 utf-8 解码会在读取线程里抛 UnicodeDecodeError（把整轮测试炸掉）
        subprocess.run([BASH, _fwd(self.stub)], env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60)
        log = ""
        if os.path.exists(self.log):
            log = open(self.log, encoding="utf-8", errors="ignore").read()
        return "FETCH build_external.py" in log

    def test_fetch_when_previous_segment(self):
        """数据是上一段生成的 → 本段该抓。"""
        self.assertTrue(self._run(self.seg_start - 3600))

    def test_skip_when_current_segment(self):
        """本段已抓过 → 静默跳过（这是能挂到 */30 的前提）。"""
        self.assertFalse(self._run(self.seg_start + 60))

    def test_fetch_when_long_stale(self):
        """数据是两天前的（周末/停机后）→ 该抓。"""
        self.assertTrue(self._run(self.seg_start - 86400 * 2))

    def test_force_overrides_gate(self):
        """EXTERNAL_FORCE=1 → 跳过分段判定，强制抓一次。"""
        self.assertTrue(self._run(self.seg_start + 60, force=True))


if __name__ == "__main__":
    unittest.main()
