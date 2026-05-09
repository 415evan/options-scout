'use strict';

const $ = id => document.getElementById(id);

function showOnly(id) {
  ['welcomeScreen','loadingScreen','errorScreen','dashboard','batchScreen'].forEach(s => {
    $(s).classList.toggle('hidden', s !== id);
  });
}
function showWelcome() { showOnly('welcomeScreen'); }
function showLoading(msg) { $('loadingMsg').textContent = msg || 'Fetching market data…'; showOnly('loadingScreen'); }
function showError(msg) { $('errorMsg').textContent = msg; showOnly('errorScreen'); }
function showDashboard() { showOnly('dashboard'); }
function showBatch() { showOnly('batchScreen'); }

function updateMarketBadge() {
  const et = new Date(new Date().toLocaleString('en-US', { timeZone: 'America/New_York' }));
  const day = et.getDay(), hour = et.getHours() + et.getMinutes() / 60;
  const b = $('marketBadge');
  if (day >= 1 && day <= 5 && hour >= 9.5 && hour < 16) { b.textContent = '● Market Open'; b.className = 'market-badge open'; }
  else { b.textContent = '● Market Closed'; b.className = 'market-badge closed'; }
}

const fmt$  = n => n != null ? '$' + Number(n).toFixed(2) : '—';
const fmtVol = n => { if (!n) return '—'; if (n>=1e9) return (n/1e9).toFixed(1)+'B'; if (n>=1e6) return (n/1e6).toFixed(1)+'M'; if (n>=1e3) return (n/1e3).toFixed(1)+'K'; return n.toString(); };
const fmtMktCap = n => { if (!n) return '—'; if (n>=1e12) return '$'+(n/1e12).toFixed(2)+'T'; if (n>=1e9) return '$'+(n/1e9).toFixed(1)+'B'; return '$'+n; };

function renderLevel(lvl, kind) {
  const row = document.createElement('div');
  row.className = `level-row ${kind}`;
  row.innerHTML = `
    <span class="level-price">${fmt$(lvl.price)}</span>
    <span class="level-label-small">${lvl.label || ''}</span>
    <div class="level-dots">
      ${[0,1,2,3,4].map(i => `<div class="level-dot${i < (lvl.strength||1) ? ` filled ${kind}` : ''}"></div>`).join('')}
    </div>`;
  return row;
}

const scoreTier = s => s >= 70 ? 'score-high' : (s >= 45 ? 'score-mid' : 'score-low');

function renderOptions(calls, currentPrice) {
  const tbody = $('optsBody');
  tbody.innerHTML = '';
  if (!calls || !calls.length) {
    $('noOptions').classList.remove('hidden');
    $('optCount').textContent = '0 found';
    return;
  }
  $('noOptions').classList.add('hidden');
  $('optCount').textContent = `${calls.length} found`;

  calls.forEach((opt, i) => {
    const tr = document.createElement('tr');
    tr.className = i < 3 ? `rank-${i+1}` : '';
    const dteClass = opt.dte <= 1 ? 'dte-urgent' : (opt.dte > 7 ? 'dte-longer' : 'dte-good');
    const tier = scoreTier(opt.score);
    const barPct = Math.min(Math.round(opt.score / 120 * 100), 100);
    const sigHtml = (opt.reasons || []).slice(0, 4).map(r => `<span class="signal-tag">${r}</span>`).join('');
    const otmPct = ((opt.strike - currentPrice) / currentPrice * 100).toFixed(1);
    const otmStr = opt.itm ? `<span class="itm-badge">ITM</span>` : `<span style="font-size:11px;color:var(--text3)">${otmPct}%</span>`;
    tr.innerHTML = `
      <td>${i+1}</td>
      <td><span class="strike-val">${fmt$(opt.strike)}</span> ${otmStr}</td>
      <td style="color:var(--text2)">${opt.expiry}</td>
      <td><span class="dte-chip ${dteClass}">${opt.dte}d</span></td>
      <td class="green">${fmt$(opt.bid)}</td>
      <td class="red">${fmt$(opt.ask)}</td>
      <td style="font-weight:600">${fmt$(opt.mid)}</td>
      <td>${fmtVol(opt.volume)}</td>
      <td>${fmtVol(opt.open_interest)}</td>
      <td style="color:${opt.iv>80?'var(--red)':opt.iv>50?'var(--yellow)':'var(--text2)'}">${opt.iv}%</td>
      <td><div class="score-cell ${tier}"><span class="score-num">${opt.score}</span><div class="score-bar-track"><div class="score-bar-fill" style="width:${barPct}%"></div></div></div></td>
      <td><div class="signals-cell">${sigHtml}</div></td>`;
    tbody.appendChild(tr);
  });
}

function renderVolume(vol) {
  $('volToday').textContent = fmtVol(vol.today);
  $('volAvg').textContent   = fmtVol(vol.avg);
  const sig = $('volSignal'); sig.textContent = vol.signal;
  sig.className = 'vol-stat-val vol-badge-' + vol.signal.toLowerCase();
  const ratio = vol.ratio || 1;
  const pct   = Math.min(Math.round(ratio / 2 * 100), 100);
  const bar = $('volBar');
  bar.style.width = pct + '%';
  bar.style.background = ratio > 1.4 ? 'var(--green)' : ratio < 0.6 ? 'var(--yellow)' : 'var(--blue)';

  const sparkEl = $('volSparkline');
  sparkEl.innerHTML = '';
  if (vol.history?.length) {
    const maxV = Math.max(...vol.history.map(d => d.volume), 1);
    vol.history.forEach(d => {
      const b = document.createElement('div');
      b.className = 'spark-bar' + (d.above_avg ? ' above' : '');
      b.style.height = Math.max(Math.round(d.volume / maxV * 38), 2) + 'px';
      b.title = `${d.date}: ${fmtVol(d.volume)}`;
      sparkEl.appendChild(b);
    });
  }
}

function renderNews(items) {
  const grid = $('newsGrid'); const none = $('noNews');
  grid.innerHTML = '';
  if (!items || !items.length) { none.classList.remove('hidden'); $('newsCount').textContent = '0 articles'; return; }
  none.classList.add('hidden');
  $('newsCount').textContent = `${items.length} articles`;
  items.forEach(item => {
    const card = document.createElement('div');
    card.className = 'news-item';
    const titleEl = item.url ? `<a href="${item.url}" target="_blank" rel="noopener noreferrer">${item.title}</a>` : item.title;
    card.innerHTML = `
      <div class="news-top">
        <span class="news-source">${item.source || 'Unknown'}</span>
        <span class="news-sentiment sent-${item.sentiment}">${item.sentiment}</span>
      </div>
      <div class="news-title">${titleEl}</div>
      ${item.summary ? `<div class="news-summary">${item.summary}</div>` : ''}`;
    grid.appendChild(card);
  });
}

async function analyze(ticker) {
  ticker = ticker.trim().toUpperCase();
  if (!ticker) return;
  $('analyzeBtn').disabled = true;
  showLoading('Fetching market data for ' + ticker + '…');
  try {
    const res = await fetch(`/api/analyze/${ticker}`);
    const data = await res.json();
    if (data.error) { showError(data.error); return; }

    $('st-ticker').textContent = data.ticker;
    $('st-price').textContent  = fmt$(data.current_price);
    $('st-company').textContent = data.company_name || data.ticker;
    $('st-52h').textContent = fmt$(data.stats?.['52w_high']);
    $('st-52l').textContent = fmt$(data.stats?.['52w_low']);
    $('st-avgvol').textContent = fmtVol(data.stats?.avg_vol);
    $('st-mktcap').textContent = fmtMktCap(data.stats?.mkt_cap);

    $('currentPriceLine').textContent = fmt$(data.current_price);
    const rl = $('resistList'); rl.innerHTML = '';
    (data.resistance_levels||[]).slice(0,5).forEach(l => rl.appendChild(renderLevel(l,'resist')));
    if (!data.resistance_levels?.length) rl.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:4px">None detected</div>';
    const sl = $('supportList'); sl.innerHTML = '';
    (data.support_levels||[]).slice(0,5).forEach(l => sl.appendChild(renderLevel(l,'support')));
    if (!data.support_levels?.length) sl.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:4px">None detected</div>';

    if (data.volume) renderVolume(data.volume);
    renderOptions(data.top_calls, data.current_price);
    showDashboard();

    fetch(`/api/news/${ticker}`).then(r=>r.json()).then(nd=>renderNews(nd.news||[])).catch(()=>renderNews([]));
  } catch (err) {
    showError('Network error: ' + err.message);
  } finally {
    $('analyzeBtn').disabled = false;
  }
}

// ── Watchlist import ──────────────────────────────────────────────────────────

async function handleWatchlistFile(file) {
  if (!file) return;
  showLoading('Parsing watchlist…');
  try {
    const text = await file.text();
    const parseRes = await fetch('/api/parse-watchlist', {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain' },
      body: text
    });
    const parsed = await parseRes.json();
    if (parsed.error) { showError(parsed.error); return; }
    if (!parsed.tickers?.length) { showError('No tickers found in that file.'); return; }

    showBatch();
    $('batchSubtitle').textContent = `Scanning ${parsed.count} tickers from your watchlist…`;
    $('batchProgress').style.width = '0%';
    $('batchBody').innerHTML = '';
    $('batchCount').textContent = `0 / ${parsed.count}`;

    // Run batch
    const r = await fetch('/api/analyze-batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tickers: parsed.tickers })
    });
    const batch = await r.json();
    $('batchProgress').style.width = '100%';
    $('batchSubtitle').textContent = `${batch.count} tickers scanned, ranked by best call score`;
    $('batchCount').textContent = `${batch.count} ranked`;

    const tbody = $('batchBody');
    batch.results.forEach((row, i) => {
      const tr = document.createElement('tr');
      if (row.error || !row.top_score) {
        tr.innerHTML = `
          <td>${i+1}</td>
          <td><strong>${row.ticker}</strong></td>
          <td colspan="11" class="batch-error">${row.error || 'No tradeable options'}</td>`;
      } else {
        const tier = scoreTier(row.top_score);
        const barPct = Math.min(Math.round(row.top_score / 120 * 100), 100);
        const sigHtml = (row.reasons || []).map(s => `<span class="signal-tag">${s}</span>`).join('');
        tr.innerHTML = `
          <td>${i+1}</td>
          <td><strong style="color:var(--blue)">${row.ticker}</strong></td>
          <td style="color:var(--text2)">${row.company_name || ''}</td>
          <td>${fmt$(row.current_price)}</td>
          <td><strong>${fmt$(row.top_strike)}</strong></td>
          <td>${row.top_expiry}</td>
          <td><span class="dte-chip ${row.top_dte<=1?'dte-urgent':row.top_dte>7?'dte-longer':'dte-good'}">${row.top_dte}d</span></td>
          <td>${fmt$(row.top_mid)}</td>
          <td>${fmtVol(row.top_volume)}</td>
          <td><span class="vol-badge-${(row.volume_signal||'normal').toLowerCase()}">${row.volume_signal}</span></td>
          <td><div class="score-cell ${tier}"><span class="score-num">${row.top_score}</span><div class="score-bar-track"><div class="score-bar-fill" style="width:${barPct}%"></div></div></div></td>
          <td><div class="signals-cell">${sigHtml}</div></td>
          <td><button class="btn-secondary" style="padding:3px 10px;font-size:11px" data-drill="${row.ticker}">Details →</button></td>`;
      }
      tbody.appendChild(tr);
    });

    tbody.querySelectorAll('button[data-drill]').forEach(btn => {
      btn.addEventListener('click', () => analyze(btn.dataset.drill));
    });
  } catch (err) {
    showError('Watchlist error: ' + err.message);
  }
}

// ── Auto-update badge (Electron only) ─────────────────────────────────────────
if (window.electronAPI) {
  window.electronAPI.getVersion().then(v => { $('appVersion').textContent = 'v' + v; });
  window.electronAPI.onUpdateAvailable(info => {
    const b = $('updateBadge');
    b.textContent = `Update v${info.version} available — downloading…`;
    b.classList.remove('hidden');
  });
  window.electronAPI.onUpdateDownloaded(info => {
    const b = $('updateBadge');
    b.textContent = `✓ v${info.version} ready — click to restart`;
    b.classList.remove('hidden');
    b.onclick = () => window.electronAPI.installUpdate();
  });
}

// ── Wire up ──────────────────────────────────────────────────────────────────
$('analyzeBtn').addEventListener('click', () => analyze($('tickerInput').value));
$('tickerInput').addEventListener('keydown', e => { if (e.key === 'Enter') analyze($('tickerInput').value); });

document.querySelectorAll('.qt-btn').forEach(btn =>
  btn.addEventListener('click', () => { $('tickerInput').value = btn.dataset.t; analyze(btn.dataset.t); })
);

$('watchlistBtn').addEventListener('click', () => $('watchlistFile').click());
$('watchlistFile').addEventListener('change', e => handleWatchlistFile(e.target.files[0]));

const dz = $('dropzone');
dz.addEventListener('click', () => $('watchlistFile').click());
['dragenter','dragover'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add('drag-over'); }));
['dragleave','drop'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove('drag-over'); }));
dz.addEventListener('drop', e => { e.preventDefault(); handleWatchlistFile(e.dataTransfer.files[0]); });

updateMarketBadge();
setInterval(updateMarketBadge, 60000);
