#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ETF 成分拉取: 遍历自选池内 ETF, 拉天天基金 F10 前5大持仓 -> data/etf_components.json
- kind=dom: A股成分数>=3 -> 拆出这些 A股成分(并入全池候选, 需复盘评分)
- kind=hk/oversea: 成分主要为港股/海外 -> 不拆, ETF 自身入池(直接交易 A股 ETF 份额)
- kind=none: 贵金属/商品/现金持仓 -> 不拆, ETF 自身入池
季报数据(季度更新), 每周自动刷新; 限频自动退避重试。
输出给 run_review.py / sim_live.py 做"全池=自选∪成分"扩展。
"""
import json
import os
import re
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
OUT = os.path.join(DATA, "etf_components.json")
TOPLINE = 10  # 拉前10, 截取前5

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update_watchlist_data.py")


def load_watchlist():
    """返回 [(code, name, sector)] 单一真源 = update_watchlist_data.py WATCHLIST"""
    s = open(_SRC, encoding="utf-8").read()
    return [(c, n, sec) for n, c, sec in re.findall(
        r'\{\s*"name"\s*:\s*"([^"]*)"\s*,\s*"code"\s*:\s*"(\d{6})"\s*,\s*"sector"\s*:\s*"([^"]*)"\s*}', s)]


def is_etf(code):
    c = str(code)
    return c.startswith(("5", "1", "16")) or c[:3] in ("159", "501", "502", "506", "508")


def fetch_top(etf_code, tries=3):
    url = ("https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc"
           f"&code={etf_code}&topline={TOPLINE}&year=&month=")
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "Referer": f"http://fundf10.eastmoney.com/{etf_code}.html",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            raw_b = urllib.request.urlopen(req, timeout=15).read()
            head = raw_b[:3000].lower()
            if b"charset=utf-8" in head or b"charset=\"utf-8\"" in head:
                raw = raw_b.decode("utf-8", "ignore")
            elif b"charset=gbk" in head or b"charset=gb2312" in head or b"charset=gb18030" in head:
                raw = raw_b.decode("gbk", "ignore")
            else:
                try:
                    raw = raw_b.decode("utf-8")
                except UnicodeDecodeError:
                    raw = raw_b.decode("gbk", "ignore")
            m = re.search(r"截止至：<font[^>]*>([\d\-]+)</font>", raw)
            date = m.group(1) if m else ""
            tops = []
            seen = set()
            for row in re.findall(r"<tr>(.*?)</tr>", raw, re.S):
                mm = re.search(r"unify/r/(\d)\.(\d{5,6})'[^>]*>\d{5,6}</a>", row)
                if not mm:
                    continue
                mk, code = mm.group(1), mm.group(2)
                if code in seen:
                    continue
                seen.add(code)
                nm = re.search(r"<td class='tol'><a[^>]*>([^<]+)</a>", row)
                tops.append({"market": mk, "code": code,
                             "name": nm.group(1).strip() if nm else code})
                if len(tops) >= 5:
                    break
            return {"ok": True, "date": date, "tops": tops}
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    return {"ok": False, "date": "", "tops": [], "error": str(last)}


def main():
    os.makedirs(DATA, exist_ok=True)
    wl = load_watchlist()
    etfs = [(c, n, sec) for c, n, sec in wl if is_etf(c)]
    print(f"自选共 {len(wl)} 只, 其中 ETF/基金 {len(etfs)} 只", flush=True)

    etf_list = []
    extra = {}
    dom_cnt = hk_cnt = none_cnt = fail_cnt = 0
    for idx, (code, name, sec) in enumerate(etfs, 1):
        r = fetch_top(code)
        if not r["ok"]:
            fail_cnt += 1
            etf_list.append({"code": code, "name": name, "sector": sec, "kind": "fail",
                             "date": "", "tops": [], "error": r["error"]})
            print(f"  [{idx}/{len(etfs)}] {code} {name} 失败: {r['error']}", flush=True)
            continue
        a_tops = [t for t in r["tops"] if t["market"] in ("0", "1")]
        if not r["tops"]:
            kind = "none"
        elif len(a_tops) >= 3:
            kind = "dom"
        elif a_tops:
            kind = "hk/oversea"
        else:
            kind = "hk/oversea"
        if kind == "dom":
            dom_cnt += 1
            for t in a_tops[:5]:
                extra.setdefault(t["code"], {"name": t["name"], "sector": sec})
        elif kind == "hk/oversea":
            hk_cnt += 1
        else:
            none_cnt += 1
        etf_list.append({"code": code, "name": name, "sector": sec, "kind": kind,
                         "date": r["date"], "tops": r["tops"]})
        time.sleep(0.35)

    wl_codes = {c for c, _, _ in wl}
    new_extra = {c: v for c, v in extra.items() if c not in wl_codes}
    out = {
        "fetchedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reportDate": "",
        "etfCount": len(etfs),
        "domEtf": dom_cnt, "hkEtf": hk_cnt, "noneEtf": none_cnt, "failEtf": fail_cnt,
        "domExtraNew": len(new_extra),
        "etfs": etf_list,
        "extra": new_extra,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成: 境内{dom_cnt} 跨境{hk_cnt} 无成分{none_cnt} 失败{fail_cnt}")
    print(f"境内前5成分净新增(不在自选) {len(new_extra)} 只")
    print(f"输出: {OUT}")


if __name__ == "__main__":
    main()
