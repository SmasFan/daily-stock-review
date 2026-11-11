/* ===== smas·fx 适配层：手机底部视图条 + 更多面板 + 顶栏品牌标 =====
   独立脚本，不依赖 RV / common.js，可挂到任何页面（含自包含页面）。
   只注入 DOM，不改任何页面数据逻辑；菜单栏 popover (dsrbar_card.html) 明确排除。 */
(function () {
  'use strict';
  const page = (location.pathname.split('/').pop() || 'index.html').toLowerCase();
  if (page === 'dsrbar_card.html') return;

  // 没引 common.js 的页面用内联 SVG，避免依赖 FontAwesome
  const PATH = {
    'chart-line': 'M3 3v18h18M7 15l4-5 3 3 5-7',
    'clipboard-list': 'M9 4h6v3H9zM6 5h12v15H6zM9 11h6M9 15h4',
    'bullseye': 'M12 3v3M12 18v3M3 12h3M18 12h3M12 8a4 4 0 100 8 4 4 0 000-8zM12 10.5a1.5 1.5 0 100 3 1.5 1.5 0 000-3z',
    'gamepad': 'M7 9h10a4 4 0 014 4l1 4a2 2 0 01-3.6 1.4L17 17H7l-1.4 1.4A2 2 0 012 17l1-4a4 4 0 014-4zM7 12v3M5.5 13.5h3M16 13h.01M18.5 15h.01',
    'bars': 'M4 6h16M4 12h16M4 18h16',
  };
  const I = n => (window.RV && window.RV.ui && window.RV.ui.icon)
    ? window.RV.ui.icon(n)
    : '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" '
      + 'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      + '<path d="' + (PATH[n] || PATH['chart-line']) + '"/></svg>';

  const ALL = [
    ['index.html', '工作台', 'chart-line'], ['review.html', '复盘', 'clipboard-list'],
    ['uptrend.html', '趋势', 'chart-line'], ['mainline.html', '主线', 'bars'],
    ['recommend.html', '推荐', 'bullseye'], ['lowval.html', '低估值', 'chart-line'],
    ['institution.html', '资金', 'bars'], ['macro.html', '宏观', 'bars'],
    ['holdings.html', '持仓', 'clipboard-list'], ['backtest.html', '回测', 'chart-line'],
    ['tracking.html', '跟踪', 'clipboard-list'], ['metals.html', '期货', 'bars'],
    ['industry.html', '产业', 'bars'], ['sim.html', '模拟盘', 'gamepad'],
    ['sim_live.html', '实时模拟', 'chart-line'], ['sim_dividend.html', '红利池', 'chart-line'],
    ['us-sim.html', '美股模拟', 'chart-line'], ['binance.html', '币安 Bot', 'chart-line'],
    ['perp.html', '全天候盘', 'chart-line'], ['watchlist.html', '自选股', 'bars'],
    ['docs.html', '说明', 'clipboard-list'],
  ];
  const MAIN = [
    ['index.html', '总览', 'chart-line'],
    ['review.html', '复盘', 'clipboard-list'],
    ['recommend.html', '推荐', 'bullseye'],
    ['sim_live.html', '模拟', 'gamepad'],
    ['#more', '更多', 'bars'],
  ];
  const isOn = href => href === page;
  const barHtml = MAIN.map(([href, label, icon]) => href === '#more'
    ? '<button class="sfx-tab" id="sfx-more-btn" type="button">' + I(icon) + '<span>' + label + '</span></button>'
    : '<a class="sfx-tab' + (isOn(href) ? ' on' : '') + '" href="' + href + '">'
      + I(icon) + '<span>' + label + '</span></a>').join('');
  const sheetHtml = ALL.map(([href, label, icon]) =>
    '<a class="' + (isOn(href) ? 'on' : '') + '" href="' + href + '">'
    + I(icon) + '<span>' + label + '</span></a>').join('')
    + '<a class="repo" href="https://github.com/SmasFan/daily-stock-review" target="_blank" rel="noopener">项目源码 ↗</a>';

  function brand() {
    const tb = document.querySelector('body > .tb');
    if (!tb || tb.querySelector('.sfx-brand')) return;
    const b = document.createElement('div');
    b.className = 'sfx-brand';
    b.innerHTML = '<span class="sfx-mark" aria-hidden="true"></span><strong>每日复盘</strong>';
    tb.insertBefore(b, tb.firstChild);
    const clk = document.createElement('span');
    clk.className = 'sfx-clock';
    const tick = () => {
      const d = new Date();
      clk.textContent = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    };
    tick(); setInterval(tick, 30000);
    tb.appendChild(clk);
  }

  const STANDALONE = ['binance.html', 'perp.html', 'watchlist.html'];
  function mount() {
    if (!document.body || document.querySelector('.sfx-tabbar')) return;
    brand();
    // 自包含页面（不引 common.js、无统一 .container 留白）单独补偿
    if (STANDALONE.indexOf(page) >= 0) document.body.classList.add('sfx-has-nav');
    const bar = document.createElement('nav');
    bar.className = 'sfx-tabbar';
    bar.setAttribute('aria-label', '视图切换');
    bar.innerHTML = barHtml;
    const ov = document.createElement('div');
    ov.className = 'sfx-overlay';
    const sheet = document.createElement('div');
    sheet.className = 'sfx-sheet';
    sheet.innerHTML = '<div class="sfx-sheet-head"><span>全部页面</span>'
      + '<button type="button" id="sfx-sheet-close">关闭</button></div>'
      + '<div class="sfx-links">' + sheetHtml + '</div>';
    document.body.appendChild(bar);
    document.body.appendChild(ov);
    document.body.appendChild(sheet);
    const close = () => { sheet.classList.remove('open'); ov.classList.remove('show'); };
    const more = bar.querySelector('#sfx-more-btn');
    if (more) more.addEventListener('click', () => {
      sheet.classList.toggle('open');
      ov.classList.toggle('show');
    });
    ov.addEventListener('click', close);
    sheet.querySelector('#sfx-sheet-close').addEventListener('click', close);
    document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  }
  if (document.body) mount(); else document.addEventListener('DOMContentLoaded', mount);
  // RV.ui.header() 在 setTimeout(0) 里才把 .tb 提到 body，补一次品牌注入
  setTimeout(mount, 0);
  setTimeout(brand, 250);
  // common.js 放在 <head> 的页面（body 还不存在），load 事件再兜一次
  window.addEventListener('load', mount);
})();
