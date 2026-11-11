#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一 LLM 决策路由（2026-11-08 本地 vs 云端 ds4.1 对比结论落地）。

对比实测（3 张卷 × 3 模型，9 次全部 JSON 合规）：
  - 交易类短 JSON（gate / bn_gate / candidates / stock_news / bn_review）
    本地 27B 与云端 ds4.1 结论一致（gate 7/7 allow、币安 hold 0 仓位），
    且短 JSON 不需要长上下文推理 → **固定走纯本地模型，不出网**。
  - 宏观（macro）长上下文 + 联网简报：本地 27B 偏乐观（中性 55/均衡、
    仅 2 个板块、把 11/5 纳指大跌误写成 11/3），云端 ds4.1 给防御 42/防守、
    5 个板块并引用 678涨4633跌 / 创业板 -3.96% / PMI 49.3 → **宏观走云端 ds4.1**，
    本地只在云端不可用时做「初稿」并显式标记。
  - 耗时：ds4.1 2.6~5.2s；本地 27B 33.6~63.6s（盘中 30s 闸门等不起），
    因此短决策本地默认用 qwen3-coder:30b（69 tok/s）而不是 27B。

通道：全部经本机 Magpie OpenAI 兼容端点 http://127.0.0.1:3425/v1
      （apiKey=magpie；group/* 纯本地，workbuddy/* 云端）。
      不依赖 Pi 进程，也不读 .env；launchd / nohup 起的子进程都能直接用。
降级：Magpie 不可达 → 本地直连 ollama（纯本地）；云端宏观失败 → 本地初稿。

用法：
    from llm_router import chat, route_of
    content, backend = chat("macro", system, user, expect_json=True)
    content, backend = chat("bn_gate", system, user, expect_json=True)
"""
import json
import os
import sys
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE_DIR, "config", "llm_routing.json")
LOG = os.path.join(BASE_DIR, "data", "llm_route_log.jsonl")

MAGPIE_URL = os.environ.get("DSR_MAGPIE_URL", "http://127.0.0.1:3425/v1")
MAGPIE_KEY = os.environ.get("DSR_MAGPIE_KEY", "magpie")
OLLAMA_URL = os.environ.get("DSR_OLLAMA", "http://localhost:11434")

# 内置默认（config/llm_routing.json 可覆盖；环境变量优先级最高）
DEFAULT_ROUTES = {
    "macro": {
        "model": "workbuddy/deepseek-v4.1-flash",
        "tier": "cloud", "web": True, "timeout": 60, "max_tokens": 1400,
        "fallback": "local", "label": "云端 ds4.1",
    },
    "gate": {
        "model": "group/local-code",
        "tier": "local", "web": False, "timeout": 45, "max_tokens": 900,
        "fallback": "ollama", "label": "本地 qwen3-coder",
    },
    "bn_gate": {
        "model": "group/local-code",
        "tier": "local", "web": False, "timeout": 30, "max_tokens": 900,
        "fallback": "ollama", "label": "本地 qwen3-coder",
    },
    "candidates": {
        "model": "group/local-code",
        "tier": "local", "web": False, "timeout": 90, "max_tokens": 1400,
        "fallback": "ollama", "label": "本地 qwen3-coder",
    },
    "stock_news": {
        "model": "group/local-code",
        "tier": "local", "web": False, "timeout": 90, "max_tokens": 1200,
        "fallback": "ollama", "label": "本地 qwen3-coder",
    },
    "bn_review": {
        "model": "group/local-code",
        "tier": "local", "web": False, "timeout": 120, "max_tokens": 1800,
        "fallback": "ollama", "label": "本地 qwen3-coder",
    },
}

# 环境变量覆盖（便于临时切模型，不改文件）
ENV_MODEL = {
    "macro": "DSR_MACRO_MODEL",
    "gate": "DSR_DECISION_MODEL",
    "bn_gate": "DSR_DECISION_MODEL",
    "candidates": "DSR_DECISION_MODEL",
    "stock_news": "DSR_DECISION_MODEL",
    "bn_review": "DSR_DECISION_MODEL",
}
# 强制纯本地：设 1 后连宏观都不出网（隐私/断网场景）
LOCAL_ONLY = os.environ.get("DSR_LLM_LOCAL_ONLY", "0").lower() in ("1", "true", "yes", "on")


def _load_config():
    routes = {k: dict(v) for k, v in DEFAULT_ROUTES.items()}
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in (cfg.get("routes") or {}).items():
            if k in routes and isinstance(v, dict):
                routes[k].update(v)
    except (OSError, ValueError):
        pass
    for kind, env in ENV_MODEL.items():
        val = os.environ.get(env)
        if val:
            routes[kind]["model"] = val
            routes[kind]["tier"] = "local" if ("group/" in val or "ollama/" in val or "/" not in val) else "cloud"
    if LOCAL_ONLY:
        for k in routes:
            routes[k]["tier"] = "local"
            # ENV_MODEL 覆盖过（如 macro_local.py 传 ollama/...）就不再替换，但仍统一标签
            if routes[k]["model"].startswith("workbuddy/"):
                routes[k]["model"] = os.environ.get("DSR_LOCAL_MODEL", "group/local-all")
            routes[k]["label"] = "本地初稿（强制纯本地）"
    return routes


def route_of(kind):
    """返回该决策类型的路由配置（未知类型按本地短决策处理，绝不出网）。"""
    r = _load_config().get(kind)
    if r is None:
        r = dict(DEFAULT_ROUTES["gate"])
        r["label"] = "本地（默认）"
    return r


def _log(kind, model, served, backend, latency, ok, err=""):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind,
               "model": model, "served": served, "backend": backend,
               "latency_s": round(latency, 2), "ok": bool(ok), "err": err[:160]}
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _chat_magpie(model, system, user, timeout, max_tokens):
    body = json.dumps({
        "model": model, "stream": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.2, "max_tokens": max_tokens,
        "reasoning_effort": "none",   # 本地组 thinking off，避免 content 被 reasoning 吃空
    }).encode()
    req = urllib.request.Request(MAGPIE_URL + "/chat/completions", data=body, headers={
        "Authorization": "Bearer " + MAGPIE_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
        served = r.headers.get("X-Magpie-Model") or r.headers.get("x-magpie-model") or model
    content = (d["choices"][0]["message"]["content"] or "").strip()
    if not content:
        raise RuntimeError("空 content（finish_reason=%s）" % d["choices"][0].get("finish_reason"))
    return content, served


def _chat_ollama(system, user, timeout, max_tokens):
    model = os.environ.get("DSR_LOCAL_MODEL", "qwen3-coder:30b")
    body = json.dumps({
        "model": model, "stream": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "format": "json",
        "options": {"num_predict": max_tokens, "temperature": 0.2, "num_ctx": 16384},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    content = (d.get("message") or {}).get("content") or ""
    if not content.strip():
        raise RuntimeError("ollama 空 content")
    return content, "ollama/" + model


def chat(kind, system, user, expect_json=True, timeout=None, meta=None):
    """按决策类型路由一次 LLM 调用，返回 (content, backend)。

    backend 形如：
      "workbuddy/deepseek-v4.1-flash"（宏观正选）
      "group/local-code → ollama/qwen3-coder:30b"（短决策正选）
      "ollama/qwen3-coder:30b (fallback)"（Magpie 挂了，直连本地）
      "local-draft: ..."（宏观云端失败后的本地初稿）
    expect_json=True 时会校验返回能抠出 JSON（容错 ```json 包裹）。
    """
    r = route_of(kind)
    tmo = timeout or r["timeout"]
    maxtok = r["max_tokens"]
    model = r["model"]
    t0 = time.time()
    errs = []

    def _ok(c):
        if not c or not c.strip():
            return False
        if not expect_json:
            return True
        try:
            json_of(c)
            return True
        except Exception as e:
            errs.append("JSON非法(%s)" % str(e)[:60])
            return False

    try:
        c, served = _chat_magpie(model, system, user, tmo, maxtok)
        if _ok(c):
            backend = served if served == model else "%s → %s" % (model, served)
            _log(kind, model, served, backend, time.time() - t0, True)
            return c, backend
        errs.append("magpie %s 内容不合格" % model)
    except Exception as e:
        errs.append("magpie %s: %s" % (model, str(e)[:100]))

    # 降级：一律回纯本地（宏观此时只算「初稿」，由 backend 显式标记）
    try:
        c, served = _chat_ollama(system, user, tmo, maxtok)
        if _ok(c):
            backend = ("local-draft: " + served) if r["tier"] == "cloud" else (served + " (fallback)")
            _log(kind, model, served, backend, time.time() - t0, True, "; ".join(errs))
            return c, backend
        errs.append("ollama 内容不合格")
    except Exception as e:
        errs.append("ollama: %s" % str(e)[:100])

    _log(kind, model, "", "failed", time.time() - t0, False, "; ".join(errs))
    raise RuntimeError("LLM 路由 %s 全部失败: %s" % (kind, "; ".join(errs)))


def json_of(text):
    """从 LLM 返回里抠 JSON 对象（容错 ```json 包裹 / 前后废话）。"""
    import re as _re
    if isinstance(text, (dict, list)):
        return text
    if not text or not str(text).strip():
        raise ValueError("空返回")
    t = str(text).strip()
    if "```" in t:
        m = _re.search(r"```(?:json)?\s*([\s\S]*?)```", t)
        if m:
            t = m.group(1).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("无 JSON")
    return json.loads(t[i:j + 1])


def summary():
    """给页面/CLI 看的当前路由表。"""
    return {k: {"model": v["model"], "tier": v["tier"], "web": v["web"],
                "label": v["label"]} for k, v in _load_config().items()}


if __name__ == "__main__":
    print(json.dumps(summary(), ensure_ascii=False, indent=2))
