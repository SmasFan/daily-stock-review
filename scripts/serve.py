#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地静态服务器（禁止缓存，保证改动即时生效）。"""
import functools
import http.server
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()


def serve(port=8000):
    handler = functools.partial(NoCacheHandler, directory='/home/smas/daily-stock-review')
    with http.server.ThreadingHTTPServer(('0.0.0.0', port), handler) as httpd:
        print(f'每日复盘本地服务: http://0.0.0.0:{port} (no-cache)', flush=True)
        httpd.serve_forever()


if __name__ == '__main__':
    serve(PORT)
