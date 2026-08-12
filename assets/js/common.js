/* ===== 每日复盘共享前端层：数据获取 + 渲染工具 ===== */
(function (global) {
  'use strict';

  /* ---------- API 层：统一数据获取（对齐 axios 单例思路） ---------- */
  const API = {
    base: 'data/',
    async fetch(name, retries = 2) {
      // 超时 + 重试：GitHub Pages 在国内网络下偶发 10s+ 延迟/断连
      let lastErr;
      for (let attempt = 0; attempt <= retries; attempt++) {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 20000);
        try {
          const res = await fetch(this.base + name + '?t=' + Date.now(), { signal: ctrl.signal });
          clearTimeout(timer);
          if (!res.ok) throw new Error('加载失败: ' + name + ' (' + res.status + ')');
          return await res.json();
        } catch (e) {
          clearTimeout(timer);
          lastErr = e;
          if (attempt < retries) await new Promise(r => setTimeout(r, 800 * (attempt + 1)));
        }
      }
      throw lastErr;
    },
    review() { return this.fetch('review_data.json'); },
    recommend() { return this.fetch('recommend_data.json'); },
    backtest() { return this.fetch('backtest_index.json'); },
    backtestStock(name) { return this.fetch('backtest/' + encodeURIComponent(name) + '.json'); },
    uptrend() { return this.fetch('uptrend_data.json'); },
    heat() { return this.fetch('market_heat.json'); },
    holdings() { return this.fetch('holdings_data.json'); },
    metals() { return this.fetch('metals_data.json'); },
    institution() { return this.fetch('institution_data.json'); },
  };

  /* ---------- 格式化工具 ---------- */
  const fmt = {
    pct(v, digits = 2) {
      if (v === null || v === undefined || isNaN(v)) return '--';
      return (v > 0 ? '+' : '') + Number(v).toFixed(digits) + '%';
    },
    num(v, digits = 2) {
      if (v === null || v === undefined || isNaN(v)) return '--';
      return Number(v).toFixed(digits);
    },
    money(v) {
      if (v === null || v === undefined || isNaN(v)) return '--';
      return Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
    },
    amount(v) {  // 成交额人性化
      if (!v) return '--';
      if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿';
      if (v >= 1e4) return (v / 1e4).toFixed(2) + '万';
      return String(Math.round(v));
    },
    cls(v) { return v > 0 ? 'up' : (v < 0 ? 'down' : 'neutral'); },
  };

  /* ---------- 通用组件 ---------- */
  const ui = {
    /* Font Awesome 图标：icon('chart-line') -> <i class="fa-solid fa-chart-line"></i> */
    icon(name, cls = '') {
      return `<i class="fa-solid fa-${name} ${cls}" aria-hidden="true"></i>`;
    },
    /* 页头（含导航），active 为高亮页 key */
    header(title, subtitle, active, genTime) {
      const navs = [
        ['review', '复盘', 'clipboard-list', 'review.html'],
        ['uptrend', '趋势', 'arrow-trend-up', 'uptrend.html'],
        ['recommend', '推荐', 'bullseye', 'recommend.html'],
        ['institution', '资金', 'landmark', 'institution.html'],
        ['macro', '宏观', 'newspaper', 'macro.html'],
        ['holdings', '持仓', 'briefcase', 'holdings.html'],
        ['backtest', '回测', 'flask', 'backtest.html'],
        ['tracking', '跟踪', 'route', 'tracking.html'],
        ['metals', '有色', 'coins', 'metals.html'],
        ['docs', '说明', 'book-open', 'docs.html'],
      ];
      const navHtml = navs.map(([k, label, icon, href]) =>
        `<a class="nav-link ${k === active ? 'active' : ''}" href="${href}">${ui.icon(icon)} ${label}</a>`
      ).join('');
      return `
      <header class="top">
        <h1>${title}</h1>
        <div class="gen-time">${subtitle || ''}${genTime ? ' · 生成于 ' + genTime : ''}</div>
        <nav class="nav">${navHtml}
          <a class="nav-link repo-link" href="https://github.com/SmasFan/daily-stock-review" target="_blank" rel="noopener">${ui.icon('github')} 项目源码</a>
        </nav>
      </header>`;
    },

    signalBadge(key, label) {
      return `<span class="signal-badge s-${key}">${label}</span>`;
    },
    ratingBadge(r) {
      return `<span class="rating-badge rating-${r}">${r}</span>`;
    },
    pctBadge(v) {
      return `<span class="pct-badge ${fmt.cls(v)}">${fmt.pct(v)}</span>`;
    },
    progress(value, min = 0, max = 100) {
      const pct = Math.max(0, Math.min(100, (value - min) / (max - min) * 100));
      let color = '#c78d5a';
      if (pct < 30) color = '#16a34a';
      else if (pct > 70) color = '#dc2626';
      return `<div class="progress"><div class="progress-fill" style="width:${pct.toFixed(1)}%;background:${color}"></div></div>`;
    },
    metric(val, label, cls = '', icon = '') {
      return `<div class="metric-card"><div class="m-val ${cls}">${icon ? ui.icon(icon) + ' ' : ''}${val}</div><div class="m-label">${label}</div></div>`;
    },
    summaryCard(icon, val, label, cls = '') {
      return `<div class="summary-card"><div class="icon">${ui.icon(icon)}</div>
        <div class="summary-text"><div class="val ${cls}">${val}</div><div class="label">${label}</div></div></div>`;
    },
    outcomeLabel(o) {
      const map = {
        win: `${ui.icon('check', 'fa-xs')} 兑现`,
        loss: `${ui.icon('xmark', 'fa-xs')} 未兑现`,
        neutral: `${ui.icon('minus', 'fa-xs')} 中性`
      };
      return `<span class="outcome-${o}">${map[o] || o}</span>`;
    },
    /* 右上角小问号：悬停/点击弹出详情。浮层由共享监听器挂到 body，避免被 overflow 裁剪 */
    infoDot(content, label = '') {
      const data = JSON.stringify({ label: label || '', content: content || '' })
        .replace(/"/g, '&quot;');
      return `<span class="info-dot" tabindex="0" role="button" data-info='${data}'>
        <span class="info-dot-ic">${ui.icon('circle-question')}</span>
      </span>`;
    },
    loading(msg = '数据加载中…') { return `<div class="loading">${msg}</div>`; },
    empty(msg = '暂无数据') { return `<div class="empty">${msg}</div>`; },
    error(e) { return `<div class="empty">${ui.icon('triangle-exclamation')} ${e.message || e}<br><small>请先运行 <code>python run_review.py</code> 生成数据</small></div>`; },

    /* 市场温度 & 走势按钮（点击弹出双Y轴联动图） */
    heatBtn(code, name, kind = 'stock') {
      return `<span class="heat-btn" data-heat="${code}" data-heat-name="${name}" data-heat-kind="${kind}"
        title="市场温度 & ${name} 走势" role="button" tabindex="0">${ui.icon('temperature-half')}</span>`;
    },

    /* 侧边栏章节导航：扫描带 data-nav 的区块，生成右侧固定导航 + 滚动高亮
       用法：区块 <div class="section" data-nav="名称" data-nav-icon="chart-line"></div>
       页面渲染完成后调用 RV.ui.initSidebar() */
    initSidebar() {
      if (!document || typeof window === 'undefined') return;
      // 移除旧的侧边栏（页面重渲染时重建）
      const old = document.querySelector('.sidebar-nav');
      if (old) old.remove();
      const oldFab = document.querySelector('.sidebar-fab');
      if (oldFab) oldFab.remove();
      const oldSheet = document.querySelector('.sidebar-sheet');
      if (oldSheet) oldSheet.remove();
      const sections = Array.from(document.querySelectorAll('[data-nav]')).filter(s => s.offsetParent !== null);
      if (sections.length < 2) return;
      const navItems = sections.map((s, i) => ({
        icon: s.getAttribute('data-nav-icon') || '',
        label: s.getAttribute('data-nav') || '',
        idx: i,
      }));

      // 渲染条目 HTML
      const itemHtml = (cls = '') => navItems.map(it => `
        <a href="#" data-sidebar-target="${it.idx}" class="${cls && it.idx === 0 ? cls : ''}">
          ${it.icon ? ui.icon(it.icon) : ''}
          <span>${it.label}</span>
        </a>`).join('');

      // 大屏：右侧固定导航
      const nav = document.createElement('div');
      nav.className = 'sidebar-nav';
      nav.innerHTML = itemHtml('active');
      document.body.appendChild(nav);

      // 小屏：浮动按钮 + 抽屉
      const fab = document.createElement('div');
      fab.className = 'sidebar-fab';
      fab.innerHTML = ui.icon('list-ul');
      fab.setAttribute('aria-label', '章节导航');
      const sheet = document.createElement('div');
      sheet.className = 'sidebar-sheet';
      sheet.innerHTML = `<div class="sidebar-sheet-head">${ui.icon('list-ul')} 章节导航
        <a class="sidebar-sheet-close" href="#" aria-label="关闭">${ui.icon('xmark')}</a></div>
        <div class="sidebar-sheet-body">${itemHtml('active')}</div>`;
      document.body.appendChild(fab);
      document.body.appendChild(sheet);
      // 遮罩
      const overlay = document.createElement('div');
      overlay.className = 'sidebar-overlay';
      document.body.appendChild(overlay);

      function openSheet() { sheet.classList.add('open'); overlay.classList.add('show'); document.body.style.overflow = 'hidden'; }
      function closeSheet() { sheet.classList.remove('open'); overlay.classList.remove('show'); document.body.style.overflow = ''; }

      fab.addEventListener('click', openSheet);
      overlay.addEventListener('click', closeSheet);
      sheet.querySelector('.sidebar-sheet-close').addEventListener('click', e => { e.preventDefault(); closeSheet(); });

      function triggerTarget(idx) {
        const sec = sections[idx];
        if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }

      nav.addEventListener('click', e => {
        const a = e.target.closest('[data-sidebar-target]');
        if (!a) return;
        e.preventDefault();
        triggerTarget(+a.getAttribute('data-sidebar-target'));
      });
      sheet.addEventListener('click', e => {
        const a = e.target.closest('[data-sidebar-target]');
        if (!a) return;
        e.preventDefault();
        triggerTarget(+a.getAttribute('data-sidebar-target'));
        closeSheet();
      });

      // 滚动高亮：当前视口内的区块高亮
      let activeIdx = 0;
      const update = () => {
        const mid = window.innerHeight * 0.3;
        let idx = activeIdx;
        for (let i = 0; i < sections.length; i++) {
          const r = sections[i].getBoundingClientRect();
          if (r.top <= mid) idx = i;
        }
        if (idx !== activeIdx) {
          activeIdx = idx;
          nav.querySelectorAll('a').forEach((a, i) => a.classList.toggle('active', i === idx));
          sheet.querySelectorAll('a').forEach((a, i) => a.classList.toggle('active', i === idx));
        }
      };
      let ticking = false;
      window.addEventListener('scroll', () => {
        if (ticking) return;
        ticking = true;
        requestAnimationFrame(() => { update(); ticking = false; });
      }, { passive: true });
      update();
    },
  };

  global.RV = { API, fmt, ui };
})(window);

/* ===== 卡片展开/收起：点击头部在下方展开/收起详情 ===== */
(function () {
  if (!document || typeof window === 'undefined') return;
  document.addEventListener('click', e => {
    const skip = e.target.closest('.info-dot, a, button');
    if (skip) return;
    const bar = e.target.closest('.card-header, .card-summary, .mini-head');
    if (!bar) return;
    const host = bar.closest('.card') || bar.closest('.mini-card');
    if (host) host.classList.toggle('open');
  });
})();

/* ===== 全局浮层监听：.info-dot 共享一个浮层，挂到 body 顶层 ===== */
(function () {
  if (!document || typeof window === 'undefined') return;
  let tipEl = null;
  const TIP = document.createElement('div');
  TIP.className = 'info-tip-global';
  TIP.style.cssText = 'display:none;position:fixed;z-index:9999;max-width:380px;width:max-content;' +
    'max-width:calc(100vw - 16px);padding:12px 14px;border-radius:10px;background:var(--surface,#fff);' +
    'border:1px solid var(--border,#e5e7eb);box-shadow:0 8px 24px rgba(0,0,0,0.12);' +
    'color:var(--text,#111827);font-size:12px;line-height:1.6;text-align:left;' +
    'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;';
  document.body.appendChild(TIP);

  function show(dot) {
    try {
      const d = JSON.parse(dot.getAttribute('data-info') || '{}');
      TIP.innerHTML = (d.label ? '<b>' + d.label + '</b>' : '') + (d.content || '');
      TIP.style.display = 'block';
      position(dot);
    } catch (e) { hide(); }
  }
  function hide() { TIP.style.display = 'none'; }
  function position(dot) {
    const r = dot.getBoundingClientRect();
    const tw = TIP.offsetWidth, th = TIP.offsetHeight;
    const vw = window.innerWidth, vh = window.innerHeight;
    let x = r.left + r.width / 2 - tw / 2;
    if (x < 8) x = 8;
    if (x + tw > vw - 8) x = vw - tw - 8;
    let y = r.bottom + 8;
    if (y + th > vh - 8) y = r.top - th - 8;
    TIP.style.left = Math.round(x) + 'px';
    TIP.style.top = Math.round(y) + 'px';
  }

  // 触摸设备（无 hover）：mouseover 会由触摸模拟触发，只在非触摸端使用 hover 显示
  const isTouch = ('ontouchstart' in window) || navigator.maxTouchPoints > 0;
  document.addEventListener('mouseover', e => {
    if (isTouch) return;
    const dot = e.target.closest('.info-dot');
    if (dot && !dot.classList.contains('info-active')) { dot.classList.add('info-active'); show(dot); }
    else if (!dot && tipEl) hide();
  });
  document.addEventListener('click', e => {
    const dot = e.target.closest('.info-dot');
    if (dot) {
      e.stopPropagation();
      // 已展开则再次点击关闭
      if (tipEl === dot && TIP.style.display === 'block') { hide(); tipEl = null; return; }
      show(dot);
      tipEl = dot;
    } else {
      hide();
      tipEl = null;
    }
  });
  window.addEventListener('scroll', () => { if (tipEl) position(tipEl); }, { passive: true });
  window.addEventListener('resize', () => { if (tipEl) position(tipEl); });
})();

/* ===== 市场温度 & 走势弹窗：双Y轴折线图（温度橙 + 走势蓝） ===== */
(function () {
  if (!document || typeof window === 'undefined') return;
  const CDN = 'https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js';
  let heatCache = null;
  let echartsPromise = null;
  let chart = null;
  let overlayEl = null;

  function loadEcharts() {
    if (window.echarts) return Promise.resolve();
    if (echartsPromise) return echartsPromise;
    echartsPromise = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = CDN;
      s.onload = () => resolve();
      s.onerror = () => reject(new Error('ECharts 加载失败，请检查网络'));
      document.head.appendChild(s);
    });
    return echartsPromise;
  }

  async function openHeat(code, name, kind) {
    try {
      await loadEcharts();
      if (!heatCache) heatCache = await window.RV.API.heat();
      const d = heatCache;
      const x = d.tempDates || [];
      const temp = d.temps || [];
      let series2 = null;
      if (kind === 'sector') {
        const s = (d.sectors || {})[code];
        series2 = s ? { name: name || code, data: s.nav, navBase: s.nav && s.nav.find(v => v != null) } : null;
      } else {
        const s = (d.stocks || {})[code];
        series2 = s ? { name: s.name || name || code, data: s.close } : null;
      }
      if (!series2 || !series2.data) { alert('暂无该标的的走势数据'); return; }

      overlayEl = document.createElement('div');
      overlayEl.className = 'heat-overlay';
      overlayEl.style.cssText = 'position:fixed;inset:0;background:rgba(20,18,16,0.55);z-index:9998;display:flex;align-items:center;justify-content:center;padding:16px;';
      overlayEl.innerHTML = `<div class="heat-modal" style="background:var(--surface,#fff);border-radius:14px;box-shadow:0 16px 48px rgba(0,0,0,.25);width:min(860px,96vw);padding:18px 20px;position:relative">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
          <span style="font-weight:800;font-size:15px">市场温度 & ${series2.name} 走势</span>
          <span style="font-size:11px;color:var(--text-2)">${d.tempNote || ''} · ${d.generatedAt || ''}</span>
          <button class="heat-close" style="margin-left:auto;border:1px solid var(--border);background:var(--surface-2);border-radius:8px;padding:4px 12px;font-size:12px;cursor:pointer">${window.RV.ui.icon('xmark')} 关闭</button>
        </div>
        <div class="heat-chart" style="width:100%;height:420px"></div>
        <div style="font-size:11px;color:var(--text-2);margin-top:8px">${window.RV.ui.icon('hand-pointer', 'fa-xs')} 悬停查看每日数值 · 拖拽平移 · 滚轮缩放</div>
      </div>`;
      document.body.appendChild(overlayEl);
      overlayEl.addEventListener('click', e => { if (e.target === overlayEl) close(); });
      overlayEl.querySelector('.heat-close').addEventListener('click', close);
      document.addEventListener('keydown', escClose);

      chart = window.echarts.init(overlayEl.querySelector('.heat-chart'));
      chart.setOption({
        animation: false,
        tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
        legend: { data: ['市场温度', series2.name], textStyle: { color: '#6b6258' } },
        grid: { left: 64, right: 70, top: 40, bottom: 56 },
        dataZoom: [{ type: 'inside', xAxisIndex: 0 }],
        xAxis: { type: 'category', data: x, boundaryGap: false,
          axisLabel: { color: '#6b6258', formatter: v => v.slice(5) }, axisLine: { lineStyle: { color: '#d1c9bf' } } },
        yAxis: [
          { type: 'value', name: '温度', min: 0, max: 100, axisLabel: { color: '#b0864a' }, splitLine: { lineStyle: { color: '#efe9df' } } },
          { type: 'value', name: kind === 'sector' ? '板块净值' : '价格', scale: true, axisLabel: { color: '#4a7db0' }, splitLine: { show: false } }
        ],
        series: [
          { name: '市场温度', type: 'line', data: temp, yAxisIndex: 0, showSymbol: false,
            lineStyle: { width: 2.2, color: '#e8a33d' }, itemStyle: { color: '#e8a33d' },
            areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [{ offset: 0, color: 'rgba(232,163,61,0.25)' }, { offset: 1, color: 'rgba(232,163,61,0.02)' }] } } },
          { name: series2.name, type: 'line', data: series2.data, yAxisIndex: 1, showSymbol: false,
            lineStyle: { width: 1.8, color: '#5b8fd6' }, itemStyle: { color: '#5b8fd6' } }
        ]
      });
      const resize = () => chart && chart.resize();
      window.addEventListener('resize', resize);
      overlayEl._resize = resize;
    } catch (e) {
      alert('打开温度图失败: ' + (e.message || e));
    }
  }

  function escClose(e) {
    if (e.key === 'Escape') close();
  }
  function close() {
    document.removeEventListener('keydown', escClose);
    if (chart) { chart.dispose(); chart = null; }
    if (overlayEl) {
      if (overlayEl._resize) window.removeEventListener('resize', overlayEl._resize);
      overlayEl.remove(); overlayEl = null;
    }
    document.body.style.overflow = '';
  }

  document.addEventListener('click', e => {
    const btn = e.target.closest('[data-heat]');
    if (!btn) return;
    e.stopPropagation();
    openHeat(btn.getAttribute('data-heat'), btn.getAttribute('data-heat-name'), btn.getAttribute('data-heat-kind') || 'stock');
  });
})();
