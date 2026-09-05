/* =========================================================================
 * stockchart.js — 现代专业行情图（对标券商 App：长桥 / 同花顺 / 富途）
 * 依赖：echarts 全局 + 数据源（src/data_provider.py 生成的 JSON / 页面注入）
 *
 * 布局：主图(K线+MA) / 成交量 / MACD 三格联动；十字光标；红涨绿跌；
 *       支持 日K(默认)/周/月 + 当日分时(价+均线+涨跌区)。
 * 用：<script src="assets/js/stockchart.js"></script>
 *     SC.stockChart(domId, {code, name, kline:{dates,opens,closes,highs,lows,volumes},
 *                            intraday:{trends,pre_close}})   // 前端已有数据时
 *     SC.mountFromAPI(domId, code)   // 页面只给 code，内部 fetch /api?（无后端则不推荐）
 * 配色近似券商浅色专业版；图表内文字用页面 CSS 变量兼容明暗。
 * ========================================================================= */
(function (global) {
  'use strict';
  if (typeof echarts === 'undefined') { console.warn('stockchart: echarts 未加载'); return; }
  const UP = '#dc2626', DOWN = '#16a34a';          // A股 红涨绿跌
  const MA_COLORS = { ma5: '#f59e0b', ma10: '#3b82f6', ma20: '#a855f7' };

  function sma(arr, n) {
    const out = new Array(arr.length).fill(null);
    let s = 0;
    for (let i = 0; i < arr.length; i++) { s += arr[i]; if (i >= n) s -= arr[i - n]; if (i >= n - 1) out[i] = +(s / n).toFixed(3); }
    return out;
  }
  function emaArr(arr, n) {
    const out = []; const k = 2 / (n + 1); let e = arr[0];
    for (let i = 0; i < arr.length; i++) { e = i === 0 ? arr[0] : arr[i] * k + e * (1 - k); out.push(+e.toFixed(3)); }
    return out;
  }
  function macdCalc(closes) {
    const d12 = emaArr(closes, 12), d26 = emaArr(closes, 26);
    const dif = closes.map((_, i) => +(d12[i] - d26[i]).toFixed(3));
    const dea = emaArr(dif, 9);
    const bar = dif.map((v, i) => +((v - dea[i]) * 2).toFixed(3));
    return { dif, dea, bar };
  }
  function rsiCalc(closes, n) {
    const out = new Array(closes.length).fill(null); let g = 0, l = 0;
    for (let i = 1; i < closes.length; i++) {
      const ch = closes[i] - closes[i - 1];
      g += Math.max(ch, 0); l += Math.max(-ch, 0);
      if (i > n) { const back = closes[i - n] - closes[i - n - 1]; g -= Math.max(back, 0); l -= Math.max(-back, 0); }
      if (i >= n) out[i] = +(100 - 100 / (1 + (l ? g / l : 99))).toFixed(1);
    }
    return out;
  }

  /* ---------- 周/月 聚合 ---------- */
  function aggregate(dates, opens, closes, highs, lows, vols, type) {
    const step = type === 'week' ? 'week' : 'month';
    const map = {}; const order = [];
    dates.forEach((d, i) => {
      let key;
      if (step === 'week') { const dt = new Date(d); const day = (dt.getDay() + 6) % 7; dt.setDate(dt.getDate() - day); key = dt.toISOString().slice(0, 10); }
      else key = d.slice(0, 7);
      if (!(key in map)) { map[key] = { o: opens[i], h: highs[i], l: lows[i], c: closes[i], v: 0 }; order.push(key); }
      else { const m = map[key]; m.h = Math.max(m.h, highs[i]); m.l = Math.min(m.l, lows[i]); m.c = closes[i]; }
      map[key].v += vols[i];
    });
    return {
      dates: order, opens: order.map(k => map[k].o), closes: order.map(k => map[k].c),
      highs: order.map(k => map[k].h), lows: order.map(k => map[k].l), volumes: order.map(k => map[k].v),
    };
  }

  /* ---------- 主渲染 ---------- */
  function render(domId, opt) {
    const el = document.getElementById(domId);
    if (!el) return null;
    const chart = echarts.init(el);
    const type = opt.type || 'day';
    let D = { dates: opt.kline.dates.slice(), opens: opt.kline.opens.slice(), closes: opt.kline.closes.slice(),
      highs: opt.kline.highs.slice(), lows: opt.kline.lows.slice(), volumes: opt.kline.volumes.slice() };
    if (type !== 'day') D = aggregate(D.dates, D.opens, D.closes, D.highs, D.lows, D.volumes, type);
    const closes = D.closes;
    // 指标
    const ma5 = sma(closes, 5), ma10 = sma(closes, 10), ma20 = sma(closes, 20);
    const macd = macdCalc(closes);
    // 分时模式：单独构建
    if (opt.intraday) return renderIntraday(chart, domId, opt.intraday);
    const n = D.dates.length;
    const volColor = D.closes.map((c, i) => (i && c >= D.closes[i - 1]) ? UP : DOWN);
    const showLast = Math.min(n, opt.bars || 130);
    const startIdx = n - showLast;
    function zr(v) { return v == null ? '-' : v; }
    const axisDate = (idx) => { const i = startIdx + idx; const d = D.dates[i]; return type === 'month' ? d : d; };
    const option = {
      animation: false,
      axisPointer: { link: [{ xAxisIndex: 'all' }], label: { backgroundColor: '#777' } },
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, backgroundColor: 'rgba(17,24,39,.92)', borderWidth: 0,
        textStyle: { color: '#fff', fontSize: 11 }, confine: true,
        formatter(ps) {
          const p = ps[0]; const i = startIdx + p.dataIndex;
          const c = D.closes[i], o = D.opens[i], h = D.highs[i], l = D.lows[i], v = D.volumes[i];
          const chg = i ? ((c / D.closes[i - 1] - 1) * 100) : 0;
          const col = c >= o ? UP : DOWN;
          const row = (k, val) => `<div style="display:flex;justify-content:space-between;gap:16px"><span style="color:#a3a6ad">${k}</span><span>${val}</span></div>`;
          return `<div style="min-width:180px">${D.dates[i]}${type !== 'day' ? '（' + type + '）' : ''}
            ${row('开盘', o)}${row('最高', `<span style="color:${UP}">${h}</span>`)}${row('最低', `<span style="color:${DOWN}">${l}</span>`)}
            ${row('收盘', `<b style="color:${col}">${c}</b>`)}${row('涨跌', `<span style="color:${col}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span>`)}
            ${row('成交量', v >= 1e8 ? (v / 1e8).toFixed(2) + '亿' : (v / 1e4).toFixed(0) + '万')}
            ${row('MA5', zr(ma5[i]))} ${row('MA10', zr(ma10[i]))} ${row('MA20', zr(ma20[i]))}
            ${row('MACD', (macd.bar[i] || 0).toFixed(3))} ${row('DIF', (macd.dif[i] || 0).toFixed(3))} ${row('DEA', (macd.dea[i] || 0).toFixed(3))}</div>`;
        } },
      grid: [
        { left: 52, right: 16, top: 12, height: '52%' },
        { left: 52, right: 16, top: '68%', height: '12%' },
        { left: 52, right: 16, top: '84%', height: '12%' },
      ],
      xAxis: [
        { type: 'category', data: D.dates.slice(startIdx), gridIndex: 0, boundaryGap: true,
          axisLine: { lineStyle: { color: '#ddd' } }, axisLabel: { show: false } },
        { type: 'category', data: D.dates.slice(startIdx), gridIndex: 1, axisLabel: { show: false }, axisLine: { lineStyle: { color: '#eee' } } },
        { type: 'category', data: D.dates.slice(startIdx), gridIndex: 2, axisLabel: { fontSize: 9, color: '#9a9a9a' },
          axisLine: { lineStyle: { color: '#eee' } } },
      ],
      yAxis: [
        { scale: true, gridIndex: 0, position: 'right', splitNumber: 5,
          axisLabel: { fontSize: 9, color: '#9a9a9a' }, splitLine: { lineStyle: { color: '#f0f0f0' } } },
        { gridIndex: 1, scale: true, splitNumber: 2, axisLabel: { fontSize: 9, color: '#bbb' }, splitLine: { show: false } },
        { gridIndex: 2, scale: true, splitNumber: 2, axisLabel: { fontSize: 9, color: '#bbb' }, splitLine: { show: false } },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1, 2], start: (1 - showLast / n) * 100, end: 100 },
        { type: 'slider', xAxisIndex: [0, 1, 2], bottom: 2, height: 14, borderColor: '#eee', fillerColor: 'rgba(199,141,90,.12)' },
      ],
      series: [
        // K线
        { name: 'K线', type: 'candlestick', data: D.dates.slice(startIdx).map((_, k) => {
            const i = startIdx + k; return [D.opens[i], D.closes[i], D.lows[i], D.highs[i]]; }),
          itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN } },
        { name: 'MA5', type: 'line', data: ma5.slice(startIdx), smooth: true, symbol: 'none',
          lineStyle: { width: 1, color: MA_COLORS.ma5 }, itemStyle: { color: MA_COLORS.ma5 } },
        { name: 'MA10', type: 'line', data: ma10.slice(startIdx), smooth: true, symbol: 'none',
          lineStyle: { width: 1, color: MA_COLORS.ma10 }, itemStyle: { color: MA_COLORS.ma10 } },
        { name: 'MA20', type: 'line', data: ma20.slice(startIdx), smooth: true, symbol: 'none',
          lineStyle: { width: 1, color: MA_COLORS.ma20 }, itemStyle: { color: MA_COLORS.ma20 } },
        // 成交量
        { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: D.volumes.slice(startIdx).map((v, k) => ({
            value: v, itemStyle: { color: volColor[startIdx + k], opacity: .85 } })), barWidth: '60%' },
        // MACD
        { name: 'MACD', type: 'bar', xAxisIndex: 2, yAxisIndex: 2, data: macd.bar.slice(startIdx).map((v, k) => ({
            value: v, itemStyle: { color: v >= 0 ? UP : DOWN, opacity: .8 } })), barWidth: '55%' },
        { name: 'DIF', type: 'line', xAxisIndex: 2, yAxisIndex: 2, data: macd.dif.slice(startIdx), symbol: 'none',
          lineStyle: { width: 1, color: '#f59e0b' } },
        { name: 'DEA', type: 'line', xAxisIndex: 2, yAxisIndex: 2, data: macd.dea.slice(startIdx), symbol: 'none',
          lineStyle: { width: 1, color: '#3b82f6' } },
      ],
    };
    chart.setOption(option);
    const resize = () => chart.resize();
    window.addEventListener('resize', resize);
    chart.__dispose = () => { window.removeEventListener('resize', resize); chart.dispose(); };
    return chart;
  }

  /* ---------- 分时模式 ---------- */
  function renderIntraday(chart, domId, id) {
    const trends = id.trends || []; const pc = id.pre_close || (trends[0] && trends[0].price) || 0;
    const times = [], prices = [], avgs = [];
    trends.forEach(t => { times.push(t.time.slice(11)); prices.push(t.price); avgs.push(t.avg || null); });
    const maxP = Math.max(...prices.map(p => Math.abs(p - pc))) || pc * 0.01;
    const upper = pc * 1.001 + maxP, lower = pc * 0.999 - maxP;
    const firstIdx = prices.findIndex(p => p != null && p > 0);
    const base = pc;
    const opt = {
      animation: false,
      axisPointer: { label: { backgroundColor: '#777' } },
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, backgroundColor: 'rgba(17,24,39,.92)', borderWidth: 0,
        textStyle: { color: '#fff', fontSize: 11 }, confine: true,
        formatter(ps) {
          const i = ps[0].dataIndex; const p = prices[i]; if (p == null) return '';
          const chg = ((p / base) - 1) * 100; const col = p >= base ? UP : DOWN;
          return `${trends[i].time}<br>价 <b style="color:${col}">${p}</b>（${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%）<br>均价 ${avgs[i] || '--'}`;
        } },
      grid: [{ left: 52, right: 16, top: 14, height: '78%' }],
      xAxis: { type: 'category', data: times, boundaryGap: false, axisLine: { lineStyle: { color: '#ddd' } }, axisLabel: { fontSize: 9, color: '#9a9a9a', interval: Math.floor(times.length / 5) } },
      yAxis: { scale: false, min: lower, max: upper, position: 'right', splitNumber: 5,
        axisLabel: { fontSize: 9, color: '#9a9a9a', formatter: v => v.toFixed(2) }, splitLine: { lineStyle: { color: '#f0f0f0' } } },
      series: [
        { type: 'line', data: prices, showSymbol: false, smooth: true,
          lineStyle: { width: 1.4, color: '#3b82f6' }, areaStyle: { color: 'rgba(59,130,246,.12)' },
          markLine: { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: '#f59e0b', type: 'dashed', width: 1 }, data: [{ yAxis: base }] } },
        { type: 'line', data: avgs, showSymbol: false, smooth: true, lineStyle: { width: 1, color: '#f59e0b' } },
      ],
    };
    chart.setOption(opt);
    return chart;
  }

  function fromMinuteIntraday(domId, klineObj, prevClose) {
    const el = document.getElementById(domId);
    if (!el) return null;
    const ch = echarts.init(el);
    const dates = klineObj.dates || [];
    const lastDay = dates.length ? dates[dates.length - 1].slice(0, 10) : '';
    const idxs = [];
    dates.forEach((d, i) => { if (d.slice(0, 10) === lastDay) idxs.push(i); });
    if (!idxs.length) { ch.dispose(); return null; }
    const times = idxs.map(i => dates[i].slice(11, 16));
    const prices = idxs.map(i => klineObj.closes[i]);
    const vols = idxs.map(i => klineObj.volumes[i] || 0);
    const cumVol = []; let cv = 0; vols.forEach(v => { cv += v; cumVol.push(cv); });
    const amts = (klineObj.amounts && klineObj.amounts.length) ? idxs.map(i => klineObj.amounts[i] || 0) : null;
    const avgs = []; let cAmt = 0;
    prices.forEach((p, k) => { cAmt += amts ? amts[k] : (p * (vols[k] || 0)); avgs.push(cumVol[k] > 0 ? +(cAmt / cumVol[k]).toFixed(3) : null); });
    const base = prevClose || prices[0];
    ch.setOption({
      animation: false,
      axisPointer: { label: { backgroundColor: '#777' } },
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, backgroundColor: 'rgba(17,24,39,.92)', borderWidth: 0,
        textStyle: { color: '#fff', fontSize: 11 }, confine: true,
        formatter(ps) { const i = ps[0].dataIndex; const p = prices[i]; if (p == null) return '';
          const chg = (p / base - 1) * 100; const col = p >= base ? UP : DOWN;
          return lastDay + ' ' + times[i] + '<br>价 <b style="color:' + col + '">' + p + '</b>（' + (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%）<br>均价 ' + (avgs[i] || '--'); } },
      grid: [{ left: 52, right: 16, top: 12, height: '78%' }],
      xAxis: { type: 'category', data: times, boundaryGap: false, axisLine: { lineStyle: { color: '#ddd' } },
        axisLabel: { fontSize: 9, color: '#9a9a9a', interval: Math.max(1, Math.floor(times.length / 6)) } },
      yAxis: { scale: true, position: 'right', splitNumber: 5, axisLabel: { fontSize: 9, color: '#9a9a9a', formatter: v => v.toFixed(2) },
        splitLine: { lineStyle: { color: '#f0f0f0' } } },
      series: [
        { type: 'line', data: prices, showSymbol: false, smooth: true, lineStyle: { width: 1.4, color: '#2563eb' },
          areaStyle: { color: 'rgba(37,99,235,.1)' },
          markLine: { silent: true, symbol: 'none', label: { show: true, position: 'insideEndTop', formatter: '昨收 ' + base, fontSize: 9, color: '#f59e0b' },
            lineStyle: { color: '#f59e0b', type: 'dashed', width: 1 }, data: [{ yAxis: base }] } },
        { type: 'line', data: avgs, showSymbol: false, smooth: true, lineStyle: { width: 1, color: '#f59e0b' } },
      ],
    });
    return ch;
  }
  global.SC = {
    stockChart: render,
    renderIntraday,
    fromIntradayMinutes: fromMinuteIntraday,
    // 便捷：从 kline JSON（{dates,opens,closes,highs,lows,volumes}）画
    fromKline(domId, code, name, klineObj, extra) {
      return render(domId, Object.assign({ code, name, type: extra && extra.type || 'day', kline: klineObj }, extra || {}));
    },
    dispose(domId) { const c = echarts.getInstanceByDom(document.getElementById(domId)); if (c) c.dispose(); },
  };
})(window);
