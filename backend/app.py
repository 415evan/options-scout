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
import math
import platform
import logging
import os
import re
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def _sf(v, default=0.0):
    """NaN/None-safe float conversion — numpy.nan is truthy so `x or 0` doesn't work."""
    try:
        f = float(v)
        return default if (f != f) else f  # f != f is True only for NaN
    except (TypeError, ValueError):
        return default


# ─── yfinance retry + per-ticker cache ───────────────────────────────────────

# Global semaphore: at most 2 yfinance network calls at once across all threads
_yf_sem = threading.Semaphore(2)

def _yf_call(fn, retries=6, base_delay=4.0):
    """Call a yfinance function, retrying on 429 / rate-limit errors.
    Holds a global semaphore so at most 2 threads hit yfinance simultaneously.
    IMPORTANT: sleep happens *outside* the semaphore so other threads can proceed."""
    for attempt in range(retries):
        rate_limited = False
        delay = 0.0
        with _yf_sem:
            try:
                return fn()
            except Exception as e:
                msg = str(e).lower()
                if any(x in msg for x in ('too many requests', '429', 'rate limit', 'rate_limit', 'temporarily')):
                    if attempt < retries - 1:
                        delay = min(base_delay * (2 ** attempt), 60)  # cap at 60s
                        rate_limited = True
                    else:
                        raise
                else:
                    raise
        # Sleep OUTSIDE the semaphore so other threads aren't blocked
        if rate_limited:
            logger.warning('yf rate limited — retry in %.0fs (attempt %d/%d)', delay, attempt + 1, retries)
            time.sleep(delay)
    return None


_ticker_cache      = {}
_ticker_cache_lock = threading.Lock()
TICKER_CACHE_TTL   = 600  # 10 min — reuse recent results, reduces rate limit hits


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


# ─── Technical Indicators ────────────────────────────────────────────────────

def compute_indicators(hist):
    """Compute MACD, Bollinger Bands, ATR, ADX from OHLCV history.

    Returns a dict with the most recent value of each. Uses pandas/numpy only.
    """
    if hist is None or len(hist) < 20:
        return {}

    close = hist['Close']
    high  = hist['High']  if 'High'  in hist.columns else close
    low   = hist['Low']   if 'Low'   in hist.columns else close

    out = {}

    # ── MACD (12, 26, 9) ──────────────────────────────────────────────────────
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line  = ema12 - ema26
    signal_ln  = macd_line.ewm(span=9, adjust=False).mean()
    histogram  = macd_line - signal_ln
    out['macd']        = round(float(macd_line.iloc[-1]),  3)
    out['macd_signal'] = round(float(signal_ln.iloc[-1]),  3)
    out['macd_hist']   = round(float(histogram.iloc[-1]),  3)
    # Bullish if MACD > signal AND histogram increasing
    prev_hist = float(histogram.iloc[-2]) if len(histogram) >= 2 else 0
    out['macd_bullish'] = bool(out['macd'] > out['macd_signal'] and out['macd_hist'] > prev_hist)

    # ── Bollinger Bands (20, 2σ) ──────────────────────────────────────────────
    sma20  = close.rolling(20).mean()
    std20  = close.rolling(20).std(ddof=0)
    bb_mid = float(sma20.iloc[-1])
    bb_up  = float((sma20 + 2 * std20).iloc[-1])
    bb_lo  = float((sma20 - 2 * std20).iloc[-1])
    price  = float(close.iloc[-1])
    # %B: where is price relative to bands  (0 = lower, 1 = upper)
    pct_b  = (price - bb_lo) / (bb_up - bb_lo) if (bb_up - bb_lo) > 0 else 0.5
    out.update({
        'bb_upper': round(bb_up, 2), 'bb_mid': round(bb_mid, 2),
        'bb_lower': round(bb_lo, 2), 'bb_pct':  round(pct_b, 3),
    })

    # ── ATR-14 (Average True Range) ───────────────────────────────────────────
    if len(hist) >= 14 and 'High' in hist.columns:
        prev_close = close.shift(1)
        tr = pd.concat([(high - low),
                        (high - prev_close).abs(),
                        (low  - prev_close).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        out['atr']     = round(float(atr.iloc[-1]), 2)
        out['atr_pct'] = round(float(atr.iloc[-1]) / price * 100, 2) if price > 0 else 0

    # ── ADX-14 (trend strength, 0-100) ────────────────────────────────────────
    if len(hist) >= 28 and 'High' in hist.columns:
        up_move   = high.diff()
        down_move = -low.diff()
        plus_dm   = ((up_move   > down_move) & (up_move   > 0)).astype(float) * up_move.clip(lower=0)
        minus_dm  = ((down_move > up_move)   & (down_move > 0)).astype(float) * down_move.clip(lower=0)
        atr_for_dx = tr.rolling(14).mean()
        plus_di   = 100 * (plus_dm.rolling(14).mean()  / atr_for_dx).fillna(0)
        minus_di  = 100 * (minus_dm.rolling(14).mean() / atr_for_dx).fillna(0)
        dx        = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx       = dx.rolling(14).mean()
        if not adx.empty and not pd.isna(adx.iloc[-1]):
            out['adx']      = round(float(adx.iloc[-1]), 1)
            out['plus_di']  = round(float(plus_di.iloc[-1]), 1)
            out['minus_di'] = round(float(minus_di.iloc[-1]), 1)

    return out


# ─── Trend Analysis ──────────────────────────────────────────────────────────

def compute_trend(hist):
    """
    Derive a trend score in [-100, +100] and a label from price history.
    Positive = bullish, negative = bearish, 0 = neutral.
    Factors: price vs SMA20/SMA50, 5-day & 10-day momentum, RSI-14.
    """
    if hist is None or len(hist) < 10:
        return 0, 'Neutral', {}

    closes = hist['Close'].dropna()
    if len(closes) < 5:
        return 0, 'Neutral', {}

    price  = float(closes.iloc[-1])
    sma20  = float(closes.rolling(min(20, len(closes))).mean().iloc[-1])
    sma50  = float(closes.rolling(min(50, len(closes))).mean().iloc[-1])

    score = 0

    # ── SMA alignment ─────────────────────────────────────────────────────────
    if price > sma20: score += 20
    else:             score -= 20
    if sma20 > sma50: score += 20    # golden-cross territory
    else:             score -= 20    # death-cross territory
    if price > sma50: score += 10
    else:             score -= 10

    # ── Recent momentum ───────────────────────────────────────────────────────
    ret5  = (price / float(closes.iloc[max(-6,  -len(closes))]) - 1) * 100
    ret10 = (price / float(closes.iloc[max(-11, -len(closes))]) - 1) * 100

    if   ret5 >  3: score += 20
    elif ret5 >  0: score += 10
    elif ret5 < -3: score -= 20
    else:           score -= 10

    if   ret10 >  5: score += 15
    elif ret10 >  0: score +=  8
    elif ret10 < -5: score -= 15
    else:            score -=  8

    # ── RSI-14 ────────────────────────────────────────────────────────────────
    delta = closes.diff().dropna()
    gain  = delta.clip(lower=0).rolling(14).mean().iloc[-1]
    loss  = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
    rsi   = 100 - (100 / (1 + gain / loss)) if loss and loss > 0 else 50.0

    if   rsi > 60: score += 15
    elif rsi > 50: score +=  5
    elif rsi < 40: score -= 15
    elif rsi < 50: score -=  5

    # ── MACD / ADX confirmation ──────────────────────────────────────────────
    indicators = compute_indicators(hist)
    if indicators:
        # MACD bullish/bearish cross
        if indicators.get('macd', 0) > indicators.get('macd_signal', 0):
            score += 8
        else:
            score -= 8
        # ADX strength weights the signal direction
        adx = indicators.get('adx', 0)
        plus_di  = indicators.get('plus_di',  0)
        minus_di = indicators.get('minus_di', 0)
        if adx >= 25:
            if plus_di > minus_di: score += 10
            else:                  score -= 10
        # Bollinger Bands — extreme positions
        bb_pct = indicators.get('bb_pct', 0.5)
        if   bb_pct > 0.95: score += 5    # near upper band
        elif bb_pct < 0.05: score -= 5    # near lower band

    score = max(-100, min(100, score))

    if   score >=  50: label = 'Bullish'
    elif score >=  20: label = 'Slightly Bullish'
    elif score <= -50: label = 'Bearish'
    elif score <= -20: label = 'Slightly Bearish'
    else:              label = 'Neutral'

    meta = {
        'score': score, 'label': label,
        'sma20': round(sma20, 2), 'sma50': round(sma50, 2),
        'rsi': round(rsi, 1),
        'ret5d': round(ret5, 2), 'ret10d': round(ret10, 2),
        **indicators,
    }
    return score, label, meta


def trend_score_modifier(trend_score, option_type):
    """
    Return (pts, reason) based on how aligned the option direction is with trend.
    Goes against trend → big penalty. Aligned → bonus.
    """
    if option_type == 'call':
        if   trend_score >=  50: return +25, 'Aligned with bullish trend'
        elif trend_score >=  25: return +12, 'Mild bullish tailwind'
        elif trend_score <= -50: return -45, 'Against strong bearish trend'
        elif trend_score <= -25: return -22, 'Headwind — stock in downtrend'
    else:  # put
        if   trend_score <= -50: return +25, 'Aligned with bearish trend'
        elif trend_score <= -25: return +12, 'Mild bearish tailwind'
        elif trend_score >=  50: return -45, 'Against strong bullish trend'
        elif trend_score >=  25: return -22, 'Headwind — stock in uptrend'
    return 0, ''


# ─── Option Scoring ───────────────────────────────────────────────────────────

# ─── Black-Scholes Greeks (pure Python — no scipy) ───────────────────────────

_RISK_FREE = 0.045   # approx 4.5% short-term rate

def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

def _norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2))) / 2.0

def compute_greeks(S, K, T_days, iv, option_type='call'):
    """
    Black-Scholes delta/gamma/theta/vega for a European option.
    Returns a dict, or None if inputs are degenerate.
      theta  — per-share per calendar-day (negative = option loses value)
      vega   — per-share per 1 percentage-point move in IV
    """
    if T_days <= 0 or iv <= 0 or S <= 0 or K <= 0:
        return None
    T = max(T_days, 0.5) / 365.0   # floor at half a day to avoid div-by-zero
    r = _RISK_FREE
    try:
        d1 = (math.log(S / K) + (r + 0.5 * iv**2) * T) / (iv * math.sqrt(T))
        d2 = d1 - iv * math.sqrt(T)
    except (ValueError, ZeroDivisionError):
        return None

    if option_type == 'call':
        delta = _norm_cdf(d1)
    else:
        delta = _norm_cdf(d1) - 1.0   # negative for puts

    gamma = _norm_pdf(d1) / (S * iv * math.sqrt(T))

    # Theta: full annual, then divide by 365 → per calendar day per share
    theta_yr = -(S * _norm_pdf(d1) * iv / (2.0 * math.sqrt(T)))
    if option_type == 'call':
        theta_yr -= r * K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        theta_yr += r * K * math.exp(-r * T) * _norm_cdf(-d2)
    theta = theta_yr / 365.0

    # Vega: per 1 pp move in IV (×0.01 converts from ∂/∂σ to ∂/∂(σ%))
    vega = S * _norm_pdf(d1) * math.sqrt(T) * 0.01

    return {
        'delta': round(delta, 3),
        'gamma': round(gamma, 5),
        'theta': round(theta, 4),   # $/share/day  (negative)
        'vega':  round(vega, 4),    # $/share per 1pp IV
    }


def compute_pop(greeks, option_type='call'):
    """
    Probability of expiring in-the-money under Black-Scholes:
      call: N(d2)
      put:  N(-d2) = 1 - N(d2)
    We approximate d2 from delta when full inputs aren't handy. Here we already
    have delta + iv; for accuracy compute from S/K/T/iv directly upstream.
    """
    if not greeks: return 0.0
    delta = greeks.get('delta', 0)
    if option_type == 'call':
        # Delta is approximately N(d1); POP is N(d2). For OTM options POP < |delta|.
        return max(0.0, min(1.0, abs(delta) * 0.92))  # rough approximation
    else:
        return max(0.0, min(1.0, abs(delta) * 0.92))


def compute_pop_exact(S, K, T_days, iv, option_type='call'):
    """Exact POP from Black-Scholes d2."""
    if T_days <= 0 or iv <= 0 or S <= 0 or K <= 0:
        return 0.0
    T = max(T_days, 0.5) / 365.0
    r = _RISK_FREE
    try:
        d1 = (math.log(S / K) + (r + 0.5 * iv**2) * T) / (iv * math.sqrt(T))
        d2 = d1 - iv * math.sqrt(T)
    except (ValueError, ZeroDivisionError):
        return 0.0
    return _norm_cdf(d2) if option_type == 'call' else _norm_cdf(-d2)


def compute_expected_value(S, K, T_days, iv, ask, option_type='call'):
    """
    Crude EV: probability-weighted payoff at expiry vs premium paid.
    Returns expected $ profit per contract (×100 shares).
    Uses log-normal price distribution under Black-Scholes assumptions.
    """
    if T_days <= 0 or iv <= 0 or S <= 0 or K <= 0 or ask <= 0:
        return None
    T = max(T_days, 0.5) / 365.0
    r = _RISK_FREE
    try:
        d1 = (math.log(S / K) + (r + 0.5 * iv**2) * T) / (iv * math.sqrt(T))
        d2 = d1 - iv * math.sqrt(T)
        # E[max(S_T - K, 0)] for call, K - S_T for put (Black-Scholes intrinsic)
        if option_type == 'call':
            intrinsic = S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
        else:
            intrinsic = K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)
    except (ValueError, ZeroDivisionError):
        return None
    ev_per_share = intrinsic - ask
    return round(ev_per_share * 100, 2)  # per contract


def compute_max_pain(chain_calls, chain_puts):
    """
    Max pain price: the strike where total option holder losses are minimized
    (i.e. where market makers minimize payouts at expiry).
    Returns (max_pain_strike, total_call_oi, total_put_oi).
    """
    if chain_calls is None or chain_puts is None or chain_calls.empty or chain_puts.empty:
        return None, 0, 0

    call_oi_total = int(chain_calls['openInterest'].fillna(0).sum())
    put_oi_total  = int(chain_puts['openInterest'].fillna(0).sum())

    # Build clean arrays — drop rows with NaN strikes, coerce OI to safe int
    c_strikes = chain_calls['strike'].dropna().values
    c_oi      = chain_calls['openInterest'].fillna(0).values
    p_strikes = chain_puts['strike'].dropna().values
    p_oi      = chain_puts['openInterest'].fillna(0).values

    strikes = sorted(set(c_strikes.tolist() + p_strikes.tolist()))
    if not strikes:
        return None, call_oi_total, put_oi_total

    # Pre-build clean float arrays for fast iteration
    c_s = [float(s) for s in c_strikes]
    c_o = [_sf(o) for o in c_oi]
    p_s = [float(s) for s in p_strikes]
    p_o = [_sf(o) for o in p_oi]

    best_strike, best_pain = None, float('inf')
    for K in strikes:
        K = float(K)
        call_pain = sum(max(0.0, K - s) * o for s, o in zip(c_s, c_o))
        put_pain  = sum(max(0.0, s - K) * o for s, o in zip(p_s, p_o))
        total = call_pain + put_pain
        # Guard: NaN totals (from bad data) must not win
        if total == total and total < best_pain:
            best_pain, best_strike = total, K

    if best_strike is None:
        # Fallback: use the median strike
        best_strike = strikes[len(strikes) // 2]

    return round(float(best_strike), 2), call_oi_total, put_oi_total


def compute_pc_ratio(chain_calls, chain_puts):
    """Put/Call ratio by volume and by open interest."""
    if chain_calls is None or chain_puts is None:
        return {'pc_volume': 0.0, 'pc_oi': 0.0}
    cv = float(chain_calls['volume'].fillna(0).sum())       if not chain_calls.empty else 0
    pv = float(chain_puts['volume'].fillna(0).sum())        if not chain_puts.empty  else 0
    co = float(chain_calls['openInterest'].fillna(0).sum()) if not chain_calls.empty else 0
    po = float(chain_puts['openInterest'].fillna(0).sum())  if not chain_puts.empty  else 0
    return {
        'pc_volume': round(pv / cv, 3) if cv > 0 else 0.0,
        'pc_oi':     round(po / co, 3) if co > 0 else 0.0,
        'call_volume': int(cv), 'put_volume': int(pv),
        'call_oi':     int(co), 'put_oi':     int(po),
    }


# ─── Liquidity guardrails ────────────────────────────────────────────────────

MIN_OI         = 50      # minimum open interest to consider tradeable
MAX_SPREAD_PCT = 0.30    # max bid-ask spread as % of mid

def liquidity_grade(bid, ask, oi):
    """
    Returns ('A'|'B'|'C'|'F', score_adjust, reason).
    Hard filter F = effectively un-tradeable for retail.
    """
    if ask <= 0: return 'F', -100, 'No ask price'
    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid if mid > 0 else 1.0

    if oi < MIN_OI:
        return 'F', -60, f'OI {oi} — illiquid, hard to exit'
    if spread_pct > MAX_SPREAD_PCT:
        return 'F', -55, f'Spread {spread_pct*100:.0f}% — unfillable'

    if oi >= 1000 and spread_pct < 0.05:
        return 'A', +6, 'Deep liquid, tight spread'
    if oi >= 500 and spread_pct < 0.10:
        return 'B', +2, ''
    if oi >= 100 and spread_pct < 0.20:
        return 'C', 0, ''
    return 'C', -8, f'Marginal liquidity'


# ─── IV Rank / Percentile — SQLite-backed ────────────────────────────────────

import sqlite3

_DB_PATH = os.path.expanduser('~/.optionsscout/optionsscout.db')
os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
_db_lock = threading.Lock()

def _db():
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
    """Create tables if they don't exist."""
    with _db_lock, _db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS iv_history (
            ticker TEXT, date TEXT, iv REAL,
            PRIMARY KEY (ticker, date))""")
        c.execute("""CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            option_type TEXT NOT NULL,        -- 'call' | 'put'
            strike REAL NOT NULL,
            expiry TEXT NOT NULL,
            entry_date TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_date TEXT, exit_price REAL,
            contracts INTEGER NOT NULL,
            thesis TEXT,
            tags TEXT,                        -- comma-separated
            score_at_entry INTEGER,
            paper INTEGER DEFAULT 0,          -- 1 = paper trade
            closed INTEGER DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            option_type TEXT NOT NULL,
            strike REAL NOT NULL,
            expiry TEXT NOT NULL,
            entry_price REAL NOT NULL,
            contracts INTEGER NOT NULL,
            opened_at TEXT NOT NULL
        )""")
        c.commit()

_init_db()


def record_iv_today(ticker, atm_iv):
    """Persist today's ATM IV for later IV rank/percentile calc."""
    if atm_iv is None or atm_iv <= 0: return
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        with _db_lock, _db() as c:
            c.execute("INSERT OR REPLACE INTO iv_history(ticker, date, iv) VALUES (?,?,?)",
                      (ticker, today, float(atm_iv)))
            c.commit()
    except Exception as e:
        logger.warning('record_iv_today %s: %s', ticker, e)


def compute_iv_rank(ticker, current_iv):
    """
    IV Rank (0-100): where current IV sits between the 52-week min and max.
    IV Percentile: % of days in last 252 where IV was BELOW current.
    """
    if current_iv is None or current_iv <= 0:
        return None
    try:
        with _db_lock, _db() as c:
            rows = c.execute("""SELECT iv FROM iv_history WHERE ticker=?
                                ORDER BY date DESC LIMIT 252""", (ticker,)).fetchall()
    except Exception:
        return None

    ivs = [float(r['iv']) for r in rows if r['iv']]
    if len(ivs) < 5:
        return {'rank': None, 'percentile': None, 'days': len(ivs),
                'min': None, 'max': None, 'note': 'Building history…'}

    iv_min = min(ivs); iv_max = max(ivs)
    rank = (current_iv - iv_min) / (iv_max - iv_min) * 100 if iv_max > iv_min else 50
    pct  = sum(1 for v in ivs if v < current_iv) / len(ivs) * 100
    return {
        'rank':       round(max(0, min(100, rank)), 1),
        'percentile': round(pct, 1),
        'days':       len(ivs),
        'min':        round(iv_min * 100, 1),
        'max':        round(iv_max * 100, 1),
        'current':    round(current_iv * 100, 1),
    }


# ─── Earnings & Dividend Awareness ───────────────────────────────────────────

def detect_earnings_in_window(stock, target_date):
    """
    Returns earnings date if it falls before target_date, else None.
    Returns string YYYY-MM-DD or None.
    """
    try:
        cal = _yf_call(lambda: stock.calendar)
        if cal is None: return None
        # yfinance .calendar can be a DataFrame or dict depending on version
        if hasattr(cal, 'to_dict'):
            cal = cal.to_dict()
        # Try common keys
        for key in ('Earnings Date', 'earningsDate', 'Earnings Average'):
            v = cal.get(key) if isinstance(cal, dict) else None
            if v is None: continue
            if isinstance(v, list) and v:
                v = v[0]
            try:
                if hasattr(v, 'strftime'):
                    edate = v.strftime('%Y-%m-%d')
                else:
                    edate = str(v)[:10]
                # Compare to target_date (string YYYY-MM-DD)
                if edate >= datetime.now().strftime('%Y-%m-%d') and edate <= target_date:
                    return edate
            except Exception:
                continue
    except Exception as e:
        logger.debug('earnings detect %s: %s', getattr(stock, 'ticker', '?'), e)
    return None


def _greek_score(greeks, ask, option_type='call'):
    """
    Extra score adjustments derived purely from greeks.
    Returns (score_delta, reasons_list).
    """
    if not greeks:
        return 0, []

    score, reasons = 0, []
    delta = greeks['delta']
    theta = greeks['theta']
    vega  = greeks['vega']
    abs_delta = abs(delta)

    # ── Delta: probability-of-profit proxy ───────────────────────────────────
    if   0.30 <= abs_delta <= 0.50:
        score += 20; reasons.append(f'Δ {delta:+.2f} (sweet spot)')
    elif 0.20 <= abs_delta <  0.30:
        score += 12; reasons.append(f'Δ {delta:+.2f}')
    elif 0.15 <= abs_delta <  0.20:
        score +=  5; reasons.append(f'Δ {delta:+.2f} (aggressive)')
    elif 0.50 <  abs_delta <= 0.65:
        score += 10; reasons.append(f'Δ {delta:+.2f} (high prob)')
    elif abs_delta < 0.10:
        score -= 30; reasons.append(f'Δ {delta:+.2f} — near-zero probability')
    elif abs_delta < 0.15:
        score -= 12; reasons.append(f'Δ {delta:+.2f} — very low probability')

    # ── Theta decay: daily bleed as % of premium ──────────────────────────────
    if ask > 0:
        theta_ratio = abs(theta) / ask   # fraction of premium lost per day
        if   theta_ratio < 0.020:
            score += 12; reasons.append(f'Θ {theta_ratio*100:.1f}%/day (slow decay)')
        elif theta_ratio < 0.050:
            score +=  5; reasons.append(f'Θ {theta_ratio*100:.1f}%/day')
        elif theta_ratio < 0.100:
            score -= 10; reasons.append(f'Θ {theta_ratio*100:.1f}%/day (fast decay)')
        elif theta_ratio < 0.200:
            score -= 25; reasons.append(f'Θ {theta_ratio*100:.1f}%/day — severe decay')
        else:
            score -= 45; reasons.append(f'Θ {theta_ratio*100:.0f}%/day — expires worthless')

    # ── Vega / IV crush risk ──────────────────────────────────────────────────
    if ask > 0:
        vega_ratio = vega / ask
        if   vega_ratio > 0.40:
            score -= 20; reasons.append(f'High IV crush risk (V/ask {vega_ratio:.2f})')
        elif vega_ratio > 0.20:
            score -= 10; reasons.append(f'Elevated IV crush risk')

    return score, reasons


def score_call(opt, current_price, supports, resistances, dte, greeks=None):
    strike = _sf(opt.get('strike'))
    bid    = _sf(opt.get('bid'))
    ask    = _sf(opt.get('ask'))
    vol    = int(_sf(opt.get('volume')))
    oi     = int(_sf(opt.get('openInterest')))
    iv     = _sf(opt.get('impliedVolatility'))

    if strike <= 0 or ask <= 0:
        return None

    score, reasons = 0, []

    # ── Moneyness ────────────────────────────────────────────────────────────
    # Favor realistic targets: 2-6% OTM is the sweet spot; high OTM only makes
    # sense with more time — penalised below via DTE×OTM interaction.
    otm_pct = (strike - current_price) / current_price * 100
    if   2 < otm_pct <=  5:   score += 30; reasons.append(f'{otm_pct:.1f}% OTM — realistic target')
    elif  5 < otm_pct <= 10:  score += 22; reasons.append(f'{otm_pct:.1f}% OTM')
    elif -1 <= otm_pct <= 2:  score += 18; reasons.append('ATM strike')
    elif 10 < otm_pct <= 20:  score += 14; reasons.append(f'{otm_pct:.1f}% OTM — breakout play')
    elif -5 <= otm_pct < -1:  score +=  8; reasons.append(f'ITM {abs(otm_pct):.1f}%')
    elif 20 < otm_pct <= 40:  score +=  8; reasons.append(f'{otm_pct:.1f}% OTM — lottery')
    elif otm_pct > 40:        score +=  4; reasons.append(f'{otm_pct:.1f}% OTM — deep lottery')
    else:                      score +=  3

    # ── DTE ──────────────────────────────────────────────────────────────────
    if   2 <= dte <= 5:   score += 18; reasons.append(f'{dte}d expiry (weekly)')
    elif dte == 1:        score += 8;  reasons.append('1d expiry (high gamma)')
    elif  5 < dte <= 10:  score += 16; reasons.append(f'{dte}d expiry')
    elif 10 < dte <= 21:  score += 14; reasons.append(f'{dte}d expiry')
    elif 21 < dte <= 45:  score += 18; reasons.append(f'{dte}d expiry (swing)')
    elif 45 < dte <= 60:  score += 14; reasons.append(f'{dte}d expiry (swing)')

    # ── DTE × OTM interaction penalty ────────────────────────────────────────
    # Short-dated options need to be close to the money to have real probability.
    # Realistic max OTM scales with sqrt(DTE): ~3% at 1d, ~6% at 4d, ~9% at 9d.
    if otm_pct > 0 and dte > 0:
        max_reasonable = 3.0 * (dte ** 0.5)          # e.g. 1d→3%, 4d→6%, 9d→9%
        if otm_pct > max_reasonable * 2.5:
            pen = min(70, int((otm_pct - max_reasonable * 2.5) * 4) + 30)
            score -= pen; reasons.append(f'Way too far OTM for {dte}d (-{pen}pts)')
        elif otm_pct > max_reasonable * 1.6:
            pen = min(40, int((otm_pct - max_reasonable * 1.6) * 3) + 10)
            score -= pen; reasons.append(f'Too far OTM for {dte}d (-{pen}pts)')

    # ── Premium ───────────────────────────────────────────────────────────────
    # Cheap premium is good, but don't bonus sub-$0.20 lottery tickets heavily
    if   ask <= 0.50: score += 16; reasons.append(f'Cheap premium ${ask:.2f}')
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

    # ── Greeks ────────────────────────────────────────────────────────────────
    g_score, g_reasons = _greek_score(greeks, ask, 'call')
    score += g_score; reasons.extend(g_reasons)

    return max(score, 0), reasons


def score_put(opt, current_price, supports, resistances, dte, greeks=None):
    """Score a put option — mirror of score_call but for bearish plays."""
    strike = _sf(opt.get('strike'))
    bid    = _sf(opt.get('bid'))
    ask    = _sf(opt.get('ask'))
    vol    = int(_sf(opt.get('volume')))
    oi     = int(_sf(opt.get('openInterest')))
    iv     = _sf(opt.get('impliedVolatility'))

    if strike <= 0 or ask <= 0:
        return None

    score, reasons = 0, []

    # ── Moneyness (OTM = strike below current price) ──────────────────────────
    otm_pct = (current_price - strike) / current_price * 100
    if   2 < otm_pct <=  5:   score += 30; reasons.append(f'{otm_pct:.1f}% OTM — realistic target')
    elif  5 < otm_pct <= 10:  score += 22; reasons.append(f'{otm_pct:.1f}% OTM')
    elif -1 <= otm_pct <= 2:  score += 18; reasons.append('ATM strike')
    elif 10 < otm_pct <= 20:  score += 14; reasons.append(f'{otm_pct:.1f}% OTM — breakdown play')
    elif -5 <= otm_pct < -1:  score +=  8; reasons.append(f'ITM {abs(otm_pct):.1f}%')
    elif 20 < otm_pct <= 40:  score +=  8; reasons.append(f'{otm_pct:.1f}% OTM — deep put')
    elif otm_pct > 40:        score +=  4; reasons.append(f'{otm_pct:.1f}% OTM — deep lottery')
    else:                      score +=  3

    # ── DTE ──────────────────────────────────────────────────────────────────
    if   2 <= dte <= 5:   score += 18; reasons.append(f'{dte}d expiry (weekly)')
    elif dte == 1:        score += 8;  reasons.append('1d expiry (high gamma)')
    elif  5 < dte <= 10:  score += 16; reasons.append(f'{dte}d expiry')
    elif 10 < dte <= 21:  score += 14; reasons.append(f'{dte}d expiry')
    elif 21 < dte <= 45:  score += 18; reasons.append(f'{dte}d expiry (swing)')
    elif 45 < dte <= 60:  score += 14; reasons.append(f'{dte}d expiry (swing)')

    # ── DTE × OTM interaction penalty ────────────────────────────────────────
    if otm_pct > 0 and dte > 0:
        max_reasonable = 3.0 * (dte ** 0.5)
        if otm_pct > max_reasonable * 2.5:
            pen = min(70, int((otm_pct - max_reasonable * 2.5) * 4) + 30)
            score -= pen; reasons.append(f'Way too far OTM for {dte}d (-{pen}pts)')
        elif otm_pct > max_reasonable * 1.6:
            pen = min(40, int((otm_pct - max_reasonable * 1.6) * 3) + 10)
            score -= pen; reasons.append(f'Too far OTM for {dte}d (-{pen}pts)')

    # ── Premium ───────────────────────────────────────────────────────────────
    if   ask <= 0.50: score += 16; reasons.append(f'Cheap premium ${ask:.2f}')
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

    # ── Bearish setup: near resistance is good for puts ───────────────────────
    for lvl in resistances:
        prox = abs(current_price - lvl['price']) / current_price
        if prox < 0.025:
            score += 12 * min(lvl['strength'], 3)
            reasons.append(f"Near resistance ${lvl['price']} — bearish setup"); break
        elif prox < 0.05:
            score += 6
            reasons.append(f"Near resistance ${lvl['price']}"); break

    # Strike near support = natural target for the move
    for lvl in supports:
        if abs(strike - lvl['price']) / strike < 0.025:
            score += 10
            reasons.append(f"Strike at support ${lvl['price']}"); break

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

    # ── Greeks ────────────────────────────────────────────────────────────────
    g_score, g_reasons = _greek_score(greeks, ask, 'put')
    score += g_score; reasons.extend(g_reasons)

    return max(score, 0), reasons


# ─── Vertical Spread Scoring & Generation ────────────────────────────────────

def _score_spread(spread_type, current_price, breakeven, net_cost, max_profit, max_loss,
                  rr_ratio, dte, iv, long_oi, short_oi, t_score, near_earn=None, pop=None):
    """Score a vertical spread. Returns (score, reasons)."""
    score, reasons = 0, []

    be_pct = abs(breakeven - current_price) / current_price * 100

    # ── DTE × Breakeven interaction (must pass first) ─────────────────────────
    # Same formula as single-leg: realistic move ≈ 3√DTE %
    if dte > 0:
        max_reasonable_pct = 3.0 * (dte ** 0.5)
        if be_pct > max_reasonable_pct * 2.5:
            pen = min(75, int((be_pct - max_reasonable_pct * 2.5) * 5) + 35)
            score -= pen; reasons.append(f'Breakeven {be_pct:.0f}% from stock — unreachable in {dte}d (-{pen}pts)')
        elif be_pct > max_reasonable_pct * 1.6:
            pen = min(40, int((be_pct - max_reasonable_pct * 1.6) * 3) + 12)
            score -= pen; reasons.append(f'Breakeven too far for {dte}d (-{pen}pts)')

    # ── R/R — capped bonus when POP is very low ───────────────────────────────
    pop_frac = pop if pop is not None else 0.5
    rr_cap = 3.0 if pop_frac < 0.25 else 5.0   # discount extreme R/R at low probability
    effective_rr = min(rr_ratio, rr_cap) if net_cost > 0 else rr_ratio
    if   effective_rr >= 2.5: score += 35; reasons.append(f'R/R {rr_ratio:.1f}:1')
    elif effective_rr >= 1.5: score += 22; reasons.append(f'R/R {rr_ratio:.1f}:1')
    elif effective_rr >= 1.0: score += 10; reasons.append(f'R/R {rr_ratio:.1f}:1')
    else:                     score -= 15; reasons.append(f'Poor R/R {rr_ratio:.1f}:1')

    # ── POP ───────────────────────────────────────────────────────────────────
    if pop is not None:
        p = pop_frac * 100
        if   p >= 60: score += 25; reasons.append(f'POP {p:.0f}% — high probability')
        elif p >= 45: score += 15; reasons.append(f'POP {p:.0f}%')
        elif p >= 30: score +=  5; reasons.append(f'POP {p:.0f}%')
        elif p >= 20: score -=  8
        else:         score -= 25; reasons.append(f'POP {p:.0f}% — long shot')

    # ── Breakeven distance ────────────────────────────────────────────────────
    if   be_pct <= 1.0: score += 25; reasons.append(f'Breakeven only {be_pct:.1f}% away')
    elif be_pct <= 3.0: score += 16; reasons.append(f'Breakeven {be_pct:.1f}% away')
    elif be_pct <= 6.0: score +=  8; reasons.append(f'Breakeven {be_pct:.1f}% away')
    elif be_pct <= 10.0: score +=  2

    # ── DTE (21-45d is optimal for spreads) ───────────────────────────────────
    if   21 <= dte <= 45: score += 22; reasons.append(f'{dte}d — ideal spread timing')
    elif 14 <= dte <  21: score += 15; reasons.append(f'{dte}d expiry')
    elif 45 < dte <= 60:  score += 14
    elif  7 <= dte <  14: score +=  8; reasons.append(f'{dte}d — short-dated')
    elif dte <  7:        score -= 15; reasons.append(f'Only {dte}d — gamma risk')

    # ── Net cost / credit ─────────────────────────────────────────────────────
    if net_cost > 0:   # debit
        if   net_cost <= 0.75: score += 15; reasons.append(f'Cheap debit ${net_cost:.2f}')
        elif net_cost <= 1.50: score +=  8; reasons.append(f'Low debit ${net_cost:.2f}')
        elif net_cost <= 3.00: score +=  3
        elif net_cost >  5.00: score -= 10
    else:              # credit received
        cr = abs(net_cost)
        if   cr >= 1.00: score += 15; reasons.append(f'Strong credit ${cr:.2f}/share')
        elif cr >= 0.50: score += 8;  reasons.append(f'Credit ${cr:.2f}/share')

    # ── Liquidity (both legs) ─────────────────────────────────────────────────
    min_oi = min(long_oi, short_oi)
    if   min_oi >= 1000: score += 18; reasons.append('Both legs deep liquid')
    elif min_oi >=  500: score += 12; reasons.append('Both legs liquid')
    elif min_oi >=  100: score +=  5
    elif min_oi <    50: score -= 25; reasons.append('Thin leg — hard to exit')

    # ── Trend alignment ───────────────────────────────────────────────────────
    is_bull = spread_type in ('bull_call', 'bull_put')
    t_pts, t_reason = trend_score_modifier(t_score, 'call' if is_bull else 'put')
    score += t_pts
    if t_reason: reasons.append(t_reason)

    # ── IV environment ────────────────────────────────────────────────────────
    if   0.15 <= iv <= 0.50: score +=  8; reasons.append('Healthy IV for spreads')
    elif iv > 0.80:           score -=  5; reasons.append('High IV — wide natural spreads')

    # ── Earnings ──────────────────────────────────────────────────────────────
    if near_earn:
        score -= 12; reasons.append(f'Earnings {near_earn} before expiry')

    return max(0, score), reasons


def generate_vertical_spreads(calls_df, puts_df, current_price, dte, ds, t_score,
                               ticker, near_earn=None):
    """
    Generate all four vertical spread types (Bull Call, Bear Put, Bull Put, Bear Call)
    from the raw option chain DataFrames for a single expiry date.
    Returns a list of scored dicts, sorted best-first, capped at 30.
    """
    if not current_price or current_price <= 0:
        return []

    spreads = []
    max_width = current_price * 0.15   # cap spread width at 15% of stock price
    min_width = max(0.50, current_price * 0.005)

    def _clean(df, lo_pct, hi_pct):
        """Return sorted list of clean dicts filtered to a strike range."""
        if df is None or df.empty:
            return []
        lo, hi = current_price * lo_pct, current_price * hi_pct
        out = []
        for _, row in df.iterrows():
            opt = row.to_dict()
            strike = _sf(opt.get('strike'))
            if strike < lo or strike > hi:
                continue
            bid  = _sf(opt.get('bid'))
            ask  = _sf(opt.get('ask'))
            last = _sf(opt.get('lastPrice'))
            oi   = int(_sf(opt.get('openInterest')))
            iv   = _sf(opt.get('impliedVolatility'))
            if bid <= 0 and ask <= 0 and last > 0:
                bid = round(last * 0.95, 2); ask = round(last * 1.05, 2)
            if ask <= 0:
                continue
            out.append({'strike': strike, 'bid': bid, 'ask': ask, 'oi': oi, 'iv': iv})
        return sorted(out, key=lambda x: x['strike'])

    def _mk(stype, direct, long_s, short_s, net_cost, mp, ml, be, rr, iv_avg, l_oi, s_oi, pop, sc, reas, l, s):
        return {
            'spread_type': stype, 'direction': direct,
            'long_strike': long_s, 'short_strike': short_s,
            'expiry': ds, 'dte': dte,
            'net_cost': net_cost,
            'max_profit_per_contract': round(mp * 100, 0),
            'max_loss_per_contract':   round(ml * 100, 0),
            'breakeven': be, 'rr_ratio': rr,
            'width': round(abs(short_s - long_s), 2),
            'pop': round(max(0.0, min(1.0, pop)) * 100, 1),
            'score': sc, 'reasons': reas,
            'long_bid': l['bid'], 'long_ask': l['ask'],
            'short_bid': s['bid'], 'short_ask': s['ask'],
            'long_oi': l_oi, 'short_oi': s_oi,
            'iv': round((iv_avg or 0) * 100, 1),
            'near_earnings': near_earn,
        }

    calls = _clean(calls_df, 0.85, 1.30)
    puts  = _clean(puts_df,  0.70, 1.10)

    # ── Bull Call Spread (debit, bullish) ─────────────────────────────────────
    for i, lg in enumerate(calls):
        if lg['strike'] > current_price * 1.08: continue
        if lg['oi'] < MIN_OI: continue
        for sh in calls[i+1:]:
            w = sh['strike'] - lg['strike']
            if w > max_width: break
            if w < min_width: continue
            if sh['oi'] < MIN_OI: continue
            nd = round(lg['ask'] - sh['bid'], 2)
            if nd < 0.05: continue
            mp = round(w - nd, 2)
            if mp <= 0: continue
            rr = round(mp / nd, 2)
            be = round(lg['strike'] + nd, 2)
            iv = (lg['iv'] + sh['iv']) / 2 if lg['iv'] and sh['iv'] else (lg['iv'] or 0.3)
            pop = compute_pop_exact(current_price, be, dte, iv, 'call')
            sc, reas = _score_spread('bull_call', current_price, be, nd, mp, nd, rr, dte, iv, lg['oi'], sh['oi'], t_score, near_earn, pop)
            spreads.append(_mk('Bull Call', 'bullish', lg['strike'], sh['strike'], nd, mp, nd, be, rr, iv, lg['oi'], sh['oi'], pop, sc, reas, lg, sh))

    # ── Bear Put Spread (debit, bearish) ──────────────────────────────────────
    puts_rev = list(reversed(puts))
    for i, lg in enumerate(puts_rev):
        if lg['strike'] < current_price * 0.92: continue
        if lg['oi'] < MIN_OI: continue
        for sh in puts_rev[i+1:]:
            w = lg['strike'] - sh['strike']
            if w > max_width: break
            if w < min_width: continue
            if sh['oi'] < MIN_OI: continue
            nd = round(lg['ask'] - sh['bid'], 2)
            if nd < 0.05: continue
            mp = round(w - nd, 2)
            if mp <= 0: continue
            rr = round(mp / nd, 2)
            be = round(lg['strike'] - nd, 2)
            iv = (lg['iv'] + sh['iv']) / 2 if lg['iv'] and sh['iv'] else (lg['iv'] or 0.3)
            pop = compute_pop_exact(current_price, be, dte, iv, 'put')
            sc, reas = _score_spread('bear_put', current_price, be, nd, mp, nd, rr, dte, iv, lg['oi'], sh['oi'], t_score, near_earn, pop)
            spreads.append(_mk('Bear Put', 'bearish', lg['strike'], sh['strike'], nd, mp, nd, be, rr, iv, lg['oi'], sh['oi'], pop, sc, reas, lg, sh))

    # ── Bull Put Spread (credit, bullish) ─────────────────────────────────────
    for i, sh in enumerate(puts_rev):          # sh = higher strike (sell)
        if sh['strike'] > current_price * 1.05: continue
        if sh['strike'] < current_price * 0.88: continue
        if sh['oi'] < MIN_OI or sh['bid'] <= 0: continue
        for lg in puts_rev[i+1:]:              # lg = lower strike (buy)
            w = sh['strike'] - lg['strike']
            if w > max_width: break
            if w < min_width: continue
            if lg['oi'] < MIN_OI: continue
            nc = round(sh['bid'] - lg['ask'], 2)
            if nc < 0.05: continue
            ml = round(w - nc, 2)
            if ml <= 0: continue
            rr = round(nc / ml, 2)
            be = round(sh['strike'] - nc, 2)
            iv = (sh['iv'] + lg['iv']) / 2 if sh['iv'] and lg['iv'] else (sh['iv'] or 0.3)
            pop = 1.0 - compute_pop_exact(current_price, be, dte, iv, 'put')
            sc, reas = _score_spread('bull_put', current_price, be, -nc, nc, ml, rr, dte, iv, sh['oi'], lg['oi'], t_score, near_earn, pop)
            reas = [f'Credit ${nc:.2f}/sh (${round(nc*100):.0f}/contract)'] + reas
            spreads.append(_mk('Bull Put', 'bullish', lg['strike'], sh['strike'], -nc, nc, ml, be, rr, iv, lg['oi'], sh['oi'], pop, sc, reas, lg, sh))

    # ── Bear Call Spread (credit, bearish) ────────────────────────────────────
    for i, sh in enumerate(calls):             # sh = lower strike (sell)
        if sh['strike'] < current_price * 0.95: continue
        if sh['strike'] > current_price * 1.08: continue
        if sh['oi'] < MIN_OI or sh['bid'] <= 0: continue
        for lg in calls[i+1:]:                 # lg = higher strike (buy)
            w = lg['strike'] - sh['strike']
            if w > max_width: break
            if w < min_width: continue
            if lg['oi'] < MIN_OI: continue
            nc = round(sh['bid'] - lg['ask'], 2)
            if nc < 0.05: continue
            ml = round(w - nc, 2)
            if ml <= 0: continue
            rr = round(nc / ml, 2)
            be = round(sh['strike'] + nc, 2)
            iv = (sh['iv'] + lg['iv']) / 2 if sh['iv'] and lg['iv'] else (sh['iv'] or 0.3)
            pop = 1.0 - compute_pop_exact(current_price, be, dte, iv, 'call')
            sc, reas = _score_spread('bear_call', current_price, be, -nc, nc, ml, rr, dte, iv, sh['oi'], lg['oi'], t_score, near_earn, pop)
            reas = [f'Credit ${nc:.2f}/sh (${round(nc*100):.0f}/contract)'] + reas
            spreads.append(_mk('Bear Call', 'bearish', sh['strike'], lg['strike'], -nc, nc, ml, be, rr, iv, sh['oi'], lg['oi'], pop, sc, reas, sh, lg))

    spreads.sort(key=lambda x: x['score'], reverse=True)
    return spreads[:30]


# ─── Chart Pattern Detection ─────────────────────────────────────────────────

def detect_chart_patterns(hist, current_price, supports, resistances):
    """Detect common chart patterns from OHLCV history.
    Returns list of dicts: {pattern, signal, description, bar_index (optional)}
    """
    patterns = []
    if hist is None or len(hist) < 20:
        return patterns

    closes = hist['Close'].values.astype(float)
    highs  = hist['High'].values.astype(float)
    lows   = hist['Low'].values.astype(float)
    n      = len(closes)

    # ── 1. Trend direction (linear regression over last 20 bars) ─────────────
    recent = closes[-20:]
    x      = np.arange(len(recent), dtype=float)
    slope  = float(np.polyfit(x, recent, 1)[0])
    slope_pct = slope / current_price * 100   # % of price per bar

    if slope_pct > 0.12:
        patterns.append({
            'pattern': 'Uptrend',
            'signal': 'bullish',
            'description': f'Price trending up ~{slope_pct:.2f}%/day over 20 sessions'
        })
    elif slope_pct < -0.12:
        patterns.append({
            'pattern': 'Downtrend',
            'signal': 'bearish',
            'description': f'Price trending down ~{abs(slope_pct):.2f}%/day over 20 sessions'
        })
    else:
        patterns.append({
            'pattern': 'Sideways',
            'signal': 'neutral',
            'description': 'Price consolidating — watch for a breakout in either direction'
        })

    # ── 2. Breakout / Breakdown (vs 20-day range) ────────────────────────────
    high20 = float(max(highs[-20:]))
    low20  = float(min(lows[-20:]))
    if current_price >= high20 * 0.985:
        patterns.append({
            'pattern': 'Breakout',
            'signal': 'bullish',
            'description': f'Trading at/near 20-day high ${high20:.2f} — potential upside breakout'
        })
    elif current_price <= low20 * 1.015:
        patterns.append({
            'pattern': 'Breakdown',
            'signal': 'bearish',
            'description': f'Trading at/near 20-day low ${low20:.2f} — potential downside breakdown'
        })

    # ── 3. Bull Flag / Bear Flag ─────────────────────────────────────────────
    # Pole = bars[-16:-6], flag = bars[-6:-1]
    if n >= 16:
        prior_move  = (closes[-6] - closes[-16]) / max(closes[-16], 0.01) * 100
        recent_move = (closes[-1] - closes[-6])  / max(closes[-6],  0.01) * 100
        if prior_move > 5 and -4 < recent_move < 0.5:
            patterns.append({
                'pattern': 'Bull Flag',
                'signal': 'bullish',
                'description': f'Strong +{prior_move:.1f}% move followed by mild {recent_move:.1f}% pullback — bullish continuation setup'
            })
        elif prior_move < -5 and -0.5 < recent_move < 4:
            patterns.append({
                'pattern': 'Bear Flag',
                'signal': 'bearish',
                'description': f'Strong {prior_move:.1f}% drop followed by mild +{recent_move:.1f}% bounce — bearish continuation setup'
            })

    # ── 4. Double Top / Double Bottom ────────────────────────────────────────
    if n >= 40:
        left_highs  = highs[n-40:n-20]
        right_highs = highs[n-20:]
        left_lows   = lows[n-40:n-20]
        right_lows  = lows[n-20:]

        peak_l  = float(max(left_highs));  peak_r  = float(max(right_highs))
        trough_l = float(min(left_lows));  trough_r = float(min(right_lows))

        if (abs(peak_l - peak_r) / max(peak_r, 0.01) < 0.025
                and current_price < peak_r * 0.975):
            patterns.append({
                'pattern': 'Double Top',
                'signal': 'bearish',
                'description': f'Two peaks near ${max(peak_l, peak_r):.2f} — potential bearish reversal'
            })

        if (abs(trough_l - trough_r) / max(abs(trough_r), 0.01) < 0.025
                and current_price > trough_r * 1.025):
            patterns.append({
                'pattern': 'Double Bottom',
                'signal': 'bullish',
                'description': f'Two troughs near ${min(trough_l, trough_r):.2f} — potential bullish reversal'
            })

    # ── 5. Volume confirmation ────────────────────────────────────────────────
    if 'Volume' in hist.columns and n >= 10:
        vols    = hist['Volume'].values.astype(float)
        avg_vol = float(np.mean(vols[-20:])) if n >= 20 else float(np.mean(vols))
        last_v  = float(vols[-1])
        if avg_vol > 0 and last_v > avg_vol * 1.5:
            direction = 'bullish' if slope_pct > 0 else ('bearish' if slope_pct < 0 else 'neutral')
            emoji     = '📈' if direction == 'bullish' else ('📉' if direction == 'bearish' else '⚡')
            patterns.append({
                'pattern': f'Volume Surge {emoji}',
                'signal': direction,
                'description': f'Today\'s volume is {last_v/avg_vol:.1f}× the 20-day average — confirms {direction} momentum'
            })

    return patterns


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

    # ── Trend analysis (uses full 3-month history) ────────────────────────────
    t_score, t_label, t_meta = compute_trend(hist)

    today = datetime.now()
    best_calls   = []
    best_puts    = []
    best_spreads = []
    atm_ivs   = []                 # collect for IV rank (avg of near-ATM nearest expiry)
    full_call_chains = []          # for max pain and P/C
    full_put_chains  = []

    def _make_opt_row(opt, ds, dte, bid, ask, greeks, pop, ev, liq_grade, eng, near_earn):
        strike_v = _sf(opt.get('strike'))
        return {
            'strike': strike_v, 'expiry': ds, 'dte': dte,
            'bid': round(bid, 2), 'ask': round(ask, 2), 'mid': round((bid+ask)/2, 2),
            'volume': int(_sf(opt.get('volume'))),
            'open_interest': int(_sf(opt.get('openInterest'))),
            'iv': round(_sf(opt.get('impliedVolatility')) * 100, 1),
            'itm': bool(opt.get('inTheMoney', False)),
            'greeks': greeks or {},
            'pop':            round(pop * 100, 1) if pop else 0,    # percentage
            'expected_value': ev,                                    # dollars per contract
            'liquidity':      liq_grade,                             # 'A'|'B'|'C'|'F'
            'plain':          eng,                                   # plain-english explanation
            'near_earnings':  near_earn,                             # earnings date if in expiry window
        }

    for ds in exp_dates[:15]:
        exp = datetime.strptime(ds, '%Y-%m-%d')
        dte = (exp - today).days
        if dte < 0 or dte > 60: continue

        # Earnings warning if any earnings between today and expiry
        near_earn = detect_earnings_in_window(stock, ds)

        try:
            full_chain = _yf_call(lambda d=ds: stock.option_chain(d))
            if full_chain is None: continue

            # ── Track full chains for aggregate metrics ────────────────────────
            if full_chain.calls is not None and not full_chain.calls.empty:
                full_call_chains.append(full_chain.calls)
            if full_chain.puts is not None and not full_chain.puts.empty:
                full_put_chains.append(full_chain.puts)

            # ── Capture ATM IV (nearest strike) for IV rank ────────────────────
            try:
                near_atm = full_chain.calls.iloc[(full_chain.calls['strike'] - current_price).abs().argsort()[:1]]
                if not near_atm.empty:
                    iv_atm = _sf(near_atm.iloc[0].get('impliedVolatility'))
                    if iv_atm > 0: atm_ivs.append(iv_atm)
            except Exception: pass

            # ── Calls ──────────────────────────────────────────────────────────
            calls_df = full_chain.calls
            if calls_df is not None and not calls_df.empty:
                calls_df = calls_df[(calls_df['strike'] >= current_price * 0.90) &
                                    (calls_df['strike'] <= current_price * 1.60)]
                for _, row in calls_df.iterrows():
                    opt  = row.to_dict()
                    bid  = _sf(opt.get('bid'))
                    ask  = _sf(opt.get('ask'))
                    last = _sf(opt.get('lastPrice'))
                    iv   = _sf(opt.get('impliedVolatility'))
                    oi   = int(_sf(opt.get('openInterest')))
                    strike_v = _sf(opt.get('strike'))
                    if bid <= 0 and ask <= 0 and last > 0:
                        bid = round(last * 0.95, 2); ask = round(last * 1.05, 2)
                        opt['bid'] = bid; opt['ask'] = ask

                    # Liquidity gate
                    liq_grade, liq_adj, liq_reason = liquidity_grade(bid, ask, oi)
                    if liq_grade == 'F':
                        continue   # hard filter — un-tradeable

                    greeks = compute_greeks(current_price, strike_v, dte, iv, 'call')
                    res = score_call(opt, current_price, supports, resistances, dte, greeks)
                    if not res: continue
                    sc, reasons = res
                    sc += liq_adj
                    if liq_reason: reasons = list(reasons) + [liq_reason]
                    t_pts, t_reason = trend_score_modifier(t_score, 'call')
                    if t_pts != 0:
                        sc = max(0, sc + t_pts)
                        if t_reason: reasons = list(reasons) + [t_reason]
                    # Earnings penalty if expiry straddles earnings
                    if near_earn:
                        sc = max(0, sc - 12)
                        reasons = list(reasons) + [f'Earnings {near_earn} before expiry — IV crush risk']

                    pop = compute_pop_exact(current_price, strike_v, dte, iv, 'call')
                    ev  = compute_expected_value(current_price, strike_v, dte, iv, ask, 'call')

                    # Plain-english explanation
                    move_pct = (strike_v - current_price) / current_price * 100
                    breakeven = strike_v + ask
                    be_pct = (breakeven - current_price) / current_price * 100
                    plain = (f"If {ticker} rises {be_pct:.1f}% to ${breakeven:.2f} by {ds}, "
                             f"you break even. Stock above ${breakeven:.2f} = profit. "
                             f"Stock at or below ${strike_v:.2f} at expiry = lose 100% (-${ask*100:.0f}/contract).")

                    best_calls.append({**_make_opt_row(opt, ds, dte, bid, ask, greeks, pop, ev, liq_grade, plain, near_earn),
                                       'score': max(0, sc), 'reasons': reasons})

            # ── Puts ───────────────────────────────────────────────────────────
            puts_df = full_chain.puts
            if puts_df is not None and not puts_df.empty:
                puts_df = puts_df[(puts_df['strike'] >= current_price * 0.40) &
                                   (puts_df['strike'] <= current_price * 1.10)]
                for _, row in puts_df.iterrows():
                    opt  = row.to_dict()
                    bid  = _sf(opt.get('bid'))
                    ask  = _sf(opt.get('ask'))
                    last = _sf(opt.get('lastPrice'))
                    iv   = _sf(opt.get('impliedVolatility'))
                    oi   = int(_sf(opt.get('openInterest')))
                    strike_v = _sf(opt.get('strike'))
                    if bid <= 0 and ask <= 0 and last > 0:
                        bid = round(last * 0.95, 2); ask = round(last * 1.05, 2)
                        opt['bid'] = bid; opt['ask'] = ask

                    liq_grade, liq_adj, liq_reason = liquidity_grade(bid, ask, oi)
                    if liq_grade == 'F':
                        continue

                    greeks = compute_greeks(current_price, strike_v, dte, iv, 'put')
                    res = score_put(opt, current_price, supports, resistances, dte, greeks)
                    if not res: continue
                    sc, reasons = res
                    sc += liq_adj
                    if liq_reason: reasons = list(reasons) + [liq_reason]
                    t_pts, t_reason = trend_score_modifier(t_score, 'put')
                    if t_pts != 0:
                        sc = max(0, sc + t_pts)
                        if t_reason: reasons = list(reasons) + [t_reason]
                    if near_earn:
                        sc = max(0, sc - 12)
                        reasons = list(reasons) + [f'Earnings {near_earn} before expiry — IV crush risk']

                    pop = compute_pop_exact(current_price, strike_v, dte, iv, 'put')
                    ev  = compute_expected_value(current_price, strike_v, dte, iv, ask, 'put')

                    move_pct = (current_price - strike_v) / current_price * 100
                    breakeven = strike_v - ask
                    be_pct = (current_price - breakeven) / current_price * 100
                    plain = (f"If {ticker} falls {be_pct:.1f}% to ${breakeven:.2f} by {ds}, "
                             f"you break even. Stock below ${breakeven:.2f} = profit. "
                             f"Stock at or above ${strike_v:.2f} at expiry = lose 100% (-${ask*100:.0f}/contract).")

                    best_puts.append({**_make_opt_row(opt, ds, dte, bid, ask, greeks, pop, ev, liq_grade, plain, near_earn),
                                      'score': max(0, sc), 'reasons': reasons})

            # ── Vertical Spreads ──────────────────────────────────────────────
            try:
                exp_spreads = generate_vertical_spreads(
                    full_chain.calls, full_chain.puts,
                    current_price, dte, ds, t_score, ticker, near_earn
                )
                best_spreads.extend(exp_spreads)
            except Exception as se:
                logger.warning('spread gen %s %s: %s', ticker, ds, se)

        except Exception as e:
            logger.warning('chain err %s %s: %s', ticker, ds, e); continue

    best_calls.sort(key=lambda x: x['score'], reverse=True)
    best_puts.sort(key=lambda x: x['score'], reverse=True)
    best_spreads.sort(key=lambda x: x['score'], reverse=True)

    # ── Aggregate options metrics ──────────────────────────────────────────────
    iv_rank_info = None
    if atm_ivs:
        avg_atm_iv = sum(atm_ivs) / len(atm_ivs)
        record_iv_today(ticker, avg_atm_iv)
        iv_rank_info = compute_iv_rank(ticker, avg_atm_iv)

    max_pain_strike, call_oi_total, put_oi_total = (None, 0, 0)
    pc_ratio_data = {}
    if full_call_chains and full_put_chains:
        combined_calls = pd.concat(full_call_chains, ignore_index=True)
        combined_puts  = pd.concat(full_put_chains,  ignore_index=True)
        max_pain_strike, call_oi_total, put_oi_total = compute_max_pain(combined_calls, combined_puts)
        pc_ratio_data = compute_pc_ratio(combined_calls, combined_puts)

    avg_vol = float(hist['Volume'].mean()) if not hist.empty else 0
    today_v = int(hist['Volume'].iloc[-1])  if not hist.empty else 0
    ratio   = round(today_v / avg_vol, 2) if avg_vol > 0 else 1.0
    sig     = 'High' if ratio > 1.4 else ('Low' if ratio < 0.6 else 'Normal')
    vhist   = [{'date': dt.strftime('%m/%d'), 'volume': int(r['Volume']), 'above_avg': r['Volume'] > avg_vol}
               for dt, r in hist.tail(20).iterrows()]

    # Price history for chart (last 60 trading days)
    price_history = []
    try:
        for dt, r in hist.tail(60).iterrows():
            price_history.append({
                'date':  dt.strftime('%Y-%m-%d'),
                'open':  round(float(r['Open']),  2),
                'high':  round(float(r['High']),  2),
                'low':   round(float(r['Low']),   2),
                'close': round(float(r['Close']), 2),
            })
    except Exception: pass

    # ── Chart pattern recognition ─────────────────────────────────────────────
    chart_patterns = detect_chart_patterns(hist, current_price, supports, resistances)

    # ── Day trade picks ────────────────────────────────────────────────────────
    day_trade_picks = []
    for opt in best_calls:
        dt_score = _score_for_daytrading(opt, current_price)
        if dt_score > 0:
            day_trade_picks.append({**opt, 'dt_score': dt_score, 'opt_type': 'call'})
    for opt in best_puts:
        dt_score = _score_for_daytrading(opt, current_price)
        if dt_score > 0:
            day_trade_picks.append({**opt, 'dt_score': dt_score, 'opt_type': 'put'})
    day_trade_picks.sort(key=lambda x: x['dt_score'], reverse=True)

    result = {
        'ticker': ticker,
        'company_name':   info.get('longName', ticker),
        'current_price':  round(current_price, 2),
        'support_levels': supports,
        'resistance_levels': resistances,
        'top_calls':        best_calls[:25],
        'top_puts':         best_puts[:25],
        'top_spreads':      best_spreads[:25],
        'day_trade_picks':  day_trade_picks[:12],
        'trend': t_meta,
        'iv_rank': iv_rank_info,
        'options_flow': {
            'max_pain':   max_pain_strike,
            'call_oi':    call_oi_total,
            'put_oi':     put_oi_total,
            **pc_ratio_data,
        },
        'chart_patterns': chart_patterns,
        'price_history': price_history,
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

PICKS_TTL = 300  # 5 min cache — avoids constant rescans

_picks_cache     = {'data': None, 'ts': 0, 'refreshing': False}
_picks_lock      = threading.Lock()
_picks_ready     = threading.Event()   # fired when a refresh completes
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
    """Return ~20 tickers weighted toward hot sectors — fast enough for <30s scan."""
    ranked = list(sector_perf.keys())
    # Slots: top 2 sectors get 3, next 2 get 2, rest get 1 — cap ~15 sector picks
    slots  = [3, 3, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1]
    seen, result = set(), []

    for i, sector in enumerate(ranked):
        n = slots[i] if i < len(slots) else 1
        for t in SECTOR_TICKERS.get(sector, [])[:n]:
            if t not in seen:
                seen.add(t); result.append((t, sector))

    # Only add 5 broad market anchors for market-wide signals
    for t in SECTOR_TICKERS.get('Broad Market', [])[:5]:
        if t not in seen:
            seen.add(t); result.append((t, 'Broad Market'))

    # Hard cap at 20 tickers total
    return result[:20]


def _score_for_daytrading(opt, current_price):
    """
    Score an option for same-day intraday trading suitability.
    The two numbers that matter most for day traders:
      • Spread tightness — you pay this TWICE (entry + exit)
      • Delta — how much premium moves per $1 stock move
    Returns 0 if the option fails hard filters (too wide, no volume, too far OTM).
    """
    bid    = opt.get('bid', 0) or 0
    ask    = opt.get('ask', 0) or 0
    mid    = (bid + ask) / 2 if (bid + ask) > 0 else 0
    vol    = int(opt.get('volume', 0) or 0)
    oi     = int(opt.get('open_interest', 0) or 0)
    dte    = int(opt.get('dte', 99) or 99)
    strike = opt.get('strike', current_price) or current_price
    greeks = opt.get('greeks') or {}
    delta  = abs(greeks.get('delta', 0) or 0)

    # ── Hard filters ─────────────────────────────────────────────────────────
    if ask <= 0:   return 0
    if oi  < 50:   return 0
    if dte > 21:   return 0  # too far out — gamma too slow for same-day moves
    spread_pct = (ask - bid) / mid if mid > 0 else 1.0
    if spread_pct > 0.25: return 0  # eating >25% on entry+exit = unworkable

    otm_pct = abs(strike - current_price) / current_price * 100
    if otm_pct > 12: return 0       # too far OTM — delta too low to respond intraday

    score = 0

    # ── Spread tightness (most critical — you pay it twice) ───────────────────
    if   spread_pct < 0.04: score += 40
    elif spread_pct < 0.08: score += 30
    elif spread_pct < 0.12: score += 20
    elif spread_pct < 0.18: score += 10

    # ── Delta (premium movement per $1 stock move) ────────────────────────────
    if   0.45 <= delta <= 0.65: score += 35   # sweet spot: ATM-ish
    elif 0.35 <= delta < 0.45:  score += 25
    elif 0.65 < delta <= 0.80:  score += 20   # deep ITM — moves less %-wise
    elif 0.25 <= delta < 0.35:  score += 15
    else:                        score +=  5

    # ── Volume today (intraday exit liquidity) ────────────────────────────────
    if   vol >= 2000: score += 30
    elif vol >= 500:  score += 20
    elif vol >= 200:  score += 12
    elif vol >= 100:  score +=  6
    elif vol >=  50:  score +=  2

    # ── DTE (shorter = more gamma punch per point of stock move) ─────────────
    if   dte == 0:  score += 25
    elif dte <= 2:  score += 22
    elif dte <= 5:  score += 18
    elif dte <= 7:  score += 14
    elif dte <= 10: score += 10
    elif dte <= 14: score +=  6
    else:           score +=  2

    # ── Proximity to ATM ──────────────────────────────────────────────────────
    if   otm_pct < 1: score += 20
    elif otm_pct < 2: score += 15
    elif otm_pct < 4: score += 10
    elif otm_pct < 7: score +=  5

    # ── Premium range (too cheap = lottery, too expensive = capital drain) ────
    if   0.20 <= ask <= 3.00: score += 10
    elif 3.00 <  ask <= 8.00: score +=  5

    return score


def compute_confidence(score, vol_signal, reasons, sector_ret=0.0, trend_score=0, option_type='call'):
    base = min(score / 1.15, 78.0)
    if vol_signal == 'High':    base += 12
    elif vol_signal == 'Normal': base += 3
    base += min(len(reasons) * 2, 10)
    # Hot sector bonus
    if sector_ret >= 3:   base += 8
    elif sector_ret >= 1: base += 4
    # Trend alignment
    if option_type == 'call':
        if   trend_score >=  50: base += 10
        elif trend_score >=  20: base +=  5
        elif trend_score <= -50: base -= 18
        elif trend_score <= -20: base -=  9
    else:  # put
        if   trend_score <= -50: base += 10
        elif trend_score <= -20: base +=  5
        elif trend_score >=  50: base -= 18
        elif trend_score >=  20: base -=  9
    return min(max(round(base), 0), 95)


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


# DTE buckets — one pick per bucket per ticker so every filter tab has a full list
_DTE_BUCKETS = [(0, 7), (8, 14), (15, 30), (31, 60)]

def _picks_from_calls(ticker, company_name, current_price, sector, sector_ret,
                      vol_sig, best_calls, supports, resistances, trend_score=0):
    """Return one call pick dict per DTE bucket (Weekly/2-Week/Monthly/Swing)."""
    res_prices = [l['price'] for l in resistances[:3]]
    sup_prices = [l['price'] for l in supports[:3]]
    picks = []
    for dte_min, dte_max in _DTE_BUCKETS:
        bucket = sorted([c for c in best_calls if dte_min <= c['dte'] <= dte_max],
                        key=lambda x: x['score'], reverse=True)
        if not bucket:
            continue
        top  = bucket[0]
        conf = compute_confidence(top['score'], vol_sig, top['reasons'], sector_ret,
                                  trend_score=trend_score, option_type='call')
        picks.append({
            'opt_type':          'call',
            'ticker':            ticker,
            'company_name':      company_name,
            'current_price':     round(current_price, 2),
            'sector':            sector,
            'sector_return':     sector_ret,
            'strike':            top['strike'],
            'expiry':            top['expiry'],
            'dte':               top['dte'],
            'bid':               top['bid'],
            'ask':               top['ask'],
            'mid':               top['mid'],
            'volume':            top['volume'],
            'open_interest':     top['open_interest'],
            'iv':                top['iv'],
            'score':             top['score'],
            'confidence':        conf,
            'conviction':        conviction_label(conf),
            'conviction_reason': conviction_reason(top, sector, sector_ret, vol_sig),
            'signals':           top['reasons'][:4],
            'volume_signal':     vol_sig,
            'itm':               top.get('itm', False),
            'support_levels':    sup_prices,
            'resistance_levels': res_prices,
            'dt_score':          _score_for_daytrading(top, current_price),
        })
    return picks


def _picks_from_puts(ticker, company_name, current_price, sector, sector_ret,
                     vol_sig, best_puts, supports, resistances, trend_score=0):
    """Return one put pick dict per DTE bucket — mirror of _picks_from_calls."""
    res_prices = [l['price'] for l in resistances[:3]]
    sup_prices = [l['price'] for l in supports[:3]]
    picks = []
    for dte_min, dte_max in _DTE_BUCKETS:
        bucket = sorted([p for p in best_puts if dte_min <= p['dte'] <= dte_max],
                        key=lambda x: x['score'], reverse=True)
        if not bucket:
            continue
        top  = bucket[0]
        conf = compute_confidence(top['score'], vol_sig, top['reasons'], sector_ret,
                                  trend_score=trend_score, option_type='put')
        picks.append({
            'opt_type':          'put',
            'ticker':            ticker,
            'company_name':      company_name,
            'current_price':     round(current_price, 2),
            'sector':            sector,
            'sector_return':     sector_ret,
            'strike':            top['strike'],
            'expiry':            top['expiry'],
            'dte':               top['dte'],
            'bid':               top['bid'],
            'ask':               top['ask'],
            'mid':               top['mid'],
            'volume':            top['volume'],
            'open_interest':     top['open_interest'],
            'iv':                top['iv'],
            'score':             top['score'],
            'confidence':        conf,
            'conviction':        conviction_label(conf),
            'conviction_reason': conviction_reason(top, sector, sector_ret, vol_sig),
            'signals':           top['reasons'][:4],
            'volume_signal':     vol_sig,
            'itm':               top.get('itm', False),
            'support_levels':    sup_prices,
            'resistance_levels': res_prices,
            'dt_score':          _score_for_daytrading(top, current_price),
        })
    return picks


def _picks_from_spreads(ticker, company_name, current_price, sector, sector_ret,
                        vol_sig, best_spreads, supports, resistances, trend_score=0):
    """Return one spread pick per DTE bucket for the welcome page."""
    res_prices = [l['price'] for l in resistances[:3]]
    sup_prices = [l['price'] for l in supports[:3]]
    picks = []
    for dte_min, dte_max in _DTE_BUCKETS:
        bucket = sorted([s for s in best_spreads if dte_min <= s['dte'] <= dte_max],
                        key=lambda x: x['score'], reverse=True)
        if not bucket:
            continue
        top = bucket[0]
        opt_dir = 'call' if top['direction'] == 'bullish' else 'put'
        conf = compute_confidence(top['score'], vol_sig, top.get('reasons', []),
                                  sector_ret, trend_score=trend_score, option_type=opt_dir)
        mp = top.get('max_profit_per_contract', 0)
        ml = top.get('max_loss_per_contract', 0)
        rr = top.get('rr_ratio', 0)
        picks.append({
            'opt_type':          'spread',
            'spread_type':       top['spread_type'],
            'direction':         top['direction'],
            'ticker':            ticker,
            'company_name':      company_name,
            'current_price':     round(current_price, 2),
            'sector':            sector,
            'sector_return':     sector_ret,
            'long_strike':       top['long_strike'],
            'short_strike':      top['short_strike'],
            'strike':            top['long_strike'],
            'expiry':            top['expiry'],
            'dte':               top['dte'],
            'net_cost':          top['net_cost'],
            'max_profit':        mp,
            'max_loss':          ml,
            'breakeven':         top['breakeven'],
            'rr_ratio':          rr,
            'pop':               top.get('pop', 0),
            'width':             top.get('width', 0),
            'bid':               0,
            'ask':               round(abs(top['net_cost']), 2),
            'mid':               round(abs(top['net_cost']), 2),
            'score':             top['score'],
            'confidence':        conf,
            'conviction':        conviction_label(conf),
            'conviction_reason': f"R/R {rr:.1f}:1 — +${mp:.0f} / −${ml:.0f} per contract",
            'signals':           top.get('reasons', [])[:4],
            'volume_signal':     vol_sig,
            'itm':               False,
            'support_levels':    sup_prices,
            'resistance_levels': res_prices,
        })
    return picks


def _score_chain_rows(chain_df, current_price, supports, resistances, dte, opt_type):
    """Score all rows in a calls or puts DataFrame; return scored list."""
    scored = []
    score_fn = score_call if opt_type == 'call' else score_put
    strike_lo = current_price * (0.40 if opt_type == 'put' else 0.90)
    strike_hi = current_price * (1.10 if opt_type == 'put' else 1.60)
    df = chain_df[(chain_df['strike'] >= strike_lo) & (chain_df['strike'] <= strike_hi)]
    for _, row in df.iterrows():
        opt  = row.to_dict()
        bid  = _sf(opt.get('bid'));  ask = _sf(opt.get('ask'))
        last = _sf(opt.get('lastPrice'))
        iv   = _sf(opt.get('impliedVolatility'))
        if bid <= 0 and ask <= 0 and last > 0:
            bid = round(last * 0.95, 2); ask = round(last * 1.05, 2)
            opt['bid'] = bid; opt['ask'] = ask
        greeks_f = compute_greeks(current_price, _sf(opt.get('strike')), dte, iv, opt_type)
        res = score_fn(opt, current_price, supports, resistances, dte, greeks_f)
        if not res: continue
        sc, reasons = res
        scored.append({
            'strike': _sf(opt.get('strike')), 'expiry': None, 'dte': dte,
            'bid': round(bid, 2), 'ask': round(ask, 2), 'mid': round((bid + ask) / 2, 2),
            'volume': int(_sf(opt.get('volume'))),
            'open_interest': int(_sf(opt.get('openInterest'))),
            'iv': round(iv * 100, 1),
            'score': sc, 'reasons': reasons,
            'itm': bool(opt.get('inTheMoney', False)),
        })
    return scored


def _scan_one_with_sector(ticker, sector, sector_ret, prefetch_hist=None):
    """Scan one ticker; returns a LIST of picks (calls + puts, one per DTE bucket each)."""
    try:
        # ── Fast path: use pre-fetched batch history ──────────────────────────
        if prefetch_hist is not None and not prefetch_hist.empty:
            cached = _get_cached(ticker)
            if not cached:
                try:
                    hist = prefetch_hist.dropna()
                    if not hist.empty:
                        stock = yf.Ticker(ticker)
                        current_price = float(hist['Close'].iloc[-1])
                        sr_raw      = find_sr_levels(hist)
                        supports    = sorted([l for l in sr_raw if l['type'] == 'support'    and l['price'] < current_price * 1.03], key=lambda x: x['price'], reverse=True)[:4]
                        resistances = sorted([l for l in sr_raw if l['type'] == 'resistance' and l['price'] > current_price * 0.97], key=lambda x: x['price'])[:4]
                        # Scanner: 2 retries max so rate-limit backoff stays short
                        exp_dates   = _yf_call(lambda: list(stock.options), retries=2, base_delay=2.0) or []
                        today        = datetime.now()
                        best_calls, best_puts, best_spreads = [], [], []
                        fast_t_score, _, _ = compute_trend(hist)
                        for ds in exp_dates:
                            exp = datetime.strptime(ds, '%Y-%m-%d')
                            dte = (exp - today).days
                            if dte < 0 or dte > 60: continue
                            try:
                                full_chain = _yf_call(lambda d=ds: stock.option_chain(d), retries=2, base_delay=2.0)
                                if full_chain is None: continue
                                # Score calls
                                if full_chain.calls is not None and not full_chain.calls.empty:
                                    for rec in _score_chain_rows(full_chain.calls, current_price, supports, resistances, dte, 'call'):
                                        rec['expiry'] = ds
                                        t_pts, t_reason = trend_score_modifier(fast_t_score, 'call')
                                        if t_pts: rec['score'] = max(0, rec['score'] + t_pts); rec['reasons'] = list(rec['reasons']) + ([t_reason] if t_reason else [])
                                        best_calls.append(rec)
                                # Score puts
                                if full_chain.puts is not None and not full_chain.puts.empty:
                                    for rec in _score_chain_rows(full_chain.puts, current_price, supports, resistances, dte, 'put'):
                                        rec['expiry'] = ds
                                        t_pts, t_reason = trend_score_modifier(fast_t_score, 'put')
                                        if t_pts: rec['score'] = max(0, rec['score'] + t_pts); rec['reasons'] = list(rec['reasons']) + ([t_reason] if t_reason else [])
                                        best_puts.append(rec)
                                # Generate spreads
                                try:
                                    if full_chain.calls is not None and full_chain.puts is not None:
                                        sp = generate_vertical_spreads(
                                            full_chain.calls, full_chain.puts,
                                            current_price, dte, ds, fast_t_score, ticker
                                        )
                                        best_spreads.extend(sp)
                                except Exception as se:
                                    logger.warning('fast spread %s %s: %s', ticker, ds, se)
                            except Exception as e:
                                logger.warning('fast chain %s %s: %s', ticker, ds, e); continue
                        if best_calls or best_puts or best_spreads:
                            avg_vol = float(hist['Volume'].mean()) if not hist.empty else 0
                            today_v = int(hist['Volume'].iloc[-1])  if not hist.empty else 0
                            ratio   = round(today_v / avg_vol, 2) if avg_vol > 0 else 1.0
                            vol_sig = 'High' if ratio > 1.4 else ('Low' if ratio < 0.6 else 'Normal')
                            picks = []
                            if best_calls:
                                picks += _picks_from_calls(ticker, ticker, current_price, sector, sector_ret,
                                                           vol_sig, best_calls, supports, resistances,
                                                           trend_score=fast_t_score)
                            if best_puts:
                                picks += _picks_from_puts(ticker, ticker, current_price, sector, sector_ret,
                                                          vol_sig, best_puts, supports, resistances,
                                                          trend_score=fast_t_score)
                            if best_spreads:
                                best_spreads.sort(key=lambda x: x['score'], reverse=True)
                                picks += _picks_from_spreads(ticker, ticker, current_price, sector, sector_ret,
                                                             vol_sig, best_spreads, supports, resistances,
                                                             trend_score=fast_t_score)
                            return picks
                except Exception as e:
                    msg = str(e).lower()
                    if any(x in msg for x in ('too many requests', '429', 'rate limit', 'rate_limit', 'temporarily')):
                        # Rate-limited in fast path — skip this ticker, don't make it worse
                        logger.warning('fast scan %s: rate limited — skipping (no slow-path fallback)', ticker)
                        return []
                    logger.warning('fast scan %s: %s — falling back to slow path', ticker, e)

        # ── Slow path: full analyze_ticker (only reached if not rate-limited) ──
        r = analyze_ticker(ticker)
        if r.get('error') or (not r.get('top_calls') and not r.get('top_puts') and not r.get('top_spreads')):
            return []
        vol_sig = r['volume']['signal']
        slow_t_score = r.get('trend', {}).get('score', 0)
        picks = []
        if r.get('top_calls'):
            picks += _picks_from_calls(
                ticker, r['company_name'], r['current_price'], sector, sector_ret,
                vol_sig, r['top_calls'],
                r.get('support_levels', []), r.get('resistance_levels', []),
                trend_score=slow_t_score,
            )
        if r.get('top_puts'):
            picks += _picks_from_puts(
                ticker, r['company_name'], r['current_price'], sector, sector_ret,
                vol_sig, r['top_puts'],
                r.get('support_levels', []), r.get('resistance_levels', []),
                trend_score=slow_t_score,
            )
        if r.get('top_spreads'):
            picks += _picks_from_spreads(
                ticker, r['company_name'], r['current_price'], sector, sector_ret,
                vol_sig, r['top_spreads'],
                r.get('support_levels', []), r.get('resistance_levels', []),
                trend_score=slow_t_score,
            )
        return picks
    except Exception as e:
        logger.warning('picks scan %s: %s', ticker, e)
        return []


def _do_refresh():
    sector_perf = get_sector_performance()
    scan_list   = build_scan_list(sector_perf)
    tickers     = [t for t, _ in scan_list]
    logger.info('Scanning %d tickers (batch mode)', len(tickers))

    # ── Batch-download all price histories in one request (huge speedup) ──────
    hist_map = {}
    try:
        if len(tickers) == 1:
            df = yf.download(tickers[0], period='1mo', auto_adjust=True, progress=False)
            hist_map[tickers[0]] = df if not df.empty else None
        else:
            raw = yf.download(tickers, period='1mo', group_by='ticker',
                              auto_adjust=True, progress=False)
            for t in tickers:
                try:
                    df = raw[t].dropna() if t in raw.columns.get_level_values(0) else pd.DataFrame()
                    hist_map[t] = df if not df.empty else None
                except Exception:
                    hist_map[t] = None
    except Exception as e:
        logger.warning('Batch download failed (%s) — falling back to individual calls', e)
        hist_map = {t: None for t in tickers}

    # ── Scan each ticker using pre-fetched history ────────────────────────────
    # Hard deadline: never block the response more than 45 s.  Tickers that
    # aren't done by then are silently skipped — the stale cache path means
    # users get *something* rather than a spinner that never resolves.
    SCAN_DEADLINE = time.time() + 45
    results = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {
            ex.submit(_scan_one_with_sector, t, sec, sector_perf.get(sec, 0.0), hist_map.get(t)): t
            for t, sec in scan_list
        }
        try:
            for fut in as_completed(futs, timeout=max(2, SCAN_DEADLINE - time.time())):
                try:
                    picks = fut.result(timeout=1)
                    if picks:
                        results.extend(picks)
                except Exception:
                    pass
                if time.time() > SCAN_DEADLINE:
                    logger.warning('Scan deadline — returning %d partial picks', len(results))
                    break
        except Exception:
            # TimeoutError or other — collect whatever completed so far
            logger.warning('Scan timeout — collecting %d partial picks from completed futures', len(results))
            for fut in futs:
                if fut.done():
                    try:
                        picks = fut.result(timeout=0)
                        if picks:
                            results.extend(picks)
                    except Exception:
                        pass

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
    _picks_ready.set()
    _picks_ready.clear()
    return out


def _warmup():
    time.sleep(2)  # Give Flask a moment to bind, then start scanning immediately
    with _picks_lock:
        if _picks_cache['refreshing']:
            return   # HTTP handler already started a scan — don't duplicate
        _picks_cache['refreshing'] = True
    try:
        _do_refresh()
    except Exception as e:
        logger.warning('warmup err: %s', e)
        with _picks_lock:
            _picks_cache['refreshing'] = False
        _picks_ready.set(); _picks_ready.clear()

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
            if r.get('error') or (not r.get('top_calls') and not r.get('top_puts')):
                results.append({'ticker': t, 'error': r.get('error', 'No options'), 'top_score': 0})
                continue
            top = (r.get('top_calls') or r.get('top_puts'))[0]
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
    """Return picks immediately — never block for more than 55 s total.
    Rules:
    1. Fresh cache → return immediately.
    2. Scan in progress → wait up to 55 s; return whatever arrived (partial or full).
       NEVER start a second scan while one is running.
    3. No cache and no scan → start one in background, wait up to 55 s.
    """
    with _picks_lock:
        cached     = _picks_cache['data']
        fresh      = cached and (time.time() - _picks_cache['ts']) < PICKS_TTL
        refreshing = _picks_cache['refreshing']

        if fresh:
            return jsonify(cached)

        # Start a background scan only if none is running
        if not refreshing:
            _picks_cache['refreshing'] = True
            threading.Thread(target=_do_refresh, daemon=True).start()

        # Stale data available — return it right away; background scan will update cache
        if cached:
            stale = dict(cached)
            stale['stale'] = True
            return jsonify(stale)

    # No cached data at all — wait for the background scan (capped at 55 s)
    _picks_ready.wait(timeout=55)
    with _picks_lock:
        if _picks_cache['data']:
            return jsonify(_picks_cache['data'])

    # Still nothing after 55 s — return empty so the UI doesn't stay blank forever
    return jsonify({'picks': [], 'scanned': 0, 'sector_perf': {}, 'updated_at': time.time(), 'stale': True})


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


# ─── Trade Journal & Position Tracking ────────────────────────────────────────

@app.route('/api/journal/trades', methods=['GET'])
def list_trades():
    """List all trades from journal (open + closed)."""
    paper = request.args.get('paper')
    try:
        with _db_lock, _db() as c:
            sql = "SELECT * FROM trades"
            args = []
            if paper is not None:
                sql += " WHERE paper = ?"
                args.append(1 if paper.lower() in ('1', 'true', 'yes') else 0)
            sql += " ORDER BY entry_date DESC, id DESC"
            rows = c.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get('closed') and d.get('exit_price') is not None:
                pnl = (d['exit_price'] - d['entry_price']) * d['contracts'] * 100
                d['pnl'] = round(pnl, 2)
                d['pnl_pct'] = round((d['exit_price'] - d['entry_price']) / d['entry_price'] * 100, 1) if d['entry_price'] else 0
            else:
                d['pnl'] = None; d['pnl_pct'] = None
            out.append(d)
        return jsonify({'trades': out})
    except Exception as e:
        logger.exception('list_trades'); return jsonify({'error': str(e)}), 500


@app.route('/api/journal/trades', methods=['POST'])
def add_trade():
    """Log a new trade (open or pre-closed). JSON body matches the trades schema."""
    try:
        b = request.get_json(force=True) or {}
        with _db_lock, _db() as c:
            cur = c.execute("""INSERT INTO trades
                (ticker, option_type, strike, expiry, entry_date, entry_price,
                 exit_date, exit_price, contracts, thesis, tags, score_at_entry, paper, closed)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (b.get('ticker', '').upper(), b.get('option_type', 'call'), float(b.get('strike', 0)),
                 b.get('expiry', ''), b.get('entry_date', datetime.now().strftime('%Y-%m-%d')),
                 float(b.get('entry_price', 0)), b.get('exit_date'),
                 float(b['exit_price']) if b.get('exit_price') is not None else None,
                 int(b.get('contracts', 1)), b.get('thesis', ''), b.get('tags', ''),
                 int(b.get('score_at_entry', 0)),
                 1 if b.get('paper') else 0,
                 1 if (b.get('exit_price') is not None or b.get('closed')) else 0))
            c.commit()
            new_id = cur.lastrowid
        return jsonify({'id': new_id, 'ok': True})
    except Exception as e:
        logger.exception('add_trade'); return jsonify({'error': str(e)}), 500


@app.route('/api/journal/trades/<int:trade_id>', methods=['PUT'])
def close_trade(trade_id):
    """Close an open trade with an exit price."""
    try:
        b = request.get_json(force=True) or {}
        exit_price = float(b.get('exit_price', 0))
        exit_date  = b.get('exit_date', datetime.now().strftime('%Y-%m-%d'))
        with _db_lock, _db() as c:
            c.execute("""UPDATE trades SET exit_price=?, exit_date=?, closed=1 WHERE id=?""",
                      (exit_price, exit_date, trade_id))
            c.commit()
        return jsonify({'ok': True})
    except Exception as e:
        logger.exception('close_trade'); return jsonify({'error': str(e)}), 500


@app.route('/api/journal/trades/<int:trade_id>', methods=['DELETE'])
def delete_trade(trade_id):
    try:
        with _db_lock, _db() as c:
            c.execute("DELETE FROM trades WHERE id=?", (trade_id,))
            c.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/journal/stats')
def journal_stats():
    """Aggregate stats: win rate, avg P&L, by option type, by score band."""
    try:
        with _db_lock, _db() as c:
            rows = c.execute("SELECT * FROM trades WHERE closed=1").fetchall()
        if not rows:
            return jsonify({'total': 0, 'wins': 0, 'losses': 0, 'win_rate': 0,
                            'avg_pnl': 0, 'total_pnl': 0, 'by_type': {}, 'by_score': {}})
        total_pnl = 0; wins = 0; losses = 0
        by_type   = {'call': {'n': 0, 'wins': 0, 'pnl': 0}, 'put':  {'n': 0, 'wins': 0, 'pnl': 0}}
        by_score  = {'high': {'n': 0, 'wins': 0, 'pnl': 0},  # score >= 130
                     'mid':  {'n': 0, 'wins': 0, 'pnl': 0},  # 80-129
                     'low':  {'n': 0, 'wins': 0, 'pnl': 0}}  # < 80
        for r in rows:
            pnl = (r['exit_price'] - r['entry_price']) * r['contracts'] * 100
            total_pnl += pnl
            ot = r['option_type']
            sc = r['score_at_entry'] or 0
            sb = 'high' if sc >= 130 else 'mid' if sc >= 80 else 'low'
            if ot in by_type:
                by_type[ot]['n']   += 1; by_type[ot]['pnl'] += pnl
                if pnl > 0: by_type[ot]['wins'] += 1
            by_score[sb]['n']   += 1; by_score[sb]['pnl'] += pnl
            if pnl > 0:
                wins += 1; by_score[sb]['wins'] += 1
            elif pnl < 0:
                losses += 1
        n = len(rows)
        return jsonify({
            'total': n, 'wins': wins, 'losses': losses,
            'win_rate':  round(wins / n * 100, 1),
            'avg_pnl':   round(total_pnl / n, 2),
            'total_pnl': round(total_pnl, 2),
            'by_type':   {k: {**v, 'pnl': round(v['pnl'], 2),
                              'win_rate': round(v['wins'] / v['n'] * 100, 1) if v['n'] else 0}
                          for k, v in by_type.items()},
            'by_score':  {k: {**v, 'pnl': round(v['pnl'], 2),
                              'win_rate': round(v['wins'] / v['n'] * 100, 1) if v['n'] else 0}
                          for k, v in by_score.items()},
        })
    except Exception as e:
        logger.exception('journal_stats'); return jsonify({'error': str(e)}), 500


# ─── Multi-leg Strategy Builder ───────────────────────────────────────────────

@app.route('/api/strategy/build', methods=['POST'])
def strategy_build():
    """
    Build a multi-leg strategy and return combined Greeks, max P/L, breakevens.
    Body: { strategy: 'vertical_call_debit'|'vertical_put_debit'|'iron_condor'|
                      'straddle'|'strangle'|'butterfly_call'|...,
            ticker: 'AAPL', expiry: 'YYYY-MM-DD', current_price: 150.0,
            legs: [{type:'call'|'put', strike, qty:+1|-1, premium}, ...] }
    Returns combined metrics.
    """
    try:
        b = request.get_json(force=True) or {}
        legs = b.get('legs', [])
        if not legs:
            return jsonify({'error': 'no legs'}), 400
        S = float(b.get('current_price', 0))
        T_days = int(b.get('dte', 30))
        iv_est = float(b.get('iv', 0.30))

        # Net cost (debit positive = pays, credit negative = receives)
        net_cost = 0
        net_delta = net_gamma = net_theta = net_vega = 0
        for leg in legs:
            qty = int(leg.get('qty', 1))  # +1 long, -1 short
            premium = float(leg.get('premium', 0))
            net_cost += qty * premium * 100
            g = compute_greeks(S, float(leg.get('strike', 0)), T_days, iv_est, leg.get('type', 'call'))
            if g:
                net_delta += qty * g['delta']
                net_gamma += qty * g['gamma']
                net_theta += qty * g['theta']
                net_vega  += qty * g['vega']

        # Payoff at expiry across a price range
        strikes = sorted(set(float(l.get('strike', 0)) for l in legs))
        if not strikes:
            return jsonify({'error': 'no strikes'}), 400
        s_min = min(strikes) * 0.70; s_max = max(strikes) * 1.30
        prices, payoffs = [], []
        for i in range(101):
            s = s_min + (s_max - s_min) * i / 100
            pay = 0
            for leg in legs:
                qty = int(leg.get('qty', 1))
                K = float(leg.get('strike', 0))
                prem = float(leg.get('premium', 0))
                intrinsic = max(0, s - K) if leg.get('type') == 'call' else max(0, K - s)
                pay += qty * (intrinsic - prem) * 100
            prices.append(round(s, 2)); payoffs.append(round(pay, 2))

        max_profit = max(payoffs); max_loss = min(payoffs)
        # Find breakevens by sign changes
        breakevens = []
        for i in range(1, len(payoffs)):
            if payoffs[i-1] * payoffs[i] < 0:  # sign change
                # Linear interp
                p1, p2 = payoffs[i-1], payoffs[i]
                s1, s2 = prices[i-1], prices[i]
                breakevens.append(round(s1 + (s2 - s1) * (0 - p1) / (p2 - p1), 2))

        return jsonify({
            'net_cost':    round(net_cost, 2),
            'max_profit':  round(max_profit, 2),
            'max_loss':    round(max_loss, 2),
            'breakevens':  breakevens,
            'net_greeks': {
                'delta': round(net_delta, 3),
                'gamma': round(net_gamma, 4),
                'theta': round(net_theta, 4),
                'vega':  round(net_vega,  4),
            },
            'payoff_curve': {'prices': prices, 'payoffs': payoffs},
        })
    except Exception as e:
        logger.exception('strategy_build'); return jsonify({'error': str(e)}), 500


# ─── AI Chat Assistant ────────────────────────────────────────────────────────

def _builtin_chat_reply(message: str, ctx: dict) -> str:
    """Generate a contextual reply using live ticker data — no API key required."""
    msg   = message.lower().strip()
    tk    = ctx.get('ticker', '')
    price = ctx.get('current_price', 0)
    trend = ctx.get('trend', {})
    ivr   = ctx.get('iv_rank', {})
    pats  = ctx.get('chart_patterns', [])
    sups  = (ctx.get('support_levels') or [])[:3]
    ress  = (ctx.get('resistance_levels') or [])[:3]
    calls = (ctx.get('top_calls') or [])[:3]
    puts  = (ctx.get('top_puts') or [])[:3]
    spreads = (ctx.get('top_spreads') or [])[:2]

    def fmt(v): return f'${v:.2f}' if v else 'N/A'
    def levels(lst): return ', '.join(fmt(l.get('price', l) if isinstance(l, dict) else l) for l in lst) if lst else 'N/A'

    trend_dir   = trend.get('direction', 'neutral')
    trend_label = trend.get('label', 'neutral')
    trend_score = trend.get('score', 0)
    ivr_rank    = ivr.get('rank')
    ivr_interp  = ivr.get('interpretation', '')
    pat_names   = [p.get('pattern', '') for p in pats]
    bull_pats   = [p for p in pats if p.get('signal') == 'bullish']
    bear_pats   = [p for p in pats if p.get('signal') == 'bearish']

    # ── Trend / direction questions ────────────────────────────────────────────
    if any(w in msg for w in ('trend', 'direction', 'bullish', 'bearish', 'going up', 'going down', 'outlook')):
        sups_str = levels(sups)
        ress_str = levels(ress)
        pat_str  = ', '.join(pat_names) if pat_names else 'No patterns detected'
        bias = ('📈 Bullish' if trend_score > 15 else '📉 Bearish' if trend_score < -15 else '↔ Neutral')
        return (
            f"{tk} is showing a **{trend_label}** trend (score {trend_score:+d}).\n\n"
            f"Bias: {bias}\n"
            f"Chart patterns: {pat_str}\n"
            f"Support: {sups_str} | Resistance: {ress_str}\n\n"
            f"⚠️ Trend signals are probabilistic — not guarantees. Always size positions to limit downside."
        )

    # ── IV / volatility questions ──────────────────────────────────────────────
    if any(w in msg for w in ('implied vol', 'volatility', 'iv rank', 'expensive', 'cheap option')) or \
            re.search(r'\biv\b', msg):
        if ivr_rank is None:
            return f"IV Rank data isn't available for {tk} yet — try re-analyzing the ticker."
        action = ('selling premium (covered calls, credit spreads, iron condors)'
                  if ivr_rank >= 60
                  else 'buying premium (debit spreads, long calls/puts)'
                  if ivr_rank <= 30
                  else 'either buying or selling — IV is in the middle range')
        return (
            f"{tk}'s IV Rank is **{ivr_rank}** — {ivr_interp}.\n\n"
            f"{'🔴 High IV' if ivr_rank >= 60 else '🟢 Low IV' if ivr_rank <= 30 else '🟡 Mid IV'}: "
            f"This environment favors **{action}**.\n\n"
            f"⚠️ IV can spike suddenly on earnings or macro news — check the calendar before entering."
        )

    # ── What is / explain / define — must come BEFORE data-lookup branches ──────
    if any(w in msg for w in ('what is', 'what are', 'explain', 'define', 'how does', 'how do')):
        if 'delta' in msg:
            return "**Delta** measures how much an option's price changes for a $1 move in the stock. Delta 0.50 = ATM (50¢ per $1 move). Delta 0.70 = deeper ITM (70¢ per $1). Day traders usually want delta 0.40–0.70 for good leverage without paying full stock price."
        if 'theta' in msg:
            return "**Theta** is the daily time decay — the amount an option loses per day just from the passage of time. Theta works against buyers (you pay it) and benefits sellers (you collect it). Theta accelerates rapidly in the last 7–10 days before expiration."
        if 'vega' in msg:
            return "**Vega** measures an option's sensitivity to implied volatility changes. High vega = the option's price moves a lot when IV changes. Before earnings, vega inflates prices; after earnings, IV collapses (IV crush), deflating option values even if the stock moves your direction."
        if 'gamma' in msg:
            return "**Gamma** measures how fast delta changes as the stock moves. High gamma (ATM, short-dated options) means delta can shift rapidly — great for fast moves but dangerous if the stock reverses. 0DTE options have extremely high gamma."
        if 'iv rank' in msg or 'iv percentile' in msg or re.search(r'\biv\b', msg):
            return "**IV Rank** (IVR) tells you where current implied volatility sits within its 52-week range. IVR 80 = options are expensive (top 20% of the year). IVR 20 = cheap options. High IVR favors selling premium; low IVR favors buying premium."
        if 'pop' in msg or 'probability' in msg:
            return "**POP (Probability of Profit)** is the estimated chance the option finishes in-the-money at expiration, derived from the option's delta under Black-Scholes pricing. It assumes markets are fairly priced — it's a consensus estimate, not a guarantee."
        if 'spread' in msg:
            return "A **vertical spread** is buying one option and selling another at a different strike in the same expiry. It caps your max profit AND max loss. Debit spreads cost money upfront (you pay to enter). Credit spreads collect premium upfront. Both have defined, limited risk — ideal when IV is high."
        if 'call' in msg:
            return "A **call option** gives you the right (not obligation) to buy 100 shares at the strike price before expiration. You profit if the stock rises above the strike + premium paid. Max loss = premium paid. They're used for bullish directional plays."
        if 'put' in msg:
            return "A **put option** gives you the right (not obligation) to sell 100 shares at the strike price before expiration. You profit if the stock falls below the strike − premium paid. Max loss = premium paid. They're used for bearish directional plays or hedging."
        if 'dte' in msg or 'expir' in msg:
            return "**DTE (Days to Expiration)** is how many trading days until the option expires. Short DTE (0–7d) = high risk/reward, fast theta decay — used for day/swing trades. Longer DTE (30–60d) = more time for the trade to work, lower theta burn rate — better for multi-week positions."
        if 'day trade' in msg or 'day trading' in msg:
            return "**Day trading options** = entering and exiting the same day. Keys: tight bid-ask spread (you pay it twice), high volume (easy to exit), delta ≥ 0.35 (enough movement per stock move), DTE 0–5 (high gamma). MIN MOVE shown in the Day Trade tab = how far the stock must move just to cover your spread cost."
        if 'iron condor' in msg:
            return "An **iron condor** = sell OTM call + buy further OTM call (call spread) AND sell OTM put + buy further OTM put (put spread). You collect premium upfront and profit if the stock stays inside the short strikes at expiration. Max profit = premium collected; max loss = spread width − premium."
        # Generic fallback for "what is X"
        topic = msg.replace('what is', '').replace('what are', '').replace('explain', '').replace('define', '').strip()
        return f"Ask me about a specific options concept — delta, theta, vega, gamma, IV rank, POP, spreads, calls, puts, DTE, day trading, or iron condors. You asked about: \"{topic}\""

    # ── Best calls ─────────────────────────────────────────────────────────────
    if any(w in msg for w in ('best call', 'top call', 'call option', 'calls', 'bullish play', 'buy call')):
        if not calls:
            return f"No top calls were found for {tk} in the current scan. Try re-analyzing or check a more liquid ticker."
        lines = []
        for c in calls:
            lines.append(f"• {fmt(c.get('strike'))} strike, {c.get('expiry','?')} ({c.get('dte','?')}d) — Ask {fmt(c.get('ask'))}, Score {c.get('score','?')}")
        sups_str = levels(sups)
        return (
            f"**Top calls for {tk}** (current price {fmt(price)}):\n\n"
            + '\n'.join(lines) + '\n\n'
            f"Support at {sups_str}. Look to enter on a break above the nearest resistance level.\n"
            f"⚠️ Educational only — not financial advice."
        )

    # ── Best puts ──────────────────────────────────────────────────────────────
    if any(w in msg for w in ('best put', 'top put', 'put option', 'puts', 'bearish play', 'buy put', 'short')):
        if not puts:
            return f"No top puts were found for {tk}. Try re-analyzing or switching to a more liquid ticker."
        lines = []
        for p in puts:
            lines.append(f"• {fmt(p.get('strike'))} strike, {p.get('expiry','?')} ({p.get('dte','?')}d) — Ask {fmt(p.get('ask'))}, Score {p.get('score','?')}")
        ress_str = levels(ress)
        return (
            f"**Top puts for {tk}** (current price {fmt(price)}):\n\n"
            + '\n'.join(lines) + '\n\n'
            f"Resistance at {ress_str}. Look to enter on a break below the nearest support level.\n"
            f"⚠️ Educational only — not financial advice."
        )

    # ── Spreads ────────────────────────────────────────────────────────────────
    if any(w in msg for w in ('spread', 'debit spread', 'credit spread', 'vertical', 'iron condor', 'defined risk')):
        if not spreads:
            return f"No spread picks were found for {tk}. Spreads require liquid options — try AAPL, SPY, or QQQ."
        lines = []
        for s in spreads:
            net = s.get('net_cost', 0)
            lines.append(
                f"• {s.get('spread_type','?')} {s.get('direction','?')}: "
                f"{fmt(s.get('long_strike'))} / {fmt(s.get('short_strike'))} — "
                f"{'Debit' if net > 0 else 'Credit'} {fmt(abs(net))}, "
                f"Max profit ${s.get('max_profit',0):.0f}, R/R {s.get('rr_ratio',0):.1f}:1"
            )
        return (
            f"**Top vertical spreads for {tk}:**\n\n"
            + '\n'.join(lines) + '\n\n'
            f"Spreads cap your max loss — ideal when IV is elevated or you want defined risk.\n"
            f"⚠️ Educational only — not financial advice."
        )

    # ── Chart patterns ─────────────────────────────────────────────────────────
    if any(w in msg for w in ('pattern', 'chart', 'flag', 'breakout', 'double top', 'double bottom', 'head', 'setup')):
        if not pats:
            return f"No chart patterns were detected for {tk}. Analyze the ticker to load pattern data."
        lines = [f"{'📈' if p.get('signal')=='bullish' else '📉' if p.get('signal')=='bearish' else '—'} **{p.get('pattern')}**: {p.get('description')}" for p in pats]
        bias_str = f"{len(bull_pats)} bullish, {len(bear_pats)} bearish signal(s)"
        return f"**Chart patterns for {tk}:**\n\n" + '\n'.join(lines) + f"\n\nOverall: {bias_str}.\n⚠️ Patterns are probabilistic — confirm with volume and trend before acting."

    # ── Support / resistance ───────────────────────────────────────────────────
    if any(w in msg for w in ('support', 'resistance', 'level', 'target', 'key level', 'where is')):
        sups_str = levels(sups)
        ress_str = levels(ress)
        return (
            f"**Key levels for {tk}** (trading at {fmt(price)}):\n\n"
            f"Support: {sups_str}\n"
            f"Resistance: {ress_str}\n\n"
            f"Options strategy tip: Use support as a call entry trigger (break above) or put stop (break below)."
        )

    # ── Entry / when to enter ──────────────────────────────────────────────────
    if any(w in msg for w in ('enter', 'entry', 'when to buy', 'when should i', 'buy now', 'good time')):
        ress_str = levels(ress)
        sups_str = levels(sups)
        if trend_score > 15:
            return (
                f"{tk} is in an uptrend (score {trend_score:+d}). For calls, consider entering on a confirmed break above "
                f"**{ress_str}** with volume confirmation.\n\n"
                f"For puts, wait for a break below support at {sups_str}.\n"
                f"⚠️ Never chase a move — wait for a confirmed level break."
            )
        elif trend_score < -15:
            return (
                f"{tk} is in a downtrend (score {trend_score:+d}). For puts, consider entering on a break below "
                f"**{sups_str}**.\n\n"
                f"For calls, be cautious — the trend is against you. Wait for a trend reversal signal.\n"
                f"⚠️ Educational only — not financial advice."
            )
        else:
            return (
                f"{tk} is in a sideways/neutral trend. Directional plays are riskier here.\n\n"
                f"Consider defined-risk plays like iron condors or straddles that profit from range-bound action.\n"
                f"Key levels: Support {sups_str} | Resistance {ress_str}."
            )

    # ── Stop loss / risk management ────────────────────────────────────────────
    if any(w in msg for w in ('stop', 'stop loss', 'risk', 'manage', 'how much', 'lose', 'loss')):
        return (
            f"**Risk management for {tk} options:**\n\n"
            f"• **Stop-loss**: Exit if the option drops to 50% of what you paid (e.g. paid $1.00 → exit at $0.50)\n"
            f"• **Take-profit**: Consider locking in gains at 2×–3× your premium (paid $1.00 → target $2.00–$3.00)\n"
            f"• **Position size**: Risk no more than 1–3% of your account on any single trade\n"
            f"• **Max daily loss**: Stop trading if down 5–6% on the day\n\n"
            f"⚠️ Options can go to zero. Always size your positions so a 100% loss is acceptable."
        )

    # ── Summary / overview ─────────────────────────────────────────────────────
    if any(w in msg for w in ('summary', 'overview', 'tell me about', 'analysis', 'what do you think', 'should i', 'trade this')):
        sups_str = levels(sups)
        ress_str = levels(ress)
        pat_str  = ', '.join(pat_names) if pat_names else 'none detected'
        ivr_str  = f"IVR {ivr_rank}" if ivr_rank is not None else 'IVR N/A'
        top_pick = calls[0] if (trend_score >= 0 and calls) else (puts[0] if puts else None)
        pick_str = ''
        if top_pick:
            side = 'call' if trend_score >= 0 else 'put'
            pick_str = f"\nTop {side}: {fmt(top_pick.get('strike'))} strike, {top_pick.get('expiry','?')} — Ask {fmt(top_pick.get('ask'))}"
        return (
            f"**{tk} @ {fmt(price)} — Quick Analysis**\n\n"
            f"Trend: {trend_label} (score {trend_score:+d})\n"
            f"IV: {ivr_str} — {ivr_interp}\n"
            f"Patterns: {pat_str}\n"
            f"Support: {sups_str} | Resistance: {ress_str}"
            + pick_str + '\n\n'
            f"⚠️ This is educational analysis — not a buy/sell recommendation."
        )

    # ── Fallback ────────────────────────────────────────────────────────────────
    sups_str = levels(sups)
    ress_str = levels(ress)
    return (
        f"I can answer questions about **{tk}** using the live data I have loaded.\n\n"
        f"Try asking:\n"
        f"• \"What's the trend?\"\n"
        f"• \"Best calls / best puts\"\n"
        f"• \"What are the key levels?\"\n"
        f"• \"Explain IV rank\" (or delta, theta, spreads, DTE…)\n"
        f"• \"Give me a summary\"\n\n"
        f"Current snapshot: {fmt(price)} | Trend: {trend_label} | Support: {sups_str} | Resistance: {ress_str}"
    )


@app.route('/api/chat', methods=['POST'])
def chat_assistant():
    """AI chat assistant — built-in engine (no API key) or Anthropic API if key provided."""
    try:
        import urllib.request as urlreq
        b       = request.get_json(force=True) or {}
        message = b.get('message', '').strip()
        api_key = b.get('api_key', '').strip()
        ctx     = b.get('context', {})

        if not message:
            return jsonify({'error': 'No message provided'}), 400

        # ── No API key → use built-in response engine ─────────────────────────
        if not api_key:
            reply = _builtin_chat_reply(message, ctx)
            return jsonify({'reply': reply, 'source': 'builtin'})

        # ── API key provided → call Anthropic ─────────────────────────────────
        ticker  = ctx.get('ticker', 'N/A')
        price   = ctx.get('current_price', 0)
        trend   = ctx.get('trend', {})
        ivr     = ctx.get('iv_rank', {})
        pats    = ', '.join(p.get('pattern', '') for p in ctx.get('chart_patterns', [])) or 'None detected'
        sups    = ', '.join(f"${s:.2f}" for s in (ctx.get('support_levels') or [])[:3]) or 'N/A'
        ress    = ', '.join(f"${r:.2f}" for r in (ctx.get('resistance_levels') or [])[:3]) or 'N/A'

        system_msg = f"""You are an expert options trading assistant inside Options Scout — a desktop app for analyzing stocks and options.

Currently analyzing: {ticker} @ ${price:.2f}
Trend: {trend.get('label', 'N/A')} (score {trend.get('score', 'N/A')})
IV Rank: {ivr.get('rank', 'N/A')} — {ivr.get('interpretation', '')}
Key supports: {sups}
Key resistances: {ress}
Chart patterns: {pats}

Rules:
- Be concise and actionable (2-4 sentences max unless detail is requested)
- Reference the live data above when relevant
- Always note that options trading involves significant risk
- Never guarantee profits or give buy/sell recommendations
- This is educational — not financial advice"""

        payload = json.dumps({
            'model': 'claude-3-5-haiku-20241022',
            'max_tokens': 600,
            'system': system_msg,
            'messages': [{'role': 'user', 'content': message}]
        }).encode('utf-8')

        req = urlreq.Request(
            'https://api.anthropic.com/v1/messages',
            data=payload,
            headers={
                'x-api-key':            api_key,
                'anthropic-version':    '2023-06-01',
                'content-type':         'application/json',
            }
        )
        try:
            with urlreq.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode('utf-8'))
        except Exception as http_err:
            err_str = str(http_err)
            if '401' in err_str:
                return jsonify({'error': 'Invalid API key — check your Anthropic key'}), 401
            if '429' in err_str:
                return jsonify({'error': 'API rate limited — wait a moment and try again'}), 429
            raise

        reply = (data.get('content') or [{}])[0].get('text', 'No response')
        return jsonify({'reply': reply, 'source': 'anthropic'})

    except Exception as e:
        logger.exception('chat_assistant')
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)
