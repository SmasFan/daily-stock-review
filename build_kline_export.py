#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为实时模拟盘个股弹层导出日K（供前端 SC.stockChart 绘图）。
扫描 sim_live.json 涉及的股票代码（持仓/计划/成交），把缓存 K 线复制到
data/kline/{code}.json，页面按 code 动态加载。
用法: python3 build_kline_export.py
"""
import json
import os
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
    # 分钟K导出（5分/1分，供弹卡分时/分钟图）
    sys.path.insert(0, os.path.join(BASE, "src"))
    from src import data_provider as dp
    m_n = 0
    for c in sorted(codes):
        for scale, tag in ((5, "m5"), (1, "m1")):
            try:
                k = dp.fetch_minute_kline(c, scale=scale, datalen=240, use_cache=False)
            except Exception:
                k = None
            if k and k.get("dates"):
                with open(os.path.join(OUT, "%s_%s.json" % (c, tag)), "w", encoding="utf-8") as f:
                    json.dump(k, f, ensure_ascii=False)
                m_n += 1
    print("导出 %d 只日K → data/kline/ + %d 个分钟文件" % (len(codes), m_n))


if __name__ == "__main__":
    main()
