#!/usr/bin/env python3
"""状态页生成器: 聚合进程/持仓/净值/日志 -> status.html
常驻, 每 30s 刷新页面文件。浏览器/curl 随时看。
"""
import os, subprocess, time, html, json
from datetime import datetime
import ccxt
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
BASE = '/mnt/c/Users/z7280/daily-stock-review/binance-llm-bot'
OUT = os.path.join(BASE, 'status.html')
# 双输出: 原位置 + 每日复盘仓库镜像（供 daily-stock-review/binance.html iframe 集成）
MIRROR = '/mnt/c/Users/z7280/daily-stock-review/data/binance_status.html'
OUTS = [OUT, MIRROR]
PROXY = os.environ.get('PROXY', 'socks5h://172.25.16.1:10808')
SYMBOLS = ['TSLA/USDT:USDT', 'COIN/USDT:USDT', 'PLTR/USDT:USDT', 'MSTR/USDT:USDT', 'HOOD/USDT:USDT']
# 短线池标的 (trader_short)
SYMBOLS_SHORT = ['NVDA/USDT:USDT', 'META/USDT:USDT', 'AMZN/USDT:USDT', 'QQQ/USDT:USDT',
                 'SPY/USDT:USDT', 'GOOGL/USDT:USDT', 'INTC/USDT:USDT', 'CRCL/USDT:USDT']
NAMES_SHORT = {'NVDA/USDT:USDT': 'NVDA', 'META/USDT:USDT': 'META', 'AMZN/USDT:USDT': 'AMZN', 'QQQ/USDT:USDT': 'QQQ',
               'SPY/USDT:USDT': 'SPY', 'GOOGL/USDT:USDT': 'GOOGL', 'INTC/USDT:USDT': 'INTC', 'CRCL/USDT:USDT': 'CRCL'}
CN_SHORT = {'NVDA/USDT:USDT': '英伟达', 'META/USDT:USDT': 'Meta', 'AMZN/USDT:USDT': '亚马逊', 'QQQ/USDT:USDT': '纳指100ETF',
            'SPY/USDT:USDT': '标普500ETF', 'GOOGL/USDT:USDT': '谷歌', 'INTC/USDT:USDT': '英特尔', 'CRCL/USDT:USDT': 'Circle'}
NAMES = {'TSLA/USDT:USDT': 'TSLA', 'COIN/USDT:USDT': 'COIN', 'PLTR/USDT:USDT': 'PLTR', 'MSTR/USDT:USDT': 'MSTR', 'HOOD/USDT:USDT': 'HOOD'}
COIN_COLOR = {'TSLA': '#e82127', 'COIN': '#0f6ee2', 'PLTR': '#1f1f1f', 'MSTR': '#c7962d', 'HOOD': '#45d483',
              'TSLA/USDT:USDT': '#e82127', 'COIN/USDT:USDT': '#0f6ee2', 'PLTR/USDT:USDT': '#1f1f1f', 'MSTR/USDT:USDT': '#c7962d', 'HOOD/USDT:USDT': '#45d483',
              'NVDA': '#76b900', 'META': '#0668e1', 'AMZN': '#ff9900', 'QQQ': '#00a4e4',
              'SPY': '#b3123a', 'GOOGL': '#4285f4', 'INTC': '#0071c5', 'CRCL': '#000000',
              'NVDA/USDT:USDT': '#76b900', 'META/USDT:USDT': '#0668e1', 'AMZN/USDT:USDT': '#ff9900', 'QQQ/USDT:USDT': '#00a4e4',
              'SPY/USDT:USDT': '#b3123a', 'GOOGL/USDT:USDT': '#4285f4', 'INTC/USDT:USDT': '#0071c5', 'CRCL/USDT:USDT': '#000000'}
# 中文名 (symbol -> 显示名)
CN_NAMES = {'TSLA/USDT:USDT': '特斯拉', 'COIN/USDT:USDT': 'Coinbase', 'PLTR/USDT:USDT': 'Palantir',
            'MSTR/USDT:USDT': '微策略', 'HOOD/USDT:USDT': 'Robinhood'}


def make_fex():
    ex = ccxt.binance({
        'apiKey': os.environ.get('BN_API_KEY', ''),
        'secret': os.environ.get('BN_SECRET', ''),
        'enableRateLimit': True,
        'proxies': {'http': PROXY, 'https': PROXY},
        'options': {'defaultType': 'future', 'adjustForTimeDifference': True},
    })
    # 禁 keep-alive: 低频轮询下复用连接会被代理回收导致 RemoteDisconnected
    ex.session.headers['Connection'] = 'close'
    ex.enable_demo_trading(True)
    return ex


def proc_status():
    rows = []
    for name in ['trader100.py', 'guard.py', 'status_page.py', 'watchdog.py']:
        r = subprocess.run(['pgrep', '-f', f'{name}$|{name} '], capture_output=True, text=True)
        pids = r.stdout.split()
        rows.append((name, '✅ 运行' if pids else '❌ 挂了', ','.join(pids)))
    # cron keepalive: 常驻检测不适用, 显示为调度项
    rows.append(('keepalive (cron每分钟)', '⏱ 调度中', '-'))
    return rows


def positions(ex, syms, names, cn_map, pool_start=100.0):
    pos_list = []
    total_pnl = 0.0
    for s in syms:
        for p in ex.fetch_positions([s]):
            amt = float(p['contracts'])
            if not amt:
                continue
            entry = float(p['entryPrice'])
            mark = float(p.get('markPrice') or entry)
            pnl = float(p['unrealizedPnl'])
            liq = float(p.get('liquidationPrice') or 0)
            pct = (mark / entry - 1) * 100
            info = p.get('info', {})
            notional = float(info.get('notional') or amt * mark)
            init_margin = float(info.get('positionInitialMargin') or info.get('initialMargin') or 0)
            lev = round(notional / init_margin, 1) if init_margin > 0 else 0
            total_pnl += pnl
            pos_list.append({
                'sym': names.get(s, s), 'cn': cn_map.get(s, ''), 'amt': amt, 'entry': entry, 'mark': mark,
                'pnl': pnl, 'pct': pct, 'liq': liq, 'sl': entry * (1 - (0.03 if s in SYMBOLS_SHORT else 0.12)),
                'lev': lev, 'notional': notional,
            })
    return pos_list, total_pnl


def tail_log(fname, lines=25):
    path = os.path.join(BASE, fname)
    if not os.path.exists(path):
        return ['(无日志)']
    with open(path, 'r', errors='ignore') as f:
        return f.readlines()[-lines:]


def latest_review_html():
    """读 evolution.json 最新复盘, 渲染摘要卡"""
    evo_path = os.path.join(BASE, 'evolution.json')
    if not os.path.exists(evo_path):
        return ''
    try:
        evo = json.load(open(evo_path))
        revs = evo.get('reviews', [])
        if not revs:
            return ''
        r = revs[-1]
        review = r.get('review', {})
        act = review.get('action', '')
        act_badge = {'hold': ('维持现状', 'b-info'), 'adjust': ('建议调整', 'b-warn'), 'review': ('需关注', 'b-err')}.get(act, (act, 'b-info'))
        state = review.get('market_state', '')
        summary = html.escape(review.get('summary', ''))
        risk = html.escape(review.get('risk_note', ''))
        adjustments = review.get('adjustments', [])
        adj_html = ''
        if adjustments:
            items = ''.join(f'<li>{html.escape(a.get("param",""))}: {html.escape(a.get("from",""))} → {html.escape(a.get("to",""))} <span class="dim">({html.escape(a.get("reason",""))})</span></li>' for a in adjustments[:3])
            adj_html = f'<div style="margin-top:8px"><b style="color:var(--gold)">调整建议:</b><ul style="margin:6px 0 0 18px;font-size:12px">{items}</ul></div>'
        return f'''<div class="card"><h2>🤖 AI 复盘 <span class="badge {act_badge[1]}" style="margin-left:8px">{act_badge[0]}</span></h2>
<div style="font-size:12px;color:var(--dim);margin-bottom:6px">{r.get('ts','')} · 市场:{html.escape(state)} · 已实现 {r.get('realized_pnl',0):+.2f}U 浮动 {r.get('unrealized_pnl',0):+.2f}U</div>
<div style="font-size:13px;line-height:1.7">{summary}</div>
<div style="font-size:12px;color:var(--dim);margin-top:8px;line-height:1.6">⚠ {risk}</div>{adj_html}</div>'''
    except Exception as e:
        return f'<div class="card"><h2>AI 复盘</h2><div class="dim">读取失败 {html.escape(str(e))}</div></div>'
    path = os.path.join(BASE, fname)
    if not os.path.exists(path):
        return ['(无日志)']
    with open(path, 'r', errors='ignore') as f:
        return f.readlines()[-lines:]


def trade_stats(trades):
    """汇总: 交易数/胜率/累计盈亏"""
    closed = [t for t in trades if t['type'] in ('close', 'sl')]
    wins = [t for t in closed if t.get('pnl', 0) > 0]
    total_pnl = sum(t.get('pnl', 0) for t in closed)
    return len(closed), len(wins), total_pnl


def build_html(procs, pos_list, total_pnl, bal, trades, tstats,
               short_pos=None, short_pnl=0.0, short_n=8):
    badge = {'open': ('开仓', 'b-open'), 'close': ('平仓', 'b-close'),
             'sl': ('止损', 'b-sl'), 'cooldown': ('熔断', 'b-warn'),
             'error': ('错误', 'b-err'), 'decision': ('决策', 'b-info')}
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    short_pos = short_pos or []
    # 日线池净值
    pool_start = float(os.environ.get('POOL_START', '100'))
    pool = pool_start + total_pnl
    # 短线池净值
    short_pool_start = float(os.environ.get('SHORT_POOL', '100'))
    short_pool = short_pool_start + short_pnl
    rows_p = ''.join(
        f'<tr><td><span class="mono">{n}</span></td><td>'
        f'{"<span class=\"pill ok\"></span><span>运行中</span>" if "✅" in st else "<span class=\"pill dead\"></span><span style=\"color:var(--down)\u003e挂掉</span>"}'
        f'</td><td class="pid">{pids}</td></tr>' for n, st, pids in procs)
    if pos_list:
        rows_c = ''
        for p in pos_list:
            cls = 'up' if p['pct'] > 0 else 'down'
            cc = COIN_COLOR.get(p['sym'], '#888')
            rows_c += (f'<tr><td><span class="coin"><span class="cdot" style="background:{cc}">{p["sym"][0]}</span>{p["cn"]} <span class="dim" style="font-size:12px">{p["sym"]}</span></span></td>'
                       f'<td>{p["amt"]:.4f}<br><span class="dim" style="font-size:11px">≈${p["notional"]:,.1f}</span></td>'
                       f'<td>{p["entry"]:,.2f}</td><td>{p["mark"]:,.2f}</td>'
                       f'<td><span class="badge b-info">{p["lev"]}x</span></td>'
                       f'<td class="dim">{p["notional"]:,.1f}</td>'
                       f'<td class="{cls}">{p["pct"]:+.2f}%</td>'
                       f'<td class="{cls}">{p["pnl"]:+.2f} U</td>'
                       f'<td class="dim">{p["sl"]:,.2f}</td>'
                       f'<td class="dim">{p["liq"]:,.0f}</td></tr>')
    else:
        rows_c = '<tr><td colspan=10 class="dim">空仓 — 等待趋势信号</td></tr>'
    # 短线池持仓行
    if short_pos:
        rows_s = ''
        for p in short_pos:
            cls = 'up' if p['pct'] > 0 else 'down'
            cc = COIN_COLOR.get(p['sym'], '#888')
            rows_s += (f'<tr><td><span class="coin"><span class="cdot" style="background:{cc}">{p["sym"][0]}</span>{p["cn"]} <span class="dim" style="font-size:12px">{p["sym"]}</span></span></td>'
                       f'<td>{p["amt"]:.4f}<br><span class="dim" style="font-size:11px">≈${p["notional"]:,.1f}</span></td>'
                       f'<td>{p["entry"]:,.2f}</td><td>{p["mark"]:,.2f}</td>'
                       f'<td><span class="badge b-info">{p["lev"]}x</span></td>'
                       f'<td class="dim">{p["notional"]:,.1f}</td>'
                       f'<td class="{cls}">{p["pct"]:+.2f}%</td>'
                       f'<td class="{cls}">{p["pnl"]:+.2f} U</td>'
                       f'<td class="dim">{p["sl"]:,.2f}</td>'
                       f'<td class="dim">{p["liq"]:,.0f}</td></tr>')
    else:
        rows_s = '<tr><td colspan=10 class="dim">空仓 — 等待趋势信号</td></tr>'
    pool_cls = 'up' if total_pnl > 0 else 'down'
    rev_html = latest_review_html()
    gate_html = llm_gate_html()
    shadow = shadow_html()

    # 交易记录表
    t_closed, t_wins, t_pnl = tstats
    if trades:
        rows_t = ''
        for t in reversed(trades):  # 新的在前
            cls = 'up' if t['pnl'] > 0 else ('down' if t['pnl'] < 0 else 'dim')
            bl, bc = badge.get(t['type'], (t['type'], 'b-info'))
            pnl_s = f'<td class="{cls}">{t["pnl"]:+.2f} U</td>' if t['type'] in ('close', 'sl') else '<td class="dim">-</td>'
            cc = COIN_COLOR.get(t['symbol'], '#888')
            cn = CN_NAMES.get(t['symbol'], t['symbol'].split('/')[0])
            lev = t.get('lev')
            lev_s = f'<td><span class="badge b-info">{lev:g}x</span></td>' if lev else '<td class="dim">-</td>'
            rows_t += (f'<tr><td class="dim">{t["ts"]}</td>'
                       f'<td><span class="badge {bc}">{bl}</span></td>'
                       f'<td><span class="coin"><span class="cdot" style="background:{cc}">{t["symbol"].split("/")[0][0]}</span>{cn} <span class="dim" style="font-size:12px">{t["symbol"].split("/")[0]}</span></span></td>'
                       f'<td>{t["qty"]:.4f}<br><span class="dim" style="font-size:11px">≈${t["qty"]*t["price"]:,.1f}</span></td>'
                       f'<td>{t["price"]:,.2f}</td>'
                       f'{lev_s}{pnl_s}<td class="dim">{html.escape(t["detail"])}</td></tr>')
    else:
        rows_t = '<tr><td colspan=8 class="dim">暂无交易记录</td></tr>'
    winrate = f'{t_wins/max(t_closed,1)*100:.0f}%' if t_closed else '-'

    def log_block(title, lines):
        body = ''.join(f'<div class="logline">{html.escape(l.strip())}</div>' for l in lines)
        return f'<details class="logcard" open><summary>{title}</summary>{body}</details>'

    # ---- 交易类型 badge ----

    return f'''<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>量化交易仪表盘</title>
<style>
:root{{
  --bg:#0b0e14; --panel:#131722; --panel2:#1a2030; --border:#232a3b;
  --txt:#e6e9f0; --dim:#8b93a7; --up:#22c55e; --down:#ef4444;
  --acc:#6366f1; --acc2:#22d3ee; --gold:#f59e0b;
}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:var(--bg);color:var(--txt);font-family:'Segoe UI',system-ui,-apple-system,sans-serif;min-height:100vh}}
.wrap{{max-width:1100px;margin:0 auto;padding:24px 20px 60px}}
header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:22px}}
h1{{font-size:20px;font-weight:700;letter-spacing:.3px}}
h1 .dot{{color:var(--up);font-size:10px;vertical-align:middle;margin-right:8px}}
.live{{font-size:12px;color:var(--dim)}}
.live b{{color:var(--acc2);font-weight:600}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-bottom:22px}}
.stat{{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:16px 18px}}
.stat .lb{{font-size:11px;color:var(--dim);letter-spacing:1px;text-transform:uppercase;margin-bottom:8px}}
.stat .vl{{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}}
.stat .sub{{font-size:12px;color:var(--dim);margin-top:6px}}
.up{{color:var(--up)}}.down{{color:var(--down)}}
.card{{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:18px;margin-bottom:16px}}
.card h2{{font-size:14px;color:var(--acc2);margin-bottom:14px;font-weight:600;display:flex;align-items:center;gap:8px}}
.card h2::before{{content:'';width:4px;height:14px;background:var(--acc2);border-radius:2px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
.tblwrap{{overflow-x:auto}}
table td, table th{{white-space:nowrap}}
th{{text-align:left;color:var(--dim);font-weight:500;padding:8px 10px;border-bottom:1px solid var(--border);font-size:11px;letter-spacing:.5px}}
td{{padding:9px 10px;border-bottom:1px solid #1d2433;font-variant-numeric:tabular-nums}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#161b29}}
.coin{{display:inline-flex;align-items:center;gap:8px;font-weight:600}}
.cdot{{width:22px;height:22px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;color:#fff}}
.badge{{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600}}
.b-open{{background:rgba(34,197,94,.15);color:var(--up)}}
.b-close{{background:rgba(99,102,241,.18);color:#a5b4fc}}
.b-sl{{background:rgba(239,68,68,.15);color:var(--down)}}
.b-warn{{background:rgba(245,158,11,.15);color:var(--gold)}}
.b-err{{background:rgba(239,68,68,.2);color:#fca5a5}}
.b-info{{background:rgba(34,211,238,.12);color:var(--acc2)}}
.logcard{{background:var(--panel2);border-radius:10px;margin-bottom:10px;overflow:hidden}}
.logcard summary{{padding:10px 14px;cursor:pointer;font-size:12px;color:var(--dim);font-weight:600;letter-spacing:.5px}}
.logcard summary:hover{{color:var(--txt)}}
.logline{{color:#5b6478;font-size:11px;padding:2px 14px;font-family:'Cascadia Code',Consolas,monospace;line-height:1.6;white-space:pre-wrap;word-break:break-all}}
.logline:last-child{{padding-bottom:12px}}
.pill{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}}
.ok{{background:var(--up);box-shadow:0 0 6px var(--up)}}
.dead{{background:var(--down);box-shadow:0 0 6px var(--down)}}
.pid{{color:var(--dim);font-size:11px}}
.dim{{color:var(--dim)}}.gold{{color:var(--gold)}}
.mono{{font-family:Consolas,monospace}}
</style></head><body>
<div class="wrap">
<header><h1><span class="dot">●</span>量化交易仪表盘</h1>
<div class="live">🟢 运行中 · 更新 <b>{now.split(" ")[1]}</b> · {now.split(" ")[0]} · 30s 自动刷新</div></header>

<div class="grid">
  <div class="stat"><div class="lb">日线池净值</div><div class="vl {pool_cls}">{pool:.2f}<span style="font-size:14px;color:var(--dim)"> U</span></div><div class="sub">SMA50日线 起始 100 U · {len(pos_list)}/{len(SYMBOLS)}仓</div></div>
  <div class="stat"><div class="lb">日线浮盈</div><div class="vl {pool_cls}">{total_pnl:+.2f} U</div><div class="sub">已实现 {t_pnl:+.2f} U · 平仓 {t_closed}笔</div></div>
  <div class="stat"><div class="lb">短线池净值</div><div class="vl {'up' if short_pnl>=0 else 'down'}">{short_pool:.2f}<span style="font-size:14px;color:var(--dim)"> U</span></div><div class="sub">SMA50 1h · 起始 {short_pool_start:.0f} U · {len(short_pos)}/{short_n}仓</div></div>
  <div class="stat"><div class="lb">短线浮盈</div><div class="vl {'up' if short_pnl>=0 else 'down'}">{short_pnl:+.2f} U</div><div class="sub">3x · 止损-3%</div></div>
  <div class="stat"><div class="lb">账户权益</div><div class="vl">{bal:.0f} <span style="font-size:14px;color:var(--dim)">U</span></div><div class="sub">Demo 合约</div></div>
</div>

{rev_html}
{gate_html}
{shadow}
<div class="card"><h2>日线持仓 (SMA50 日线 3x -12%)</h2>
<div class="tblwrap"><table><tr><th>标的</th><th>持仓量</th><th>入场价</th><th>Mark 价</th><th>杠杆</th><th>名义</th><th>盈亏</th><th>盈亏额</th><th>止损价</th><th>爆仓价</th></tr>{rows_c}</table></div></div>

<div class="card"><h2>短线持仓 (SMA50 1h 3x -3%)</h2>
<div class="tblwrap"><table><tr><th>标的</th><th>持仓量</th><th>入场价</th><th>Mark 价</th><th>杠杆</th><th>名义</th><th>盈亏</th><th>盈亏额</th><th>止损价</th><th>爆仓价</th></tr>{rows_s}</table></div></div>

<div class="card"><h2>交易记录</h2>
<div class="tblwrap"><table><tr><th>时间</th><th>类型</th><th>标的</th><th>数量</th><th>价格</th><th>杠杆</th><th>盈亏</th><th>说明</th></tr>{rows_t}</table></div></div>

<div class="card"><h2>系统进程</h2>
<div class="tblwrap"><table><tr><th>进程</th><th>状态</th><th>PID</th></tr>{rows_p}</table></div></div>

<div class="card"><h2>运行日志</h2>
{log_block('📈 策略 trader100', tail_log('run100.log'))}
{log_block('🛡 止损守护 guard', tail_log('guard.log'))}
{log_block('🔁 看门狗 watchdog', tail_log('watchdog.log', 10))}
{log_block('⏰ cron 重启记录', tail_log('watchdog_restart.log', 10))}
</div>
</div></body></html>'''


def llm_gate_html():
    """LLM 成交前风控闸门状态卡：今日复核统计 + 最近否决/放行 + 熔断状态。

    数据源：state_pool.json / state_short.json（gate 缓存 + 否决日志）。
    """
    pools = [('日线池', 'state_pool.json'), ('短线池', 'state_short.json')]
    today = datetime.now().strftime('%Y-%m-%d')
    blocks, tot_items, tot_avoid, tot_allow = [], 0, 0, 0
    for label, fname in pools:
        fp = os.path.join(BASE, fname)
        if not os.path.exists(fp):
            continue
        try:
            st = json.load(open(fp))
        except Exception:
            continue
        g = st.get('llm_gate') or {}
        items = g.get('items') if g.get('date') == today else {}
        items = items or {}
        cb = st.get('llm_gate_cb') or {}
        n_avoid = sum(1 for v in items.values() if v.get('verdict') == 'avoid')
        n_allow = sum(1 for v in items.values() if v.get('verdict') != 'avoid')
        tot_items += len(items); tot_avoid += n_avoid; tot_allow += n_allow
        hb = ''
        if cb.get('until'):
            hb = f' <span class="badge b-err">熔断至 {cb["until"]}</span>'
        rows = ''
        for k, v in list(items.items())[:8]:
            sym = k.split('|')[1] if '|' in k else k
            act = '买入' if k.endswith('buy') else '卖出'
            ok = v.get('verdict') != 'avoid'
            rows += (f'<tr><td class="dim">{sym.split("/")[0]}</td><td>{act}</td>'
                     f'<td><span class="badge {"b-info" if ok else "b-err"}">{"放行" if ok else "否决"}</span></td>'
                     f'<td class="dim">{html.escape(str(v.get("note") or ""))}</td></tr>')
        # 否决日志（state.log）
        logs = [l for l in (st.get('log') or []) if 'LLM' in str(l.get('msg', ''))]
        log_html = ''
        if logs:
            log_html = ('<div style="margin-top:6px;font-size:12px;color:var(--gold)">最近否决：</div>' +
                        ''.join(f'<div class="logline">{html.escape(x["ts"] + " " + x["msg"])}</div>'
                                for x in logs[-4:]))
        body = (f'<div style="font-size:12px;color:var(--dim);margin-bottom:6px">'
                f'今日复核 {len(items)} 条 · 放行 {n_allow} · 否决 {n_avoid}（规则先触发，LLM 成交前复核）{hb}</div>')
        body += (f'<div class="tblwrap"><table><tr><th>标的</th><th>方向</th><th>LLM</th><th>理由</th></tr>{rows}</table></div>'
                 if rows else '<div class="dim" style="font-size:12px">今日暂无复核（无触发候选，或有候选但已缓存）</div>')
        blocks.append(f'<div class="card"><h2>🧠 {label} LLM 闸门</h2>{body}{log_html}</div>')
    if not blocks:
        return ''
    head = ('<div class="card"><h2>🧠 LLM 成交前风控</h2>'
            f'<div style="font-size:13px">今日共复核 <b>{tot_items}</b> 条 · 放行 <b class="up">{tot_allow}</b> · '
            f'否决 <b class="down">{tot_avoid}</b></div>'
            '<div style="font-size:12px;color:var(--dim);margin-top:6px;line-height:1.7">'
            '规则（收盘价 vs SMA50）先出候选 → LLM 复核通过才下单。买入可否决（跌破SMA20/偏离过大/追高）；'
            '趋势离场属硬规则，LLM 只能确认。LLM 不可用自动放行并熔断 10 分钟，绝不停盘。</div></div>')
    return head + ''.join(blocks)


def shadow_html():
    """LLM 否决单的影子跟踪卡：否决后价格怎么走了 → 闸门到底有没有用。"""
    pools = [('日线池', 'state_pool.json'), ('短线池', 'state_short.json')]
    cards = []
    all_open, tot = [], {'n': 0, 'correct': 0, 'wrong': 0, 'flat': 0, 'avg_ret': 0, 'avg_max': 0, 'avg_min': 0}
    for label, fname in pools:
        fp = os.path.join(BASE, fname)
        if not os.path.exists(fp):
            continue
        try:
            st = json.load(open(fp))
        except Exception:
            continue
        sm = None
        try:
            import importlib
            g = importlib.import_module('llm_gate')
            sm = g.shadow_summary(st)
        except Exception:
            sh = st.get('shadow') or {}
            sm = {'open': sh.get('open') or [], 'done': (sh.get('done') or [])[-20:],
                  'stats': sh.get('stats') or {}}
        stt = sm['stats'] or {}
        for k in tot:
            tot[k] += stt.get(k, 0) or 0
        for x in (sm.get('open') or []):
            x = dict(x); x['_pool'] = label
            all_open.append(x)
        rd = ''
        for x in list(reversed(sm.get('done') or []))[:5]:
            v = x.get('verdict')
            cls = 'up' if v == 'wrong' else ('down' if v == 'correct' else 'dim')
            tag = {'correct': '避跌✓', 'wrong': '踏空✗', 'flat': '持平'}.get(v, v)
            rd += (f'<tr><td class="dim">{str(x.get("ts",""))[5:16]}</td>'
                   f'<td>{html.escape(str(x.get("name") or x.get("symbol","")))}</td>'
                   f'<td class="dim">{html.escape(str(x.get("note") or ""))}</td>'
                   f'<td class="{cls}">{tag} {x.get("ret",0):+.2f}%</td>'
                   f'<td class="dim">{x.get("max_ret",0):+.1f}% / {x.get("min_ret",0):+.1f}%</td></tr>')
        n = stt.get('n', 0)
        head = (f'<div style="font-size:12px;color:var(--dim);margin-bottom:6px">'
                f'已结算 {n} 条 · 避跌 <b class="up">{stt.get("correct",0)}</b> · '
                f'踏空 <b class="down">{stt.get("wrong",0)}</b> · 持平 {stt.get("flat",0)}'
                + (f' · 否决后均收益 <b>{stt.get("avg_ret",0):+.2f}%</b>'
                   f'（期间最大 {stt.get("avg_max",0):+.1f}% / 最小 {stt.get("avg_min",0):+.1f}%）'
                   if n else '') +
                f' · 跟踪中 {len(sm.get("open") or [])} 条</div>')
        body = head
        if rd:
            body += ('<div class="tblwrap"><table><tr><th>否决时间</th><th>标的</th><th>理由</th>'
                     '<th>3日后</th><th>期间 最高/最低</th></tr>' + rd + '</table></div>')
        elif not (sm.get('open') or []):
            body += '<div class="dim" style="font-size:12px">暂无记录（还没有被否决的买入）</div>'
        cards.append(f'<div class="card" style="flex:1;min-width:320px"><h2>🎯 {label} 否决单跟踪</h2>{body}</div>')
    if not cards:
        return ''
    n = tot['n']
    summary = (f'<div class="card"><h2>🎯 闸门有效性（否决单影子跟踪）</h2>'
               f'<div style="font-size:13px">已结算 <b>{n}</b> 条 · 避跌 <b class="up">{tot["correct"]}</b> · '
               f'踏空 <b class="down">{tot["wrong"]}</b> · 持平 {tot["flat"]}</div>'
               + (f'<div style="font-size:12px;color:var(--dim);margin-top:6px">'
                  f'否决后 {os.environ.get("BN_SHADOW_DAYS","3")} 天平均收益 <b>{tot["avg_ret"]:+.2f}%</b>'
                  f'（<span class="down">负=否决有效</span>／<span class="up">正=踏空</span>）'
                  f' · 平均最大回撤 {tot["avg_min"]:+.1f}% · 平均最高反弹 {tot["avg_max"]:+.1f}%</div>'
                  if n else '<div style="font-size:12px;color:var(--dim);margin-top:6px">还没有结算数据，'
                            '被 LLM 否决的买入会登记为影子单，跟踪 3 天后自动结算</div>') +
               '</div>')
    # 跟踪中的明细
    if all_open:
        rows = ''
        for x in all_open[:10]:
            r = x.get('ret_now', 0)
            cls = 'up' if r > 0 else 'down'
            rows += (f'<tr><td class="dim">{str(x.get("ts",""))[5:16]}</td>'
                     f'<td class="dim">{x["_pool"]}</td>'
                     f'<td>{html.escape(str(x.get("name") or x.get("symbol","")))}</td>'
                     f'<td class="dim">{html.escape(str(x.get("note") or ""))}</td>'
                     f'<td>{x.get("price",0):,.2f}</td><td>{x.get("last",0):,.2f}</td>'
                     f'<td class="{cls}">{r:+.2f}%</td></tr>')
        summary += (f'<div class="card"><h2>跟踪中（未到结算日）</h2><div class="tblwrap"><table>'
                    f'<tr><th>否决时间</th><th>池</th><th>标的</th><th>理由</th><th>否决价</th>'
                    f'<th>现价</th><th>至今</th></tr>{rows}</table></div></div>')
    return summary + ''.join(cards)


def main():
    ex = make_fex()
    ex.load_markets()
    while True:
        try:
            procs = proc_status()
            # 日线池
            pos_list, total_pnl = positions(ex, SYMBOLS, NAMES, CN_NAMES, pool_start=float(os.environ.get('POOL_START', '100')))
            # 短线池
            short_pos, short_pnl = positions(ex, SYMBOLS_SHORT, NAMES_SHORT, CN_SHORT, pool_start=float(os.environ.get('SHORT_POOL', '100')))
            bal = float(ex.fetch_balance()['info'].get('totalWalletBalance', 0))
            from trade_log import read_trades
            trades = read_trades(100)
            page = build_html(procs, pos_list, total_pnl, bal, trades, trade_stats(trades),
                              short_pos=short_pos, short_pnl=short_pnl, short_n=len(SYMBOLS_SHORT))
            for o in OUTS:
                with open(o, 'w') as f:
                    f.write(page)
        except Exception as e:
            for o in OUTS:
                with open(o, 'w') as f:
                    f.write(f'<html><body><h1>状态页生成失败</h1><pre>{html.escape(str(e))}</pre></body></html>')
        time.sleep(30)


if __name__ == '__main__':
    main()
