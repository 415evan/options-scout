'use strict';

const $ = id => document.getElementById(id);

// ── Persistent store (survives app restarts via backend file) ─────────────────
let STORE = {};  // loaded async at startup

async function initStore() {
  try {
    const res = await fetch('/api/store');
    STORE = await res.json();
  } catch { STORE = {}; }
}

async function persistStore() {
  try {
    await fetch('/api/store', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(STORE),
    });
  } catch (e) { console.error('Store save failed:', e); }
}

function loadSettings() { return STORE.settings || {}; }
function saveSettings(s) { STORE.settings = s; persistStore(); }

let SETTINGS = {};

function updatePortfolioBtnText() {
  const t = $('portfolioBtnText');
  if (!t) return;
  if (SETTINGS.portfolioValue && SETTINGS.buyingPower) {
    t.textContent = '$' + (SETTINGS.buyingPower >= 1000 ? (SETTINGS.buyingPower/1000).toFixed(1)+'K' : SETTINGS.buyingPower) + ' BP';
  } else {
    t.textContent = 'Portfolio';
  }
}

function openPortfolioModal() {
  $('setPortfolio').value   = SETTINGS.portfolioValue || '';
  $('setBuyingPower').value = SETTINGS.buyingPower || '';
  $('setRiskPct').value     = SETTINGS.riskPct || 2;
  $('riskPctLabel').textContent = (SETTINGS.riskPct || 2) + '%';
  $('portfolioModal').classList.remove('hidden');
}
function closePortfolioModal() { $('portfolioModal').classList.add('hidden'); }
function savePortfolio() {
  const pv = parseFloat($('setPortfolio').value) || 0;
  const bp = parseFloat($('setBuyingPower').value) || 0;
  const rp = parseFloat($('setRiskPct').value)   || 2;
  SETTINGS = { portfolioValue: pv, buyingPower: bp, riskPct: rp, lastConfirmedDate: todayStr() };
  saveSettings(SETTINGS);
  closePortfolioModal();
  updatePortfolioBtnText();
  // Re-render last results if dashboard is showing
  if (window._lastAnalysis) renderOptions(window._lastAnalysis.top_calls, window._lastAnalysis.current_price);
}

function isPortfolioSet() {
  return SETTINGS.portfolioValue > 0 && SETTINGS.buyingPower > 0;
}

// Position sizing math for a long call
function computePosition(opt) {
  if (!isPortfolioSet() || !opt.ask || opt.ask <= 0) return null;
  const costPerContract = opt.ask * 100; // each contract = 100 shares
  const maxRiskDollars  = SETTINGS.portfolioValue * (SETTINGS.riskPct / 100);
  const byRisk = Math.floor(maxRiskDollars / costPerContract);
  const byBP   = Math.floor(SETTINGS.buyingPower / costPerContract);
  const contracts = Math.max(0, Math.min(byRisk, byBP));
  const totalCost = contracts * costPerContract;
  const breakeven = opt.strike + opt.ask;
  const target2x  = opt.strike + opt.ask * 2;  // stock price where premium ≈ doubles
  return { contracts, totalCost, maxLoss: totalCost, breakeven, target2x, costPerContract };
}

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

// ── Best Picks ────────────────────────────────────────────────────────────────
let _picksTimer = null;
let _countdownTimer = null;
let _nextRefreshAt = 0;

const convictionClass = c => c === 'Strong' ? 'conv-strong' : c === 'High' ? 'conv-high' : c === 'Moderate' ? 'conv-moderate' : 'conv-speculative';

function renderSectorHeat(sectorPerf) {
  const bar = $('sectorHeatBar');
  if (!sectorPerf) { bar.classList.add('hidden'); return; }
  bar.classList.remove('hidden');
  const entries = Object.entries(sectorPerf).sort((a, b) => b[1] - a[1]);
  bar.innerHTML = entries.map(([sec, ret]) => {
    const cls = ret >= 2 ? 'heat-hot' : ret >= 0.5 ? 'heat-warm' : ret >= -0.5 ? 'heat-flat' : 'heat-cold';
    const sign = ret >= 0 ? '+' : '';
    return `<div class="heat-chip ${cls}" title="${sec}">${sec.replace('Consumer ','').replace(' Disc','CD').replace(' Market','')}<span>${sign}${ret.toFixed(1)}%</span></div>`;
  }).join('');
}

function renderBestPicks(data) {
  const tbody = $('picksBody');
  tbody.innerHTML = '';

  renderSectorHeat(data.sector_perf);

  const updated = data.updated_at ? new Date(data.updated_at * 1000) : new Date();
  const timeStr = updated.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  $('picksStatus').textContent = `${data.picks.length} picks from ${data.scanned} tickers — last scan ${timeStr}${data.stale ? ' (refreshing…)' : ''}`;

  data.picks.forEach((pick, i) => {
    const tr = document.createElement('tr');
    tr.className = i < 3 ? `rank-${i+1}` : '';

    const dteClass  = pick.dte <= 1 ? 'dte-urgent' : pick.dte > 21 ? 'dte-swing' : pick.dte > 7 ? 'dte-longer' : 'dte-good';
    const confPct   = Math.min(pick.confidence, 100);
    const confColor = confPct >= 80 ? 'var(--green)' : confPct >= 65 ? 'var(--blue)' : confPct >= 50 ? 'var(--yellow)' : 'var(--text3)';
    const otmStr    = pick.itm ? `<span class="itm-badge">ITM</span>` : '';
    const contractsFor500 = pick.ask > 0 ? Math.floor(500 / (pick.ask * 100)) : 0;
    const cheapTag  = contractsFor500 >= 5 ? `<span class="cheap-tag">${contractsFor500}× for $500</span>` : '';
    const sectorRet = pick.sector_return || 0;
    const sectorCls = sectorRet >= 2 ? 'heat-hot' : sectorRet >= 0.5 ? 'heat-warm' : sectorRet >= -0.5 ? 'heat-flat' : 'heat-cold';
    const sectorSign = sectorRet >= 0 ? '+' : '';

    tr.innerHTML = `
      <td>${i + 1}</td>
      <td><strong style="color:var(--blue);font-size:13px">${pick.ticker}</strong>${otmStr}${cheapTag}</td>
      <td><span class="heat-chip ${sectorCls}" style="font-size:10px">${pick.sector || ''}<span style="margin-left:4px">${sectorSign}${sectorRet.toFixed(1)}%</span></span></td>
      <td>${fmt$(pick.current_price)}</td>
      <td><strong>${fmt$(pick.strike)}</strong></td>
      <td style="color:var(--text2)">${pick.expiry}</td>
      <td><span class="dte-chip ${dteClass}">${pick.dte}d</span></td>
      <td style="font-weight:600">${fmt$(pick.mid)}</td>
      <td>
        <div class="conf-cell">
          <span class="conf-num" style="color:${confColor}">${confPct}%</span>
          <div class="conf-bar-track"><div class="conf-bar-fill" style="width:${confPct}%;background:${confColor}"></div></div>
        </div>
      </td>
      <td><span class="conviction-badge ${convictionClass(pick.conviction)}">${pick.conviction}</span></td>
      <td style="max-width:200px;font-size:11px;color:var(--text2);white-space:normal;line-height:1.3">${pick.conviction_reason || ''}</td>
      <td><button class="btn-secondary picks-drill-btn" data-t="${pick.ticker}">Details →</button></td>`;
    tbody.appendChild(tr);
  });

  tbody.querySelectorAll('.picks-drill-btn').forEach(btn =>
    btn.addEventListener('click', () => analyze(btn.dataset.t))
  );

  $('picksLoading').classList.add('hidden');
  $('picksTable').classList.remove('hidden');
}

function startPicksCountdown(seconds) {
  if (_countdownTimer) clearInterval(_countdownTimer);
  _nextRefreshAt = Date.now() + seconds * 1000;
  const el = $('picksCountdown');
  el.classList.remove('hidden');

  function tick() {
    const secs = Math.max(0, Math.round((_nextRefreshAt - Date.now()) / 1000));
    el.textContent = `Refreshing in ${secs}s`;
    if (secs === 0) el.textContent = 'Refreshing…';
  }
  tick();
  _countdownTimer = setInterval(tick, 1000);
}

async function loadBestPicks(manual = false) {
  if (manual) {
    $('picksLoading').classList.remove('hidden');
    $('picksTable').classList.add('hidden');
    $('picksStatus').textContent = 'Scanning markets…';
    if (_picksTimer) clearInterval(_picksTimer);
    if (_countdownTimer) clearInterval(_countdownTimer);
    $('picksCountdown').classList.add('hidden');
  }

  try {
    const res  = await fetch('/api/best-picks');
    const data = await res.json();
    if (data.picks) {
      renderBestPicks(data);
      // schedule next auto-refresh in 60s
      if (_picksTimer) clearInterval(_picksTimer);
      _picksTimer = setTimeout(() => loadBestPicks(), 60000);
      startPicksCountdown(60);
    }
  } catch (err) {
    $('picksStatus').textContent = 'Error fetching picks — ' + err.message;
  }
}

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
  // Show/hide the "set portfolio" banner
  $('positionBanner').classList.toggle('hidden', isPortfolioSet());

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
    if (isPortfolioSet()) tr.classList.add('sizing-set');

    const dteClass = opt.dte <= 1 ? 'dte-urgent' : (opt.dte > 7 ? 'dte-longer' : 'dte-good');
    const tier = scoreTier(opt.score);
    const barPct = Math.min(Math.round(opt.score / 120 * 100), 100);
    const sigHtml = (opt.reasons || []).slice(0, 4).map(r => `<span class="signal-tag">${r}</span>`).join('');
    const otmPct = ((opt.strike - currentPrice) / currentPrice * 100).toFixed(1);
    const otmStr = opt.itm ? `<span class="itm-badge">ITM</span>` : `<span style="font-size:11px;color:var(--text3)">${otmPct}%</span>`;

    // Position sizing
    const pos = computePosition(opt);
    let buyCell, costCell, lossCell, beCell, targetCell;
    if (!pos) {
      const empty = '<span class="sizing-empty">—</span>';
      buyCell = costCell = lossCell = beCell = targetCell = empty;
    } else if (pos.contracts === 0) {
      buyCell    = `<span class="contracts-pill contracts-zero">0</span>`;
      costCell   = `<span class="sizing-empty">too costly</span>`;
      lossCell   = `<span class="sizing-empty">${fmt$(pos.costPerContract)}/ea</span>`;
      beCell     = `<span class="breakeven-cell">${fmt$(pos.breakeven)}</span>`;
      targetCell = `<span class="target-cell">${fmt$(pos.target2x)}</span>`;
    } else {
      buyCell    = `<span class="contracts-pill">${pos.contracts}×</span>`;
      costCell   = `<span class="sizing-cell">${fmt$(pos.totalCost)}</span>`;
      lossCell   = `<span class="max-loss-cell">−${fmt$(pos.maxLoss)}</span>`;
      beCell     = `<span class="breakeven-cell">${fmt$(pos.breakeven)}</span>`;
      targetCell = `<span class="target-cell">${fmt$(pos.target2x)}</span>`;
    }

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
      <td>${buyCell}</td>
      <td>${costCell}</td>
      <td>${lossCell}</td>
      <td>${beCell}</td>
      <td>${targetCell}</td>
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
    window._lastAnalysis = data;
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

// ── Morning check ─────────────────────────────────────────────────────────────
function todayStr() { return new Date().toISOString().slice(0, 10); }

function checkMorning() {
  if (!SETTINGS.portfolioValue) return; // no portfolio set yet
  if (SETTINGS.lastConfirmedDate === todayStr()) return; // already confirmed today
  $('morningPortfolioVal').textContent = '$' + Number(SETTINGS.portfolioValue).toLocaleString();
  $('morningBuyingPower').value = SETTINGS.buyingPower || '';
  $('morningModal').classList.remove('hidden');
}

function skipMorningCheck() {
  SETTINGS.lastConfirmedDate = todayStr();
  saveSettings(SETTINGS);
  $('morningModal').classList.add('hidden');
}

function saveMorningCheck() {
  const bp = parseFloat($('morningBuyingPower').value) || SETTINGS.buyingPower || 0;
  SETTINGS.buyingPower = bp;
  SETTINGS.lastConfirmedDate = todayStr();
  saveSettings(SETTINGS);
  $('morningModal').classList.add('hidden');
  updatePortfolioBtnText();
  if (window._lastAnalysis) renderOptions(window._lastAnalysis.top_calls, window._lastAnalysis.current_price);
}

window.skipMorningCheck = skipMorningCheck;
window.saveMorningCheck = saveMorningCheck;

// ── Trade log ─────────────────────────────────────────────────────────────────
let _exitingTradeId = null;

function loadTrades() { return STORE.trades || []; }
function saveTrades(t) { STORE.trades = t; persistStore(); }

function openTradeLog() {
  renderTradeLog();
  $('tradeLogModal').classList.remove('hidden');
}
function closeTradeLog() { $('tradeLogModal').classList.add('hidden'); }

function openLogEntry(prefill) {
  $('le-ticker').value   = prefill?.ticker   || '';
  $('le-strike').value   = prefill?.strike   || '';
  $('le-expiry').value   = prefill?.expiry   || '';
  $('le-price').value    = prefill?.ask      || '';
  $('le-contracts').value = 1;
  $('le-date').value     = todayStr();
  $('le-notes').value    = '';
  $('logEntryModal').classList.remove('hidden');
}
function closeLogEntry() { $('logEntryModal').classList.add('hidden'); }

function saveLogEntry() {
  const ticker    = $('le-ticker').value.trim().toUpperCase();
  const strike    = parseFloat($('le-strike').value);
  const expiry    = $('le-expiry').value;
  const contracts = parseInt($('le-contracts').value) || 1;
  const price     = parseFloat($('le-price').value);
  const date      = $('le-date').value || todayStr();
  const notes     = $('le-notes').value.trim();

  if (!ticker || !strike || !price) { alert('Ticker, strike, and price are required.'); return; }

  const trades = loadTrades();
  trades.push({ id: Date.now(), ticker, strike, expiry, contracts, entryPrice: price, entryDate: date, notes, status: 'open' });
  saveTrades(trades);
  closeLogEntry();
  renderTradeLog();
  updateTradesBtnBadge();
}

function openLogExit(id) {
  const trade = loadTrades().find(t => t.id === id);
  if (!trade) return;
  _exitingTradeId = id;
  $('logExitTitle').textContent = `Close ${trade.ticker} $${trade.strike} × ${trade.contracts}`;
  $('logExitDesc').textContent  = `Entry: $${trade.entryPrice}/share · Cost: $${(trade.entryPrice * 100 * trade.contracts).toFixed(2)}`;
  $('le-exit-price').value = '';
  $('le-exit-date').value  = todayStr();
  $('logExitPnl').classList.add('hidden');
  $('logExitModal').classList.remove('hidden');

  $('le-exit-price').addEventListener('input', previewExitPnl);
}
function closeLogExit() { $('logExitModal').classList.add('hidden'); _exitingTradeId = null; }

function previewExitPnl() {
  const trades = loadTrades();
  const trade  = trades.find(t => t.id === _exitingTradeId);
  if (!trade) return;
  const exitP  = parseFloat($('le-exit-price').value);
  if (!exitP) { $('logExitPnl').classList.add('hidden'); return; }
  const pnl = (exitP - trade.entryPrice) * 100 * trade.contracts;
  const el  = $('logExitPnl');
  el.textContent = `P&L: ${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`;
  el.className   = 'exit-pnl-preview' + (pnl >= 0 ? ' pnl-win' : ' pnl-loss');
  el.classList.remove('hidden');
}

function saveLogExit() {
  const exitPrice = parseFloat($('le-exit-price').value);
  const exitDate  = $('le-exit-date').value || todayStr();
  if (!exitPrice) { alert('Enter an exit price.'); return; }

  const trades = loadTrades();
  const idx    = trades.findIndex(t => t.id === _exitingTradeId);
  if (idx === -1) return;
  const trade  = trades[idx];
  const pnl    = (exitPrice - trade.entryPrice) * 100 * trade.contracts;
  trades[idx]  = { ...trade, exitPrice, exitDate, pnl, status: 'closed' };
  saveTrades(trades);
  closeLogExit();
  renderTradeLog();
  updateTradesBtnBadge();
}

function renderTradeLog() {
  const trades  = loadTrades();
  const open    = trades.filter(t => t.status === 'open');
  const closed  = trades.filter(t => t.status === 'closed');

  $('openCount').textContent   = open.length;
  $('closedCount').textContent = closed.length;

  // Open trades
  const ob = $('openTradesBody'); ob.innerHTML = '';
  $('noOpenTrades').classList.toggle('hidden', open.length > 0);
  open.forEach(t => {
    const cost = (t.entryPrice * 100 * t.contracts).toFixed(2);
    const tr   = document.createElement('tr');
    tr.innerHTML = `
      <td><strong style="color:var(--blue)">${t.ticker}</strong></td>
      <td>${fmt$(t.strike)}</td>
      <td>${t.expiry || '—'}</td>
      <td>${t.contracts}</td>
      <td>${fmt$(t.entryPrice)}/sh</td>
      <td style="font-weight:600">$${cost}</td>
      <td style="color:var(--text3)">${t.entryDate}</td>
      <td style="color:var(--text2);font-size:12px;max-width:180px;overflow:hidden;text-overflow:ellipsis">${t.notes || '—'}</td>
      <td><button class="btn-secondary" style="padding:3px 10px;font-size:11px" data-exit="${t.id}">Exit →</button></td>`;
    ob.appendChild(tr);
  });
  ob.querySelectorAll('[data-exit]').forEach(b => b.addEventListener('click', () => openLogExit(Number(b.dataset.exit))));

  // Closed trades
  const cb = $('closedTradesBody'); cb.innerHTML = '';
  $('noClosedTrades').classList.toggle('hidden', closed.length > 0);
  let totalPnl = 0, wins = 0;
  const pnls = closed.map(t => t.pnl || 0);
  closed.forEach(t => {
    totalPnl += t.pnl || 0;
    if ((t.pnl || 0) > 0) wins++;
    const win = (t.pnl || 0) >= 0;
    const tr  = document.createElement('tr');
    tr.innerHTML = `
      <td><strong style="color:var(--blue)">${t.ticker}</strong></td>
      <td>${fmt$(t.strike)}</td>
      <td>${t.expiry || '—'}</td>
      <td>${t.contracts}</td>
      <td>${fmt$(t.entryPrice)}/sh</td>
      <td>${fmt$(t.exitPrice)}/sh</td>
      <td style="font-weight:700;color:${win ? 'var(--green)' : 'var(--red)'}">${win ? '+' : ''}$${(t.pnl||0).toFixed(2)}</td>
      <td><span class="conviction-badge ${win ? 'conv-strong' : 'conv-speculative'}">${win ? 'WIN' : 'LOSS'}</span></td>
      <td style="color:var(--text3)">${t.exitDate || '—'}</td>`;
    cb.appendChild(tr);
  });

  // Summary
  if (closed.length > 0) {
    $('tradeSummary').classList.remove('hidden');
    const winRate = Math.round(wins / closed.length * 100);
    $('ts-pnl').textContent   = (totalPnl >= 0 ? '+' : '') + '$' + totalPnl.toFixed(2);
    $('ts-pnl').className     = 'tsstat-val ' + (totalPnl >= 0 ? 'green' : 'red');
    $('ts-wr').textContent    = winRate + '% (' + wins + '/' + closed.length + ')';
    $('ts-count').textContent = closed.length + ' closed, ' + open.length + ' open';
    const best  = Math.max(...pnls); const worst = Math.min(...pnls);
    $('ts-best').textContent  = '+$' + best.toFixed(2);
    $('ts-worst').textContent = '$' + worst.toFixed(2);
  } else {
    $('tradeSummary').classList.add('hidden');
  }
}

function updateTradesBtnBadge() {
  const open = loadTrades().filter(t => t.status === 'open').length;
  $('tradesBtnText').textContent = open > 0 ? `Trades (${open})` : 'Trades';
}

window.openTradeLog  = openTradeLog;
window.closeTradeLog = closeTradeLog;
window.openLogEntry  = openLogEntry;
window.closeLogEntry = closeLogEntry;
window.saveLogEntry  = saveLogEntry;
window.openLogExit   = openLogExit;
window.closeLogExit  = closeLogExit;
window.saveLogExit   = saveLogExit;

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

// Portfolio button + slider
$('portfolioBtn').addEventListener('click', openPortfolioModal);
$('portfolioModal').addEventListener('click', e => { if (e.target.id === 'portfolioModal') closePortfolioModal(); });
$('setRiskPct').addEventListener('input', e => { $('riskPctLabel').textContent = e.target.value + '%'; });
window.openPortfolioModal = openPortfolioModal;
window.closePortfolioModal = closePortfolioModal;
window.savePortfolio = savePortfolio;

$('tradesBtn').addEventListener('click', openTradeLog);

updateMarketBadge();
setInterval(updateMarketBadge, 60000);

// Load persistent store first, then initialize everything that depends on saved data
async function init() {
  await initStore();
  SETTINGS = loadSettings();
  updatePortfolioBtnText();
  updateTradesBtnBadge();
  loadBestPicks();
  // Delay morning check slightly so picks start loading first
  setTimeout(checkMorning, 800);
}
init();
