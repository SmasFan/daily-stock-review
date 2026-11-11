#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用本地 ollama 模型跑 macro_llm.py 的宏观消息面分析（强制纯本地初稿）。

2026-11-08 起默认路由是「宏观走云端 ds4.1 + 联网简报」，本脚本用于断网/隐私场景
强制把宏观也压回纯本地：设 DSR_LLM_LOCAL_ONLY=1，并把模型名转成 Magpie 能识别的
ollama/<model> 形式。联网简报（data/external_data.json + 国际要闻）依然会注入，
只是推理不出网。

与直接跑 macro_llm.py 的区别：
  1) 强制纯本地，--model 指定 ollama 模型（默认 qwen3-coder:30b）
  2) 本机 getaddrinfo 故障时用 nslookup 兜底解析，否则拉不到旧缓存

用法:
  python3 scripts/mac/macro_local.py                      # 30B，仅宏观
  python3 scripts/mac/macro_local.py --model qwen3.5:9b-local-q8 --pool all
"""
import argparse
import os
import socket
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def dns_fallback():
    """getaddrinfo 失败时用 nslookup 拿 A 记录（重启修复账户服务后可删掉）。"""
    orig = socket.getaddrinfo

    def patched(host, port, *a, **kw):
        try:
            return orig(host, port, *a, **kw)
        except OSError:
            ips = []
            try:
                out = subprocess.run(["nslookup", host], capture_output=True, text=True, timeout=8).stdout
                for line in out.splitlines():
                    line = line.strip()
                    if line.startswith("Address:") and "#" not in line:
                        ip = line.split(":", 1)[1].strip()
                        if ip and ip not in ips and ":" not in ip:
                            ips.append(ip)
            except Exception:
                pass
            if not ips:
                raise
            res = []
            for ip in ips:
                res += orig(ip, port, *a, **kw)
            return res

    socket.getaddrinfo = patched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3-coder:30b")
    ap.add_argument("--no-dns-fix", action="store_true")
    args, rest = ap.parse_known_args()

    if not args.no_dns_fix:
        dns_fallback()
    # 2026-11-08 路由落地后：默认宏观走云端 ds4.1，这里强制压回纯本地
    os.environ.setdefault("AGENT_LLM", "0")
    os.environ["DSR_LLM_LOCAL_ONLY"] = "1"
    os.environ["DSR_LOCAL_MODEL"] = args.model
    # Magpie 的模型 id 要带 ollama/ 前缀；纯裸名会 404
    magpie_model = args.model if args.model.startswith(("ollama/", "group/")) else "ollama/" + args.model
    os.environ["DSR_MACRO_MODEL"] = magpie_model
    os.environ["DSR_DECISION_MODEL"] = magpie_model
    os.environ["DSR_LLM_MODEL"] = args.model  # _call_ollama 直连兜底仍在用
    os.environ.setdefault("DSR_LLM_STREAM", "1")
    os.chdir(BASE)
    sys.path.insert(0, BASE)

    import macro_llm

    print("模型: %s  后端: 强制纯本地（Magpie %s）" % (args.model, magpie_model))
    sys.argv = ["macro_llm.py"] + [a for a in rest if a != "--"]
    macro_llm.main()


if __name__ == "__main__":
    main()
