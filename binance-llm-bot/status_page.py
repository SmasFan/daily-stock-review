#!/usr/bin/env python3
"""状态页生成器: 聚合进程/持仓/净值/日志 -> status.html
常驻, 每 30s 刷新页面文件。浏览器/curl 随时看。
"""
import os, subprocess, time, html, json, sys
from datetime import datetime, timezone
import ccxt
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
# 路径全部相对脚本位置推导（原硬编码 /mnt/c/Users/z7280/...，WSL 之外也能跑）
BASE = os.path.dirname(os.path.abspath(__file__))
DSR = os.path.dirname(BASE)
OUT = os.path.join(BASE, 'status.html')
# 双输出: 原位置 + 每日复盘仓库镜像（供 daily-stock-review/binance.html iframe 集成）
MIRROR = os.path.join(DSR, 'data', 'binance_status.html')
# 结构化数据：菜单栏 app（dsrbar）与外部程序读这个，不用解析 HTML
STATUS_JSON = os.path.join(DSR, 'data', 'binance_status.json')
OUTS = [OUT, MIRROR]
PROXY = os.environ.get('PROXY', '' if sys.platform == 'darwin' else 'socks5h://172.25.16.1:10808')
SYMBOLS = ['TSLA/USDT:USDT', 'COIN/USDT:USDT', 'PLTR/USDT:USDT', 'MSTR/USDT:USDT', 'HOOD/USDT:USDT']
# 短线池标的 (trader_short)
SYMBOLS_SHORT = ['NVDA/USDT:USDT', 'META/USDT:USDT', 'AMZN/USDT:USDT', 'QQQ/USDT:USDT',
                 'SPY/USDT:USDT', 'GOOGL/USDT:USDT', 'INTC/USDT:USDT', 'CRCL/USDT:USDT']
# 止损口径与 trader100 / trader_short / guard.py 保持一致（2026-11-10 日内混合改造）
DAILY_SL_PCT = float(os.environ.get('SL_PCT', '0.02'))
SHORT_SL_PCT = float(os.environ.get('SHORT_SL_PCT', '0.02'))
SHORT_STOP_PCT = float(os.environ.get('BN_SHORT_SL', '0.02'))
ALLOW_SHORT = os.environ.get('BN_ALLOW_SHORT', '0').lower() not in ('0', 'false', 'off', 'no')
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

# 利润保护下一动作 → 徽章文案
PG_TAG = {'close': ('待止盈', 'b-err'), 'trim': ('待减仓', 'b-warn'),
          'be_lock': ('待保本', 'b-info'), 'hold': ('持有', 'b-info')}


def make_fex():
    ex = ccxt.binance({
        'apiKey': os.environ.get('BN_API_KEY', ''),
        'secret': os.environ.get('BN_SECRET', ''),
        'enableRateLimit': True,
        'timeout': 60000,   # demo-api 的 exchangeInfo 实测 14.5s，默认超时会抛 NetworkError
        'proxies': {'http': PROXY, 'https': PROXY},
        'options': {'defaultType': 'future', 'adjustForTimeDifference': True,
                # 只拉 linear（demo-fapi）；demo-api / demo-dapi 常超时，会让 load_markets 整体失败
                'fetchMarkets': ['linear']},
    })
    # 禁 keep-alive: 低频轮询下复用连接会被代理回收导致 RemoteDisconnected
    ex.session.headers['Connection'] = 'close'
    ex.enable_demo_trading(True)
    return ex


def proc_status():
    """跨平台进程检测。

    不用 pgrep -f：macOS 的 pgrep 是 basic regex，模式里的 `|` 不生效，
    `status_page.py$|status_page.py ` 会永远匹配失败（页面显示“未运行”）。
    改用 ps -axo 解析命令行，只认 python 解释器启动的脚本名，避免
    pgrep/ps 自己或 shell 包装命令被误算进去。
    """
    try:
        out = subprocess.check_output(['ps', '-axo', 'pid=,command='],
                                      stderr=subprocess.DEVNULL).decode('utf-8', 'replace')
    except Exception:
        out = ''
    all_pids = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, cmd = parts
        toks = cmd.split()
        if not toks or 'python' not in toks[0].lower():
            continue
        for name in ['trader100.py', 'trader_short.py', 'guard.py', 'status_page.py', 'watchdog.py', 'perp_sim.py']:
            # perp_sim.py 以 --loop 启动，命令行带参数，不能用 endswith
            hit = (name + ' ' in cmd or name in cmd) if name == 'perp_sim.py' else \
                  (cmd.endswith(' ' + name) or cmd.endswith('/' + name))
            if hit:
                all_pids.setdefault(name, []).append(pid)
    rows = []
    for name in ['trader100.py', 'trader_short.py', 'guard.py', 'status_page.py', 'watchdog.py', 'perp_sim.py']:
        pids = all_pids.get(name, [])
        label = 'perp_sim.py · 全天候盘' if name == 'perp_sim.py' else name
        rows.append((label, '运行' if pids else '挂掉', ','.join(pids)))
    # cron keepalive: 常驻检测不适用, 显示为调度项
    rows.append(('keepalive (cron每分钟)', '调度中', '-'))
    # 全天候盘状态以数据新鲜度为准（进程可能在另一台机器跑）
    try:
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'binance_perp.json')
        if os.path.exists(p):
            with open(p) as f:
                gen = json.load(f).get('generatedAt', '')
            ts = datetime.fromisoformat(gen.replace('Z', '+00:00'))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            age = max(0, age)
            rows.append(('全天候盘数据', '新鲜' if age < 1800 else '过期', '%d 分钟前' % (age / 60)))
    except Exception:
        pass
    return rows


def load_state(fname):
    """读 bot 状态 json（失败返回空 dict，状态页绝不能因为读不到就挂）"""
    if not fname:
        return {}
    try:
        return json.load(open(os.path.join(BASE, fname)))
    except Exception:
        return {}


def positions(ex, syms, names, cn_map, pool_start=100.0, profile='daily', state_file=None,
              pg_profile='intraday'):
    """取持仓 + 利润保护状态。

    peak/trimmed/sl 来自 profit_guard 写进 state 的字段；
    下一步动作直接复用 profit_guard.plan()，保证「页面显示」与「bot 实际行为」一致。
    profile 只作池标签（daily/short），利润保护阈值统一用 pg_profile='intraday'，
    与 2026-11-10 日内混合策略的实际 scan(profile='intraday') 对齐。
    """
    pstate = (load_state(state_file).get('real_pos') or {})
    try:
        import profit_guard
        pcfg = profit_guard.cfg(pg_profile)
    except Exception:
        profit_guard, pcfg = None, None
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
            # 方向感知：策略只做多，但历史事故会留反向仓（META -18.56 张）：
            # 空头的涨跌幅/名义/杠杆全部要反号并取绝对值，否则页面显示 +1.18% / -13724 这种鬼数字。
            try:
                import pos_guard as _pg
                side = _pg.side_of(p)
            except Exception:
                side = 'long' if float(p.get('contracts') or 0) > 0 else 'flat'
            sign = -1 if side == 'short' else 1
            pct = (mark / entry - 1) * 100 * sign
            info = p.get('info', {})
            notional = abs(float(info.get('notional') or amt * mark))
            init_margin = float(info.get('positionInitialMargin') or info.get('initialMargin') or 0)
            lev = round(notional / init_margin, 1) if init_margin > 0 else 0
            total_pnl += pnl
            # ---- T+0 利润保护状态 ----
            ps = pstate.get(s) or {}
            trimmed = bool(ps.get('trimmed'))
            # 多头看最高价、空头看最低价；profit_guard.update_peak 已按 side 写入对应极值
            _raw_peak = float(ps.get('peak') or 0)
            if side == 'short':
                peak = min(_raw_peak, mark) if _raw_peak else mark
                peak_pct = (1 - peak / entry) * 100 if entry else 0.0
                dd_peak = (mark / peak - 1) * 100 if peak else 0.0
                sl_default = entry * (1 + SHORT_STOP_PCT)
            else:
                peak = max(_raw_peak, mark)
                peak_pct = (peak / entry - 1) * 100 if entry else 0.0
                dd_peak = (1 - mark / peak) * 100 if peak else 0.0
                sl_default = entry * (1 - (SHORT_SL_PCT if s in SYMBOLS_SHORT else DAILY_SL_PCT))
            sl = float(ps.get('sl') or 0) or sl_default        # 保本上移(多)/下移(空)后会写进 state['sl']
            act = 'hold'
            if profit_guard and pcfg:
                try:
                    act = profit_guard.plan(entry, mark, peak, pcfg,
                                            trimmed=trimmed, side=side)['act']
                except Exception:
                    act = 'hold'
            pos_list.append({
                'sym': names.get(s, s), 'cn': cn_map.get(s, ''), 'amt': amt, 'entry': entry, 'mark': mark,
                'pnl': pnl, 'pct': pct, 'liq': liq, 'sl': sl, 'sl_default': sl_default,
                'lev': lev, 'notional': notional, 'side': side,
                'peak': peak, 'peak_pct': peak_pct, 'dd_peak': dd_peak,
                'trimmed': trimmed, 'pg_act': act,
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
        return f'''<div class="card"><h2>AI 复盘 <span class="badge {act_badge[1]}" style="margin-left:8px">{act_badge[0]}</span></h2>
<div style="font-size:12px;color:var(--dim);margin-bottom:6px">{r.get('ts','')} · 市场:{html.escape(state)} · 已实现 {r.get('realized_pnl',0):+.2f}U 浮动 {r.get('unrealized_pnl',0):+.2f}U</div>
<div style="font-size:13px;line-height:1.7">{summary}</div>
<div style="font-size:12px;color:var(--dim);margin-top:8px;line-height:1.6">风险：{risk}</div>{adj_html}</div>'''
    except Exception as e:
        return f'<div class="card"><h2>AI 复盘</h2><div class="dim">读取失败 {html.escape(str(e))}</div></div>'
    path = os.path.join(BASE, fname)
    if not os.path.exists(path):
        return ['(无日志)']
    with open(path, 'r', errors='ignore') as f:
        return f.readlines()[-lines:]


def pool_of(t):
    """记录归属池：优先显式 pool 标签；旧记录（pool=None）按标的集合推断（日线 5 标 / 短线 8 标）

    历史记录没有标签，但标的是不重叠的，所以能准确归类；只是那些旧的 pnl 没存下来（=0），
    金额上从本次修复后开始准确。
    """
    p = t.get('pool')
    if p in ('daily', 'short'):
        return p
    return 'daily' if t.get('symbol') in SYMBOLS else 'short'


def trade_stats(trades, pool=None):
    """汇总: 交易数/胜率/累计盈亏（可按池过滤：pool='daily'|'short'|None=全部）"""
    closed = [t for t in trades if t['type'] in ('close', 'sl') and (pool is None or pool_of(t) == pool)]
    wins = [t for t in closed if t.get('pnl', 0) > 0]
    total_pnl = sum(t.get('pnl', 0) for t in closed)
    return len(closed), len(wins), total_pnl


def realized_pnl(trades, pool):
    """池内已实现盈亏（带 pool 标签 or 按标的归属；历史 pool=None 且 pnl 假 0 的记录不影响总额）"""
    return sum(float(t.get('pnl') or 0) for t in trades
               if t['type'] in ('close', 'sl') and pool_of(t) == pool)


PAGE_CSS = r"""
:root{
  --bg:#f2f3f5; --panel:#fff; --panel2:#fafbfc; --border:#e3e5e8; --border2:#f0f1f3;
  --txt:#16181d; --dim:#6b7280; --muted:#9aa1ab;
  --up:#c62828; --down:#1b7f3b; --warn:#8a5900; --info:#1f5fa8;
  --shadow:none;
}
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--txt);font:13px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding:14px 20px 36px}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:12px;padding-bottom:10px;margin-bottom:10px;border-bottom:1px solid var(--border)}
.brand{display:flex;align-items:baseline;gap:10px;min-width:0}
h1{font-size:16px;font-weight:600;letter-spacing:0;white-space:nowrap}
.brand .sub{font-size:11px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.live{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--dim);white-space:nowrap}
.live b{color:var(--txt);font-weight:600}
.dot{width:6px;height:6px;border-radius:1px;background:#1b7f3b}
.dot.idle{background:#c98a00}
.banner{padding:7px 10px;border:1px solid;border-radius:3px;margin-bottom:10px;font-size:11px;line-height:1.5}
.banner .b-title{font-weight:600;margin-right:8px}
.banner .b-text{color:var(--dim)}
.banner.ok{background:#f0f7f2;border-color:#cfe6d8}
.banner.idle{background:#fdf6e7;border-color:#f0dfb8}
.banner.warn{background:#fdf0ef;border-color:#f0cfcb}
.nav{display:flex;gap:16px;flex-wrap:wrap;margin:0 0 12px;padding-bottom:8px;border-bottom:1px solid var(--border)}
.nav a{color:var(--dim);text-decoration:none;font-size:11px}
.nav a:hover{color:var(--txt);text-decoration:underline}
.hero{display:grid;grid-template-columns:1.6fr 1fr 1fr 1fr;border:1px solid var(--border);border-radius:3px;background:var(--panel);overflow:hidden;margin-bottom:12px}
.hero-main,.kpi{padding:12px 14px;border-right:1px solid var(--border);min-width:0}
.hero-main{background:#fbfbfc}
.kpi:last-child{border-right:0}
.k-label{font-size:10.5px;color:var(--dim);margin-bottom:6px}
.big{font-size:28px;line-height:1.15;font-weight:600;font-variant-numeric:tabular-nums;letter-spacing:-.3px}
.unit{font-size:12px;font-weight:400;color:var(--dim);margin-left:4px}
.hero-delta{margin-top:4px;font-size:12px;font-weight:600;font-variant-numeric:tabular-nums}
.hero-delta span{font-size:10.5px;font-weight:400;color:var(--muted);margin-left:6px}
.hero-meta{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;padding-top:8px;border-top:1px solid var(--border);font-size:11px;color:var(--dim)}
.hero-meta b{color:var(--txt);font-weight:600}
.k-val{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums}
.k-sub{font-size:10.5px;color:var(--dim);margin-top:4px;line-height:1.5}
.bar{height:3px;background:#eceef1;overflow:hidden;margin-top:9px}
.bar>span{display:block;height:100%;background:#525a66}
.mini{display:flex;gap:10px;flex-wrap:wrap;font-size:10.5px;color:var(--dim);margin-top:8px}
.panel,.card{background:var(--panel);border:1px solid var(--border);border-radius:3px;padding:13px 14px;margin-bottom:12px}
.panel-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:9px;flex-wrap:wrap}
.panel-head h2,.section-head h2,.card h2{font-size:13px;font-weight:600;color:var(--txt);display:flex;align-items:center;gap:6px}
.panel-head h2::before,.section-head h2::before,.card h2::before{content:'';width:3px;height:12px;background:var(--txt);border-radius:0}
.hint{font-size:10.5px;color:var(--muted)}
.split{display:grid;grid-template-columns:minmax(0,1.9fr) minmax(280px,1fr);gap:12px;align-items:start}
.side{display:flex;flex-direction:column;gap:12px}
.pool-block+.pool-block{margin-top:14px;padding-top:12px;border-top:1px solid var(--border)}
.pool-head{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:6px;font-size:11px;color:var(--dim)}
.pool-title{font-size:12px;font-weight:600;color:var(--txt)}
.pool-pnl{margin-left:auto;font-weight:600;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-size:12px}
.tblwrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table td,table th{white-space:nowrap}
th{text-align:left;color:var(--dim);font-weight:500;padding:6px 8px;border-bottom:1px solid var(--border);font-size:10.5px;background:#fafbfc}
td{padding:6px 8px;border-bottom:1px solid var(--border2);font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
tr:hover td{background:#f8f9fa}
.up{color:var(--up)}.down{color:var(--down)}.dim{color:var(--dim)}.gold{color:var(--warn)}
.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11px}
.coin{display:inline-flex;align-items:center;gap:6px;font-weight:600}
.cdot{width:18px;height:18px;border-radius:2px;display:inline-flex;align-items:center;justify-content:center;font-size:9px;font-weight:600;color:#fff}
.badge{display:inline-block;padding:1px 5px;border-radius:2px;font-size:10px;font-weight:500;line-height:1.5;background:#f0f1f3;color:#4b5563}
.b-open{background:#e8f5ec;color:#1b7f3b}
.b-close{background:#eef0f4;color:#374151}
.b-sl{background:#fbecec;color:#a12323}
.b-warn{background:#fdf4e3;color:#8a5900}
.b-err{background:#fbecec;color:#a12323}
.b-info{background:#edf3fb;color:#1f5fa8}
.proc-list{display:flex;flex-direction:column}
.proc{display:flex;align-items:center;gap:6px;padding:6px 0;border-bottom:1px solid var(--border2);font-size:11px}
.proc:last-child{border-bottom:none}
.proc-name{flex:1;color:var(--txt)}
.pid{color:var(--muted);font-size:10px;font-family:ui-monospace,Consolas,monospace}
.pill{display:inline-block;width:6px;height:6px;border-radius:1px;margin-right:2px}
.ok{background:#1b7f3b}
.dead{background:#c62828}
.idle{background:#c98a00}
.note{font-size:10.5px;color:var(--muted);line-height:1.6;margin-top:8px}
.param-grid{display:grid;grid-template-columns:1fr;gap:8px}
.param-strip{display:grid;grid-template-columns:repeat(6,1fr);gap:8px}
.param-strip .pitem{background:#fafbfc;border:1px solid var(--border);border-radius:2px;padding:8px 10px}
.param-strip .pitem span{display:block;font-size:10px;color:var(--dim);margin-bottom:3px}
.param-strip .pitem b{font-size:11px;font-weight:600;color:var(--txt)}
@media(max-width:1100px){.param-strip{grid-template-columns:repeat(3,1fr)}}
@media(max-width:900px){.param-strip{grid-template-columns:1fr 1fr}}
@media(max-width:600px){.param-strip{grid-template-columns:1fr}}
.param{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-bottom:1px solid var(--border2);font-size:11px}
.param:last-child{border-bottom:none}
.param span{color:var(--dim)}
.param b{font-weight:600;text-align:right}
.section-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:16px 0 8px;flex-wrap:wrap}
.risk-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px;align-items:start}
.risk-grid>.card{margin-bottom:0}
.logcard{background:#fafbfc;border:1px solid var(--border);border-radius:2px;margin-bottom:6px;overflow:hidden}
.logcard summary{padding:7px 10px;cursor:pointer;font-size:11px;color:var(--dim);font-weight:500}
.logcard summary:hover{color:var(--txt)}
.logline{color:#4b5563;font-size:10.5px;padding:1px 10px;font-family:ui-monospace,Consolas,monospace;line-height:1.55;white-space:pre-wrap;word-break:break-all}
.logline:last-child{padding-bottom:8px}
.empty{padding:16px 8px;text-align:center;color:var(--muted);font-size:11px}
@media(max-width:1000px){.hero{grid-template-columns:1fr 1fr}.hero-main,.kpi{border-bottom:1px solid var(--border)}.split{grid-template-columns:1fr}}
@media(max-width:620px){.wrap{padding:12px 12px 28px}.topbar{flex-direction:column;align-items:flex-start}.big{font-size:24px}.hero{grid-template-columns:1fr}.hero-main,.kpi{border-right:0}.risk-grid{grid-template-columns:1fr}}
"""


def llm_route_label():
    """从仓库根 config/llm_routing.json 读当前决策路由，显示在策略参数卡。"""
    try:
        with open(os.path.join(DSR, 'config', 'llm_routing.json'), encoding='utf-8') as f:
            r = json.load(f)['routes']
        macro = r['macro']['model'].split('/')[-1]
        dec = r['bn_gate']['model'].split('/')[-1]
        local_only = os.environ.get('DSR_LLM_LOCAL_ONLY', '0') in ('1', 'true', 'yes', 'on')
        return ('本地初稿（强制纯本地）' if local_only else
                '交易复核 %s · 宏观 %s + 联网' % (dec, macro))
    except Exception:
        return '交易复核本地 · 宏观云端 ds4.1'


def dump_status_json(procs, pos_list, short_pos, bal, trades,
                     pool, pool_start, short_pool, short_pool_start,
                     daily_realized, short_realized, total_pnl, short_pnl):
    """把状态页用到的同一批数字写成 JSON，供菜单栏 app 直接读。

    口径与页面一致：净值 = 起始 + 已实现 + 未实现；账户总额以 ACCOUNT_START 为初始本金。
    todayRet 需要前一日基准，故把上一次的净值与日期存在同一个 JSON 里滚动。
    """
    acct_start = float(os.environ.get('ACCOUNT_START', '100'))
    equity = acct_start + daily_realized + short_realized + total_pnl + short_pnl
    now = datetime.now()
    prev = {}
    try:
        with open(STATUS_JSON, encoding='utf-8') as f:
            prev = json.load(f) or {}
    except Exception:
        prev = {}
    day = now.strftime('%Y-%m-%d')
    day_open = float(prev.get('dayOpen') or 0)
    if prev.get('dayDate') != day or day_open <= 0:
        day_open, day_date = equity, day
    else:
        day_date = prev.get('dayDate')
    today_ret = (equity / day_open - 1) * 100 if day_open else 0.0

    def pack(rows, profile):
        out = []
        for p in rows:
            out.append({
                'symbol': p['sym'], 'name': p['cn'] or p['sym'], 'side': p.get('side', 'long'),
                'amt': round(p['amt'], 6), 'entry': round(p['entry'], 4), 'mark': round(p['mark'], 4),
                'pnlPct': round(p['pct'], 2), 'pnlUsd': round(p['pnl'], 2),
                'lev': p['lev'], 'notional': round(p['notional'], 2),
                'sl': round(p['sl'], 4), 'pgAct': p.get('pg_act', 'hold'), 'pool': profile,
            })
        return out

    positions = pack(pos_list, 'daily') + pack(short_pos, 'short')
    n_long = sum(1 for p in positions if p['side'] == 'long')
    n_short = sum(1 for p in positions if p['side'] == 'short')
    today_trades = [t for t in trades if str(t.get('ts') or t.get('time') or '').startswith(day)]
    data = {
        'generatedAt': now.strftime('%Y-%m-%d %H:%M:%S'),
        'account': {
            'start': round(acct_start, 2), 'equity': round(equity, 4),
            'pnl': round(equity - acct_start, 4),
            'pnlPct': round((equity / acct_start - 1) * 100, 3) if acct_start else 0.0,
            'todayRet': round(today_ret, 3), 'dayOpen': round(day_open, 4), 'dayDate': day_date,
            'demoWallet': round(float(bal or 0), 2),
        },
        'daily': {
            'start': round(pool_start, 2), 'pool': round(pool, 4),
            'pnl': round(pool - pool_start, 4),
            'pnlPct': round((pool / pool_start - 1) * 100, 3) if pool_start else 0.0,
            'realized': round(daily_realized, 4), 'unrealized': round(total_pnl, 4),
            'n': sum(1 for p in positions if p['pool'] == 'daily'),
            'slots': len(SYMBOLS),
        },
        'short': {
            'start': round(short_pool_start, 2), 'pool': round(short_pool, 4),
            'pnl': round(short_pool - short_pool_start, 4),
            'pnlPct': round((short_pool / short_pool_start - 1) * 100, 3) if short_pool_start else 0.0,
            'realized': round(short_realized, 4), 'unrealized': round(short_pnl, 4),
            'n': sum(1 for p in positions if p['pool'] == 'short'),
            'slots': len(SYMBOLS_SHORT),
        },
        'positions': positions,
        'nPos': len(positions), 'nLong': n_long, 'nShort': n_short,
        'todayTrades': len(today_trades),
        'procs': procs,
    }
    # 竞技场上线后，Go 菜单栏仍只读 binance_status.json 的旧字段（用户要求不重编译 app）。
    # 这里用 arena 口径覆盖，让菜单栏常驻标题 / tooltip 不再是过期数据。
    _arena = _arena_data()
    if _arena:
        _apos = _arena.get('positions') or []
        data['account']['equity'] = round(float(_arena.get('equity') or equity), 4)
        data['account']['pnl'] = round(float(_arena.get('cumPnl') or 0), 4)
        data['account']['pnlPct'] = round(float(_arena.get('cumPnlPct') or 0), 3)
        data['account']['todayRet'] = round(float(_arena.get('dayPnlPct') or 0), 3)
        data['nPos'] = len(_apos)
        data['nLong'] = sum(1 for p in _apos if p.get('side') == 'long')
        data['nShort'] = sum(1 for p in _apos if p.get('side') == 'short')
    os.makedirs(os.path.dirname(STATUS_JSON), exist_ok=True)
    tmp = STATUS_JSON + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, STATUS_JSON)
    return data



ARENA_JSON = os.path.join(BASE, '..', 'data', 'binance_arena.json')


def _arena_data():
    try:
        with open(ARENA_JSON) as f:
            return json.load(f)
    except Exception:
        return None


def arena_panels():
    """策略竞技场：6 策略排名 + 当前持仓（arena_engine 写 data/binance_arena.json）。"""
    a = _arena_data()
    if not a:
        return ('<div class="panel" id="positions"><div class="panel-head"><h2>策略竞技场</h2>'
                '<span class="hint">等待 arena_engine 首轮</span></div>'
                '<div class="note">6 个策略共用 100U、同一批 23 只标的、symbol 级仲裁。'
                '启动 arena_engine.py 后这里显示排名与持仓。</div></div>')
    eq = float(a.get('equity') or 0)
    ret = float(a.get('returnPct') or 0)
    cls = 'up' if ret > 0 else ('down' if ret < 0 else 'dim')
    rows = []
    for i, s in enumerate(a.get('ranked') or [], 1):
        pc = float(s.get('pnl_pct') or 0)
        c2 = 'up' if pc > 0 else ('down' if pc < 0 else 'dim')
        wr = ('%.1f%%' % s['win_rate']) if s.get('win_rate') is not None else '—'
        rows.append(
            '<tr><td>%d</td><td><b>%s</b><br><span class="dim">%s</span></td><td>%g%%</td>'
            '<td class="%s"><b>%+.2f%%</b></td><td class="%s">%+.2f U</td><td>%s</td>'
            '<td>%d</td><td>%d/%d</td><td>%.0fU</td></tr>'
            % (i, s.get('id') or '', s.get('name') or '', float(s.get('weight') or 0) * 100,
               c2, pc, c2, float(s.get('pnl_usd') or 0), wr,
               int(s.get('trades') or 0), int(s.get('open') or 0),
               int(s.get('max_pos') or 0), float(s.get('notional') or 0)))
    def _nm(p):
        code = p.get('name') or p.get('symbol', '').split('/')[0]
        cn = p.get('cn') or ''
        return (code + ' <span class="dim">' + cn + '</span>') if cn else code
    pos = a.get('positions') or []
    prow = ''.join(
        '<tr><td>%s</td><td>%s</td><td class="%s">%s</td><td>%.6g</td><td>%.2f</td>'
        '<td>%.0fU</td><td>%.1fU</td><td>%.1fx</td><td>%s</td><td>%s</td><td class="%s">%s</td></tr>'
        % (_nm(p), p.get('strategy') or '?',
           'up' if p.get('side') == 'long' else 'down',
           '多' if p.get('side') == 'long' else '空', float(p.get('amt') or 0),
           float(p.get('entry') or 0), float(p.get('notional') or 0),
           float(p.get('margin') or 0),
           (float(p.get('notional') or 0) / float(p['margin'])) if p.get('margin') else 0.0,
           p.get('holdText') or '—',
           ('%.2f' % p['sl']) if p.get('sl') else '—',
           ('up' if p['dayChange'] > 0 else ('down' if p['dayChange'] < 0 else 'dim'))
           if p.get('dayChange') is not None else 'dim',
           ('%+.2f%%' % p['dayChange']) if p.get('dayChange') is not None else '—')
        for p in pos) or '<tr><td colspan="11" class="dim">当前空仓</td></tr>'
    return (
        '<div class="panel" id="positions">'
        '<div class="panel-head"><h2>策略竞技场</h2>'
        '<span class="hint">%.2f U · 收益 <b class="%s">%+.2f%%</b> · %gx 杠杆 · 已平仓 %d 笔 · '
        '每 %.0fmin 巡检 · %g 标</span></div>'
        '<div class="note" style="margin-bottom:10px">6 个策略共用同一 100U 与同一批标的：'
        '空仓时按胜率优先级（S1&gt;S5&gt;S2&gt;S6&gt;S4）仲裁，每只票同一时刻只归一个策略；'
        '平仓只由开仓策略决定。收益按各策略分配的权重折算，胜率按该策略自己的成交统计。</div>'
        '<div class="tblwrap"><table><tr><th>#</th><th>策略</th><th>权重</th><th>收益</th>'
        '<th>盈亏</th><th>胜率</th><th>成交</th><th>持仓</th><th>单笔名义</th></tr>'
        '%s</table></div>'
        '<div class="panel-head" style="margin-top:18px"><h2>当前持仓</h2>'
        '<span class="hint">共 %d 笔 · 已实现 %+.2f U · 浮动 %+.2f U</span></div>'
        '<div class="tblwrap"><table><tr><th>标的</th><th>策略</th><th>方向</th><th>张数</th>'
        '<th>入场</th><th>名义</th><th>保证金</th><th>杠杆</th><th>持仓时间</th><th>止损</th>'
        '<th>当日涨幅</th></tr>%s</table></div>'
        '</div>' % (eq, cls, ret, float(a.get('lev') or 5), int(a.get('trades') or 0),
                    float(a.get('loopMin') or 15),
                    int(a.get('symbols') or 0), ''.join(rows), len(pos),
                    float(a.get('realized') or 0), float(a.get('unrealized') or 0), prow))


def build_html(procs, pos_list, total_pnl, bal, trades, tstats,
               short_pos=None, short_pnl=0.0, short_n=8,
               daily_realized=0.0, short_realized=0.0, short_bug_loss=0.0):
    badge = {'open': ('开仓', 'b-open'), 'close': ('平仓', 'b-close'),
             'sl': ('止损', 'b-sl'), 'cooldown': ('熔断', 'b-warn'),
             'error': ('错误', 'b-err'), 'decision': ('决策', 'b-info')}
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    short_pos = short_pos or []
    # 日线池净值 = 起始 + 已实现 + 未实现（旧版只看未实现，平仓后盈亏“消失”，这才是收益对不上的根因之一）
    pool_start = float(os.environ.get('POOL_START', '100'))
    pool = pool_start + daily_realized + total_pnl
    # 短线池净值
    short_pool_start = float(os.environ.get('SHORT_POOL', '100'))
    short_pool = short_pool_start + short_realized + short_pnl
    short_lev = float(os.environ.get('SHORT_LEV', '5'))
    daily_lev = float(os.environ.get('LEV', '5'))
    HDR = ('<tr><th>标的</th><th>持仓量</th><th>入场价</th><th>Mark 价</th><th>杠杆</th><th>名义</th>'
           '<th>盈亏</th><th>盈亏额</th><th title="持仓期间最高价对应的浮盈">峰值浮盈</th>'
           '<th title="利润保护下一步动作">保护状态</th><th>止损价</th><th>爆仓价</th></tr>')

    def pos_rows(lst):
        """持仓行：额外渲染 T+0 利润保护的 峰值/回撤/下一步动作"""
        out = ''
        for p in lst:
            cls = 'up' if p['pct'] > 0 else 'down'
            cc = COIN_COLOR.get(p['sym'], '#888')
            act = p.get('pg_act', 'hold')
            tag, bc = PG_TAG.get(act, ('持有', 'b-info'))
            badge_html = (f'<span class="badge {bc}">{tag}</span>' if act != 'hold'
                          else '<span class="dim">持有</span>')
            if p.get('trimmed'):
                badge_html += '<br><span class="dim" style="font-size:11px">已减半</span>'
            # 止损价已上移(多)/下移(空)到保本以上 → 用金色标出来（盈利单不会再变亏损单）
            if p.get('side') == 'short':
                sl_cls = 'gold' if p['sl'] < p['sl_default'] * 0.9999 else 'dim'
            else:
                sl_cls = 'gold' if p['sl'] > p['sl_default'] * 1.0001 else 'dim'
            short_badge = (' <span class="badge b-sl">空</span>'
                          if p.get('side') == 'short' else '')
            out += (f'<tr><td><span class="coin"><span class="cdot" style="background:{cc}">{p["sym"][0]}</span>{p["cn"]}'
                    f'{short_badge}'
                    f' <span class="dim" style="font-size:12px">{p["sym"]}</span></span></td>'
                    f'<td>{p["amt"]:.4f}<br><span class="dim" style="font-size:11px">≈${p["notional"]:,.1f}</span></td>'
                    f'<td>{p["entry"]:,.2f}</td><td>{p["mark"]:,.2f}</td>'
                    f'<td><span class="badge b-info">{p["lev"]}x</span></td>'
                    f'<td class="dim">{p["notional"]:,.1f}</td>'
                    f'<td class="{cls}">{p["pct"]:+.2f}%</td>'
                    f'<td class="{cls}">{p["pnl"]:+.2f} U</td>'
                    f'<td class="{"up" if p["peak_pct"] > 0 else "dim"}">{p["peak_pct"]:+.2f}%'
                    f'<br><span class="dim" style="font-size:11px">回撤 {p["dd_peak"]:.2f}%</span></td>'
                    f'<td>{badge_html}</td>'
                    f'<td class="{sl_cls}">{p["sl"]:,.2f}</td>'
                    f'<td class="dim">{p["liq"]:,.0f}</td></tr>')
        return out

    if pos_list:
        rows_c = pos_rows(pos_list)
    else:
        rows_c = '<tr><td colspan=12 class="dim">空仓 — 等待趋势信号</td></tr>'
    # 短线池持仓行
    if short_pos:
        rows_s = pos_rows(short_pos)
    else:
        rows_s = '<tr><td colspan=12 class="dim">空仓 — 等待趋势信号</td></tr>'
    pool_cls = 'up' if pool > pool_start else ('down' if pool < pool_start else 'dim')
    rev_html = latest_review_html()
    gate_html = llm_gate_html()
    agent_queue = agent_queue_html()
    shadow = shadow_html()

    # 交易记录表
    t_closed, t_wins, t_pnl = tstats
    if trades:
        rows_t = ''
        for t in reversed(trades):  # 新的在前
            cls = 'up' if t['pnl'] > 0 else ('down' if t['pnl'] < 0 else 'dim')
            bl, bc = badge.get(t['type'], (t['type'], 'b-info'))
            pnl_s = f'<td class="{cls}">{t["pnl"]:+.2f} U</td>' if t['type'] in ('close', 'sl') and (t.get('price') or t.get('pnl')) else '<td class="dim">-</td>'
            # 旧记录（本次修复前）价格/盈亏存不下来：不显示假 0，用 — 代替
            px_s = f'{t["price"]:,.2f}' if t.get('price') else '<span class="dim">—</span>'
            cc = COIN_COLOR.get(t['symbol'], '#888')
            cn = CN_NAMES.get(t['symbol'], t['symbol'].split('/')[0])
            lev = t.get('lev')
            lev_s = f'<td><span class="badge b-info">{lev:g}x</span></td>' if lev else '<td class="dim">-</td>'
            _p = t.get('pool')
            pool_s = ('<span class="badge b-info" style="margin-left:6px">短线</span>' if pool_of(t) == 'short'
                      else '<span class="badge b-warn" style="margin-left:6px">日线</span>')
            rows_t += (f'<tr><td class="dim">{t["ts"]}</td>'
                       f'<td><span class="badge {bc}">{bl}</span>{pool_s}</td>'
                       f'<td><span class="coin"><span class="cdot" style="background:{cc}">{t["symbol"].split("/")[0][0]}</span>{cn} <span class="dim" style="font-size:12px">{t["symbol"].split("/")[0]}</span></span></td>'
                       f'<td>{t["qty"]:.4f}<br><span class="dim" style="font-size:11px">≈${t["qty"]*t["price"]:,.1f}</span></td>'
                       f'<td>{px_s}</td>'
                       f'{lev_s}{pnl_s}<td class="dim">{html.escape(t["detail"])}</td></tr>')
    else:
        rows_t = '<tr><td colspan=8 class="dim">暂无交易记录</td></tr>'
    winrate = f'{t_wins/max(t_closed,1)*100:.0f}%' if t_closed else '-'

    def log_block(title, lines):
        body = ''.join(f'<div class="logline">{html.escape(l.strip())}</div>' for l in lines)
        return f'<details class="logcard"><summary>{title}</summary>{body}</details>'

    # ---- 交易类型 badge ----

    # ---- 顶部状态与派生指标 ----
    core_procs = ['trader100.py', 'trader_short.py', 'guard.py']
    running_core = sum(1 for n, st, _ in procs if n in core_procs and st.startswith('运行'))
    if running_core == len(core_procs):
        banner_cls, banner_icon, banner_title = 'ok', '✓', '交易进程全部运行'
        banner_text = '日线 / 短线 / 止损守护均在运行；日内混合信号每 15min 巡检；状态页每 30 秒同步一次。'
    elif running_core == 0:
        banner_cls, banner_icon, banner_title = 'idle', 'i', '仅展示模式 · 交易进程未启动'
        banner_text = '当前只运行状态页，不会下单。账户与持仓来自 Demo 合约，可正常查看。'
    else:
        banner_cls, banner_icon, banner_title = 'warn', '!', '部分交易进程未运行'
        banner_text = f'核心进程 {running_core}/{len(core_procs)} 在线，策略可能不会按预期执行。'
    all_closed, all_wins, all_pnl = trade_stats(trades, None)
    all_winrate = f'{all_wins / all_closed * 100:.0f}%' if all_closed else '—'
    total_slots = len(SYMBOLS) + short_n
    total_positions = len(pos_list) + len(short_pos)
    daily_ret = (pool / pool_start - 1) * 100 if pool_start else 0.0
    short_ret = (short_pool / short_pool_start - 1) * 100 if short_pool_start else 0.0
    strat_pnl = (pool - pool_start) + (short_pool - short_pool_start)
    strat_cls = 'up' if strat_pnl > 0 else ('down' if strat_pnl < 0 else 'dim')
    # 账户总金额按策略初始本金计（默认 100 U），不取交易所 Demo 钱包余额；
    # 交易所钱包只作参考展示，避免把 4657 U 误当成策略本金。
    account_start = float(os.environ.get('ACCOUNT_START', '100'))
    # 竞技场口径：顶栏以 arena_engine 的 100U 总盘为准（旧 state_pool/state_short 已停更）
    try:
        arena = json.load(open(os.path.join(BASE, '..', 'data', 'binance_arena.json')))
    except Exception:
        arena = {}
    arena_eq = float(arena.get('equity') or 0) or (account_start + strat_pnl)
    arena_lev = float(arena.get('lev') or os.environ.get('LEV', '5'))
    arena_margin = float(arena.get('marginUsed') or 0)
    arena_gross = float(arena.get('grossNotional') or 0)
    arena_slots = int(arena.get('slots') or 13)
    arena_pos_n = len(arena.get('positions') or [])
    arena_realized = float(arena.get('realized') or 0)
    arena_unreal = float(arena.get('unrealized') or 0)
    arena_wr = arena.get('winRate')
    arena_wr_txt = ('%.1f%%' % arena_wr) if arena_wr is not None else '—'
    arena_day_pnl = float(arena.get('dayPnl') or 0)
    arena_day_pct = float(arena.get('dayPnlPct') or 0)
    arena_cum_pnl = float(arena.get('cumPnl') or 0)
    arena_cum_pct = float(arena.get('cumPnlPct') or 0)
    arena_day_cls = 'up' if arena_day_pnl > 0 else ('down' if arena_day_pnl < 0 else 'dim')
    arena_hold_max = arena.get('holdMax') or '—'
    arena_hold_avg = arena.get('holdAvg') or '—'
    arena_dchg = arena.get('dayChangeAvg')
    arena_dchg_txt = ('%+.2f%%' % arena_dchg) if arena_dchg is not None else '—'
    arena_dchg_cls = ('up' if arena_dchg > 0 else ('down' if arena_dchg < 0 else 'dim')) \
        if arena_dchg is not None else 'dim'
    account_total = arena_eq
    strat_pnl = arena_eq - account_start
    strat_cls = 'up' if strat_pnl > 0 else ('down' if strat_pnl < 0 else 'dim')
    daily_sl_pct = float(os.environ.get('SL_PCT', '0.02'))
    short_sl_pct = float(os.environ.get('SHORT_SL_PCT', '0.02'))
    daily_signal = os.environ.get('DAILY_SIGNAL', 'mom').lower()
    mom_n = int(os.environ.get('DAILY_MOM_N', '20'))
    mom_th = float(os.environ.get('DAILY_MOM_TH', '0.02'))
    exec_tf = os.environ.get('BN_EXEC_TF', '1h')
    exec_n = int(os.environ.get('BN_EXEC_N', '12'))
    exec_th = float(os.environ.get('BN_EXEC_TH', '0.01'))
    trend_n = int(os.environ.get('BN_TREND_N', '200'))
    loop_min = float(os.environ.get('LOOP_MIN', os.environ.get('SHORT_LOOP_MIN', '15')))
    daily_sig_label = (f'MOM{mom_n}>{mom_th * 100:g}%' if daily_signal == 'mom'
                       else f'SMA{int(os.environ.get("SMA_TSLA", "50"))}')
    hybrid_label = f'{exec_tf}{exec_n}±{exec_th * 100:g}% · SMA{trend_n}'
    short_enabled = os.environ.get('SHORT_ENABLED', '1').lower() in ('1', 'true', 'yes', 'on')
    short_mode = '正常' if short_enabled else '只平不开'
    # 双向做空：v3 回测里做空是样本外转正的关键（短pool 与日线池同一套参数）
    if ALLOW_SHORT:
        short_mom_th = float(os.environ.get('BN_SHORT_MOM_TH', '0.02'))
        short_risk = float(os.environ.get('BN_SHORT_RISK', '0.5'))
        bidir_label = (f'开 · 日线跌超{short_mom_th * 100:g}% · '
                       f'{short_risk:g}x仓 · SL +{SHORT_STOP_PCT * 100:g}%')
    else:
        bidir_label = '关（BN_ALLOW_SHORT=0）'
    proc_rows = [r for r in procs if r[1] in ('运行', '挂掉')]
    running_all = sum(1 for _, st, _ in proc_rows if st == '运行')

    def proc_badge(st):
        if st == '运行' or st == '新鲜':
            return '<span class="pill ok"></span><span class="up">' + ('运行中' if st == '运行' else '新鲜') + '</span>'
        if st == '调度中':
            return '<span class="pill idle"></span><span class="gold">调度中</span>'
        if st == '过期':
            return '<span class="pill dead"></span><span class="down">数据过期</span>'
        return '<span class="pill dead"></span><span class="down">未运行</span>'

    rows_p = ''.join(
        f'<div class="proc"><span class="proc-name">{n.replace(".py", "")}</span>'
        f'{proc_badge(st)}<span class="pid">{pids or "—"}</span></div>'
        for n, st, pids in procs)

    return f'''<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>币安 Bot 状态</title>
<style>{PAGE_CSS}</style></head><body>
<div class="wrap">

  <header class="topbar">
    <div class="brand">
      <h1>币安 Bot 状态</h1>
      <div class="sub">Demo 合约 · TRADIFI 永续 7x24（夜盘/盘前/周末照常巡检）· 只读</div>
    </div>
    <div class="live"><a href="../perp.html" style="margin-right:12px">全天候双向盘 →</a><span class="dot {'idle' if running_core == 0 else ''}"></span>状态页运行中 · 更新 <b>{now.split(" ")[1]}</b> · {now.split(" ")[0]}</div>
  </header>

  <div class="banner {banner_cls}"><span class="b-title">{banner_title}</span><span class="b-text">{banner_text}</span></div>

  <nav class="nav">
    <a href="#positions">竞技场</a>
    <a href="#risk">风控与复盘</a>
    <a href="#trades">交易记录</a>
    <a href="#logs">运行日志</a>
  </nav>

  <section class="hero">
    <div class="hero-main">
      <div class="k-label">账户总金额 · 初始本金 {account_start:g} U</div>
      <div class="big">{account_total:,.2f}<span class="unit">U</span></div>
      <div class="hero-delta {strat_cls}">{strat_pnl:+.2f} U<span>较初始本金</span></div>
      <div class="hero-meta">
        <span>竞技场 <b>{arena_eq:.2f} U</b> <span class="{strat_cls}">({arena_eq / max(account_start, 1) * 100 - 100:+.2f}%)</span></span>
        <span>杠杆 <b>{arena_lev:g}x</b> · 保证金 {arena_margin:.0f} U</span>
        <span>当日 <b class="{arena_day_cls}">{arena_day_pnl:+.2f} U</b>（{arena_day_pct:+.2f}%）</span>
        <span>累计 <b class="{strat_cls}">{arena_cum_pnl:+.2f} U</b>（{arena_cum_pct:+.2f}%）</span>
        <span>持仓 <b>{arena_hold_max}</b>（均 {arena_hold_avg}）</span>
        <span>持仓当日涨幅 <b class="{arena_dchg_cls}">{arena_dchg_txt}</b></span>
        <span>持仓 <b>{total_positions}/{total_slots}</b></span>
        <span>交易所 Demo 钱包 <b>{bal:,.0f} U</b>（非策略本金）</span>
      </div>
    </div>

    <div class="kpi">
      <div class="k-label">竞技场权益 · 累计收益</div>
      <div class="k-val {strat_cls}">{arena_eq:.2f}<span class="unit">U</span></div>
      <div class="k-sub">累计 <b>{arena_cum_pnl:+.2f} U</b>（{arena_cum_pct:+.2f}%）· 已实现 {arena_realized:+.2f} · 浮动 {arena_unreal:+.2f}</div>
      <div class="bar"><span style="width:{min(100, arena_pos_n / max(arena_slots, 1) * 100):.0f}%"></span></div>
      <div class="mini"><span>{arena_pos_n}/{arena_slots} 仓</span><span>{arena_lev:g}x 杠杆</span><span>SL 2~8%</span></div>
    </div>

    <div class="kpi">
      <div class="k-label">当日收益 · {arena_lev:g}x 杠杆</div>
      <div class="k-val {arena_day_cls}">{arena_day_pnl:+.2f}<span class="unit">U</span></div>
      <div class="k-sub">{arena_day_pct:+.2f}% · 名义 {arena_gross:.0f} U · 保证金 {arena_margin:.0f} U</div>
      <div class="bar"><span style="width:{min(100, arena_margin / max(arena_eq, 1) * 100):.0f}%"></span></div>
      <div class="mini"><span>占用 {arena_margin / max(arena_eq, 1) * 100:.0f}%</span><span>23 标</span><span>每 {loop_min:g}min</span></div>
    </div>

    <div class="kpi">
      <div class="k-label">持仓与胜率</div>
      <div class="k-val">{arena_pos_n}<span class="unit">/{arena_slots}</span></div>
      <div class="k-sub">最长 {arena_hold_max} · 均 {arena_hold_avg} · 当日涨幅 <b class="{arena_dchg_cls}">{arena_dchg_txt}</b></div>
      <div class="mini"><span>平仓 {all_closed} 笔</span><span>胜率 {arena_wr_txt}</span><span>累计 {arena_cum_pnl:+.2f} U</span></div>
    </div>
  </section>

  <section class="split">
    {arena_panels()}

    <aside class="side">
      <div class="panel">
        <div class="panel-head"><h2>系统状态</h2><span class="badge {'b-open' if running_core == len(core_procs) else 'b-warn'}">{running_core}/{len(core_procs)} 核心在线</span></div>
        <div class="proc-list">{rows_p}</div>
        <div class="note">全部进程 {running_all}/{len(proc_rows)} 在线。status_page.py 每 30 秒生成镜像并写入 data/binance_status.html；交易进程未启动时页面仍可查看账户。</div>
      </div>
    </aside>
  </section>

  <div class="panel">
    <div class="panel-head"><h2>策略参数</h2><span class="hint">只读 · 改参数需重启对应进程</span></div>
    <div class="param-strip">
      <div class="pitem"><span>日线趋势</span><b>{daily_sig_label} 定方向 + {exec_tf}{exec_n}±{exec_th * 100:g}% 择时 · {daily_lev:g}x · SL -{daily_sl_pct * 100:g}%</b></div>
      <div class="pitem"><span>短线趋势</span><b>同一套日内信号 · SMA{trend_n} 过滤 · {short_lev:g}x · SL -{short_sl_pct * 100:g}% · {short_mode}</b></div>
      <div class="pitem"><span>利润保护</span><b>intraday 1/2/3.5% · 回撤 0.6% · T+0 落袋</b></div>
      <div class="pitem"><span>巡检 / 杠杆</span><b>每 {loop_min:g}min · {daily_lev:g}x（弹性来自日内波动放大，可调）</b></div>
      <div class="pitem"><span>池回撤熔断</span><b>回撤 20% 暂停开新仓</b></div>
      <div class="pitem"><span>LLM 决策路由</span><b>{llm_route_label()}</b></div>
      <div class="pitem"><span>做空 / 双向</span><b>{bidir_label}</b></div>
    </div>
  </div>

  <div class="section-head" id="risk"><h2>风控与复盘</h2><span class="hint">LLM 闸门 · 否决单影子跟踪 · Agent 队列</span></div>
  <div class="risk-grid">{rev_html}{gate_html}{agent_queue}{shadow}</div>

  <div class="panel" id="trades" style="margin-top:16px">
    <div class="panel-head"><h2>交易记录</h2><span class="hint">共 {len(trades)} 条 · 平仓 {all_closed} 笔 · 胜率 {all_winrate} · 累计 {all_pnl:+.2f} U</span></div>
    <div class="tblwrap"><table><tr><th>时间</th><th>类型</th><th>标的</th><th>数量</th><th>价格</th><th>杠杆</th><th>盈亏</th><th>说明</th></tr>{rows_t}</table></div>
  </div>

  <div class="panel" id="logs">
    <div class="panel-head"><h2>运行日志</h2><span class="hint">点击展开 · 最新 25 行</span></div>
    {log_block('trader100 · 日内混合(5标)', tail_log('run100.log'))}
    {log_block('trader_short · 日内混合(8标)', tail_log('run_short.log'))}
    {log_block('guard · 止损守护', tail_log('guard.log'))}
    {log_block('watchdog · 守护进程', tail_log('watchdog.log', 10))}
    {log_block('cron · 重启记录', tail_log('watchdog_restart.log', 10))}
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


def agent_queue_html():
    """Agent LLM 队列卡：有多少提问在等我回答（答不上就按规则放行）。"""
    try:
        import sys
        root = os.path.dirname(BASE)
        if root not in sys.path:
            sys.path.insert(0, root)
        import llm_agent
        pend_all = llm_agent.pending(limit=300)
        # 只显示币安闸门的提问（kind=bn_gate）。仓库根的 data/llm_queue 与 A 股项目共用，
        # 不过滤会把 A 股盘中风控/宏观问题误显示到币安页面。
        pend = [d for d in pend_all
                if str(d.get('kind') or '').startswith('bn_')
                or str(d.get('id') or '').startswith('bn_')]
        today = time.strftime('%Y-%m-%d')
        done_dir = os.path.join(llm_agent.qdir(), 'done')
        n_done = 0
        try:
            n_done = sum(1 for f in os.listdir(done_dir)
                         if f.endswith('.json') and today in f
                         and (f.startswith('bn_') or f.startswith('bn_gate')))
        except Exception:
            pass
    except Exception as e:
        return (f'<div class="card"><h2>Agent LLM 队列</h2>'
                f'<div class="dim">读取失败 {html.escape(str(e))}</div></div>')

    if pend:
        rows = ''
        for d in pend[:5]:
            prev = html.escape((d.get('user') or '').replace('\n', ' ')[:110])
            rows += (f'<tr><td class="mono">{html.escape(d.get("id", ""))}</td>'
                     f'<td>{html.escape(str(d.get("kind", "?")))}</td>'
                     f'<td class="dim">{html.escape(str(d.get("ts", "?")))}</td>'
                     f'<td style="text-align:left">{prev}</td></tr>')
        body = ('<div class="tblwrap"><table><tr><th>问题 ID</th><th>类型</th>'
                '<th>时间</th><th style="text-align:left">提问预览</th></tr>'
                f'{rows}</table></div>')
        if len(pend) > 5:
            body += f'<div class="hint" style="margin-top:6px">仅显示最新 5 条，还有 {len(pend) - 5} 条待答。</div>'
        tip = (f'<div style="font-size:12px;color:var(--gold);margin-top:6px">'
               f'这 {len(pend)} 条超时未按规则放行了，答复后程序会自动重跑命中。</div>')
    else:
        body = '<div class="dim">币安闸门无待答问题</div>'
        tip = ''
    return (f'<div class="card"><h2>币安 Agent 队列 <span class="badge b-info">'
            f'待答 {len(pend)} · 今日已答 {n_done}</span></h2>{body}{tip}'
            '<div style="font-size:12px;color:var(--dim);margin-top:6px;line-height:1.7">'
            'LLM 提问先落到 data/llm_queue/pending/，由我（WorkBuddy）免费回答；'
            '等不到答案则按原规则降级（闸门=放行、复盘=跳过），绝不卡主流程。'
            '只显示 kind=bn_gate 的币安问题，避免与 A 股队列混在一起。</div></div>')


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
        cards.append(f'<div class="card" style="flex:1;min-width:320px"><h2>{label} 否决单跟踪</h2>{body}</div>')
    if not cards:
        return ''
    n = tot['n']
    summary = (f'<div class="card"><h2>闸门有效性（否决单影子跟踪）</h2>'
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
    try:
        ex.load_markets()
    except Exception as e:      # 首次失败不退出，交给循环重试
        print('load_markets 失败(循环内重试):', str(e)[:120], flush=True)
    _tick = 0
    _last_page = None
    while True:
        _tick += 1
        try:
            # WSL 时钟会漂移（实测本机比币安快 ~2.4s）：ccxt 的 adjustForTimeDifference
            # 只在 load_markets 时校准一次，之后漂移会让所有请求 -1021，状态页长期停在
            # 「生成失败」。故每 10 轮（≈5 分钟）重新校准一次时间差。
            if _tick % 10 == 1:
                try:
                    ex.load_time_difference()
                except Exception:
                    pass
            if not getattr(ex, 'markets', None):
                ex.load_markets()
            procs = proc_status()
            # 日线池
            pos_list, total_pnl = positions(ex, SYMBOLS, NAMES, CN_NAMES,
                                            pool_start=float(os.environ.get('POOL_START', '100')),
                                            profile='daily', state_file='state_pool.json')
            # 短线池
            short_pos, short_pnl = positions(ex, SYMBOLS_SHORT, NAMES_SHORT, CN_SHORT,
                                             pool_start=float(os.environ.get('SHORT_POOL', '100')),
                                             profile='short', state_file='state_short.json')
            bal = float(ex.fetch_balance()['info'].get('totalWalletBalance', 0))
            from trade_log import read_trades
            trades = read_trades(400)
            try:
                _sb = json.load(open(os.path.join(BASE, 'state_short.json'))).get('bug_loss') or 0.0
            except Exception:
                _sb = 0.0
            page = build_html(procs, pos_list, total_pnl, bal, trades, trade_stats(trades, 'daily'),
                              short_pos=short_pos, short_pnl=short_pnl, short_n=len(SYMBOLS_SHORT),
                              daily_realized=realized_pnl(trades, 'daily'),
                              short_realized=realized_pnl(trades, 'short'),
                              short_bug_loss=float(_sb))
            _last_page = page
            for o in OUTS:
                os.makedirs(os.path.dirname(o), exist_ok=True)
                with open(o, 'w') as f:
                    f.write(page)
            try:
                _ps = float(os.environ.get('POOL_START', '100'))
                _ss = float(os.environ.get('SHORT_POOL', '100'))
                _dr = realized_pnl(trades, 'daily')
                _sr = realized_pnl(trades, 'short')
                dump_status_json(procs, pos_list, short_pos, bal, trades,
                                 _ps + _dr + total_pnl, _ps,
                                 _ss + _sr + short_pnl, _ss,
                                 _dr, _sr, total_pnl, short_pnl)
            except Exception as e:
                print('JSON 输出失败:', str(e)[:160], flush=True)
        except Exception as e:
            # demo-fapi 偶发 SSLEOF / 限流时保留上一版页面，别把状态页写成白屏；
            # 30s 后循环会重试。只有首次启动就失败才写错误页。
            print('状态页刷新失败(保留上一版):', str(e)[:160], flush=True)
            if _last_page is None:
                for o in OUTS:
                    os.makedirs(os.path.dirname(o), exist_ok=True)
                    with open(o, 'w') as f:
                        f.write(f'<html><body><h1>状态页生成失败</h1><pre>{html.escape(str(e))}</pre></body></html>')
        time.sleep(30)


if __name__ == '__main__':
    main()
