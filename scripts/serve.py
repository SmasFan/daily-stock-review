#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地静态服务器（禁止缓存，保证改动即时生效）。"""
import functools
import http.server
import os
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
# 项目根目录 = 本文件上级目录（修复旧版硬编码 /home/smas/... 在其他机器上不可用的问题）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()


def serve(port=8000):
    handler = functools.partial(NoCacheHandler, directory=ROOT)
    with http.server.ThreadingHTTPServer(('0.0.0.0', port), handler) as httpd:
        print(f'每日复盘本地服务: http://0.0.0.0:{port} (no-cache, root={ROOT})', flush=True)
        httpd.serve_forever()


if __name__ == '__main__':
    serve(PORT)
