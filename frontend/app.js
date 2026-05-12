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
  if (window._lastAnalysis) renderOptions(window._lastAnalysis.top_calls, window._lastAnalysis.current_price, window._lastAnalysis.resistance_levels);
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
let _allPicks = [];
let _activeSector = null;
let _lastSectorPerf = null;
let _minPremium = 0;

// ── Level Watch / Price Alerts ────────────────────────────────────────────────
// _watchedTickers: { TICKER: { supports, resistances, lastPrice, intervalId } }
const _watchedTickers = {};

function toggleWatch(ticker, supportLevels, resistanceLevels, currentPrice) {
  if (_watchedTickers[ticker]) {
    stopWatch(ticker);
  } else {
    startWatch(ticker, supportLevels, resistanceLevels, currentPrice);
  }
  // Update all watch buttons for this ticker
  document.querySelectorAll(`.watch-btn[data-t="${ticker}"]`).forEach(btn => {
    const watching = !!_watchedTickers[ticker];
    btn.classList.toggle('watch-active', watching);
    btn.title = watching ? 'Watching — click to cancel' : 'Watch for level break';
    btn.textContent = watching ? '🔔' : '🔕';
  });
}

function startWatch(ticker, supportLevels, resistanceLevels, lastPrice, silent = false) {
  if (_watchedTickers[ticker]) return;
  const intervalId = setInterval(async () => {
    try {
      const res = await fetch(`/api/price/${ticker}`);
      if (!res.ok) return;
      const data = await res.json();
      if (!data.price) return;
      const price = data.price;
      const prev  = _watchedTickers[ticker]?.lastPrice;
      if (prev != null) {
        // Check resistance breaks (price crossed above)
        for (const lvl of (resistanceLevels || [])) {
          if (prev < lvl && price >= lvl) {
            showLevelAlert(ticker, lvl, 'above', 'resistance');
          }
        }
        // Check support breaks (price crossed below)
        for (const lvl of (supportLevels || [])) {
          if (prev > lvl && price <= lvl) {
            showLevelAlert(ticker, lvl, 'below', 'support');
          }
        }
      }
      if (_watchedTickers[ticker]) _watchedTickers[ticker].lastPrice = price;
    } catch (e) { console.warn('Watch poll error:', e); }
  }, 30000); // poll every 30s

  _watchedTickers[ticker] = { supportLevels, resistanceLevels, lastPrice, intervalId };
  if (!silent) showToast(`👀 Watching ${ticker} for level breaks`, 'info', 3000);
}

function stopWatch(ticker) {
  if (!_watchedTickers[ticker]) return;
  clearInterval(_watchedTickers[ticker].intervalId);
  delete _watchedTickers[ticker];
  showToast(`Stopped watching ${ticker}`, 'info', 2000);
}

function showLevelAlert(ticker, level, direction, levelType) {
  const dirWord = direction === 'above' ? 'broke above' : 'dropped below';
  const emoji   = direction === 'above' ? '🚨' : '⚠️';
  const msg     = `${emoji} ${ticker} ${dirWord} $${level.toFixed(2)} ${levelType} — enter the call`;
  showToast(msg, direction === 'above' ? 'alert-up' : 'alert-down', 12000);
}

function showToast(message, type = 'info', duration = 5000) {
  const container = $('alertContainer');
  const toast = document.createElement('div');
  toast.className = `alert-toast alert-toast-${type}`;
  toast.textContent = message;
  toast.style.pointerEvents = 'auto';
  toast.onclick = () => toast.remove();
  container.appendChild(toast);
  // Animate in
  requestAnimationFrame(() => toast.classList.add('alert-toast-show'));
  setTimeout(() => {
    toast.classList.remove('alert-toast-show');
    setTimeout(() => toast.remove(), 400);
  }, duration);
}
// ── Individual ticker entry signal ────────────────────────────────────────────
let _entrySignalData = null;

function renderEntrySignal(ticker, resistanceLevels, supportLevels, currentPrice) {
  const el = $('entrySignal');
  if (!el) return;

  // Normalize: levels may be dicts {price,...} or plain numbers
  const toNum = l => (typeof l === 'object' && l !== null) ? l.price : l;
  const resistPrices = (resistanceLevels || []).map(toNum).filter(p => p > currentPrice).sort((a,b) => a-b);
  const supportPrices = (supportLevels   || []).map(toNum).filter(p => p < currentPrice).sort((a,b) => b-a);
  const entryLevel = resistPrices[0] || null;

  _entrySignalData = { ticker, resistanceLevels: resistPrices, supportLevels: supportPrices, currentPrice };

  if (!entryLevel) { el.classList.add('hidden'); return; }
  el.classList.remove('hidden');
  $('entrySignalTitle').textContent = `Enter ${ticker} calls above ${fmt$(entryLevel)}`;
  $('entrySignalSub').textContent   = `Nearest resistance at ${fmt$(entryLevel)} — wait for a clean break & hold above this level before buying`;
  _updateEntryWatchBtn();
}

function _updateEntryWatchBtn() {
  const btn = $('entryWatchBtn');
  if (!btn || !_entrySignalData) return;
  const watching = !!_watchedTickers[_entrySignalData.ticker];
  btn.textContent = watching ? '🔔 Watching' : '🔕 Watch';
  btn.classList.toggle('watch-active', watching);
  btn.title = watching ? 'Click to stop watching' : 'Click to get an alert when this level breaks';
}

function toggleTickerWatch() {
  if (!_entrySignalData) return;
  const { ticker, supportLevels, resistanceLevels, currentPrice } = _entrySignalData;
  toggleWatch(ticker, supportLevels, resistanceLevels, currentPrice);
  _updateEntryWatchBtn();
}
window.toggleTickerWatch = toggleTickerWatch;

let _maxPremium = 0;

// ── Expiry / DTE filter ───────────────────────────────────────────────────────
// null = all, or { min, max } DTE range
let _dteFilter = null;

const DTE_PRESETS = [
  { label: 'All',       min: 0,  max: 999 },
  { label: 'Weekly',    min: 0,  max: 7   },
  { label: '2-Week',    min: 8,  max: 14  },
  { label: 'Monthly',   min: 15, max: 30  },
  { label: 'Swing',     min: 31, max: 60  },
];

function renderDteFilter() {
  const bar = $('dteFilterBar');
  if (!bar) return;
  bar.innerHTML = DTE_PRESETS.map(p => {
    const active = !_dteFilter && p.label === 'All'
      ? ' dte-btn-active'
      : (_dteFilter && _dteFilter.label === p.label ? ' dte-btn-active' : '');
    return `<button class="dte-preset-btn${active}" data-label="${p.label}" data-min="${p.min}" data-max="${p.max}">${p.label}</button>`;
  }).join('');
  bar.querySelectorAll('.dte-preset-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const label = btn.dataset.label;
      _dteFilter = label === 'All' ? null : { label, min: +btn.dataset.min, max: +btn.dataset.max };
      renderDteFilter();
      renderPicksTable(applyFilters(_allPicks));
    });
  });
}

function applyFilters(picks) {
  let out = picks;
  if (_activeSector) out = out.filter(p => p.sector === _activeSector);
  if (_minPremium > 0) out = out.filter(p => (p.ask || 0) >= _minPremium);
  if (_maxPremium > 0) out = out.filter(p => (p.ask || 0) <= _maxPremium);
  if (_dteFilter) out = out.filter(p => p.dte >= _dteFilter.min && p.dte <= _dteFilter.max);
  return out;
}

function updateBudgetHint() {
  const hint = $('budgetHint');
  const clearBtn = $('clearPremiumBtn');
  const active = _minPremium > 0 || _maxPremium > 0;
  clearBtn.classList.toggle('hidden', !active);
  if (!active) { hint.textContent = ''; return; }
  const minC = _minPremium > 0 ? '$' + (_minPremium * 100).toFixed(0) : '$0';
  const maxC = _maxPremium > 0 ? '$' + (_maxPremium * 100).toFixed(0) : '∞';
  hint.textContent = `= ${minC}–${maxC} per contract`;
}

function clearPremiumFilter() {
  _minPremium = 0; _maxPremium = 0;
  $('minPremium').value = '';
  $('maxPremium').value = '';
  updateBudgetHint();
  renderPicksTable(applyFilters(_allPicks));
}
window.clearPremiumFilter = clearPremiumFilter;

const convictionClass = c => c === 'Strong' ? 'conv-strong' : c === 'High' ? 'conv-high' : c === 'Moderate' ? 'conv-moderate' : 'conv-speculative';

const SECTOR_SHORT = {
  'Consumer Discretionary': 'Cons.D', 'Consumer Staples': 'Cons.S',
  'Communication': 'Comm', 'Technology': 'Tech', 'Healthcare': 'Health',
  'Financials': 'Fin', 'Industrials': 'Indust', 'Semiconductors': 'Semi',
  'Broad Market': 'Broad', 'Materials': 'Mater', 'Utilities': 'Util',
  'Real Estate': 'RE', 'Energy': 'Energy'
};
const shortSector = s => SECTOR_SHORT[s] || s;

function renderSectorHeat(sectorPerf) {
  if (sectorPerf) _lastSectorPerf = sectorPerf;
  const sp = _lastSectorPerf;
  const bar = $('sectorHeatBar');
  if (!sp) { bar.classList.add('hidden'); return; }
  bar.classList.remove('hidden');
  const entries = Object.entries(sp).sort((a, b) => b[1] - a[1]);
  bar.innerHTML = entries.map(([sec, ret]) => {
    const cls = ret >= 2 ? 'heat-hot' : ret >= 0.5 ? 'heat-warm' : ret >= -0.5 ? 'heat-flat' : 'heat-cold';
    const sign = ret >= 0 ? '+' : '';
    const active = _activeSector === sec ? ' heat-active' : '';
    return `<div class="heat-chip${active} ${cls}" data-sector="${sec}" title="Click to filter ${sec}">${shortSector(sec)}<span>${sign}${ret.toFixed(1)}%</span></div>`;
  }).join('');

  bar.querySelectorAll('.heat-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const sec = chip.dataset.sector;
      _activeSector = _activeSector === sec ? null : sec;
      renderPicksTable(applyFilters(_allPicks));
      renderSectorHeat(null);
      renderFilterBar();
    });
  });
}

function renderFilterBar() {
  const bar = $('sectorFilterBar');
  if (!bar) return;
  if (_activeSector) {
    bar.innerHTML = `<span>Showing: <strong>${_activeSector}</strong></span><button onclick="clearSectorFilter()">× Clear</button>`;
    bar.classList.remove('hidden');
  } else {
    bar.classList.add('hidden');
  }
}

function clearSectorFilter() {
  _activeSector = null;
  renderPicksTable(applyFilters(_allPicks));
  renderSectorHeat(null);
  renderFilterBar();
}
window.clearSectorFilter = clearSectorFilter;

function renderPicksTable(picks) {
  const tbody = $('picksBody');
  tbody.innerHTML = '';
  if (!picks.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td colspan="14" style="text-align:center;padding:32px;color:var(--text3);font-size:13px">
      No picks found in ${_activeSector || 'this sector'} this scan — try ↻ Refresh or select another sector.
    </td>`;
    tbody.appendChild(tr);
    $('picksLoading').classList.add('hidden');
    $('picksTable').classList.remove('hidden');
    return;
  }
  picks.forEach((pick, i) => {
    const tr = document.createElement('tr');
    tr.className = i < 3 ? `rank-${i+1}` : '';
    const dteClass  = pick.dte <= 1 ? 'dte-urgent' : pick.dte > 21 ? 'dte-swing' : pick.dte > 7 ? 'dte-longer' : 'dte-good';
    const confPct   = Math.min(pick.confidence, 100);
    const confColor = confPct >= 80 ? 'var(--green)' : confPct >= 65 ? 'var(--blue)' : confPct >= 50 ? 'var(--yellow)' : 'var(--text3)';
    const otmStr    = pick.itm ? `<span class="itm-badge">ITM</span>` : '';
    const contractsFor500 = pick.ask > 0 ? Math.floor(500 / (pick.ask * 100)) : 0;
    const cheapTag  = contractsFor500 >= 5 ? `<span class="cheap-tag">${contractsFor500}×$500</span>` : '';
    const sectorRet = pick.sector_return || 0;
    const sectorCls = sectorRet >= 2 ? 'heat-hot' : sectorRet >= 0.5 ? 'heat-warm' : sectorRet >= -0.5 ? 'heat-flat' : 'heat-cold';
    const sectorSign = sectorRet >= 0 ? '+' : '';
    const why = pick.conviction_reason || '';

    // Entry signal for picks table: fraction of gap, scales with OTM% so each pick differs
    const pickStep = pick.current_price >= 500 ? 10 : pick.current_price >= 100 ? 5 : 2.5;
    const pickRoundUnit = pickStep / 2;
    const pickRes  = (pick.resistance_levels || []).filter(l => l > pick.current_price && l < pick.strike);
    const pickOtmFrac = (pick.strike - pick.current_price) / pick.current_price;
    const pickFraction = Math.min(0.15 + pickOtmFrac * 1.5, 0.40);
    let   pickEntry = pick.current_price + (pick.strike - pick.current_price) * pickFraction;
    pickEntry = Math.round(pickEntry / pickRoundUnit) * pickRoundUnit;
    const pickSnap = pickRes.find(r => Math.abs(r - pickEntry) / pickEntry < 0.005);
    if (pickSnap) pickEntry = pickSnap;
    if (pickEntry <= pick.current_price) pickEntry = Math.ceil((pick.current_price + pickRoundUnit) / pickRoundUnit) * pickRoundUnit;
    if (pickEntry >= pick.strike)        pickEntry = parseFloat((pick.strike - pickRoundUnit).toFixed(2));
    pickEntry = parseFloat(pickEntry.toFixed(2));
    const pickUpside = (((pick.strike - pickEntry) / pickEntry) * 100).toFixed(0);

    tr.innerHTML = `
      <td>${i + 1}</td>
      <td><strong style="color:var(--blue)">${pick.ticker}</strong>${otmStr}${cheapTag}</td>
      <td class="entry-cell">${pickEntry > pick.current_price ? `<span class="entry-level-badge" title="Enter when price breaks ${fmt$(pickEntry)} — still ${pickUpside}% to ${fmt$(pick.strike)} strike">⚡ &gt; ${fmt$(pickEntry)}</span>` : '<span style="color:var(--text3);font-size:11px">—</span>'}</td>
      <td><span class="sec-chip ${sectorCls}">${shortSector(pick.sector || '')}<span>${sectorSign}${sectorRet.toFixed(1)}%</span></span></td>
      <td>${fmt$(pick.current_price)}</td>
      <td><strong>${fmt$(pick.strike)}</strong></td>
      <td style="color:var(--text2)">${pick.expiry}</td>
      <td><span class="dte-chip ${dteClass}">${pick.dte}d</span></td>
      <td style="font-weight:600">${fmt$(pick.mid)}</td>
      <td>
        <div class="conf-cell">
          <span class="conf-num" style="color:${confColor}">${confPct}%</span>
          <div class="conf-bar-track conf-bar-sm"><div class="conf-bar-fill" style="width:${confPct}%;background:${confColor}"></div></div>
        </div>
      </td>
      <td><span class="conviction-badge ${convictionClass(pick.conviction)}" title="${why}">${pick.conviction}</span></td>
      <td class="why-cell" title="${why}">${why}</td>
      <td><button class="watch-btn${_watchedTickers[pick.ticker] ? ' watch-active' : ''}" data-t="${pick.ticker}" title="${_watchedTickers[pick.ticker] ? 'Watching — click to cancel' : 'Watch for level break'}">${_watchedTickers[pick.ticker] ? '🔔' : '🔕'}</button></td>
      <td><button class="btn-secondary picks-drill-btn" data-t="${pick.ticker}">Details →</button></td>`;
    tbody.appendChild(tr);
  });
  tbody.querySelectorAll('.picks-drill-btn').forEach(btn =>
    btn.addEventListener('click', () => analyze(btn.dataset.t))
  );
  tbody.querySelectorAll('.watch-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const ticker = btn.dataset.t;
      const pick = _allPicks.find(p => p.ticker === ticker);
      if (!pick) return;
      toggleWatch(ticker, pick.support_levels || [], pick.resistance_levels || [], pick.current_price);
    });
  });
  $('picksLoading').classList.add('hidden');
  $('picksTable').classList.remove('hidden');
}

function renderBestPicks(data) {
  _allPicks = data.picks || [];
  renderSectorHeat(data.sector_perf);
  renderFilterBar();
  renderDteFilter();
  const updated = data.updated_at ? new Date(data.updated_at * 1000) : new Date();
  const timeStr = updated.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  $('picksStatus').textContent = `${data.picks.length} picks from ${data.scanned} tickers — last scan ${timeStr}${data.stale ? ' (refreshing…)' : ''}`;
  renderPicksTable(applyFilters(_allPicks));

  // Auto-watch top 8 picks silently — alert fires automatically when a level breaks
  _allPicks.slice(0, 8).forEach(pick => {
    if (!_watchedTickers[pick.ticker] &&
        (pick.resistance_levels?.length || pick.support_levels?.length)) {
      startWatch(pick.ticker, pick.support_levels || [], pick.resistance_levels || [], pick.current_price, true);
    }
  });
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

function renderOptions(calls, currentPrice, resistanceLevels) {
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

    const toNum = l => (typeof l === 'object' && l !== null) ? l.price : l;
    const knownRes = (resistanceLevels || []).map(toNum).filter(p => p > currentPrice && p < opt.strike);
    const step = currentPrice >= 500 ? 10 : currentPrice >= 100 ? 5 : 2.5;
    const roundUnit = step / 2; // finer grid so adjacent strikes get distinct values

    // Entry = fraction of the way from current price to strike.
    // Fraction scales with how OTM the strike is: near-ATM entries are low,
    // far-OTM entries are higher — so every strike gets a different level.
    const otmFrac = (opt.strike - currentPrice) / currentPrice;
    const fraction = Math.min(0.15 + otmFrac * 1.5, 0.40);
    let entryLvl = currentPrice + (opt.strike - currentPrice) * fraction;
    entryLvl = Math.round(entryLvl / roundUnit) * roundUnit;

    // Snap to a known resistance only if essentially the same price (0.5%)
    const snap = knownRes.find(r => Math.abs(r - entryLvl) / entryLvl < 0.005);
    if (snap) entryLvl = snap;

    if (entryLvl <= currentPrice) entryLvl = Math.ceil((currentPrice + roundUnit) / roundUnit) * roundUnit;
    if (entryLvl >= opt.strike)   entryLvl = parseFloat((opt.strike - roundUnit).toFixed(2));
    entryLvl = parseFloat(entryLvl.toFixed(2));

    const upside = (((opt.strike - entryLvl) / entryLvl) * 100).toFixed(0);
    let entryCellHtml;
    if (!entryLvl || entryLvl <= currentPrice) {
      entryCellHtml = `<span style="color:var(--text3);font-size:11px">—</span>`;
    } else {
      entryCellHtml = `<span class="entry-level-badge" title="Enter when price breaks ${fmt$(entryLvl)} — still ${upside}% to your ${fmt$(opt.strike)} strike from here">⚡ &gt; ${fmt$(entryLvl)}</span>`;
    }

    tr.innerHTML = `
      <td>${i+1}</td>
      <td><span class="strike-val">${fmt$(opt.strike)}</span> ${otmStr}</td>
      <td class="entry-cell">${entryCellHtml}</td>
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
  _entrySignalData = null;
  const esBanner = $('entrySignal');
  if (esBanner) esBanner.classList.add('hidden');
  showLoading('Fetching market data for ' + ticker + '…');
  try {
    const res = await fetch(`/api/analyze/${ticker}`);
    const data = await res.json();
    if (data.error) {
      const isRateLimit = data.error.toLowerCase().includes('rate');
      if (isRateLimit) {
        // Auto-retry after 5s instead of showing error screen
        let secs = 5;
        showLoading(`Rate limited — retrying in ${secs}s…`);
        const countdown = setInterval(() => {
          secs--;
          if (secs > 0) showLoading(`Rate limited — retrying in ${secs}s…`);
        }, 1000);
        await new Promise(r => setTimeout(r, 5000));
        clearInterval(countdown);
        $('analyzeBtn').disabled = false;
        analyze(ticker);
        return;
      }
      showError(data.error); return;
    }

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
    renderOptions(data.top_calls, data.current_price, data.resistance_levels);
    renderEntrySignal(data.ticker, data.resistance_levels, data.support_levels, data.current_price);
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
    b.innerHTML = `✓ v${info.version} ready — <u style="cursor:pointer" id="installBtn">restart</u> · <u style="cursor:pointer" id="dlBtn">download manually</u>`;
    b.classList.remove('hidden');
    b.onclick = null;
    $('installBtn').onclick = e => { e.stopPropagation(); window.electronAPI.installUpdate(); };
    $('dlBtn').onclick = e => { e.stopPropagation(); window.electronAPI.openReleasePage(); };
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
  if (window._lastAnalysis) renderOptions(window._lastAnalysis.top_calls, window._lastAnalysis.current_price, window._lastAnalysis.resistance_levels);
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

// Premium range filter
function onPremiumChange() {
  _minPremium = parseFloat($('minPremium').value) || 0;
  _maxPremium = parseFloat($('maxPremium').value) || 0;
  updateBudgetHint();
  if (_allPicks.length) renderPicksTable(applyFilters(_allPicks));
}
$('minPremium').addEventListener('input', onPremiumChange);
$('maxPremium').addEventListener('input', onPremiumChange);

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
