#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llm_router 决策路由测试：宏观走云端 ds4.1，短决策固定纯本地。

不发起任何真实网络请求（全部 mock），验证 2026-11-08 对比结论的落地接线：
  route_of 分流 / 配置与环境变量覆盖 / 强制纯本地 / 云端失败降级为本地初稿。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import llm_router  # noqa: E402


class TestRoutes(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in
                     ("DSR_MACRO_MODEL", "DSR_DECISION_MODEL", "DSR_LOCAL_MODEL")}
        self._local_only = llm_router.LOCAL_ONLY
        llm_router.LOCAL_ONLY = False
        for k in self._env:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        llm_router.LOCAL_ONLY = self._local_only

    def test_macro_goes_cloud_with_web(self):
        r = llm_router.route_of("macro")
        self.assertEqual(r["tier"], "cloud")
        self.assertTrue(r["web"])
        self.assertIn("deepseek-v4.1-flash", r["model"])

    def test_all_decision_kinds_are_local(self):
        for kind in ("gate", "bn_gate", "candidates", "stock_news", "bn_review"):
            r = llm_router.route_of(kind)
            self.assertEqual(r["tier"], "local", kind)
            self.assertFalse(r["web"], kind)
            self.assertTrue(r["model"].startswith("group/") or "/" not in r["model"], kind)

    def test_unknown_kind_defaults_local(self):
        r = llm_router.route_of("some_future_task")
        self.assertEqual(r["tier"], "local")
        self.assertFalse(r["web"])

    def test_env_override(self):
        os.environ["DSR_MACRO_MODEL"] = "group/local-all"
        os.environ["DSR_DECISION_MODEL"] = "ollama/qwen3-coder:30b"
        self.assertEqual(llm_router.route_of("macro")["tier"], "local")
        self.assertEqual(llm_router.route_of("gate")["model"], "ollama/qwen3-coder:30b")

    def test_config_override(self):
        cfg = {"routes": {"macro": {"model": "workbuddy/other-model", "timeout": 12}}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(cfg, f)
            tmp = f.name
        try:
            with patch.object(llm_router, "CONFIG", tmp):
                r = llm_router.route_of("macro")
            self.assertEqual(r["model"], "workbuddy/other-model")
            self.assertEqual(r["timeout"], 12)
        finally:
            os.unlink(tmp)

    def test_local_only_forces_everything_local(self):
        llm_router.LOCAL_ONLY = True
        for kind in ("macro", "gate", "bn_gate", "bn_review"):
            r = llm_router.route_of(kind)
            self.assertEqual(r["tier"], "local", kind)
            self.assertFalse(r["model"].startswith("workbuddy/"), kind)


class TestJsonOf(unittest.TestCase):
    def test_plain_and_fenced(self):
        self.assertEqual(llm_router.json_of('{"ok":1}')["ok"], 1)
        self.assertEqual(llm_router.json_of('说明\n```json\n{"ok":2}\n```\n')["ok"], 2)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            llm_router.json_of("   ")


class TestChat(unittest.TestCase):
    def setUp(self):
        self._local_only = llm_router.LOCAL_ONLY
        llm_router.LOCAL_ONLY = False
        self._env = {k: os.environ.get(k) for k in ("DSR_MACRO_MODEL", "DSR_DECISION_MODEL")}
        for k in self._env:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        llm_router.LOCAL_ONLY = self._local_only

    def test_macro_uses_cloud(self):
        with patch.object(llm_router, "_chat_magpie",
                          return_value=('{"score":42}', "workbuddy/deepseek-v4.1-flash")) as m, \
             patch.object(llm_router, "_log"):
            c, backend = llm_router.chat("macro", "s", "u")
        self.assertTrue(m.called)
        self.assertEqual(m.call_args[0][0], "workbuddy/deepseek-v4.1-flash")
        self.assertIn("deepseek-v4.1-flash", backend)
        self.assertEqual(c, '{"score":42}')

    def test_gate_uses_local_group(self):
        with patch.object(llm_router, "_chat_magpie",
                          return_value=('{"decisions":[]}', "ollama/qwen3-coder:30b")) as m, \
             patch.object(llm_router, "_log"):
            _, backend = llm_router.chat("bn_gate", "s", "u")
        self.assertEqual(m.call_args[0][0], "group/local-code")
        self.assertIn("qwen3-coder", backend)

    def test_cloud_failure_becomes_local_draft(self):
        with patch.object(llm_router, "_chat_magpie", side_effect=OSError("magpie down")), \
             patch.object(llm_router, "_chat_ollama",
                          return_value=('{"score":55}', "ollama/qwen3-coder:30b")), \
             patch.object(llm_router, "_log"):
            c, backend = llm_router.chat("macro", "s", "u")
        self.assertTrue(backend.startswith("local-draft: "))
        self.assertEqual(c, '{"score":55}')

    def test_local_fallback_marked(self):
        with patch.object(llm_router, "_chat_magpie", side_effect=OSError("magpie down")), \
             patch.object(llm_router, "_chat_ollama",
                          return_value=('{"decisions":[]}', "ollama/qwen3-coder:30b")), \
             patch.object(llm_router, "_log"):
            _, backend = llm_router.chat("gate", "s", "u")
        self.assertIn("(fallback)", backend)

    def test_all_channels_fail_raises(self):
        with patch.object(llm_router, "_chat_magpie", side_effect=OSError("down")), \
             patch.object(llm_router, "_chat_ollama", side_effect=OSError("down")), \
             patch.object(llm_router, "_log"):
            with self.assertRaises(RuntimeError) as cm:
                llm_router.chat("macro", "s", "u")
        self.assertIn("全部失败", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
