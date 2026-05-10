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
import logging
import os
import re
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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

    otm_pct = (strike - current_price) / current_price * 100
    if -1 <= otm_pct <= 2:   score += 30; reasons.append('ATM strike')
    elif 2 < otm_pct <= 5:   score += 24; reasons.append(f'{otm_pct:.1f}% OTM')
    elif 5 < otm_pct <= 10:  score += 14; reasons.append(f'{otm_pct:.1f}% OTM')
    elif -5 <= otm_pct < -1: score += 18; reasons.append(f'ITM {abs(otm_pct):.1f}%')
    else: score += 4

    if 2 <= dte <= 5:    score += 20; reasons.append(f'{dte}d expiry (optimal weekly)')
    elif dte == 1:       score += 10; reasons.append('1d expiry (high gamma)')
    elif 5 < dte <= 10:  score += 14; reasons.append(f'{dte}d expiry')
    elif 10 < dte <= 21: score += 8;  reasons.append(f'{dte}d expiry')

    if   vol >= 2000: score += 25; reasons.append(f'Vol {vol:,} (very active)')
    elif vol >= 500:  score += 18; reasons.append(f'Vol {vol:,}')
    elif vol >= 100:  score += 10; reasons.append(f'Vol {vol:,}')
    elif vol > 0:     score += 3

    if   oi >= 10000: score += 18; reasons.append(f'OI {oi:,} (deep liquid)')
    elif oi >= 2000:  score += 12; reasons.append(f'OI {oi:,}')
    elif oi >= 500:   score += 6;  reasons.append(f'OI {oi:,}')

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

    mid = (bid + ask) / 2
    sp  = (ask - bid) / mid if mid > 0 else 1
    if sp < 0.08:   score += 10; reasons.append('Tight spread')
    elif sp < 0.15: score += 5
    elif sp > 0.40: score -= 8;  reasons.append('Wide spread')

    if iv > 2.0:     score -= 15; reasons.append('Very high IV')
    elif iv > 1.2:   score -= 5;  reasons.append('Elevated IV')
    elif 0.2 <= iv <= 0.7: score += 8; reasons.append('Reasonable IV')

    if oi > 0 and (vol / oi) > 0.2:
        score += 8; reasons.append('High V/OI ratio')

    return max(score, 0), reasons


# ─── Core analyze function (reused for single + batch) ────────────────────────

def analyze_ticker(ticker):
    ticker = ticker.upper().strip()
    stock = yf.Ticker(ticker)

    info = {}
    try: info = stock.info or {}
    except Exception: pass

    current_price = info.get('currentPrice') or info.get('regularMarketPrice') or info.get('previousClose')
    hist = stock.history(period='3mo', auto_adjust=True)
    if hist.empty:
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
        exp_dates = stock.options
    except Exception:
        exp_dates = []

    today = datetime.now()
    best_calls = []
    for ds in exp_dates[:5]:
        exp = datetime.strptime(ds, '%Y-%m-%d')
        dte = (exp - today).days
        if dte < 0 or dte > 21: continue
        try:
            chain = stock.option_chain(ds).calls
            if chain.empty: continue
            chain = chain[(chain['strike'] >= current_price * 0.85) & (chain['strike'] <= current_price * 1.15)]
            for _, row in chain.iterrows():
                res = score_call(row.to_dict(), current_price, supports, resistances, dte)
                if not res: continue
                sc, reasons = res
                if sc < 15: continue
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

    return {
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


# ─── Best Picks ───────────────────────────────────────────────────────────────

SCAN_TICKERS = [
    'SPY','QQQ','AAPL','NVDA','MSFT','TSLA','AMD','AMZN',
    'META','GOOGL','COIN','PLTR','MSTR','NFLX','IWM',
    'BAC','JPM','GLD','SOFI','HOOD',
]
PICKS_TTL = 60  # seconds

_picks_cache = {'data': None, 'ts': 0, 'refreshing': False}
_picks_lock  = threading.Lock()


def compute_confidence(score, vol_signal, reasons):
    base = min(score / 1.15, 80.0)
    if vol_signal == 'High':     base += 12
    elif vol_signal == 'Elevated': base += 6
    base += min(len(reasons) * 2, 10)
    return min(round(base), 95)


def conviction_label(conf):
    if conf >= 80: return 'Strong'
    if conf >= 65: return 'High'
    if conf >= 50: return 'Moderate'
    return 'Speculative'


def _scan_one(ticker):
    try:
        r = analyze_ticker(ticker)
        if r.get('error') or not r.get('top_calls'):
            return None
        top     = r['top_calls'][0]
        vol_sig = r['volume']['signal']
        conf    = compute_confidence(top['score'], vol_sig, top['reasons'])
        return {
            'ticker':       ticker,
            'company_name': r['company_name'],
            'current_price': r['current_price'],
            'strike':  top['strike'],
            'expiry':  top['expiry'],
            'dte':     top['dte'],
            'bid':     top['bid'],
            'ask':     top['ask'],
            'mid':     top['mid'],
            'volume':  top['volume'],
            'open_interest': top['open_interest'],
            'iv':      top['iv'],
            'score':   top['score'],
            'confidence': conf,
            'conviction': conviction_label(conf),
            'signals': top['reasons'][:4],
            'volume_signal': vol_sig,
            'itm': top.get('itm', False),
        }
    except Exception as e:
        logger.warning('picks scan %s: %s', ticker, e)
        return None


def _do_refresh():
    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_scan_one, t): t for t in SCAN_TICKERS}
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                results.append(r)
    results.sort(key=lambda x: x['confidence'], reverse=True)
    out = {
        'picks':      results[:10],
        'scanned':    len(SCAN_TICKERS),
        'updated_at': time.time(),
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


@app.route('/api/health')
def health():
    return jsonify({'ok': True})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)
