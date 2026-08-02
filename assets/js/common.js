/* ===== 每日复盘共享前端层：数据获取 + 渲染工具 ===== */
(function (global) {
  'use strict';

  /* ---------- API 层：统一数据获取（对齐 axios 单例思路） ---------- */
  const API = {
    base: 'data/',
    async fetch(name) {
      const res = await fetch(this.base + name + '?t=' + Date.now());
      if (!res.ok) throw new Error('加载失败: ' + name + ' (' + res.status + ')');
      return res.json();
    },
    review() { return this.fetch('review_data.json'); },
    recommend() { return this.fetch('recommend_data.json'); },
    backtest() { return this.fetch('backtest_data.json'); },
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
    /* 页头（含导航），active 为高亮页 key */
    header(title, subtitle, active, genTime) {
      const navs = [
        ['review', '复盘', 'review.html'],
        ['recommend', '推荐', 'recommend.html'],
        ['backtest', '回测', 'backtest.html'],
      ];
      const navHtml = navs.map(([k, label, href]) =>
        `<a class="nav-link ${k === active ? 'active' : ''}" href="${href}">${label}</a>`
      ).join('');
      return `
      <header class="top">
        <h1>${title}</h1>
        <div class="gen-time">${subtitle || ''}${genTime ? ' · 生成于 ' + genTime : ''}</div>
        <nav class="nav">${navHtml}</nav>
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
    metric(val, label, cls = '') {
      return `<div class="metric-card"><div class="m-val ${cls}">${val}</div><div class="m-label">${label}</div></div>`;
    },
    summaryCard(icon, val, label, cls = '') {
      return `<div class="summary-card"><div class="icon">${icon}</div>
        <div class="summary-text"><div class="val ${cls}">${val}</div><div class="label">${label}</div></div></div>`;
    },
    outcomeLabel(o) {
      const map = { win: '✓ 兑现', loss: '✗ 未兑现', neutral: '— 中性' };
      return `<span class="outcome-${o}">${map[o] || o}</span>`;
    },
    loading(msg = '数据加载中…') { return `<div class="loading">${msg}</div>`; },
    empty(msg = '暂无数据') { return `<div class="empty">${msg}</div>`; },
    error(e) { return `<div class="empty">⚠️ ${e.message || e}<br><small>请先运行 <code>python run_review.py</code> 生成数据</small></div>`; },
  };

  global.RV = { API, fmt, ui };
})(window);
