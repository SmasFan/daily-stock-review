#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实时模拟本地服务：静态文件 + 双池状态/重建 API。

  GET  /api/sim_state            → 双池概览 {pools:{six:{...},all:{...}}, version}
  POST /api/sim_plan {pool?:six|all} → 重建计划（省略 = 双池并行重建；--no-llm 快速）
替代 python3 -m http.server（原静态服务无 API）。
双池并行架构下无"切换"概念：six/all 常驻各自独立交易，仅计划重建入口。
"""
import functools
import http.server
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "data", "sim_live.json")
LOCK = os.path.join(ROOT, "data", "cache")
os.makedirs(LOCK, exist_ok=True)


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def pool_summary(pb):
    acc = pb.get("accounts") or {}
    plans = {k: len(a.get("plan") or []) for k, a in acc.items()}
    pos = {k: len(a.get("positions") or []) for k, a in acc.items()}
    cash = {k: round(a.get("cash") or 0, 0) for k, a in acc.items()}
    return {"plans": plans, "positions": pos, "cash": cash,
            "mix": len((pb.get("mix") or {}).get("equity_curve") or [])}


def sim_state():
    st = load_state() or {}
    meta = st.get("meta") or {}
    pools = st.get("pools") or {}
    return {
        "version": meta.get("strategy_version", "v?"),
        "pools": {p: pool_summary(pools.get(p, {})) for p in ("six", "all")},
        "accounts": meta.get("accounts", []),
        "updated": meta.get("created", ""),
    }


def run_plan(pool):
    cmd = [sys.executable, "sim_live.py", "--plan", "--no-llm"]
    if pool:
        cmd += ["--pool", pool]
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
        ok = r.returncode == 0
        tail = (r.stdout or "")[-1200:] + (r.stderr or "")[-500:]
        return {"ok": ok, "output": tail, "state": sim_state()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        super().end_headers()

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split('?')[0] == '/api/sim_state':
            return self._json(200, sim_state())
        return super().do_GET()

    def do_POST(self):
        if self.path.split('?')[0] != '/api/sim_plan':
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            body = {}
        pool = body.get("pool") or body.get("pool_mode")
        if pool not in ("six", "all", None):
            self._json(400, {"ok": False, "error": "pool 须为 six|all 或省略"})
            return
        self._json(200, run_plan(pool))


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    httpd = http.server.ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print('实时模拟服务(静态+API 双池并行): http://0.0.0.0:%d root=%s' % (port, ROOT), flush=True)
    httpd.serve_forever()
