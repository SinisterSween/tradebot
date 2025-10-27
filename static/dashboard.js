// --- Config derived from server defaults ---
// We let the server decide CSV_DEFAULT; front-end just calls /data and /meta.
const csvParam = ""; // leave empty to use server's default CSV
const barsN = 500;

let priceChart, volumeChart, equityChart, oosTrendChart;
let refreshing = false;

async function fetchJSON(url) {
  const r = await fetch(url);
  return await r.json();
}

function buildMainCharts(ts, close, volume, entries, exits) {
  const ctxP = document.getElementById('price').getContext('2d');
  const ctxV = document.getElementById('volume').getContext('2d');

  const entryPoints = entries.filter(e => e.ts).map(e => ({ x: e.ts, y: e.price ?? null }));
  const exitPoints  = exits.filter(e => e.ts).map(e => ({ x: e.ts, y: e.price ?? null }));

  priceChart = new Chart(ctxP, {
    type: 'line',
    data: {
      labels: ts,
      datasets: [
        { label: 'Close', data: close, borderWidth: 1, pointRadius: 0 },
        { label: 'Entries', data: entryPoints, type:'scatter', pointRadius: 5, showLine:false },
        { label: 'Exits',   data: exitPoints,  type:'scatter', pointRadius: 5, showLine:false },
      ]
    },
    options: { responsive:true, animation:false, parsing:false, scales: { x: { ticks: { maxTicksLimit: 10 } } } }
  });

  volumeChart = new Chart(ctxV, {
    type: 'bar',
    data: { labels: ts, datasets: [{ label:'Volume', data: volume }] },
    options: { responsive:true, animation:false, scales: { x: { ticks: { maxTicksLimit: 10 } } } }
  });
}

function buildEquityChart(equity, dd) {
  const ctx = document.getElementById('equity').getContext('2d');
  equityChart = new Chart(ctx, {
    data: {
      labels: equity.map((_,i)=>i),
      datasets: [
        { label:'Equity (cum)', type:'line', data: equity, borderWidth:1, pointRadius:0 },
        { label:'Drawdown',     type:'line', data: dd,     borderWidth:1, pointRadius:0 }
      ]
    },
    options: { responsive:true, animation:false, scales: { x: { ticks: { maxTicksLimit: 10 } } } }
  });
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;

  try {
    const q = csvParam ? `?csv=${encodeURIComponent(csvParam)}&n=${barsN}` : `?n=${barsN}`;
    const d = await fetchJSON(`/data${q}`);
    const t = await fetchJSON(`/trades`);
    if (!priceChart) buildMainCharts(d.ts, d.close, d.volume, t.entries, t.exits);
    else {
      priceChart.data.labels = d.ts;
      priceChart.data.datasets[0].data = d.close;
      priceChart.update('none');

      volumeChart.data.labels = d.ts;
      volumeChart.data.datasets[0].data = d.volume;
      volumeChart.update('none');

      priceChart.data.datasets[1].data = t.entries.filter(e=>e.ts).map(e=>({ x:e.ts, y:e.price ?? null }));
      priceChart.data.datasets[2].data = t.exits.filter(e=>e.ts).map(e=>({ x:e.ts, y:e.price ?? null }));
      priceChart.update('none');
    }

    if (!equityChart) buildEquityChart(t.equity, t.dd);
    else {
      equityChart.data.labels = t.equity.map((_,i)=>i);
      equityChart.data.datasets[0].data = t.equity;
      equityChart.data.datasets[1].data = t.dd;
      equityChart.update('none');
    }

    const m = await fetchJSON(`/meta${csvParam ? `?csv=${encodeURIComponent(csvParam)}` : ''}`);
    const metaBox = document.getElementById('metaBox');
    const lb = m.lastBar || {};
    const o  = m.oosLast || {};
    const csvInfo = document.getElementById('csvInfo');
    if (csvInfo) csvInfo.innerHTML = `CSV: <code>${m.csvPath || '—'}</code> · Last <code>${barsN}</code> bars`;

    metaBox.innerHTML = `
      <div><span>Last bar:</span> <code>${lb.ts || '—'}</code> (age: ${lb.ageSec ?? '—'}s)</div>
      <div><span>OOS ts:</span> <code>${o.ts || '—'}</code></div>
      <div><span>Trades:</span> ${o.trades ?? '—'}</div>
      <div><span>Win%:</span> ${o.win?.toFixed?.(2) ?? '—'} | <span>PF:</span> ${o.pf?.toFixed?.(2) ?? '—'}</div>
      <div><span>Expectancy:</span> ${o.exp?.toFixed?.(2) ?? '—'} | <span>NetPnL:</span> ${o.netPnL?.toFixed?.(2) ?? '—'}</div>
    `;

    // OOS trend (last 30)
    try {
      const trend = await fetchJSON('/oos_trend?n=30');
      if (!oosTrendChart) {
        const ctx = document.getElementById('oosTrend').getContext('2d');
        oosTrendChart = new Chart(ctx, {
          data: {
            labels: trend.ts,
            datasets: [
              { label:'PF',   type:'line', data: trend.pf,  borderWidth:1, pointRadius:0 },
              { label:'Win%', type:'line', data: trend.win, borderWidth:1, pointRadius:0 },
            ]
          },
          options: { responsive:true, animation:false, scales: { x: { ticks: { maxTicksLimit: 6 } } } }
        });
      } else {
        oosTrendChart.data.labels = trend.ts;
        oosTrendChart.data.datasets[0].data = trend.pf;
        oosTrendChart.data.datasets[1].data = trend.win;
        oosTrendChart.update('none');
      }
    } catch (e) {
      console.warn('oos_trend fetch failed', e);
    }

    // Adaptive refresh: 30s if "fresh", else 120s
    const ms = (m.lastBar?.ageSec ?? 9999) < 120 ? 30000 : 120000;
    const nt = document.getElementById('nextTick');
    if (nt) nt.textContent = `Next refresh in ${Math.round(ms/1000)}s`;
    setTimeout(refresh, ms);
  } finally {
    refreshing = false;
  }
}

refresh();
