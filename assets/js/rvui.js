/* =========================================================================
 * rvui.js — 全站共享 UI 组件库（页面开发规范落地层）
 * 依赖：common.js 的 RV.fmt / RV.ui（本文件在 common.js 之后引入）
 * 用法：<script src="assets/js/common.js"></script>
 *       <script src="assets/js/rvui.js"></script>
 * 所有渲染函数返回 HTML 字符串（或纯函数），页面拼接后一次性 innerHTML。
 * ========================================================================= */
(function (global) {
  'use strict';
  const R = global.RV || {};
  const fmt = R.fmt || {};
  const ui = R.ui || {};
  const F = fmt.cls ? fmt : { cls: v => v > 0 ? 'up' : (v < 0 ? 'down' : 'neutral') };

  const RVX = {
    /* ---------- 1. 区块骨架 ---------- */
    /* 标准区块：title(图标+标题) + chip(右侧说明) + bodyHTML */
    section(title, icon, bodyHTML, chips, extraCls) {
      const chip = (chips || []).map(c =>
        `<span class="sec-chip" style="font-size:11px;color:var(--text-2);background:var(--surface-2);padding:2px 8px;border-radius:6px;font-weight:600">${c}</span>`
      ).join('');
      return `<div class="sec ${extraCls || ''}">
        <div class="sec-title"><span class="ti">${ui.icon(icon)}</span>${title}${chip ? '<span style="margin-left:auto;display:flex;gap:6px">' + chip + '</span>' : ''}</div>
        ${bodyHTML}</div>`;
    },

    /* ---------- 2. KPI 统计卡 ---------- */
    /* cards = [{label,val,cls?,sub?,icon?}] */
    kpis(cards) {
      return `<div class="kpi-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:16px">` +
        cards.map(c => `
          <div class="kpi-card" style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:13px 15px">
            ${c.icon ? `<div style="font-size:18px;color:var(--accent);margin-bottom:2px">${ui.icon(c.icon)}</div>` : ''}
            <div style="font-size:11px;color:var(--text-2)">${c.label}</div>
            <div class="${c.cls || ''}" style="font-size:20px;font-weight:800;margin-top:3px">${c.val}</div>
            ${c.sub ? `<div style="font-size:10.5px;color:var(--text-2);margin-top:2px">${c.sub}</div>` : ''}
          </div>`).join('') + `</div>`;
    },

    /* ---------- 3. 涨跌着色 ---------- */
    p(v) { return v == null || isNaN(v) ? '--' : ((v > 0 ? '+' : '') + Number(v).toFixed(2) + '%'); },
    cls(v) { return F.cls(v); },
    colored(v, digits) {
      if (v == null || isNaN(v)) return '<span class="neutral">--</span>';
      const s = (v > 0 ? '+' : '') + Number(v).toFixed(digits == null ? 2 : digits) + '%';
      return `<span class="${F.cls(v)}">${s}</span>`;
    },

    /* ---------- 4. 表格 ---------- */
    /* cols=[{k,label,render(v,row)?,cls?}], rows=[] */
    table(cols, rows, opts) {
      opts = opts || {};
      const thead = `<thead><tr>${cols.map(c => `<th>${c.label}</th>`).join('')}</tr></thead>`;
      if (!rows || !rows.length) {
        return `<div class="table-wrap"><table class="data-table">${thead}<tbody><tr><td colspan="${cols.length}" style="text-align:center;color:var(--text-2);padding:16px">${opts.emptyText || '暂无数据'}</td></tr></tbody></table></div>`;
      }
      const tbody = `<tbody>${rows.map(row => {
        const trs = cols.map(c => {
          const v = c.render ? c.render(row[c.k], row) : (row[c.k] == null ? '--' : row[c.k]);
          const cs = c.cls ? (typeof c.cls === 'function' ? c.cls(row[c.k], row) : c.cls) : '';
          return `<td class="${cs}">${v}</td>`;
        }).join('');
        return `<tr>${trs}</tr>`;
      }).join('')}</tbody>`;
      return `<div class="table-wrap"><table class="data-table">${thead}${tbody}</table></div>`;
    },

    /* 折叠表：rows 按 monthKey 分组，默认只展开最新一组（历史回测/流水类用） */
    groupedTable(cols, rows, opts) {
      opts = opts || {};
      const groups = {};
      (rows || []).forEach(r => {
        const m = opts.groupOf ? opts.groupOf(r) : String(r[opts.groupKey] || '').slice(0, 7);
        (groups[m] = groups[m] || []).push(r);
      });
      const months = Object.keys(groups).sort();
      if (!months.length) return RVX.table(cols, [], opts);
      const openLast = opts.openAll !== true;
      return months.map(m => {
        const g = groups[m];
        const open = !openLast || m === months[months.length - 1];
        return `<div class="month-group" style="border:1px solid var(--border);border-radius:10px;margin-bottom:8px;overflow:hidden">
          <div class="mg-head" data-m="${m}" style="display:flex;align-items:center;gap:8px;padding:9px 12px;cursor:pointer;background:var(--surface-2);font-size:13px;user-select:none">
            <span class="mg-caret" style="color:var(--accent)">${open ? ui.icon('chevron-down','fa-xs') : ui.icon('chevron-right','fa-xs')}</span>
            <b>${m}</b><span style="font-size:11px;color:var(--text-2)">${g.length} 条</span>
            <span style="margin-left:auto;font-size:11px;color:var(--text-2)">${open ? '收起' : '展开'}</span>
          </div>
          <div class="mg-body" style="display:${open?'block':'none'};overflow-x:auto">${RVX.table(cols, g.slice().reverse(), opts)}</div>
        </div>`;
      }).join('');
    },

    /* ---------- 5. 个股明细卡（六层标准结构，见 docs/UI-STANDARD.md） ---------- */
    stockCard(item, opts) {
      opts = opts || {};
      const it = item || {};
      const f = it.factors || it;            // 因子层兼容（factor 直接或嵌套）
      const sigKey = f.signal_key || it.signal_key;
      const sig = sigKey ? ui.signalBadge(sigKey, f.signal || it.signal) : '';
      const chg = it.change_pct;
      const rk = (it.rating) ? `<span class="rating-badge">${it.rating}</span>` : '';
      // 1 头部
      const head = `<div class="stock-head" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <b style="font-size:15px">${it.name || '--'}</b>
        <span class="date-tag">${it.code || ''}</span>
        ${it.sector ? `<span class="sec-chip">${it.sector}</span>` : ''}
        ${rk}${sig}</div>`;
      // 2 价位
      let px = '';
      if (it.price || it.close) {
        const p = it.price || it.close;
        px = `<div style="display:flex;align-items:baseline;gap:10px;margin:6px 0">
          <span style="font-size:22px;font-weight:800">${fmt.num(p)}</span>
          <span class="${RVX.cls(chg)}" style="font-weight:700">${RVX.p(chg)}</span>
          ${it.ma20 ? `<span class="date-tag">MA20 ${fmt.num(it.ma20)}</span>` : ''}
          ${it.stop_loss ? `<span class="date-tag" style="color:var(--down)">止损 ${fmt.num(it.stop_loss)}</span>` : ''}
          ${it.take_profit ? `<span class="date-tag" style="color:var(--up)">目标 ${fmt.num(it.take_profit)}</span>` : ''}
        </div>`;
      }
      // 3 指标行
      const chips = [];
      if (f.trend_status) chips.push(`趋势 ${f.trend_status}`);
      if (f.macd_status) chips.push(`MACD ${f.macd_status}`);
      if (f.rsi_status && f.rsi12 != null) chips.push(`RSI ${fmt.num(f.rsi12,1)} ${f.rsi_status}`);
      if (f.volume_status) chips.push(`量 ${f.volume_status}`);
      if (it.change_60d != null) chips.push(`60日 ${RVX.p(it.change_60d)}`);
      const mline = chips.length ? `<div style="font-size:11.5px;color:var(--text-2);display:flex;flex-wrap:wrap;gap:4px 10px;margin:4px 0 2px">${chips.map(c=>`<span>${c}</span>`).join('')}</div>` : '';
      // 4 评分条
      let score = '';
      if (it.score != null) {
        const sc = Math.max(0, Math.min(100, it.score));
        score = `<div style="display:flex;align-items:center;gap:8px;margin-top:4px">
          <span style="font-weight:800;color:${sc>=68?'var(--up)':sc>=40?'var(--watch)':'var(--down)'}">${sc}分</span>
          <div style="flex:1;height:5px;border-radius:3px;background:var(--surface-2);overflow:hidden">
            <div style="width:${sc}%;height:100%;background:${sc>=68?'var(--up)':sc>=40?'var(--watch)':'var(--down)'}"></div></div></div>`;
      }
      // 5 理由
      const reason = it.reasons || it.reason
        ? `<div style="font-size:11.5px;color:var(--text-2);line-height:1.5;margin-top:4px">${(it.reasons||it.reason).slice(0,opts.reasonLen||90)}</div>` : '';
      // 6 资金
      let fund = '';
      const ff = it.fund_flow || {};
      if (ff.main_net != null || ff.main_net_5d != null) {
        const mv = x => x == null ? '--' : (Math.abs(x) >= 1e8 ? (x / 1e8).toFixed(2) + '亿' : (x / 1e4).toFixed(0) + '万');
        const mn = ff.main_net, m5 = ff.main_net_5d;
        const seg = (x) => x == null ? '' : `<span class="${F.cls(x)}">${x > 0 ? '+' : ''}${mv(x)}</span>`;
        fund = `<div style="font-size:11px;color:var(--text-2);margin-top:4px;display:flex;gap:10px">
          ${mn != null ? `<span>主力 ${seg(mn)}</span>` : ''}${m5 != null ? `<span>5日 ${seg(m5)}</span>` : ''}</div>`;
      }
      return `<div class="stock-card" ${opts.codeAttr || ''} style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:13px 15px;cursor:${opts.click?'pointer':'default'};box-shadow:var(--shadow)">
        ${head}${px}${mline}${score}${reason}${fund}</div>`;
    },

    /* ---------- 6. 图上浮层详情卡（点击图表弹当日卡，默认全展开，带 × 关闭） ---------- */
    popupCard(hostSel, contentHTML, headerLine, color) {
      const old = document.getElementById('rvx-pop');
      if (old) old.remove();
      const host = document.querySelector(hostSel);
      if (!host) return null;
      const dv = document.createElement('div');
      dv.id = 'rvx-pop';
      dv.style.cssText = `margin-top:12px;border:1px solid var(--border);border-left:4px solid ${color||'var(--accent)'};border-radius:10px;background:var(--surface);padding:12px 14px;box-shadow:var(--shadow-hover);position:relative;z-index:20`;
      dv.innerHTML = `<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px">${headerLine||''}
        <button id="rvx-pop-close" style="margin-left:auto;border:none;background:none;color:var(--text-2);cursor:pointer">${ui.icon('xmark')}</button></div>${contentHTML}`;
      host.parentElement.appendChild(dv);
      dv.querySelector('#rvx-pop-close').addEventListener('click', () => dv.remove());
      return dv;
    },

    /* ---------- 7. 空态 / 提示 ---------- */
    empty(text) { return `<div class="empty" style="color:var(--text-2);text-align:center;padding:18px">${text || '暂无数据'}</div>`; },
    hint(html) { return `<div style="font-size:11px;color:var(--text-2);margin-top:8px">${html}</div>`; },
  };

  global.RVX = RVX;

  // 折叠表事件委托（动态 HTML 也生效）
  document.addEventListener('click', function (e) {
    const h = e.target.closest('.mg-head');
    if (!h) return;
    const b = h.nextElementSibling;
    if (!b) return;
    const on = b.style.display !== 'none';
    b.style.display = on ? 'none' : 'block';
    const c = h.querySelector('.mg-caret');
    if (c) c.innerHTML = on ? RV.ui.icon('chevron-right', 'fa-xs') : RV.ui.icon('chevron-down', 'fa-xs');
  });
})(window);
