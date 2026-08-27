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
    mainline() { return this.fetch('mainline_data.json'); },
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
        ['mainline', '主线', 'crown', 'mainline.html'],
        ['recommend', '推荐', 'bullseye', 'recommend.html'],
        ['lowval', '低估值', 'magnifying-glass-chart', 'lowval.html'],
        ['institution', '资金', 'landmark', 'institution.html'],
        ['macro', '宏观', 'newspaper', 'macro.html'],
        ['holdings', '持仓', 'briefcase', 'holdings.html'],
        ['backtest', '回测', 'flask', 'backtest.html'],
        ['tracking', '跟踪', 'route', 'tracking.html'],
        ['metals', '期货', 'coins', 'metals.html'],
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

    /* 宽基指数模块：沪深300/中证500/A500/科创50/恒生科技/创新药/中概/纳指/标普/医药
       wide = { items: [{code,name,kind,close,change_pct,pe,pb,pe_percentile,factors}], source_note } */
    wideIndexSection(wide) {
      if (!wide || !(wide.items || []).length) return '';
      const items = wide.items;
      const byKind = {};
      items.forEach(x => { (byKind[x.kind || '其他'] = byKind[x.kind || '其他'] || []).push(x); });
      const peText = (x) => {
        if (x.pe == null) return 'PE --';
        const srcTag = x.pe_source === '腾讯' ? '<span style="opacity:.55">(Q)</span>' : '';
        if (x.pe_percentile != null) return `PE ${RV.fmt.num(x.pe, 1)} ${srcTag}<span class="${x.pe_percentile >= 0.7 ? 'down' : (x.pe_percentile <= 0.3 ? 'up' : '')}" style="font-size:10px">${(x.pe_percentile * 100).toFixed(0)}%分位</span>`;
        return `PE ${RV.fmt.num(x.pe, 1)} ${srcTag}`;
      };
      const card = (x) => {
        const f = x.factors || {};
        const sig = f.signal_key ? RV.ui.signalBadge(f.signal_key, f.signal) : '';
        return `
        <div class="wide-card" data-wide-code="${x.code}">
          <div class="wide-head">
            <span class="wide-name">${x.name}</span>
            <span class="wide-kind">${x.kind}</span>
          </div>
          <div class="wide-price">${RV.fmt.num(x.close)} <span class="${RV.fmt.cls(x.change_pct)}">${RV.fmt.pct(x.change_pct)}</span></div>
          <div class="wide-meta">${peText(x)} · ${f.trend_status || ''} ${f.score != null ? '· ' + f.score + '分' : ''}</div>
          ${sig ? `<div class="wide-sig">${sig}</div>` : ''}
        </div>`;
      };
      const groups = Object.keys(byKind).map(k => `
        <div class="wide-group">
          <div class="wide-group-title">${RV.ui.icon('chart-column', 'fa-xs')} ${k}</div>
          <div class="wide-cards">${byKind[k].map(card).join('')}</div>
        </div>`).join('');
      return `<div class="section" data-nav="宽基指数" data-nav-icon="chart-line">
        <div class="section-title"><span class="title-icon">${RV.ui.icon('chart-line')}</span><h2>宽基指数 · 全球配置</h2>
          ${RV.ui.infoDot(wide.source_note || '行情腾讯 · 估值蛋卷 · 技术因子同源分析器')}
        </div>
        ${groups}
        <div style="font-size:11px;color:var(--text-2);margin-top:10px">${RV.ui.icon('hand-pointer', 'fa-xs')} 点击卡片查看走势 · PE分位=近10年估值百分位（越高越贵） · 不构成投资建议</div>
      </div>`;
    },

    /* 宏观新闻弹窗：点击「宏观利好/宏观风险」标签查看相关新闻列表
       data = { title, score, bulls, risks, key: [{title, kind, score}] } */
    macroNewsModal(data) {
      const keys = (data && data.key) || [];
      const rows = keys.length
        ? keys.map(k => {
            const bull = k.kind !== 'risk';
            const score = k.score != null ? ` <span style="font-size:11px;opacity:.7">+${k.score}</span>` : '';
            return `<div style="display:flex;gap:8px;align-items:flex-start;padding:9px 0;border-bottom:1px solid var(--border)">
              <span style="flex-shrink:0;font-size:11px;font-weight:700;padding:2px 7px;border-radius:5px;margin-top:1px;background:${bull ? 'var(--buy-bg)' : 'var(--sell-bg)'};color:${bull ? 'var(--buy)' : 'var(--sell)'}">${bull ? '利好' : '风险'}</span>
              <span style="font-size:13px;line-height:1.5;color:var(--text)">${k.title || ''}${score}</span>
            </div>`;
          }).join('')
        : `<div style="padding:18px 0;color:var(--text-2);font-size:13px;text-align:center">该对象暂无命中的宏观新闻</div>`;
      const overlay = document.createElement('div');
      overlay.className = 'idx-modal-overlay';
      overlay.innerHTML = `<div class="idx-modal" style="max-width:620px">
        <div class="idx-modal-head">
          <span class="idx-m-name">${ui.icon('newspaper', 'fa-xs')} ${(data && data.title) || '宏观新闻'}</span>
          <span style="font-size:12px;color:var(--text-2)">
            ${data && data.score != null ? `净情绪 <b class="${data.score > 0 ? 'up' : 'down'}">${RV.fmt.num(data.score, 1)}</b> · ` : ''}
            <span class="up">利好 ${data && data.bulls != null ? data.bulls : 0}</span> / <span class="down">风险 ${data && data.risks != null ? data.risks : 0}</span>
          </span>
          <button class="idx-modal-close" aria-label="关闭">${ui.icon('xmark')}</button>
        </div>
        <div class="idx-modal-body" style="max-height:60vh;overflow-y:auto">${rows}</div>
      </div>`;
      document.body.appendChild(overlay);
      document.body.style.overflow = 'hidden';
      const close = () => { overlay.remove(); document.body.style.overflow = ''; };
      overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
      overlay.querySelector('.idx-modal-close').addEventListener('click', close);
      const esc = e => { if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); } };
      document.addEventListener('keydown', esc);
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
  // ECharts 加载顺序：本地文件（同站，快）→ bootcdn → jsdelivr → unpkg
  const CDNS = [
    'assets/js/echarts.min.js',
    'https://cdn.bootcdn.net/ajax/libs/echarts/5.4.3/echarts.min.js',
    'https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js',
    'https://unpkg.com/echarts@5.4.3/dist/echarts.min.js',
  ];
  let heatCache = null;
  let echartsPromise = null;
  let chart = null;
  let overlayEl = null;
  let preloaded = false;
  const loadEcharts = _loadEcharts;
  async function _loadEcharts() {
    if (window.echarts) return Promise.resolve();
    if (echartsPromise) return echartsPromise;
    echartsPromise = new Promise((resolve, reject) => {
      let i = 0;
      const tryNext = () => {
        if (i >= CDNS.length) { reject(new Error('ECharts 加载失败，请检查网络')); return; }
        const s = document.createElement('script');
        s.src = CDNS[i++];
        s.onload = () => resolve();
        s.onerror = () => { s.remove(); tryNext(); };
        document.head.appendChild(s);
      };
      tryNext();
    });
    return echartsPromise;
  }

  /* 预加载：数据立即预取（快），echarts 空闲加载（1MB 脚本不阻塞首屏） */
  function preload() {
    if (preloaded) return;
    preloaded = true;
    if (!heatCache) {
      window.RV.API.heat().then(d => { heatCache = d; }).catch(() => {});
    }
    const idle = window.requestIdleCallback || (cb => setTimeout(cb, 600));
    idle(() => { loadEcharts().catch(() => {}); });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', preload);
  } else {
    preload();
  }

  async function openHeat(code, name, kind) {
    try {
      const isMobile = window.innerWidth <= 640;
      const pad = isMobile ? '12px 10px' : '18px 20px';
      const chartH = isMobile ? 300 : 420;
      // ---- 第一步：立即显示弹窗骨架（不等 echarts/数据） ----
      overlayEl = document.createElement('div');
      overlayEl.className = 'heat-overlay';
      overlayEl.style.cssText = `position:fixed;inset:0;background:rgba(20,18,16,0.55);z-index:9998;display:flex;align-items:${isMobile ? 'flex-end' : 'center'};justify-content:center;padding:${isMobile ? '8px' : '16px'};`;
      overlayEl.innerHTML = `<div class="heat-modal" style="background:var(--surface,#fff);border-radius:${isMobile ? '16px 16px 0 0' : '14px'};box-shadow:0 16px 48px rgba(0,0,0,.25);width:min(860px,${isMobile ? '100vw' : '96vw'});padding:${pad};position:relative;max-height:${isMobile ? '88vh' : 'none'};overflow:auto">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap">
          <span class="heat-modal-title" style="font-weight:800;font-size:${isMobile ? '14px' : '15px'};min-width:0">${name || ''} · 温度 & 走势</span>
          <button class="heat-close" style="margin-left:auto;border:1px solid var(--border);background:var(--surface-2);border-radius:8px;padding:${isMobile ? '6px 12px' : '4px 10px'};font-size:12px;cursor:pointer">${window.RV.ui.icon('xmark')} 关闭</button>
        </div>
        <div class="heat-modal-note" style="font-size:10px;color:var(--text-2);margin-bottom:6px;overflow-wrap:anywhere"></div>
        <div class="heat-loading" style="display:flex;align-items:center;justify-content:center;height:${chartH}px;color:var(--text-2);font-size:13px">${window.RV.ui.icon('spinner', 'fa-spin')} 图表加载中…</div>
      </div>`;
      document.body.appendChild(overlayEl);
      overlayEl.addEventListener('click', e => { if (e.target === overlayEl) close(); });
      overlayEl.querySelector('.heat-close').addEventListener('click', close);
      document.addEventListener('keydown', escClose);

      // ---- 第二步：后台并行加载 echarts + 数据 ----
      const [d] = await Promise.all([
        heatCache ? Promise.resolve(heatCache) : window.RV.API.heat().then(dd => { heatCache = dd; return dd; }),
        loadEcharts(),
      ]);
      const x = d.tempDates || [];
      let tempSeries = null;
      let series2 = null;
      if (kind === 'sector') {
        const s = (d.sectors || {})[code];
        if (s) {
          tempSeries = { label: name + '温度', data: s.temps };
          series2 = { name: name + '走势', data: s.nav };
        }
      } else {
        const s = (d.stocks || {})[code];
        if (s) {
          const sec = (d.sectors || {})[s.sector];
          tempSeries = sec && sec.temps
            ? { label: s.sector + '温度', data: sec.temps }
            : { label: '市场温度', data: d.globalTemps };
          series2 = { name: s.name || name || code, data: s.close };
        }
      }
      if (!tempSeries || !series2 || !series2.data) { close(); alert('暂无该标的的走势数据'); return; }
      const title = kind === 'sector'
        ? `${name} · 温度 & 走势`                                    // 板块：红利金融 · 温度 & 走势
        : `${tempSeries.label} & ${series2.name} 走势`;              // 个股：新能源电力温度 & 盛新锂能 走势
      const note = isMobile
        ? `温度=板块等权净值250日收益百分位 · ${(d.generatedAt || '').slice(0, 10)}`
        : `${d.tempNote || ''} · ${d.generatedAt || ''}`;

      // ---- 第三步：更新骨架（标题/说明 + loading → 图表容器） ----
      const modal = overlayEl.querySelector('.heat-modal');
      modal.querySelector('.heat-modal-title').textContent = title;
      const noteEl = modal.querySelector('.heat-modal-note');
      if (noteEl) noteEl.textContent = note;
      modal.querySelector('.heat-loading').outerHTML =
        `<div class="heat-chart" style="width:100%;height:${chartH}px;touch-action:none"></div>`;
      modal.insertAdjacentHTML('beforeend',
        `<div style="font-size:10px;color:var(--text-2);margin-top:6px">${window.RV.ui.icon('hand-pointer', 'fa-xs')} ${isMobile ? '拖动/双指缩放查看' : '悬停查看每日数值 · 拖拽平移 · 滚轮缩放'}</div>`);

      chart = window.echarts.init(modal.querySelector('.heat-chart'));
      const fs = isMobile ? 11 : 12;          // 轴文字字号
      const lfs = isMobile ? 11 : 13;         // 图例字号
      const xInterval = isMobile ? Math.max(0, Math.ceil(x.length / 7) - 1) : 'auto';
      chart.setOption({
        animation: false,
        tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
        legend: { data: [tempSeries.label, series2.name], top: 0, textStyle: { color: '#6b6258', fontSize: lfs } },
        grid: isMobile
          ? { left: 6, right: 8, top: 30, bottom: 34, containLabel: true }
          : { left: 64, right: 70, top: 40, bottom: 56 },
        dataZoom: [{ type: 'inside', xAxisIndex: 0 }],
        xAxis: { type: 'category', data: x, boundaryGap: false,
          axisLabel: { color: '#6b6258', fontSize: fs, interval: xInterval, formatter: v => v.slice(5) }, axisLine: { lineStyle: { color: '#d1c9bf' } } },
        yAxis: [
          { type: 'value', name: tempSeries.label, min: 0, max: 100,
            nameTextStyle: { fontSize: fs }, axisLabel: { color: '#b0864a', fontSize: fs }, splitLine: { lineStyle: { color: '#efe9df' } } },
          { type: 'value', name: kind === 'sector' ? '板块净值' : '价格', scale: true,
            nameTextStyle: { fontSize: fs }, axisLabel: { color: '#4a7db0', fontSize: fs }, splitLine: { show: false } }
        ],
        series: [
          { name: tempSeries.label, type: 'line', data: tempSeries.data, yAxisIndex: 0, showSymbol: false,
            lineStyle: { width: isMobile ? 2 : 2.2, color: '#e8a33d' }, itemStyle: { color: '#e8a33d' },
            areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [{ offset: 0, color: 'rgba(232,163,61,0.25)' }, { offset: 1, color: 'rgba(232,163,61,0.02)' }] } } },
          { name: series2.name, type: 'line', data: series2.data, yAxisIndex: 1, showSymbol: false,
            lineStyle: { width: isMobile ? 1.6 : 1.8, color: '#5b8fd6' }, itemStyle: { color: '#5b8fd6' } }
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
    // 先隐藏（视觉立即消失），清理异步做（dispose 大对象不阻塞）
    if (overlayEl) {
      overlayEl.style.display = 'none';
      const el = overlayEl;
      const ch = chart;
      overlayEl = null;
      chart = null;
      setTimeout(() => {
        if (el._resize) window.removeEventListener('resize', el._resize);
        if (ch) { try { ch.dispose(); } catch (e) {} }
        el.remove();
      }, 30);
    }
    document.body.style.overflow = '';
  }

  document.addEventListener('click', e => {
    const btn = e.target.closest('[data-heat]');
    if (!btn) return;
    e.stopPropagation();
    openHeat(btn.getAttribute('data-heat'), btn.getAttribute('data-heat-name'), btn.getAttribute('data-heat-kind') || 'stock');
  });

  /* 宽基卡片点击 → 弹走势图（复用 heat 弹窗框架） */
  document.addEventListener('click', e => {
    const card = e.target.closest('.wide-card[data-wide-code]');
    if (!card) return;
    e.stopPropagation();
    openWideKline(card.getAttribute('data-wide-code'));
  });

  async function openWideKline(code) {
    try {
      const isMobile = window.innerWidth <= 640;
      const chartH = isMobile ? 280 : 360;
      const overlay = document.createElement('div');
      overlay.className = 'heat-overlay';
      overlay.style.cssText = `position:fixed;inset:0;background:rgba(20,18,16,0.55);z-index:9998;display:flex;align-items:${isMobile ? 'flex-end' : 'center'};justify-content:center;padding:${isMobile ? '8px' : '16px'};`;
      overlay.innerHTML = `<div class="heat-modal" style="background:var(--surface,#fff);border-radius:${isMobile ? '16px 16px 0 0' : '14px'};box-shadow:0 16px 48px rgba(0,0,0,.25);width:min(760px,${isMobile ? '100vw' : '92vw'});padding:${isMobile ? '12px 10px' : '18px 20px'};position:relative">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
          <span style="font-weight:800;font-size:14px">${RV.ui.icon('chart-line')} 宽基走势</span>
          <button class="heat-close" style="margin-left:auto;border:1px solid var(--border);background:var(--surface-2);border-radius:8px;padding:4px 10px;font-size:12px;cursor:pointer">${RV.ui.icon('xmark')} 关闭</button>
        </div>
        <div class="heat-loading" style="display:flex;align-items:center;justify-content:center;height:${chartH}px;color:var(--text-2);font-size:13px">${RV.ui.icon('spinner', 'fa-spin')} 加载中…</div>
      </div>`;
      document.body.appendChild(overlay);
      const close = () => { overlay.remove(); document.body.style.overflow = ''; };
      overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
      overlay.querySelector('.heat-close').addEventListener('click', close);
      document.addEventListener('keydown', function esc(e) { if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); } });
      document.body.style.overflow = 'hidden';

      await RV.loadEcharts();
      const resp = await fetch('data/cache/kline_idx_' + code + '_320.json');
      if (!resp.ok) throw new Error('无K线数据');
      const k = await resp.json();
      const n = k.closes.length;
      const dates = k.dates.slice(Math.max(0, n - 250));
      const closes = k.closes.slice(Math.max(0, n - 250));
      const ma5 = closes.map((_, i) => i < 4 ? null : +(closes.slice(i - 4, i + 1).reduce((a, b) => a + b, 0) / 5).toFixed(2));
      const ma20 = closes.map((_, i) => i < 19 ? null : +(closes.slice(i - 19, i + 1).reduce((a, b) => a + b, 0) / 20).toFixed(2));
      const loader = overlay.querySelector('.heat-loading');
      loader.outerHTML = `<div class="heat-chart" style="width:100%;height:${chartH}px"></div>`;
      const ch = window.echarts.init(overlay.querySelector('.heat-chart'));
      ch.setOption({
        animation: false,
        tooltip: { trigger: 'axis' },
        legend: { data: ['收盘', 'MA5', 'MA20'], top: 0, textStyle: { color: '#6b6258', fontSize: 11 } },
        grid: { left: 8, right: 12, top: 30, bottom: 30, containLabel: true },
        dataZoom: [{ type: 'inside' }],
        xAxis: { type: 'category', data: dates, boundaryGap: false, axisLabel: { color: '#6b6258', fontSize: 10, interval: Math.max(0, Math.ceil(dates.length / 6) - 1) } },
        yAxis: { type: 'value', scale: true, axisLabel: { color: '#6b6258', fontSize: 10 } },
        series: [
          { name: '收盘', type: 'line', data: closes, showSymbol: false, lineStyle: { width: 1.6, color: '#5b8fd6' }, itemStyle: { color: '#5b8fd6' }, areaStyle: { color: 'rgba(91,143,214,0.08)' } },
          { name: 'MA5', type: 'line', data: ma5, showSymbol: false, lineStyle: { width: 1, color: '#e8a33d' } },
          { name: 'MA20', type: 'line', data: ma20, showSymbol: false, lineStyle: { width: 1, color: '#dc2626' } },
        ],
      });
    } catch (err) {
      alert('打开走势失败: ' + (err.message || err));
    }
  }

  // 暴露 echarts 加载器给其它模块（持仓/资金页迷你K线用）
  window.RV = window.RV || {};
  window.RV.loadEcharts = loadEcharts;
})();

/* ===== 表格点击排序：通用（所有页面 .data-table / .ret-table） =====
   点击表头排序，再点反向；自动识别数字/百分比/亿万元/日期/文本列。 */
(function () {
  if (!document || typeof window === 'undefined') return;

  function cellVal(td) {
    const txt = (td.textContent || '').trim().replace(/\s+/g, ' ');
    if (!txt || txt === '--' || txt === '—' || txt === '-') return txt;
    // 日期列
    if (/^\d{4}[-/]\d{1,2}[-/]/.test(txt)) return txt;
    // 数字列：去掉 +,% , 空格；识别 亿/万 单位
    const clean = txt.replace(/[+%,\s]/g, '');
    const n = parseFloat(clean);
    if (!isNaN(n) && /[0-9]/.test(clean)) {
      let v = n;
      if (/亿/.test(txt)) v *= 1e8;
      else if (/万/.test(txt)) v *= 1e4;
      return v;
    }
    return txt;
  }

  document.addEventListener('click', e => {
    const th = e.target.closest('th');
    if (!th || e.target.closest('a, button, .info-dot')) return;
    const table = th.closest('table');
    const tbody = table && table.querySelector('tbody');
    if (!tbody) return;
    const rows = Array.from(tbody.querySelectorAll('tr'));
    if (!rows.length) return;
    const col = Array.prototype.indexOf.call(th.parentElement.children, th);
    // 全部行该列值相同则跳过（如 K线按钮列/空列）
    const firstVal = cellVal(rows[0].children[col]);
    if (!firstVal || rows.every(r => cellVal(r.children[col]) === firstVal)) return;

    const state = table._sortState || {};
    const dir = (state.col === col && state.dir === 1) ? -1 : 1;
    table._sortState = { col, dir };
    th.parentElement.querySelectorAll('th').forEach(h => {
      h.classList.remove('sort-asc', 'sort-desc');
    });
    th.classList.add(dir === 1 ? 'sort-asc' : 'sort-desc');

    rows.sort((a, b) => {
      const va = cellVal(a.children[col]), vb = cellVal(b.children[col]);
      if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
      const sa = String(va), sb = String(vb);
      if (!sa && sb) return 1;
      if (sa && !sb) return -1;
      return sa.localeCompare(sb, 'zh') * dir;
    });
    rows.forEach(r => tbody.appendChild(r));
  });
})();
