import re

# New unified nav template with warm theme
NAV_COMMON_ROOT = """<a href="review_a_share.html"><span class="nav-icon">🇨🇳</span><span>A股盘后</span></a>
<a href="review_us_pre.html"><span class="nav-icon">🌅</span><span>美股盘前</span></a>
<a href="review_us_post.html"><span class="nav-icon">🌙</span><span>美股盘后</span></a>
<a href="watchlist.html"><span class="nav-icon">📋</span><span>我的自选股</span></a>"""

NAV_COMMON_SUB = """<a href="{prefix}review_a_share.html"><span class="nav-icon">🇨🇳</span><span>A股盘后</span></a>
<a href="{prefix}review_us_pre.html"><span class="nav-icon">🌅</span><span>美股盘前</span></a>
<a href="{prefix}review_us_post.html"><span class="nav-icon">🌙</span><span>美股盘后</span></a>
<a href="{prefix}watchlist.html"><span class="nav-icon">📋</span><span>我的自选股</span></a>"""

NAV_BACKTEST = """<a href="159131_5min_optimal.html"><span class="nav-icon">📈</span><span>5分钟最优方案</span></a>
<a href="159131_5min_optimal_echarts.html"><span class="nav-icon">📊</span><span>ECharts 版本</span></a>
<a href="../strategy/report.html"><span class="nav-icon">📈</span><span>A股策略回测</span></a>"""

NAV_STRATEGY = """<a href="../backtest/159131_5min_optimal.html"><span class="nav-icon">📉</span><span>159131 5分钟回测</span></a>
<a href="report.html"><span class="nav-icon">📊</span><span>交互式回测报告</span></a>"""

SIDE_TEMPLATE = """\n<!-- unified nav -->\n<style>\n.unified-menu-btn {{\n    position: fixed;\n    top: 16px;\n    left: 16px;\n    z-index: 1001;\n    width: 40px;\n    height: 40px;\n    border-radius: 10px;\n    background: #ffffff;\n    color: #2c2420;\n    border: 1px solid #e8e2d8;\n    font-size: 18px;\n    cursor: pointer;\n    box-shadow: 0 2px 8px rgba(44,36,32,0.06);\n    display: flex;\n    align-items: center;\n    justify-content: center;\n    transition: all 0.15s;\n}}\n.unified-menu-btn:hover {{ background: #faf8f3; }}\n.unified-slidebar {{\n    position: fixed;\n    top: 0;\n    left: 0;\n    bottom: 0;\n    width: 220px;\n    background: #ffffff;\n    color: #2c2420;\n    z-index: 1002;\n    transform: translateX(-100%);\n    transition: transform 0.25s ease;\n    display: flex;\n    flex-direction: column;\n    border-right: 1px solid #e8e2d8;\n    box-shadow: 0 6px 20px rgba(44,36,32,0.1);\n}}\n.unified-slidebar.open {{ transform: translateX(0); }}\n.unified-slidebar-header {{\n    padding: 24px 20px;\n    border-bottom: 1px solid #e8e2d8;\n}}\n.unified-slidebar-logo {{\n    display: flex;\n    align-items: center;\n    gap: 10px;\n    font-size: 18px;\n    font-weight: 800;\n}}\n.unified-slidebar-logo .icon {{\n    width: 34px;\n    height: 34px;\n    background: #c78d5a;\n    color: #fff;\n    border-radius: 10px;\n    display: flex;\n    align-items: center;\n    justify-content: center;\n    font-size: 18px;\n}}\n.unified-slidebar-subtitle {{\n    font-size: 12px;\n    color: #6b6258;\n    margin-top: 6px;\n    padding-left: 44px;\n}}\n.unified-slidebar-nav {{\n    flex: 1;\n    padding: 16px 12px;\n    display: flex;\n    flex-direction: column;\n    gap: 4px;\n    overflow-y: auto;\n}}\n.unified-slidebar a {{\n    display: flex;\n    align-items: center;\n    gap: 12px;\n    padding: 11px 14px;\n    border-radius: 10px;\n    color: #6b6258;\n    text-decoration: none;\n    font-size: 14px;\n    font-weight: 600;\n    transition: all 0.15s;\n}}\n.unified-slidebar a:hover {{\n    background: #faf8f3;\n    color: #2c2420;\n}}\n.unified-slidebar a.active {{\n    background: #c78d5a;\n    color: #fff;\n}}\n.unified-slidebar a .nav-icon {{\n    font-size: 18px;\n    width: 24px;\n    text-align: center;\n}}\n.unified-slidebar-sep {{\n    height: 1px;\n    background: #e8e2d8;\n    margin: 10px 0;\n}}\n.unified-slidebar-footer {{\n    padding: 16px 12px;\n    border-top: 1px solid #e8e2d8;\n}}\n.unified-overlay {{\n    position: fixed;\n    inset: 0;\n    background: rgba(44,36,32,0.25);\n    z-index: 1000;\n    opacity: 0;\n    visibility: hidden;\n    transition: all 0.25s;\n}}\n.unified-overlay.open {{ opacity: 1; visibility: visible; }}\nbody.in-iframe .unified-menu-btn,\nbody.in-iframe .unified-slidebar,\nbody.in-iframe .unified-overlay {{\n    display: none !important;\n}}\n@media (max-width: 768px) {{\n    .unified-menu-btn {{ top: 12px; left: 12px; width: 36px; height: 36px; font-size: 16px; }}\n    .unified-slidebar {{ width: 100%; }}\n}}\n</style>\n<button class="unified-menu-btn" id="unifiedMenuBtn" title="菜单">☰</button>\n<nav class="unified-slidebar" id="unifiedSlidebar">\n    <div class="unified-slidebar-header">\n        <div class="unified-slidebar-logo">\n            <span class="icon">📈</span>\n            <span>市场复盘</span>\n        </div>\n        <div class="unified-slidebar-subtitle">Daily Market Review</div>\n    </div>\n    <div class="unified-slidebar-nav">\n{NAV_LINKS}\n    </div>\n    <div class="unified-slidebar-footer">\n        <a class="nav-item" href="{HOME_URL}"><span class="nav-icon">🏠</span><span>主页</span></a>\n        <a class="nav-item" href="{REPORT_URL}"><span class="nav-icon">📊</span><span>完整回测报告</span></a>\n        <a class="nav-item" href="https://github.com/SmasFan/daily-stock-review" target="_blank" rel="noopener"><span class="nav-icon"><i class="fab fa-github"></i></span><span>查看项目</span></a>\n    </div>\n</nav>\n<div class="unified-overlay" id="unifiedOverlay"></div>\n<script>\n(function(){{\n    if (window.self !== window.top) {{\n        document.body.classList.add('in-iframe');\n        return;\n    }}\n    const btn = document.getElementById('unifiedMenuBtn');\n    const bar = document.getElementById('unifiedSlidebar');\n    const overlay = document.getElementById('unifiedOverlay');\n    function open(){{ bar.classList.add('open'); overlay.classList.add('open'); }}\n    function close(){{ bar.classList.remove('open'); overlay.classList.remove('open'); }}\n    btn.addEventListener('click', () => bar.classList.contains('open') ? close() : open());\n    overlay.addEventListener('click', close);\n    const links = bar.querySelectorAll('a');\n    const current = location.pathname.split('/').pop();\n    links.forEach(a => {{\n        const href = a.getAttribute('href');\n        if (href && !href.startsWith('http') && !href.startsWith('#')) {{\n            const target = href.split('/').pop();\n            if (target === current) a.classList.add('active');\n        }}\n    }});\n}})();\n</script>\n<!-- /unified nav -->\n"""


def replace_unified_nav(content, nav_links, home_url, report_url):
    # remove existing unified nav
    start = content.find('\n<!-- unified nav -->')
    end = content.find('<!-- /unified nav -->\n')
    if start != -1 and end != -1:
        content = content[:start] + content[end + len('<!-- /unified nav -->\n'):]
    snippet = SIDE_TEMPLATE.format(NAV_LINKS=nav_links, HOME_URL=home_url, REPORT_URL=report_url)
    return content.replace('</body>', snippet + '</body>')


# Update root-level pages
for name in ['review_a_share.html', 'review_us_pre.html', 'review_us_post.html', 'report.html']:
    path = f'/workspace/{name}'
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    content = replace_unified_nav(content, NAV_COMMON_ROOT, 'https://smasfan.github.io/', 'report.html')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'updated nav {path}')

# Update backtest pages
common_sub_backtest = NAV_COMMON_SUB.format(prefix='../') + NAV_BACKTEST
for name in ['159131_5min_optimal.html', '159131_5min_optimal_echarts.html']:
    path = f'/workspace/backtest/{name}'
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    content = replace_unified_nav(content, common_sub_backtest, '../', '../report.html')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'updated nav {path}')

# Update strategy page
common_sub_strategy = NAV_COMMON_SUB.format(prefix='../') + NAV_STRATEGY
path = '/workspace/strategy/report.html'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()
content = replace_unified_nav(content, common_sub_strategy, '../', '../report.html')
with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print(f'updated nav {path}')

print('nav update done')
