#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地静态服务器：gzip 压缩 + ETag(304)，保证数据改动即时生效。

- `Cache-Control: no-cache`：浏览器每次都回源校验，数据一改立刻生效；
  未改动的文件返回 304，只传几十字节（不再重复下载 5MB+ 的 JSON）。
- 文本类资源按需 gzip（5.5MB 的 sim_trade_all.json → 约 560KB）。
- 内存缓存压缩结果，靠 mtime/size 失效。
"""
import functools
import gzip
import http.server
import mimetypes
import os
import sys
import threading

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
# 项目根目录 = 本文件上级目录（修复旧版硬编码 /home/smas/... 在其他机器上不可用的问题）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COMPRESS_EXT = {'.html', '.js', '.css', '.json', '.svg', '.txt', '.md', '.csv', '.map'}
TEXT_EXT = COMPRESS_EXT | {'.xml', '.yml', '.yaml'}
_cache = {}
_lock = threading.Lock()


class NoCacheGzipHandler(http.server.SimpleHTTPRequestHandler):
    def _load(self, path):
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
        with _lock:
            hit = _cache.get(path)
            if hit and hit[0] == stamp:
                return hit
        data = open(path, 'rb').read()
        ext = os.path.splitext(path)[1].lower()
        body = gzip.compress(data, 6) if ext in COMPRESS_EXT and len(data) > 1024 else None
        entry = (stamp, data, body, ext)
        with _lock:
            if len(_cache) > 500:
                _cache.clear()
            _cache[path] = entry
        return entry

    def _serve(self, with_body=True):
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            return super().do_HEAD() if not with_body else super().do_GET()
        try:
            stamp, data, gz, ext = self._load(path)
        except OSError:
            self.send_error(404, 'Not Found')
            return
        etag = '"%x-%x"' % stamp
        if self.headers.get('If-None-Match') == etag:
            self.send_response(304)
            self.send_header('ETag', etag)
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            return
        use_gz = gz is not None and 'gzip' in (self.headers.get('Accept-Encoding') or '')
        body = gz if use_gz else data
        ctype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
        if ext in TEXT_EXT and not ctype.startswith('text/'):
            ctype += '; charset=utf-8'
        elif ctype.startswith('text/'):
            ctype += '; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('ETag', etag)
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Vary', 'Accept-Encoding')
        if use_gz:
            self.send_header('Content-Encoding', 'gzip')
        self.end_headers()
        if with_body:
            self.wfile.write(body)

    def do_GET(self):
        self._serve(True)

    def do_HEAD(self):
        self._serve(False)

    def log_message(self, fmt, *args):
        # 静态资源请求太多，只记录非 200/304 的情况
        self.logs = getattr(self, 'logs', 0)


def serve(port=8000):
    handler = functools.partial(NoCacheGzipHandler, directory=ROOT)
    with http.server.ThreadingHTTPServer(('0.0.0.0', port), handler) as httpd:
        print(f'每日复盘本地服务: http://0.0.0.0:{port} (gzip + ETag, root={ROOT})', flush=True)
        httpd.serve_forever()


if __name__ == '__main__':
    serve(PORT)
