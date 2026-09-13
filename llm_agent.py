#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent LLM 桥接：把系统里原来的付费 LLM 调用，换成「由我（WorkBuddy）来答」。

背景
----
macro_llm / sim_live / 币安 llm_gate 原先走 commandcode 云端（deepseek-v4-flash，
按量付费）。现在改成 **本地文件队列**：

    程序提问  →  data/llm_queue/pending/<id>.json
    我来回答  →  data/llm_queue/done/<id>.json
    超时未答  →  调用方按原逻辑降级（本地 ollama / 规则放行），绝不卡主流程

id 是 **确定性的**（kind + 日期 + 提问内容哈希），所以：
  - 同一个问题重复问 → 命中已有答案，不重复打扰我
  - 支持「两阶段」跑法：先 AGENT_LLM_WAIT=0 跑一遍把问题收集出来，
    我答完后再 AGENT_LLM_REPLAY=1 重跑同一步骤，自动命中答案

程序侧用法
----------
    from llm_agent import ask
    ans = ask('macro', system, user, expect_json=True, wait=45, meta={'date': '2026-09-14'})
    # 返回 dict/list（expect_json）或 str；没等到答案返回 None

我（Agent）怎么答
----------------
    python3 llm_agent.py --list                       # 列出待答问题
    python3 llm_agent.py --show <id>                  # 看完整提问
    python3 llm_agent.py --answer <id> -f ans.json    # 写回答案
    python3 llm_agent.py --gc 7                       # 清理 7 天前文件

也可以直接读写 json 文件：done/<id>.json 内容形如
    {"id": "...", "answer": {...} 或 "文本", "by": "agent", "ts": "..."}

环境变量
--------
    AGENT_LLM=1            启用队列（默认 1；=0 完全跳过，走原降级链）
    AGENT_LLM_WAIT         默认等待秒数（默认 2100=35 分钟，需 > 巡检间隔；0=只登记不等待）
    AGENT_LLM_REPLAY=1     命中已有答案即可，不再写 pending（重跑阶段用）
    AGENT_LLM_DIR          队列根目录（默认 <repo>/data/llm_queue）
    AGENT_LLM_ALLOW_CLOUD=1  允许继续用付费云端（默认 0 = 停用）
"""
import argparse
import hashlib
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.path.join(BASE_DIR, "data", "llm_queue")

ON = os.environ.get("AGENT_LLM", "1").lower() not in ("0", "false", "off", "no")
REPLAY = os.environ.get("AGENT_LLM_REPLAY", "0").lower() in ("1", "true", "yes", "on")
# 默认等待 35 分钟：要大于「队列巡检」的间隔（30 分钟），保证任何提问都能被
# 下一次自动巡检接住。仅适用于【批量任务】（宏观/候选评审/事后复盘）——
# 盘中闸门必须短等（T+0 不能为等 LLM 而拖延止盈单），由调用方显式传短 wait。
DEFAULT_WAIT = float(os.environ.get("AGENT_LLM_WAIT", "2100"))


def qdir():
    d = os.environ.get("AGENT_LLM_DIR") or DEFAULT_DIR
    return d


def _paths():
    d = qdir()
    return (os.path.join(d, "pending"), os.path.join(d, "done"))


def _ensure():
    p, dn = _paths()
    for x in (p, dn):
        try:
            os.makedirs(x, exist_ok=True)
        except Exception:
            pass
    return p, dn


def make_id(kind, system, user, date=None):
    """确定性 id：同问题同日子 → 同 id（天然缓存，避免重复打扰）。"""
    h = hashlib.sha1(("%s\n%s" % (system or "", user or "")).encode("utf-8")).hexdigest()[:10]
    d = date or time.strftime("%Y-%m-%d")
    return "%s-%s-%s" % (kind, d, h)


def json_of(text):
    """从模型/我的返回里抠 JSON（容错 ```json 包裹、前后废话）。"""
    if text is None:
        raise ValueError("空返回")
    if isinstance(text, (dict, list)):
        return text
    t = str(text).strip()
    if "```" in t:
        import re
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t)
        if m:
            t = m.group(1).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        i, j = t.find("["), t.rfind("]")
    if i < 0 or j <= i:
        raise ValueError("无 JSON")
    return json.loads(t[i:j + 1])


def _read(p):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write(p, obj):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def ask(kind, system, user, expect_json=True, wait=None, meta=None, schema=None,
        date=None, verbose=True):
    """提问并等待答案。

    返回：dict/list（expect_json=True）或 str；超时/未启用 → None。
    """
    if not ON:
        return None
    wait = DEFAULT_WAIT if wait is None else float(wait)
    p, dn = _ensure()
    qid = make_id(kind, system, user, date)
    done_p = os.path.join(dn, qid + ".json")

    # 已有答案（同问题重问 / replay 阶段）→ 直接复用
    old = _read(done_p)
    if old is not None and old.get("answer") is not None:
        a = old["answer"]
        if expect_json:
            try:
                return json_of(a)
            except Exception:
                return None
        return a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)

    if REPLAY:
        return None

    _write(os.path.join(p, qid + ".json"), {
        "id": qid, "kind": kind, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "expect_json": bool(expect_json), "wait": wait,
        "system": system, "user": user,
        "meta": meta or {}, "schema": schema or "",
    })
    if verbose:
        print("  [agent-llm] 提问已登记 %s（等 %.0fs）" % (qid, wait))

    if wait <= 0:
        return None
    end = time.time() + wait
    while time.time() < end:
        time.sleep(min(2.0, max(0.5, wait / 20.0)))
        a = _read(done_p)
        if a is not None and a.get("answer") is not None:
            ans = a["answer"]
            if expect_json:
                try:
                    return json_of(ans)
                except Exception as e:
                    print("  [agent-llm] 答案非合法 JSON，忽略: %s" % str(e)[:80])
                    return None
            return ans if isinstance(ans, str) else json.dumps(ans, ensure_ascii=False)
    return None


def put_answer(qid, answer, by="agent"):
    """写回答案。answer 可为 dict/list/str。"""
    p, dn = _ensure()
    _write(os.path.join(dn, qid + ".json"), {
        "id": qid, "answer": answer, "by": by,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    return True


def pending(limit=50):
    """待答问题（有 pending 无 done）。"""
    p, dn = _ensure()
    out = []
    try:
        names = sorted(os.listdir(p))
    except Exception:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        qid = n[:-5]
        if os.path.exists(os.path.join(dn, n)):
            continue
        d = _read(os.path.join(p, n))
        if not d:
            continue
        d["_file"] = os.path.join(p, n)
        out.append(d)
    out.sort(key=lambda x: x.get("ts") or "")
    return out[-limit:]


# ---------------- CLI ----------------
def main():
    ap = argparse.ArgumentParser(description="Agent LLM 队列")
    ap.add_argument("--list", action="store_true", help="列出待答问题")
    ap.add_argument("--show", metavar="ID", help="打印某个问题的完整内容")
    ap.add_argument("--answer", metavar="ID", help="为某个问题写回答案")
    ap.add_argument("-f", "--file", help="答案文件（json 或纯文本）")
    ap.add_argument("--json", dest="jsonstr", help="答案 JSON 字符串")
    ap.add_argument("--gc", metavar="DAYS", type=int, help="清理 N 天前的文件")
    a = ap.parse_args()

    if a.show:
        p, dn = _ensure()
        for base in (p, dn):
            fp = os.path.join(base, a.show if a.show.endswith(".json") else a.show + ".json")
            if os.path.exists(fp):
                print(open(fp, "r", encoding="utf-8").read())
                return 0
        print("未找到 %s" % a.show)
        return 1

    if a.answer:
        if a.file:
            raw = open(a.file, "r", encoding="utf-8").read()
            try:
                ans = json.loads(raw)
            except Exception:
                ans = raw
        elif a.jsonstr:
            try:
                ans = json.loads(a.jsonstr)
            except Exception:
                ans = a.jsonstr
        else:
            print("需要 -f/--file 或 --json")
            return 1
        put_answer(a.answer, ans)
        print("已写回答案 %s" % a.answer)
        return 0

    if a.gc:
        p, dn = _ensure()
        cut = time.time() - a.gc * 86400
        n = 0
        for d in (p, dn):
            try:
                for f in os.listdir(d):
                    fp = os.path.join(d, f)
                    if f.endswith(".json") and os.path.getmtime(fp) < cut:
                        try:
                            os.remove(fp)
                            n += 1
                        except Exception:
                            pass
            except Exception:
                pass
        print("已清理 %d 个 %d 天前的文件" % (n, a.gc))
        return 0

    # 默认 --list
    ps = pending()
    if not ps:
        print("无待答问题（队列目录：%s）" % qdir())
        return 0
    for d in ps:
        prev = (d.get("user") or "").replace("\n", " ⏎ ")[:110]
        print("%s | %-12s | %s | %s" % (d["id"], d.get("kind", "?"),
                                        d.get("ts", "?"), prev))
    print("\n共 %d 条。查看：python3 llm_agent.py --show <id>" % len(ps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
