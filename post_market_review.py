"""
盘后/盘前复盘脚本
用法：
  python post_market_review.py --market a-share --type post
  python post_market_review.py --market us --type pre
  python post_market_review.py --market us --type post

说明：
- A股盘后复盘：交易日 16:00（北京时间）运行
- 美股盘前复盘：交易日 21:00（北京时间）运行，用于开盘前准备
- 美股盘后复盘：次工作日 05:00（北京时间）运行
"""

import json
import os
import math
import argparse
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_JSON = os.path.join(BASE_DIR, 'report_data.json')

A_SHARE_ASSETS = ['陕西煤业', '中证红利', '长江电力', '中证A500', '中远海控', '招商银行', '宁波银行']
US_ASSETS = ['纳指', '标普500', '可口可乐']


def load_data():
    with open(DATA_JSON, 'r', encoding='utf-8') as f:
        return json.load(f)


def calc_ma(closes, period):
    ma = []
    for i in range(len(closes)):
        if i < period - 1:
            ma.append(None)
        else:
            ma.append(sum(closes[i - period + 1:i + 1]) / period)
    return ma


def calc_boll(closes, period, k=2.0):
    ma = calc_ma(closes, period)
    upper, lower = [], []
    for i in range(len(closes)):
        if i < period - 1:
            upper.append(None)
            lower.append(None)
        else:
            std = math.sqrt(sum((closes[i - j] - ma[i]) ** 2 for j in range(period)) / period)
            upper.append(ma[i] + k * std)
            lower.append(ma[i] - k * std)
    return ma, upper, lower


def calc_rsi(closes, period=14):
    rsi = [50] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    for i in range(period, len(closes)):
        avg_gain = sum(gains[i - period:i]) / period
        avg_loss = sum(losses[i - period:i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else 100
        rsi[i] = 100 - 100 / (1 + rs)
    return rsi


def analyze_asset(name, kd):
    dates = kd['dates']
    ohlc = kd['values']
    closes = [v[1] for v in ohlc]
    n = len(closes)
    if n < 30:
        return None

    ma20 = calc_ma(closes, 20)
    _, upper, lower = calc_boll(closes, 20)
    rsi = calc_rsi(closes, 14)

    latest_idx = n - 1
    prev_idx = n - 2

    latest_close = closes[latest_idx]
    prev_close = closes[prev_idx]
    change_pct = (latest_close / prev_close - 1) * 100

    ma = ma20[latest_idx]
    bb_upper = upper[latest_idx]
    bb_lower = lower[latest_idx]
    r = rsi[latest_idx]

    ma_pos = (latest_close / ma - 1) * 100 if ma else 0
    bb_pos = None
    if bb_upper and bb_lower and bb_upper != bb_lower:
        bb_pos = (latest_close - bb_lower) / (bb_upper - bb_lower)

    # 形态识别：基于收盘价与 MA20 的关系及 MA20 斜率
    ma_slope = ma - ma20[latest_idx - 5] if latest_idx >= 5 else 0
    if latest_close > ma and ma_slope > 0:
        pattern = '右侧上升'
        pattern_class = 'up'
    elif latest_close < ma and ma_slope < 0:
        pattern = '左侧下跌'
        pattern_class = 'down'
    else:
        pattern = '震荡区间'
        pattern_class = 'neutral'

    # 交易信号
    if r < 30 and bb_pos is not None and bb_pos < 0.2:
        signal = '超卖/逢低关注'
        signal_class = 'up'
    elif r > 70 and bb_pos is not None and bb_pos > 0.8:
        signal = '超买/逢高减仓'
        signal_class = 'down'
    elif pattern == '右侧上升':
        signal = '趋势偏多'
        signal_class = 'up'
    elif pattern == '左侧下跌':
        signal = '趋势偏空'
        signal_class = 'down'
    else:
        signal = '震荡观望'
        signal_class = 'neutral'

    # 策略建议
    if pattern == '震荡区间':
        suggest = '布林带 / 常见网格'
    elif pattern == '右侧上升':
        suggest = '20日线穿越 / 5日新高突破'
    else:
        suggest = '买入持有 / 空仓观望'

    return {
        'name': name,
        'date': dates[latest_idx],
        'close': latest_close,
        'prev_close': prev_close,
        'change_pct': change_pct,
        'ma20': ma,
        'ma_pos': ma_pos,
        'bb_upper': bb_upper,
        'bb_lower': bb_lower,
        'bb_pos': bb_pos,
        'rsi': r,
        'pattern': pattern,
        'pattern_class': pattern_class,
        'signal': signal,
        'signal_class': signal_class,
        'suggest': suggest,
        'high20': max(closes[-20:]),
        'low20': min(closes[-20:]),
    }


def fmt_pct(v):
    return f"{v:+.2f}%"


def fmt_money(v):
    return f"{v:,.2f}"


def _progress_bar(value, min_val=0, max_val=100, low_color='#22c55e', mid_color='#c78d5a', high_color='#ef4444'):
    """生成彩色进度条，value 在 [min_val, max_val] 之间"""
    pct = max(0, min(100, (value - min_val) / (max_val - min_val) * 100))
    if pct < 30:
        color = low_color
    elif pct > 70:
        color = high_color
    else:
        color = mid_color
    return f'<div class="progress"><div class="progress-fill" style="width:{pct:.1f}%;background:{color}"></div></div>'


def gen_review_html(market, review_type, results, gen_time):
    market_name = 'A股' if market == 'a-share' else '美股'
    type_name = '盘后' if review_type == 'post' else '盘前'
    title = f'{market_name}{type_name}复盘'

    if not results:
        return _base_html(title, '<p>无可用数据</p>', gen_time)

    # 汇总统计
    avg_change = sum(r['change_pct'] for r in results) / len(results)
    sorted_by_change = sorted(results, key=lambda x: x['change_pct'], reverse=True)
    strongest = sorted_by_change[0]
    weakest = sorted_by_change[-1]
    up_count = sum(1 for r in results if r['change_pct'] > 0)
    down_count = sum(1 for r in results if r['change_pct'] < 0)
    bullish = sum(1 for r in results if r['signal_class'] == 'up')
    bearish = sum(1 for r in results if r['signal_class'] == 'down')

    # 汇总卡片
    summary_html = f'''
    <div class="summary-grid">
        <div class="summary-card">
            <div class="summary-icon">📊</div>
            <div class="summary-text">
                <div class="val {('up' if avg_change > 0 else 'down')}">{fmt_pct(avg_change)}</div>
                <div class="label">平均涨跌</div>
            </div>
        </div>
        <div class="summary-card up-card">
            <div class="summary-icon">📈</div>
            <div class="summary-text">
                <div class="val">{up_count}</div>
                <div class="label">上涨标的</div>
            </div>
        </div>
        <div class="summary-card down-card">
            <div class="summary-icon">📉</div>
            <div class="summary-text">
                <div class="val">{down_count}</div>
                <div class="label">下跌标的</div>
            </div>
        </div>
        <div class="summary-card">
            <div class="summary-icon">🎯</div>
            <div class="summary-text">
                <div class="val">{bullish}<span class="unit">/{len(results)}</span></div>
                <div class="label">看多信号</div>
            </div>
        </div>
        <div class="summary-card">
            <div class="summary-icon">🔥</div>
            <div class="summary-text">
                <div class="val up">{strongest['name']}</div>
                <div class="label">最强 {fmt_pct(strongest['change_pct'])}</div>
            </div>
        </div>
        <div class="summary-card">
            <div class="summary-icon">❄️</div>
            <div class="summary-text">
                <div class="val down">{weakest['name']}</div>
                <div class="label">最弱 {fmt_pct(weakest['change_pct'])}</div>
            </div>
        </div>
    </div>
    '''

    # 信号表
    rows = ''
    for idx, r in enumerate(results, 1):
        rows += f'''
        <tr>
            <td class="asset-name"><span class="row-num">{idx:02d}</span>{r['name']}</td>
            <td><span class="date-tag">{r['date']}</span></td>
            <td><span class="pct-badge {('up' if r['change_pct'] > 0 else 'down')}">{fmt_pct(r['change_pct'])}</span></td>
            <td>
                <div class="metric-with-bar">
                    <span class="{'rsi-low' if r['rsi'] < 30 else ('rsi-high' if r['rsi'] > 70 else '')}">{r['rsi']:.1f}</span>
                    {_progress_bar(r['rsi'], 0, 100)}
                </div>
            </td>
            <td><span class="pattern-tag {r['pattern_class']}">{r['pattern']}</span></td>
            <td><span class="signal-badge {r['signal_class']}">{r['signal']}</span></td>
            <td class="strategy-cell">{r['suggest']}</td>
        </tr>
        '''
    table_html = f'''
    <div class="table-section">
        <div class="section-title">
            <span class="title-icon">📋</span>
            <h2>标的信号一览</h2>
        </div>
        <div class="table-wrap">
            <table class="data-table">
                <thead>
                    <tr>
                        <th>标的</th><th>数据日期</th><th>日涨跌</th><th>RSI(14)</th>
                        <th>形态</th><th>信号</th><th>建议策略</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </div>
    '''

    # 标的卡片
    cards_html = '<div class="cards-section"><div class="section-title"><span class="title-icon">🃏</span><h2>标的详情卡片</h2></div><div class="cards">'
    for r in results:
        bb_pct = r['bb_pos'] * 100 if r['bb_pos'] is not None else 50
        bb_label = '下轨附近' if bb_pct < 20 else ('上轨附近' if bb_pct > 80 else '中轨附近')
        rsi_color = 'rsi-low' if r['rsi'] < 30 else ('rsi-high' if r['rsi'] > 70 else '')

        cards_html += f'''
        <div class="card card-{r['signal_class']}">
            <div class="card-header">
                <div class="card-title-wrap">
                    <span class="card-title">{r['name']}</span>
                    <span class="date-tag">{r['date']}</span>
                </div>
                <div class="card-header-right">
                    <span class="signal-badge {r['signal_class']}">{r['signal']}</span>
                    <span class="card-change {('up' if r['change_pct'] > 0 else 'down')}">{fmt_pct(r['change_pct'])}</span>
                </div>
            </div>
            <div class="card-body">
                <div class="price-row">
                    <div class="price-main">
                        <span class="price-label">最新价</span>
                        <span class="price-value">{fmt_money(r['close'])}</span>
                    </div>
                    <div class="price-side">
                        <div class="side-metric">
                            <span>MA20</span>
                            <strong>{fmt_money(r['ma20'])}</strong>
                        </div>
                        <div class="side-metric">
                            <span>偏离</span>
                            <strong class="{('up' if r['ma_pos'] > 0 else 'down')}">{fmt_pct(r['ma_pos'])}</strong>
                        </div>
                    </div>
                </div>
                <div class="bar-metrics">
                    <div class="bar-metric">
                        <div class="bar-label"><span>RSI(14)</span><span class="bar-value {rsi_color}">{r['rsi']:.1f}</span></div>
                        {_progress_bar(r['rsi'], 0, 100)}
                    </div>
                    <div class="bar-metric">
                        <div class="bar-label"><span>布林位置</span><span class="bar-value">{bb_label}</span></div>
                        {_progress_bar(bb_pct, 0, 100)}
                    </div>
                </div>
                <div class="metric-grid">
                    <div class="metric-item">
                        <span class="metric-label">20日最高</span>
                        <strong class="up">{fmt_money(r['high20'])}</strong>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">20日最低</span>
                        <strong class="down">{fmt_money(r['low20'])}</strong>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">形态</span>
                        <span class="pattern-tag {r['pattern_class']}">{r['pattern']}</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">建议策略</span>
                        <strong class="strategy-text">{r['suggest']}</strong>
                    </div>
                </div>
            </div>
        </div>
        '''
    cards_html += '</div></div>'

    body = summary_html + table_html + cards_html
    return _base_html(title, body, gen_time, market, review_type)


def _base_html(title, body, gen_time, market=None, review_type=None):
    nav_items = ''
    if market or review_type:
        nav_items = '''
        <a href="review_a_share.html" class="nav-link">A股盘后</a>
        <a href="review_us_pre.html" class="nav-link">美股盘前</a>
        <a href="review_us_post.html" class="nav-link">美股盘后</a>
        <a href="report.html" class="nav-link">完整回测报告</a>
        '''

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
:root {{
    --bg: #f5f5f7;
    --surface: #ffffff;
    --surface-2: #f9fafb;
    --text: #111827;
    --text-2: #6b7280;
    --border: #e5e7eb;
    --primary: #111827;
    --accent: #c78d5a;
    --up: #dc2626;
    --down: #16a34a;
    --neutral: #6b7280;
    --radius: 12px;
    --shadow: 0 1px 2px rgba(0,0,0,0.04);
    --shadow-hover: 0 4px 12px rgba(0,0,0,0.06);
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height:1.5;
    -webkit-font-smoothing: antialiased;
}}
.container {{ max-width:1280px; margin:0 auto; padding:24px; }}

/* Header */
header {{
    text-align:center;
    padding:40px 24px;
    background: var(--surface);
    color: var(--text);
    border-radius: var(--radius);
    margin-bottom:24px;
    border:1px solid var(--border);
}}
header h1 {{ font-size:30px; font-weight:800; margin-bottom:8px; letter-spacing:-0.5px; }}
header p {{ color: var(--text-2); font-size:14px; margin-top:10px; }}
.nav {{
    display:flex;
    justify-content:center;
    gap:8px;
    flex-wrap:wrap;
    margin-top:18px;
}}
.nav-link {{
    color: var(--text-2);
    text-decoration:none;
    padding:8px 16px;
    border-radius:20px;
    font-size:13px;
    font-weight:500;
    background: var(--surface-2);
    border:1px solid var(--border);
    transition:all 0.15s;
}}
.nav-link:hover {{ color: var(--text); background:#f3f4f6; border-color:#d1d5db; }}

/* Section */
.section {{
    background: var(--surface);
    border-radius: var(--radius);
    padding:28px;
    margin-bottom:24px;
    border:1px solid var(--border);
}}
.section-title {{
    display:flex;
    align-items:center;
    gap:10px;
    margin-bottom:20px;
}}
.title-icon {{ font-size:22px; }}
.section h2 {{ font-size:20px; font-weight:700; color: var(--text); }}

/* Summary cards */
.summary-grid {{
    display:grid;
    grid-template-columns:repeat(auto-fit, minmax(170px, 1fr));
    gap:16px;
    margin-bottom:10px;
}}
.summary-card {{
    background: var(--surface);
    border:1px solid var(--border);
    border-radius: var(--radius);
    padding:20px 16px;
    text-align:center;
    transition: border-color 0.15s;
}}
.summary-card:hover {{ border-color:#d1d5db; }}
.summary-icon {{ font-size:24px; margin-bottom:8px; }}
.summary-text {{ display:flex; flex-direction:column; align-items:center; gap:2px; }}
.summary-card .val {{ font-size:24px; font-weight:700; color: var(--text); line-height:1.2; }}
.summary-card .val .unit {{ font-size:13px; font-weight:600; color: var(--text-2); margin-left:2px; }}
.summary-card .label {{ font-size:12px; color: var(--text-2); margin-top:6px; font-weight:500; }}
.summary-card.up-card {{ border-left:4px solid var(--up); }}
.summary-card.down-card {{ border-left:4px solid var(--down); }}

/* Table */
.table-wrap {{ overflow-x:auto; border-radius:12px; border:1px solid var(--border); }}
.data-table {{
    width:100%;
    border-collapse:collapse;
    font-size:14px;
    background: var(--surface);
}}
.data-table thead {{ background: #f1f5f9; }}
.data-table th {{
    color: var(--text-2);
    padding:14px 12px;
    text-align:center;
    font-weight:600;
    font-size:12px;
    text-transform:uppercase;
    letter-spacing:0.3px;
    border-bottom:1px solid var(--border);
}}
.data-table td {{ padding:14px 12px; text-align:center; border-bottom:1px solid var(--border); }}
.data-table tbody tr {{ transition: background 0.15s; }}
.data-table tbody tr:hover {{ background: #f8fafc; }}
.data-table tbody tr:last-child td {{ border-bottom:none; }}
.asset-name {{ font-weight:700; text-align:left !important; color: var(--text); display:flex; align-items:center; gap:10px; }}
.row-num {{ color: var(--text-2); font-size:12px; font-weight:600; min-width:22px; }}
.date-tag {{ background:#f1f5f9; color: var(--text-2); padding:4px 8px; border-radius:6px; font-size:12px; font-weight:500; }}
.pct-badge {{
    display:inline-block;
    padding:6px 10px;
    border-radius:8px;
    font-weight:700;
    font-size:13px;
    background:rgba(0,0,0,0.04);
}}
.pattern-tag, .signal-badge {{
    display:inline-block;
    padding:5px 10px;
    border-radius:20px;
    font-size:12px;
    font-weight:600;
}}
.signal-badge.up {{ background:#fee2e2; color:#991b1b; }}
.signal-badge.down {{ background:#dcfce7; color:#166534; }}
.signal-badge.neutral {{ background:#f1f5f9; color:#475569; }}
.pattern-tag.up {{ background:#fef2f2; color:#991b1b; border:1px solid #fecaca; }}
.pattern-tag.down {{ background:#f0fdf4; color:#166534; border:1px solid #bbf7d0; }}
.pattern-tag.neutral {{ background:#f8fafc; color:#475569; border:1px solid var(--border); }}
.strategy-cell {{ color: var(--text-2); font-weight:500; max-width:160px; }}

/* Progress bar */
.progress {{
    width:100%;
    height:6px;
    background:#e2e8f0;
    border-radius:3px;
    overflow:hidden;
    margin-top:6px;
}}
.progress-fill {{ height:100%; border-radius:3px; transition: width 0.5s ease; }}
.metric-with-bar {{ min-width:100px; }}
.metric-with-bar span {{ font-weight:700; font-size:13px; }}
.rsi-low {{ color: var(--down) !important; }}
.rsi-high {{ color: var(--up) !important; }}

/* Cards */
.cards {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(min(100%, 360px), 1fr)); gap:20px; }}
.card {{
    background: var(--surface);
    border:1px solid var(--border);
    border-radius: var(--radius);
    overflow:hidden;
    transition: border-color 0.15s;
}}
.card:hover {{ border-color:#d1d5db; }}
.card.up {{ border-left:4px solid var(--up); }}
.card.down {{ border-left:4px solid var(--down); }}
.card.neutral {{ border-left:4px solid var(--neutral); }}
.card-header {{
    display:flex;
    justify-content:space-between;
    align-items:flex-start;
    padding:18px 20px;
    background: var(--surface-2);
    border-bottom:1px solid var(--border);
}}
.card-title-wrap {{ display:flex; flex-direction:column; gap:6px; align-items:flex-start; }}
.card-title {{ font-size:18px; font-weight:800; color: var(--text); }}
.card-header-right {{ display:flex; flex-direction:column; align-items:flex-end; gap:8px; }}
.card-change {{ font-size:20px; font-weight:800; }}

.card-body {{ padding:20px; }}
.price-row {{
    display:flex;
    justify-content:space-between;
    align-items:center;
    margin-bottom:20px;
    padding-bottom:18px;
    border-bottom:1px dashed var(--border);
}}
.price-main {{ display:flex; flex-direction:column; }}
.price-label {{ font-size:12px; color: var(--text-2); font-weight:500; margin-bottom:4px; }}
.price-value {{ font-size:32px; font-weight:800; color: var(--text); letter-spacing:-0.5px; }}
.price-side {{ display:flex; flex-direction:column; gap:8px; align-items:flex-end; }}
.side-metric {{ display:flex; gap:8px; font-size:13px; }}
.side-metric span {{ color: var(--text-2); }}
.side-metric strong {{ font-weight:700; color: var(--text); }}

.bar-metrics {{ display:flex; flex-direction:column; gap:14px; margin-bottom:20px; }}
.bar-metric {{ }}
.bar-label {{ display:flex; justify-content:space-between; font-size:12px; color: var(--text-2); margin-bottom:6px; }}
.bar-value {{ font-weight:700; color: var(--text); }}

.metric-grid {{
    display:grid;
    grid-template-columns: 1fr 1fr;
    gap:12px;
}}
.metric-item {{
    background: var(--surface-2);
    border-radius:10px;
    padding:12px;
    display:flex;
    flex-direction:column;
    gap:4px;
}}
.metric-label {{ font-size:12px; color: var(--text-2); font-weight:500; }}
.metric-item strong {{ font-size:15px; font-weight:700; color: var(--text); }}
.strategy-text {{ color: var(--primary-dark); }}

/* Colors */
.up {{ color: var(--up) !important; }}
.down {{ color: var(--down) !important; }}
.neutral {{ color: var(--neutral) !important; }}

/* Note & Footer */
.note {{
    background: var(--surface-2);
    border:1px solid var(--border);
    border-left:4px solid var(--accent);
    padding:16px 20px;
    border-radius: var(--radius);
    font-size:13px;
    color: var(--text-2);
    margin-top:24px;
    line-height:1.7;
}}
.note strong {{ color: var(--text); }}
footer {{ text-align:center; padding:28px; color: var(--text-2); font-size:12px; }}

@media (max-width: 768px) {{
    .container {{ padding:16px; }}
    header {{ padding:32px 16px; border-radius:14px; }}
    header h1 {{ font-size:26px; }}
    .nav {{ gap:8px; }}
    .nav-link {{ padding:7px 12px; font-size:12px; }}
    .section {{ padding:20px; border-radius:14px; }}
    .section-title {{ margin-bottom:16px; }}
    .section h2 {{ font-size:18px; }}
    .summary-grid {{ grid-template-columns:1fr; gap:12px; }}
    .summary-card {{ padding:16px; display:flex; align-items:center; justify-content:space-between; text-align:left; }}
    .summary-icon {{ font-size:24px; margin-bottom:0; margin-right:12px; }}
    .summary-text {{ align-items:flex-start; }}
    .summary-card .val {{ font-size:22px; }}
    .summary-card .label {{ margin-top:2px; }}
    .table-wrap {{ border-radius:10px; }}
    .data-table {{ font-size:13px; }}
    .data-table th, .data-table td {{ padding:10px 8px; }}
    .asset-name {{ gap:6px; }}
    .row-num {{ min-width:18px; }}
    .strategy-cell {{ max-width:120px; }}
    .cards {{ grid-template-columns:1fr; gap:16px; }}
    .card {{ border-radius:14px; }}
    .card-header {{ flex-direction:column; gap:12px; align-items:flex-start; }}
    .card-header-right {{ flex-direction:row; align-items:center; justify-content:space-between; width:100%; }}
    .card-title {{ font-size:16px; }}
    .card-change {{ font-size:18px; }}
    .card-body {{ padding:16px; }}
    .price-row {{ flex-direction:column; align-items:flex-start; gap:14px; }}
    .price-value {{ font-size:28px; }}
    .price-side {{ width:100%; flex-direction:row; justify-content:space-between; align-items:center; }}
    .bar-metrics {{ gap:12px; }}
    .metric-grid {{ grid-template-columns:1fr; gap:10px; }}
    .metric-item {{ padding:10px; }}
    .note {{ padding:14px; }}
}}

@media (max-width: 480px) {{
    header h1 {{ font-size:22px; }}
    header p {{ font-size:13px; }}
    .summary-card {{ padding:14px; }}
    .summary-icon {{ font-size:20px; }}
    .summary-card .val {{ font-size:20px; }}
    .data-table {{ font-size:12px; }}
    .data-table th {{ font-size:11px; }}
    .data-table th, .data-table td {{ padding:8px 6px; }}
    .pct-badge, .pattern-tag, .signal-badge {{ padding:4px 7px; font-size:11px; }}
    .price-value {{ font-size:24px; }}
}}
</style>
</head>
<body>
<div class="container">
<header>
    <h1>{title}</h1>
    <p>生成时间：{gen_time}</p>
    <div class="nav">{nav_items}</div>
</header>
<div class="section">
    {body}
    <div class="note">
        <strong>说明：</strong>本复盘基于 report_data.json 中的日线数据，自动计算 MA20、布林带、RSI 等指标。信号仅供参考，不构成投资建议。美股盘前复盘因缺少实时盘前数据，主要基于上一交易日收盘数据给出开盘观察要点。
    </div>
</div>
<footer>数据来源：BaoStock | 复盘脚本：post_market_review.py</footer>
</div>
</body>
</html>'''


def main():
    parser = argparse.ArgumentParser(description='盘后/盘前复盘脚本')
    parser.add_argument('--market', choices=['a-share', 'us'], required=True, help='市场：a-share 或 us')
    parser.add_argument('--type', choices=['pre', 'post'], default='post', help='pre=盘前复盘，post=盘后复盘')
    args = parser.parse_args()

    raw = load_data()
    kline = raw.get('kline', {})

    assets = A_SHARE_ASSETS if args.market == 'a-share' else US_ASSETS
    results = []
    for name in assets:
        if name in kline:
            r = analyze_asset(name, kline[name])
            if r:
                results.append(r)

    gen_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    html = gen_review_html(args.market, args.type, results, gen_time)

    if args.market == 'a-share':
        filename = 'review_a_share.html'
    else:
        filename = f'review_us_{args.type}.html'

    path = os.path.join(BASE_DIR, filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'复盘报告已生成: {path}')


if __name__ == '__main__':
    main()
