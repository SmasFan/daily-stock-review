#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为实时模拟盘个股弹层导出日K（供前端 SC.stockChart 绘图）。
扫描 sim_live.json 涉及的股票代码（持仓/计划/成交），把缓存 K 线复制到
data/kline/{code}.json，页面按 code 动态加载。
用法: python3 build_kline_export.py
"""
import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "data", "cache")
OUT = os.path.join(BASE, "data", "kline")
os.makedirs(OUT, exist_ok=True)


def main():
    codes = set()
    try:
        s = json.load(open(os.path.join(BASE, "data", "sim_live.json"), encoding="utf-8"))
        for k in ("aggressive", "balanced", "disciplined"):
            a = s.get("accounts", {}).get(k, {})
            for p in a.get("positions", []):
                codes.add(str(p["code"]))
            for p in a.get("plan", []):
                codes.add(str(p["code"]))
            for t in a.get("trades", []):
                codes.add(str(t["code"]))
    except Exception as e:
        print("无 sim_live: %s" % e)
    for c in sorted(codes):
        src = os.path.join(CACHE, "kline_%s.json" % c)
        if os.path.exists(src):
            dst = os.path.join(OUT, "%s.json" % c)
            # 减到最近 250 天（前端指标需足够）
            try:
                d = json.load(open(src, encoding="utf-8"))
                n = len(d["dates"])
                if n > 250:
                    sl = slice(n - 250, n)
                    d = {kk: v[sl] if isinstance(v, list) else v for kk, v in d.items()}
                with open(dst, "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False)
            except Exception as ex:
                print("skip %s %s" % (c, ex))
        else:
            print("缓存缺 %s" % c)
    print("导出 %d 只 → data/kline/" % len(codes))


if __name__ == "__main__":
    main()
