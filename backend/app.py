"""OptionsScout backend — Flask server providing options analysis."""
from flask import Flask, render_template, jsonify, request, send_from_directory
from flask.json.provider import DefaultJSONProvider
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
import json
import platform
import logging
import os
import re
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ─── yfinance retry + per-ticker cache ───────────────────────────────────────

def _yf_call(fn, retries=3, base_delay=2.0):
    """Call a yfinance function, retrying on 429 / rate-limit errors."""
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            msg = str(e).lower()
            if any(x in msg for x in ('too many requests', '429', 'rate limit', 'rate_limit')):
                if attempt < retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning('yf rate limited — retry in %.0fs (attempt %d/%d)', delay, attempt + 1, retries)
                    time.sleep(delay)
                    continue
            raise
    return None


_ticker_cache      = {}
_ticker_cache_lock = threading.Lock()
TICKER_CACHE_TTL   = 300  # 5 min — reuse recent results within same scan cycle


def _get_cached(ticker):
    with _ticker_cache_lock:
        e = _ticker_cache.get(ticker)
        if e and (time.time() - e['ts']) < TICKER_CACHE_TTL:
            return e['data']
    return None


def _set_cached(ticker, data):
    with _ticker_cache_lock:
        _ticker_cache[ticker] = {'data': data, 'ts': time.time()}


class NumpyJSONProvider(DefaultJSONProvider):
    def default(self, obj):
        if isinstance(obj, np.integer):  return int(obj)
        if isinstance(obj, np.floating): return float(obj)
        if isinstance(obj, np.bool_):    return bool(obj)
        if isinstance(obj, np.ndarray):  return obj.tolist()
        return super().default(obj)


# Resolve frontend path (works in dev, PyInstaller binary, and Electron-bundled binary)
if os.environ.get('FRONTEND_DIR'):
    FRONTEND_DIR = os.environ['FRONTEND_DIR']
elif getattr(sys, 'frozen', False):
    # Inside Electron: binary is at Resources/backend-bin/, frontend at Resources/frontend/
    exe_dir = os.path.dirname(sys.executable)
    candidate = os.path.normpath(os.path.join(exe_dir, '..', 'frontend'))
    FRONTEND_DIR = candidate if os.path.isdir(candidate) else os.path.join(sys._MEIPASS, 'frontend')
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    FRONTEND_DIR = os.path.join(BASE_DIR, 'frontend')

app = Flask(__name__, static_folder=None)
app.json_provider_class = NumpyJSONProvider
app.json = NumpyJSONProvider(app)


# ─── Support / Resistance ─────────────────────────────────────────────────────

def find_sr_levels(df, window=8, tolerance=0.018):
    raw = []
    highs = df['High'].values
    lows  = df['Low'].values
    for i in range(window, len(df) - window):
        if highs[i] == max(highs[i - window: i + window + 1]):
            raw.append(('resistance', highs[i]))
        if lows[i] == min(lows[i - window: i + window + 1]):
            raw.append(('support', lows[i]))
    if not raw:
        return []
    raw.sort(key=lambda x: x[1])
    clusters, current = [], [raw[0]]
    for lvl in raw[1:]:
        ref = sum(l[1] for l in current) / len(current)
        if abs(lvl[1] - ref) / ref < tolerance:
            current.append(lvl)
        else:
            clusters.append(current); current = [lvl]
    clusters.append(current)
    out = []
    for cluster in clusters:
        avg = sum(l[1] for l in cluster) / len(cluster)
        n_res = sum(1 for l in cluster if l[0] == 'resistance')
        kind = 'resistance' if n_res >= len(cluster) / 2 else 'support'
        out.append({'type': kind, 'price': round(avg, 2), 'strength': min(len(cluster), 5)})
    return out


def add_key_levels(df, current_price):
    out = []
    if len(df) >= 5:
        out.append({'label': 'Prev Week High', 'price': round(float(df['High'].iloc[-5:].max()), 2), 'type': 'resistance'})
        out.append({'label': 'Prev Week Low',  'price': round(float(df['Low'].iloc[-5:].min()),  2), 'type': 'support'})
    if len(df) >= 2:
        out.append({'label': 'Prev Day High', 'price': round(float(df['High'].iloc[-2]), 2), 'type': 'resistance'})
        out.append({'label': 'Prev Day Low',  'price': round(float(df['Low'].iloc[-2]),  2), 'type': 'support'})
    base = round(current_price / 5) * 5
    for off in [-10, -5, 0, 5, 10]:
        rnd = base + off
        if abs(rnd - current_price) / current_price < 0.08:
            kind = 'resistance' if rnd > current_price else 'support'
            out.append({'label': f'Round ${rnd:.0f}', 'price': round(rnd, 2), 'type': kind})
    return out


# ─── Option Scoring ───────────────────────────────────────────────────────────

def score_call(opt, current_price, supports, resistances, dte):
    strike = float(opt.get('strike', 0))
    bid    = float(opt.get('bid', 0) or 0)
    ask    = float(opt.get('ask', 0) or 0)
    vol    = int(opt.get('volume', 0) or 0)
    oi     = int(opt.get('openInterest', 0) or 0)
    iv     = float(opt.get('impliedVolatility', 0) or 0)
    if bid <= 0 or ask <= 0 or strike <= 0 or ask > 50:
        return None

    score, reasons = 0, []

    # ── Moneyness ────────────────────────────────────────────────────────────
    otm_pct = (strike - current_price) / current_price * 100
    if  10 < otm_pct <= 20:    score += 32; reasons.append(f'{otm_pct:.1f}% OTM — breakout play')
    elif 20 < otm_pct <= 40:   score += 28; reasons.append(f'{otm_pct:.1f}% OTM — lottery')
    elif  5 < otm_pct <= 10:   score += 22; reasons.append(f'{otm_pct:.1f}% OTM')
    elif  2 < otm_pct <=  5:   score += 16; reasons.append(f'{otm_pct:.1f}% OTM')
    elif -1 <= otm_pct <= 2:   score += 12; reasons.append('ATM strike')
    elif -5 <= otm_pct < -1:   score +=  8; reasons.append(f'ITM {abs(otm_pct):.1f}%')
    elif otm_pct > 40:         score += 20; reasons.append(f'{otm_pct:.1f}% OTM — deep lottery')
    else:                       score +=  3

    # ── DTE ──────────────────────────────────────────────────────────────────
    if   2 <= dte <= 5:   score += 18; reasons.append(f'{dte}d expiry (weekly)')
    elif dte == 1:        score += 8;  reasons.append('1d expiry (high gamma)')
    elif  5 < dte <= 10:  score += 16; reasons.append(f'{dte}d expiry')
    elif 10 < dte <= 21:  score += 14; reasons.append(f'{dte}d expiry')
    elif 21 < dte <= 45:  score += 18; reasons.append(f'{dte}d expiry (swing)')
    elif 45 < dte <= 60:  score += 14; reasons.append(f'{dte}d expiry (swing)')

    # ── Cheap premium bonus (more contracts, lottery upside) ─────────────────
    if   ask <= 0.20: score += 30; reasons.append(f'Ultra cheap ${ask:.2f} — stack contracts')
    elif ask <= 0.50: score += 24; reasons.append(f'Cheap premium ${ask:.2f}')
    elif ask <= 1.50: score += 16; reasons.append(f'Low premium ${ask:.2f}')
    elif ask <= 3.00: score +=  8; reasons.append(f'Affordable ${ask:.2f}')

    # ── Volume ────────────────────────────────────────────────────────────────
    if   vol >= 2000: score += 25; reasons.append(f'Vol {vol:,} (very active)')
    elif vol >= 500:  score += 18; reasons.append(f'Vol {vol:,}')
    elif vol >= 100:  score += 10; reasons.append(f'Vol {vol:,}')
    elif vol > 0:     score +=  3

    # ── Open interest ─────────────────────────────────────────────────────────
    if   oi >= 10000: score += 18; reasons.append(f'OI {oi:,} (deep liquid)')
    elif oi >= 2000:  score += 12; reasons.append(f'OI {oi:,}')
    elif oi >= 500:   score +=  6; reasons.append(f'OI {oi:,}')

    # ── Support / resistance proximity ────────────────────────────────────────
    for lvl in supports:
        prox = abs(current_price - lvl['price']) / current_price
        if prox < 0.025:
            score += 12 * min(lvl['strength'], 3)
            reasons.append(f"Near support ${lvl['price']}"); break
        elif prox < 0.05:
            score += 6
            reasons.append(f"Close to support ${lvl['price']}"); break

    for lvl in resistances:
        if abs(strike - lvl['price']) / strike < 0.025:
            score += 10
            reasons.append(f"Strike at resistance ${lvl['price']}"); break

    # ── Spread quality ────────────────────────────────────────────────────────
    mid = (bid + ask) / 2
    sp  = (ask - bid) / mid if mid > 0 else 1
    if sp < 0.08:   score += 10; reasons.append('Tight spread')
    elif sp < 0.15: score +=  5
    elif sp > 0.40: score -=  8; reasons.append('Wide spread')

    # ── IV ────────────────────────────────────────────────────────────────────
    if   iv > 2.0:            score -= 15; reasons.append('Very high IV')
    elif iv > 1.2:            score -=  5; reasons.append('Elevated IV')
    elif 0.2 <= iv <= 0.7:    score +=  8; reasons.append('Reasonable IV')

    # ── V/OI momentum ─────────────────────────────────────────────────────────
    if oi > 0 and (vol / oi) > 0.2:
        score += 8; reasons.append('High V/OI ratio')

    return max(score, 0), reasons


# ─── Core analyze function (reused for single + batch) ────────────────────────

def analyze_ticker(ticker):
    ticker = ticker.upper().strip()

    cached = _get_cached(ticker)
    if cached:
        return cached

    stock = yf.Ticker(ticker)

    info = {}
    try: info = _yf_call(lambda: stock.info or {}) or {}
    except Exception: pass

    current_price = info.get('currentPrice') or info.get('regularMarketPrice') or info.get('previousClose')
    try:
        hist = _yf_call(lambda: stock.history(period='3mo', auto_adjust=True))
    except Exception:
        return {'ticker': ticker, 'error': 'Rate limited — try again in a moment'}
    if hist is None or hist.empty:
        return {'ticker': ticker, 'error': 'No data'}
    if not current_price:
        current_price = float(hist['Close'].iloc[-1])
    current_price = float(current_price)

    sr_raw = find_sr_levels(hist)
    key_levels = add_key_levels(hist, current_price)

    supports    = [l for l in sr_raw if l['type'] == 'support'    and l['price'] < current_price * 1.03]
    resistances = [l for l in sr_raw if l['type'] == 'resistance' and l['price'] > current_price * 0.97]
    for kl in key_levels:
        (supports if kl['type'] == 'support' else resistances).append({**kl, 'strength': 2})

    supports    = sorted(supports,    key=lambda x: x['price'], reverse=True)[:6]
    resistances = sorted(resistances, key=lambda x: x['price'])[:6]

    try:
        exp_dates = _yf_call(lambda: list(stock.options)) or []
    except Exception:
        exp_dates = []

    today = datetime.now()
    best_calls = []
    for ds in exp_dates[:10]:
        exp = datetime.strptime(ds, '%Y-%m-%d')
        dte = (exp - today).days
        if dte < 0 or dte > 60: continue
        try:
            chain = _yf_call(lambda d=ds: stock.option_chain(d).calls)
            if chain is None or chain.empty: continue
            chain = chain[(chain['strike'] >= current_price * 0.90) & (chain['strike'] <= current_price * 1.60)]
            for _, row in chain.iterrows():
                res = score_call(row.to_dict(), current_price, supports, resistances, dte)
                if not res: continue
                sc, reasons = res
                bid = float(row.get('bid', 0) or 0); ask = float(row.get('ask', 0) or 0)
                best_calls.append({
                    'strike': float(row['strike']), 'expiry': ds, 'dte': dte,
                    'bid': round(bid, 2), 'ask': round(ask, 2), 'mid': round((bid+ask)/2, 2),
                    'volume': int(row.get('volume', 0) or 0),
                    'open_interest': int(row.get('openInterest', 0) or 0),
                    'iv': round(float(row.get('impliedVolatility', 0) or 0) * 100, 1),
                    'score': sc, 'reasons': reasons,
                    'itm': bool(row.get('inTheMoney', False)),
                })
        except Exception as e:
            logger.warning('chain err %s %s: %s', ticker, ds, e); continue

    best_calls.sort(key=lambda x: x['score'], reverse=True)

    avg_vol = float(hist['Volume'].mean()) if not hist.empty else 0
    today_v = int(hist['Volume'].iloc[-1])  if not hist.empty else 0
    ratio   = round(today_v / avg_vol, 2) if avg_vol > 0 else 1.0
    sig     = 'High' if ratio > 1.4 else ('Low' if ratio < 0.6 else 'Normal')
    vhist   = [{'date': dt.strftime('%m/%d'), 'volume': int(r['Volume']), 'above_avg': r['Volume'] > avg_vol}
               for dt, r in hist.tail(20).iterrows()]

    result = {
        'ticker': ticker,
        'company_name':   info.get('longName', ticker),
        'current_price':  round(current_price, 2),
        'support_levels': supports,
        'resistance_levels': resistances,
        'top_calls': best_calls[:12],
        'volume': {'today': today_v, 'avg': int(avg_vol), 'ratio': ratio, 'signal': sig, 'history': vhist},
        'stats': {
            '52w_high':  info.get('fiftyTwoWeekHigh'),
            '52w_low':   info.get('fiftyTwoWeekLow'),
            'avg_vol':   info.get('averageVolume'),
            'mkt_cap':   info.get('marketCap'),
            'pe_ratio':  info.get('trailingPE'),
        }
    }
    _set_cached(ticker, result)
    return result


# ─── Best Picks — full sector-aware scanner ───────────────────────────────────

SECTOR_ETFS = {
    'Technology':        'XLK',
    'Semiconductors':    'SMH',
    'Financials':        'XLF',
    'Energy':            'XLE',
    'Healthcare':        'XLV',
    'Industrials':       'XLI',
    'Communication':     'XLC',
    'Consumer Disc':     'XLY',
    'Consumer Staples':  'XLP',
    'Materials':         'XLB',
    'Utilities':         'XLU',
    'Real Estate':       'XLRE',
}

SECTOR_TICKERS = {
    'Technology': [
        'AAPL','MSFT','NVDA','AMD','AVGO','INTC','QCOM','TXN','AMAT','LRCX',
        'MU','KLAC','MRVL','ADI','NXPI','ON','MPWR','SWKS','MCHP',
        'CRM','ORCL','NOW','INTU','ADBE','SNOW','PANW','CRWD','PLTR',
        'NET','DDOG','FTNT','ZS','WDAY','TEAM','ANSS','CDNS','SNPS',
        'IBM','DELL','HPQ','HPE','STX','WDC','NTAP',
    ],
    'Semiconductors': [
        'NVDA','AMD','AVGO','INTC','QCOM','MU','AMAT','LRCX','KLAC','TXN',
        'ADI','MRVL','NXPI','ON','MPWR','SWKS','MCHP','SLAB','WOLF','MTSI',
        'ACLS','ONTO','COHU','AZTA','RMBS',
    ],
    'Communication': [
        'META','GOOGL','GOOG','NFLX','DIS','CMCSA','T','VZ','TMUS','CHTR',
        'SNAP','PINS','RDDT','PARA','FOXA','WBD','MTCH','IAC',
    ],
    'Consumer Disc': [
        'AMZN','TSLA','HD','MCD','NKE','SBUX','TGT','LOW','CMG','BKNG',
        'ABNB','MAR','HLT','WYNN','MGM','LVS','F','GM','RIVN','LCID',
        'TJX','ROST','ULTA','BBY','ETSY','EBAY','W','RH',
        'DHI','LEN','PHM','TOL','NVR','POOL','TREX',
    ],
    'Consumer Staples': [
        'WMT','PG','KO','PEP','COST','MDLZ','MO','PM','CL','EL',
        'CHD','KHC','GIS','CAG','SJM','CPB','KR','HSY','MKC',
    ],
    'Financials': [
        'JPM','BAC','GS','MS','WFC','C','BLK','SCHW','AXP','V','MA',
        'PYPL','COF','DFS','USB','PNC','TFC','KEY','RF','FITB','HBAN',
        'MTB','CFG','ALLY','SYF','HOOD','COIN','MSTR','SQ','SOFI',
        'ICE','CME','NDAQ','CBOE','SPGI','MCO','MSCI',
    ],
    'Healthcare': [
        'JNJ','UNH','LLY','PFE','ABBV','MRK','BMY','AMGN','GILD','BIIB',
        'REGN','VRTX','HCA','CNC','CVS','CI','ISRG','MDT','ABT','SYK',
        'EW','TMO','DHR','A','ILMN','MRNA','BNTX','NVAX','SRPT','ALNY',
        'INCY','EXEL','HALO','ACAD','RARE',
    ],
    'Energy': [
        'XOM','CVX','COP','EOG','SLB','HAL','DVN','MPC','PSX','VLO',
        'OXY','FANG','BKR','KMI','WMB','OKE','LNG','AR','RRC','EQT',
        'CTRA','MRO','APA','NOG','SM','MTDR',
    ],
    'Industrials': [
        'CAT','DE','HON','GE','BA','LMT','RTX','NOC','GD','TDG','AXON',
        'ROP','ITW','EMR','ETN','PH','MMM','UPS','FDX','DAL','UAL',
        'AAL','LUV','CSX','NSC','UNP','CARR','OTIS','JCI','IR','XYL',
        'PWR','HUBB','AME','ROK','GNRC','ACHR','JOBY',
    ],
    'Materials': [
        'LIN','APD','ECL','PPG','NEM','FCX','CTVA','DOW','DD','NUE',
        'CLF','X','AA','MP','GOLD','KGC','AEM','PAAS',
    ],
    'Real Estate': [
        'AMT','PLD','EQIX','CCI','WELL','SPG','O','DLR','PSA','EQR',
        'AVB','VTR','ARE','BXP','KIM','REG',
    ],
    'Utilities': [
        'NEE','SO','DUK','D','AEP','EXC','SRE','PCG','ES','FE',
        'ETR','XEL','WEC','CMS','EVRG',
    ],
    'Broad Market': [
        'SPY','QQQ','IWM','DIA','GLD','SLV','TLT','HYG','GDX','GDXJ',
        'XLK','XLF','XLE','XLV','XLI','XLC','XLY','SMH','SOXX',
    ],
}

PICKS_TTL = 90   # seconds — longer because we scan more tickers

_picks_cache     = {'data': None, 'ts': 0, 'refreshing': False}
_picks_lock      = threading.Lock()
_sector_cache    = {'perf': None, 'ts': 0}
_sector_lock     = threading.Lock()
SECTOR_CACHE_TTL = 1800  # 30 min


def get_sector_performance():
    with _sector_lock:
        if _sector_cache['perf'] and (time.time() - _sector_cache['ts']) < SECTOR_CACHE_TTL:
            return _sector_cache['perf']

    perf = {}
    for sector, etf in SECTOR_ETFS.items():
        try:
            hist = _yf_call(lambda e=etf: yf.Ticker(e).history(period='5d'))
            if hist is not None and len(hist) >= 2:
                ret = (float(hist['Close'].iloc[-1]) / float(hist['Close'].iloc[0]) - 1) * 100
                perf[sector] = round(ret, 2)
            else:
                perf[sector] = 0.0
        except Exception:
            perf[sector] = 0.0

    perf = dict(sorted(perf.items(), key=lambda x: x[1], reverse=True))
    with _sector_lock:
        _sector_cache['perf'] = perf
        _sector_cache['ts']   = time.time()
    return perf


def build_scan_list(sector_perf):
    """Return ~90 tickers weighted toward hot sectors, min 6 per sector."""
    ranked = list(sector_perf.keys())
    # Slots: top sector 14, 2nd 12, 3rd 10, 4th 8, rest min 6
    slots  = [14, 12, 10, 8, 6, 6, 6, 6, 6, 6, 6, 6]
    seen, result = set(), []

    for i, sector in enumerate(ranked):
        n = slots[i] if i < len(slots) else 6
        for t in SECTOR_TICKERS.get(sector, [])[:n]:
            if t not in seen:
                seen.add(t); result.append((t, sector))

    for t in SECTOR_TICKERS.get('Broad Market', []):
        if t not in seen:
            seen.add(t); result.append((t, 'Broad Market'))

    return result


def compute_confidence(score, vol_signal, reasons, sector_ret=0.0):
    base = min(score / 1.15, 78.0)
    if vol_signal == 'High':      base += 12
    elif vol_signal == 'Elevated': base += 6
    base += min(len(reasons) * 2, 10)
    # Hot sector bonus
    if sector_ret >= 3:   base += 8
    elif sector_ret >= 1: base += 4
    return min(round(base), 95)


def conviction_label(conf):
    if conf >= 80: return 'Strong'
    if conf >= 65: return 'High'
    if conf >= 50: return 'Moderate'
    return 'Speculative'


def conviction_reason(top, sector, sector_ret, vol_signal):
    """One short sentence explaining the top reason for conviction."""
    reasons = top.get('reasons', [])
    ask = top.get('ask', 0)

    if sector_ret >= 3:
        return f'Hot sector: {sector} +{sector_ret:.1f}% this week'
    if ask > 0 and ask <= 1.50:
        n = int(1000 / (ask * 100))
        return f'Cheap entry — ~{n}× contracts for $1,000'
    if any('support' in r.lower() for r in reasons):
        return 'Price sitting on key support level'
    if vol_signal == 'High':
        return 'Volume surge — unusual buying activity'
    if any('V/OI' in r for r in reasons):
        return 'High options flow relative to open interest'
    if any('ATM' in r for r in reasons):
        return 'At-the-money — maximum gamma exposure'
    if sector_ret >= 1:
        return f'{sector} sector momentum +{sector_ret:.1f}%'
    return reasons[0] if reasons else 'Multiple technical signals aligned'


def _scan_one_with_sector(ticker, sector, sector_ret):
    try:
        r = analyze_ticker(ticker)
        if r.get('error') or not r.get('top_calls'):
            return None
        top     = r['top_calls'][0]
        vol_sig = r['volume']['signal']
        conf    = compute_confidence(top['score'], vol_sig, top['reasons'], sector_ret)
        return {
            'ticker':          ticker,
            'company_name':    r['company_name'],
            'current_price':   r['current_price'],
            'sector':          sector,
            'sector_return':   sector_ret,
            'strike':          top['strike'],
            'expiry':          top['expiry'],
            'dte':             top['dte'],
            'bid':             top['bid'],
            'ask':             top['ask'],
            'mid':             top['mid'],
            'volume':          top['volume'],
            'open_interest':   top['open_interest'],
            'iv':              top['iv'],
            'score':           top['score'],
            'confidence':      conf,
            'conviction':      conviction_label(conf),
            'conviction_reason': conviction_reason(top, sector, sector_ret, vol_sig),
            'signals':         top['reasons'][:4],
            'volume_signal':   vol_sig,
            'itm':             top.get('itm', False),
            'support_levels':    r.get('support_levels', [])[:3],
            'resistance_levels': r.get('resistance_levels', [])[:3],
        }
    except Exception as e:
        logger.warning('picks scan %s: %s', ticker, e)
        return None


def _do_refresh():
    sector_perf = get_sector_performance()
    scan_list   = build_scan_list(sector_perf)
    logger.info('Scanning %d tickers across %d sectors', len(scan_list), len(SECTOR_ETFS))

    results = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(_scan_one_with_sector, t, sec, sector_perf.get(sec, 0.0)): t
                for t, sec in scan_list}
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                results.append(r)

    results.sort(key=lambda x: x['confidence'], reverse=True)
    out = {
        'picks':        results,
        'scanned':      len(scan_list),
        'sector_perf':  sector_perf,
        'updated_at':   time.time(),
    }
    with _picks_lock:
        _picks_cache['data']       = out
        _picks_cache['ts']         = time.time()
        _picks_cache['refreshing'] = False
    return out


def _warmup():
    time.sleep(4)
    try:
        _do_refresh()
    except Exception as e:
        logger.warning('warmup err: %s', e)

threading.Thread(target=_warmup, daemon=True).start()


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(FRONTEND_DIR, 'index.html')

@app.route('/static/<path:fname>')
def static_files(fname):
    return send_from_directory(FRONTEND_DIR, fname)

@app.route('/api/analyze/<ticker>')
def analyze_route(ticker):
    try:
        result = analyze_ticker(ticker)
        if result.get('error'):
            return jsonify({'error': f"Could not analyze {ticker}: {result['error']}"}), 400
        return jsonify(result)
    except Exception as e:
        logger.exception('analyze err')
        return jsonify({'error': str(e)}), 500


@app.route('/api/analyze-batch', methods=['POST'])
def analyze_batch():
    """Analyze a list of tickers, return summary scores for each."""
    data = request.get_json(silent=True) or {}
    tickers = data.get('tickers', [])
    if not tickers or not isinstance(tickers, list):
        return jsonify({'error': 'No tickers provided'}), 400

    tickers = [t.upper().strip() for t in tickers if t and isinstance(t, str)][:50]
    results = []
    for t in tickers:
        try:
            r = analyze_ticker(t)
            if r.get('error') or not r.get('top_calls'):
                results.append({'ticker': t, 'error': r.get('error', 'No options'), 'top_score': 0})
                continue
            top = r['top_calls'][0]
            results.append({
                'ticker': t,
                'company_name': r['company_name'],
                'current_price': r['current_price'],
                'top_score': top['score'],
                'top_strike': top['strike'],
                'top_expiry': top['expiry'],
                'top_dte': top['dte'],
                'top_mid': top['mid'],
                'top_volume': top['volume'],
                'volume_signal': r['volume']['signal'],
                'reasons': top['reasons'][:3],
            })
        except Exception as e:
            results.append({'ticker': t, 'error': str(e), 'top_score': 0})

    results.sort(key=lambda x: x.get('top_score', 0), reverse=True)
    return jsonify({'results': results, 'count': len(results)})


@app.route('/api/parse-watchlist', methods=['POST'])
def parse_watchlist():
    """Parse a TradingView watchlist .txt export and return ticker list."""
    if 'file' not in request.files:
        # also accept text body
        text = request.get_data(as_text=True) or ''
    else:
        text = request.files['file'].read().decode('utf-8', errors='ignore')

    if not text.strip():
        return jsonify({'error': 'Empty file'}), 400

    # TradingView exports look like: "###Section,NASDAQ:AAPL,NYSE:SPY,..." or one per line
    raw = re.split(r'[,\n\r\s]+', text)
    tickers = []
    for tok in raw:
        tok = tok.strip()
        if not tok or tok.startswith('###') or tok.startswith('#'):
            continue
        # Strip exchange prefix: NASDAQ:AAPL → AAPL
        if ':' in tok:
            tok = tok.split(':', 1)[1]
        # Skip non-ticker tokens (must be 1-6 alphanumeric, possibly with .)
        if re.match(r'^[A-Z0-9.\-]{1,8}$', tok.upper()):
            tickers.append(tok.upper())

    # Dedupe, preserve order
    seen, deduped = set(), []
    for t in tickers:
        if t not in seen:
            seen.add(t); deduped.append(t)

    return jsonify({'tickers': deduped, 'count': len(deduped)})


@app.route('/api/news/<ticker>')
def get_news(ticker):
    ticker = ticker.upper().strip()
    try:
        stock = yf.Ticker(ticker)
        raw_news = stock.news or []
        items = []
        for item in raw_news[:10]:
            if not isinstance(item, dict): continue
            content = item.get('content') or {}
            if content:
                title = content.get('title', '')
                summary = content.get('summary', '') or content.get('description', '')
                pub = content.get('pubDate', '')
                provider = (content.get('provider') or {}).get('displayName', 'Unknown')
                u = content.get('canonicalUrl') or content.get('clickThroughUrl') or {}
                url = u.get('url', '') if isinstance(u, dict) else str(u)
            else:
                title = item.get('title', '')
                summary = item.get('summary', '')
                ts = item.get('providerPublishTime', 0)
                pub = datetime.fromtimestamp(ts).isoformat() if ts else ''
                provider = item.get('publisher', 'Unknown')
                url = item.get('link', '')
            if not title: continue
            text_low = (title + ' ' + summary).lower()
            pos = ['beat','surge','rally','gain','upgrade','record','soar','buy','bull']
            neg = ['miss','fall','drop','downgrade','cut','loss','bear','sell','warn']
            sent = 'positive' if any(w in text_low for w in pos) else ('negative' if any(w in text_low for w in neg) else 'neutral')
            items.append({
                'title': title,
                'summary': summary[:220] + '…' if len(summary) > 220 else summary,
                'published': pub, 'source': provider, 'url': url, 'sentiment': sent
            })
        return jsonify({'news': items})
    except Exception as e:
        logger.exception('news err')
        return jsonify({'news': [], 'error': str(e)})


@app.route('/api/best-picks')
def best_picks():
    with _picks_lock:
        fresh = _picks_cache['data'] and (time.time() - _picks_cache['ts']) < PICKS_TTL
        if fresh:
            return jsonify(_picks_cache['data'])
        already = _picks_cache['refreshing']
        if not already:
            _picks_cache['refreshing'] = True

    if already:
        # another request is already refreshing — return stale cache if we have it
        with _picks_lock:
            if _picks_cache['data']:
                d = dict(_picks_cache['data']); d['stale'] = True
                return jsonify(d)

    out = _do_refresh()
    return jsonify(out)


def _store_path():
    system = platform.system()
    if system == 'Darwin':
        d = os.path.expanduser('~/Library/Application Support/Options Scout')
    elif system == 'Windows':
        d = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'Options Scout')
    else:
        d = os.path.expanduser('~/.config/options-scout')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, 'store.json')

@app.route('/api/store', methods=['GET'])
def get_store():
    try:
        p = _store_path()
        if os.path.exists(p):
            with open(p, 'r') as f:
                return jsonify(json.load(f))
    except Exception as e:
        logger.warning('store read err: %s', e)
    return jsonify({})

@app.route('/api/store', methods=['POST'])
def set_store():
    try:
        data = request.get_json(silent=True) or {}
        with open(_store_path(), 'w') as f:
            json.dump(data, f, indent=2)
        return jsonify({'ok': True})
    except Exception as e:
        logger.warning('store write err: %s', e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/price/<ticker>')
def get_price(ticker):
    ticker = ticker.upper().strip()
    try:
        hist = _yf_call(lambda: yf.Ticker(ticker).history(period='1d', interval='1m'))
        if hist is not None and not hist.empty:
            price = round(float(hist['Close'].iloc[-1]), 2)
            return jsonify({'ticker': ticker, 'price': price})
    except Exception:
        pass
    # fallback to cached analyze result
    cached = _get_cached(ticker)
    if cached:
        return jsonify({'ticker': ticker, 'price': cached['current_price']})
    return jsonify({'error': 'price unavailable'}), 404


@app.route('/api/health')
def health():
    return jsonify({'ok': True})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)
