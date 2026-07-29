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

    # 生成汇总卡片
    summary_html = f'''
    <div class="summary-grid">
        <div class="summary-card">
            <div class="val {('up' if avg_change > 0 else 'down')}">{fmt_pct(avg_change)}</div>
            <div class="label">平均涨跌</div>
        </div>
        <div class="summary-card">
            <div class="val up">{up_count}</div>
            <div class="label">上涨标的</div>
        </div>
        <div class="summary-card">
            <div class="val down">{down_count}</div>
            <div class="label">下跌标的</div>
        </div>
        <div class="summary-card">
            <div class="val up">{strongest['name']}</div>
            <div class="label">最强：{fmt_pct(strongest['change_pct'])}</div>
        </div>
        <div class="summary-card">
            <div class="val down">{weakest['name']}</div>
            <div class="label">最弱：{fmt_pct(weakest['change_pct'])}</div>
        </div>
    </div>
    '''

    # 生成标的卡片
    cards_html = '<div class="cards">'
    for r in results:
        bb_pos_text = '—'
        if r['bb_pos'] is not None:
            if r['bb_pos'] > 0.8:
                bb_pos_text = '上轨附近'
            elif r['bb_pos'] < 0.2:
                bb_pos_text = '下轨附近'
            else:
                bb_pos_text = '中轨附近'

        cards_html += f'''
        <div class="card">
            <div class="card-header">
                <span class="card-title">{r['name']}</span>
                <span class="card-change {('up' if r['change_pct'] > 0 else 'down')}">{fmt_pct(r['change_pct'])}</span>
            </div>
            <div class="card-body">
                <div class="metric"><span>最新价</span><strong>{fmt_money(r['close'])}</strong></div>
                <div class="metric"><span>MA20</span><strong>{fmt_money(r['ma20'])}</strong></div>
                <div class="metric"><span>MA20 偏离</span><strong class="{('up' if r['ma_pos'] > 0 else 'down')}">{fmt_pct(r['ma_pos'])}</strong></div>
                <div class="metric"><span>RSI(14)</span><strong>{r['rsi']:.1f}</strong></div>
                <div class="metric"><span>布林位置</span><strong>{bb_pos_text}</strong></div>
                <div class="metric"><span>20日高/低</span><strong>{fmt_money(r['high20'])} / {fmt_money(r['low20'])}</strong></div>
                <div class="metric"><span>形态</span><strong class="{r['pattern_class']}">{r['pattern']}</strong></div>
                <div class="metric"><span>信号</span><strong class="{r['signal_class']}">{r['signal']}</strong></div>
                <div class="metric"><span>建议策略</span><strong>{r['suggest']}</strong></div>
            </div>
        </div>
        '''
    cards_html += '</div>'

    # 生成信号表
    rows = ''
    for r in results:
        rows += f'''
        <tr>
            <td class="asset-name">{r['name']}</td>
            <td>{r['date']}</td>
            <td class="{('up' if r['change_pct'] > 0 else 'down')}">{fmt_pct(r['change_pct'])}</td>
            <td>{r['rsi']:.1f}</td>
            <td class="{r['pattern_class']}">{r['pattern']}</td>
            <td class="{r['signal_class']}">{r['signal']}</td>
            <td>{r['suggest']}</td>
        </tr>
        '''
    table_html = f'''
    <h2>标的信号一览</h2>
    <table class="data-table">
        <thead>
            <tr><th>标的</th><th>数据日期</th><th>日涨跌</th><th>RSI(14)</th><th>形态</th><th>信号</th><th>建议策略</th></tr>
        </thead>
        <tbody>{rows}</tbody>
    </table>
    '''

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
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Microsoft YaHei','Segoe UI',sans-serif; background:#f0f2f5; color:#333; line-height:1.6; }}
.container {{ max-width:1200px; margin:0 auto; padding:20px; }}
header {{ text-align:center; padding:32px 20px; background:linear-gradient(135deg,#1a237e,#283593); color:#fff; border-radius:14px; margin-bottom:20px; box-shadow:0 4px 12px rgba(0,0,0,0.15); }}
header h1 {{ font-size:28px; margin-bottom:6px; }}
header p {{ opacity:0.85; font-size:13px; margin-top:8px; }}
.nav {{ display:flex; justify-content:center; gap:12px; flex-wrap:wrap; margin-top:16px; }}
.nav-link {{ color:#fff; text-decoration:none; padding:6px 14px; border:1px solid rgba(255,255,255,0.4); border-radius:20px; font-size:13px; transition:all 0.2s; }}
.nav-link:hover {{ background:rgba(255,255,255,0.15); }}
.section {{ background:#fff; border-radius:14px; padding:24px; margin-bottom:20px; box-shadow:0 2px 10px rgba(0,0,0,0.08); }}
.section h2 {{ font-size:19px; margin-bottom:16px; padding-bottom:10px; border-bottom:2px solid #e0e0e0; color:#1a237e; }}
.summary-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(160px, 1fr)); gap:14px; margin-bottom:10px; }}
.summary-card {{ background:linear-gradient(135deg,#42a5f5,#1976d2); color:#fff; border-radius:12px; padding:16px; text-align:center; }}
.summary-card .val {{ font-size:22px; font-weight:700; }}
.summary-card .label {{ font-size:12px; opacity:0.9; margin-top:4px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:16px; margin-top:20px; }}
.card {{ background:#fff; border:1px solid #e0e0e0; border-radius:12px; overflow:hidden; box-shadow:0 2px 6px rgba(0,0,0,0.06); }}
.card-header {{ display:flex; justify-content:space-between; align-items:center; padding:14px 18px; background:#f8f9ff; border-bottom:1px solid #e0e0e0; }}
.card-title {{ font-size:16px; font-weight:700; color:#1a237e; }}
.card-change {{ font-size:16px; font-weight:700; }}
.card-body {{ padding:14px 18px; display:grid; grid-template-columns:1fr 1fr; gap:10px 16px; }}
.metric {{ display:flex; justify-content:space-between; font-size:13px; }}
.metric span {{ color:#666; }}
.metric strong {{ color:#333; }}
.data-table {{ width:100%; border-collapse:collapse; font-size:13px; margin-top:10px; }}
.data-table th {{ background:#37474f; color:#fff; padding:10px 8px; text-align:center; font-weight:500; position:sticky; top:0; }}
.data-table td {{ padding:10px 8px; text-align:center; border-bottom:1px solid #e0e0e0; }}
.data-table tr:hover {{ background:#f5f5f5; }}
.asset-name {{ font-weight:600; text-align:left !important; color:#1a237e; }}
.up {{ color:#e53935; }}
.down {{ color:#43a047; }}
.neutral {{ color:#666; }}
.note {{ background:#fff3e0; border-left:4px solid #ff9800; padding:12px 16px; border-radius:6px; font-size:13px; color:#e65100; margin-top:16px; }}
footer {{ text-align:center; padding:24px; color:#999; font-size:12px; }}
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
