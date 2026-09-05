#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实时模拟本地服务：静态文件 + 池模式切换 API。

  GET  /api/sim_state            → {pool_mode, switched_at, plans:{key:n}, 说明}
  POST /api/sim_mode {pool:six|all} → 切池 + 重建计划 → 同上
替代 python3 -m http.server（原静态服务无法切模式）。
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


def sim_state():
    st = load_state() or {}
    meta = st.get("meta") or {}
    out = {"pool_mode": meta.get("pool_mode", "all"),
           "switched_at": meta.get("pool_switched_at", "")}
    plans = {}
    for k, a in (st.get("accounts") or {}).items():
        plans[k] = len(a.get("plan") or [])
    out["plans"] = plans
    out["positions"] = {k: len(a.get("positions") or [])
                        for k, a in (st.get("accounts") or {}).items()}
    return out


def switch_mode(pool):
    if pool not in ("six", "all"):
        return {"ok": False, "error": "pool 须为 six|all"}
    try:
        r = subprocess.run([sys.executable, "sim_live.py", "--pool", pool, "--plan", "--no-llm"],
                           cwd=ROOT, capture_output=True, text=True, timeout=240)
        ok = r.returncode == 0
        tail = (r.stdout or "")[-800:] + (r.stderr or "")[-400:]
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
        if self.path.split('?')[0] != '/api/sim_mode':
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            body = {}
        pool = body.get("pool") or body.get("pool_mode")
        self._json(200, switch_mode(pool))


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    httpd = http.server.ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print('实时模拟服务(静态+API): http://0.0.0.0:%d root=%s' % (port, ROOT), flush=True)
    httpd.serve_forever()
