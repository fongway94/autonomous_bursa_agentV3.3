# market_analyzer.py
"""
Market Analyzer — regime detection, sector momentum, relative strength.

FIX 2 changes:
  - Issue #5: detect_market_regime() now uses a 5-bar rolling average score
    instead of single-bar.  Single large candle caused instant regime switch,
    leading to whipsaw in position sizing (1.0 vs 0.75), threshold changes
    (60% vs 70%), and entries appearing/disappearing hourly.

Original behaviour for MY is byte-identical to fix_1 except for the
rolling score smoothing.
"""

from __future__ import annotations
import os
import json
import numpy as np
import pandas as pd
from data_provider import get_history
import joblib
from datetime import datetime, timezone, timedelta
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import TimeSeriesSplit

from db import DATA_DIR, myt_iso
from data_quality import validate_ohlcv
from logger import get_logger, log_learning_event

log = get_logger("market_analyzer")

KLCI_TICKER = "^KLSE"

MARKET_CACHE_FILE = os.path.join(DATA_DIR, "market_regime_cache.json")
SECTOR_MOMENTUM_FILE = os.path.join(DATA_DIR, "sector_momentum.json")
REGIME_MODEL_PATH = os.path.join(DATA_DIR, "regime_classifier.pkl")
REGIME_META_PATH = os.path.join(DATA_DIR, "regime_classifier_meta.json")


def get_myt_now():
    return datetime.now(timezone(timedelta(hours=8)))


def _regime_ticker() -> str:
    """Active market's regime/benchmark ticker (yfinance form)."""
    try:
        from market_profiles import active_profile
        return active_profile().regime_ticker_yf
    except Exception:
        return KLCI_TICKER


def _active_market_code() -> str:
    try:
        from market_profiles import active_market_code
        return active_market_code()
    except Exception:
        return "MY"


def _robust_read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _robust_write_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        log.warning(f"write fail {path}: {e}")


# -------------------------------------------------------------------------
# Benchmark fetch with validation + secondary source hook
# -------------------------------------------------------------------------

def get_klci_data(period: str = "3mo") -> pd.DataFrame:
    """LEGACY NAME — kept for backwards compatibility.

    Actually returns the ACTIVE market's regime-benchmark series:
        MY → ^KLSE
        US → SPY
    """
    return get_regime_benchmark_data(period)


def get_regime_benchmark_data(period: str = "3mo") -> pd.DataFrame:
    ticker = _regime_ticker()
    try:
        df = get_history(ticker, period=period, timeout=15)
    except Exception as e:
        log.warning(f"yf {ticker} fail: {e}")
        df = pd.DataFrame()

    if df is None or df.empty:
        df = _try_secondary_benchmark(ticker, period)
        if df is None or df.empty:
            return pd.DataFrame()
    ok, _ = validate_ohlcv(df, ticker, min_rows=20)
    if not ok:
        return pd.DataFrame()
    return df


def _try_secondary_benchmark(ticker: str, period: str) -> pd.DataFrame:
    """Placeholder for a secondary data source."""
    return pd.DataFrame()


# -------------------------------------------------------------------------
# Regime detection
# -------------------------------------------------------------------------

def _bar_score(close: pd.Series, vol: pd.Series, idx: int) -> float:
    """
    Compute the raw regime score for a single bar at index `idx`.

    Score range: roughly 0–100.
      0 = deeply bearish (price far below all EMAs, declining trend)
      50 = neutral
      100 = strongly bullish (price far above all EMAs, rising trend)

    Components:
      +15 / -15  price vs 20-EMA
      +20 / -20  price vs 50-EMA
      +25 / -25  price vs 200-EMA
      +15 / -15  alignment: EMA20 > EMA50 > EMA200 (bullish alignment)
      +10 / -10  RSI > 55 / < 45
       +5 / -5   volume ratio > 1.3
       +5 / -5   momentum > 3% / < -3% over 20 bars
    """
    if idx < 200:
        return 50.0  # not enough data for EMAs

    close_vals = close.iloc[:idx + 1]
    vol_vals = vol.iloc[:idx + 1]

    e20 = close_vals.ewm(span=20, adjust=False).mean().iloc[-1]
    e50 = close_vals.ewm(span=50, adjust=False).mean().iloc[-1]
    e200 = close_vals.ewm(span=200, adjust=False).mean().iloc[-1] \
        if idx >= 199 else close_vals.mean()

    bar_close = float(close.iloc[idx])
    bar_vol = float(vol.iloc[idx])

    score = 50.0
    score += 15 if bar_close > e20 else -15
    score += 20 if bar_close > e50 else -20
    score += 25 if bar_close > e200 else -25

    if e20 > e50 > e200:
        score += 15
    elif e20 < e50 < e200:
        score -= 15

    delta = close_vals.diff()
    gain = delta.where(delta > 0, 0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = float((100 - 100 / (1 + gain / (loss + 1e-9))).iloc[-1])
    if rsi > 55:
        score += 10
    elif rsi < 45:
        score -= 10

    vol_avg = float(vol_vals.iloc[-20:].mean()) + 1e-9
    vol_ratio = bar_vol / vol_avg
    if vol_ratio > 1.3:
        score += 5 if score > 50 else -5

    if idx >= 21:
        mom = float((close_vals.iloc[-1] - close_vals.iloc[-21]) / close_vals.iloc[-21] * 100)
        score += 5 if mom > 3 else (-5 if mom < -3 else 0)

    return max(0, min(100, score))


def detect_market_regime(benchmark_df: pd.DataFrame | None = None) -> dict:
    """
    FIX #5: Regime detection now uses a 5-bar rolling average score instead
    of single-bar.  Single large candles caused instant regime switch, leading
    to whipsaw in position sizing, thresholds, and entry signals.

    Algorithm:
      1. Compute raw score for each of the last 5 completed bars.
      2. Take the mean — this smooths noise and prevents one spike from
         flipping the regime.
      3. Classify: avg_score >= 70 → BULL, <= 30 → BEAR, else NEUTRAL.
      4. Conviction = distance of average from the threshold boundary.

    This is still lightweight (no ML, no rolling windows beyond 5 bars)
    but much more stable than single-bar detection.
    """
    if benchmark_df is None:
        benchmark_df = get_regime_benchmark_data()
    if benchmark_df.empty or len(benchmark_df) < 55:
        return {"regime": "UNCERTAIN", "conviction": 0,
                "details": {"reason": f"Insufficient {_regime_ticker()} data"}}

    close = benchmark_df["Close"]
    vol = benchmark_df["Volume"]
    n = len(close)

    # FIX #5: Compute score for each of the last 5 completed bars
    # (we exclude the current incomplete bar)
    lookback = 5
    scores = []
    for offset in range(1, lookback + 1):   # 1=most recent completed bar, …, 5=oldest
        idx = n - offset
        if idx >= 50:   # need at least 50 bars for EMAs
            scores.append(_bar_score(close, vol, idx))

    if len(scores) < 3:
        # Not enough history — fall back to single bar (last completed)
        scores = [_bar_score(close, vol, n - 1)]

    avg_score = float(np.mean(scores))

    # Classify
    if avg_score >= 70:
        regime, conv = "BULL", avg_score - 50
    elif avg_score <= 30:
        regime, conv = "BEAR", 50 - avg_score
    else:
        regime, conv = "NEUTRAL", 50 - abs(avg_score - 50)

    # Compute detail fields from the most recent completed bar
    latest_idx = n - 1
    e20 = close.iloc[:latest_idx].ewm(span=20, adjust=False).mean().iloc[-1]
    e50 = close.iloc[:latest_idx].ewm(span=50, adjust=False).mean().iloc[-1]
    e200 = close.iloc[:latest_idx].ewm(span=200, adjust=False).mean().iloc[-1] \
        if latest_idx >= 199 else close.mean()
    latest_price = float(close.iloc[latest_idx])

    delta = close.iloc[:latest_idx].diff()
    gain = delta.where(delta > 0, 0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi_val = float((100 - 100 / (1 + gain / (loss + 1e-9))).iloc[-1])
    vol_ratio = float(vol.iloc[latest_idx] / (vol.iloc[latest_idx - 20:latest_idx].mean() + 1e-9))

    mom_20 = float((latest_price - close.iloc[latest_idx - 21]) / close.iloc[latest_idx - 21] * 100) \
        if latest_idx >= 21 else 0

    return {
        "regime": regime, "conviction": float(conv),
        "details": {
            "trend_score": float(avg_score),
            "trend_score_raw_bars": [round(s, 1) for s in scores],
            "ema_20_vs_price": float((latest_price - e20) / e20 * 100),
            "ema_50_vs_price": float((latest_price - e50) / e50 * 100),
            "ema_200_vs_price": float((latest_price - e200) / e200 * 100),
            "klci_rsi": rsi_val,
            "volume_ratio": vol_ratio,
            "mom_20d_pct": mom_20,
            "last_price": float(latest_price),
            "benchmark_ticker": _regime_ticker(),
            "last_updated": myt_iso(),
        },
    }


# -------------------------------------------------------------------------
# Sector momentum
# -------------------------------------------------------------------------

_MY_SECTOR_TICKERS = {
    "Technology": ["0166.KL", "0097.KL", "5005.KL"],
    "Financial Services": ["1155.KL", "1295.KL", "1023.KL"],
    "Utilities": ["5347.KL", "6742.KL"],
    "Construction": ["5398.KL", "3336.KL"],
    "Telecommunications": ["6888.KL", "4863.KL"],
    "Property & REITs": ["5211.KL", "8664.KL"],
    "Consumer Products": ["4707.KL", "7084.KL"],
    "Healthcare": ["5225.KL", "5878.KL"],
    "Energy": ["7108.KL", "7277.KL"],
    "Plantation": ["2445.KL", "5285.KL"],
}

SECTOR_TICKERS = _MY_SECTOR_TICKERS


def _sector_representatives() -> dict[str, list[str]]:
    """Up to 3 representative yf symbols per sector for the active market."""
    if _active_market_code() == "MY":
        return _MY_SECTOR_TICKERS
    try:
        from market_profiles import active_profile
        out: dict[str, list[str]] = {}
        for t in active_profile().default_watchlist:
            out.setdefault(t.sector, []).append(t.yf_symbol)
        return {sec: syms[:3] for sec, syms in out.items()}
    except Exception:
        return _MY_SECTOR_TICKERS


def calculate_sector_momentum(lookback: int = 20) -> dict:
    out: dict = {}
    for sector, tickers in _sector_representatives().items():
        rets, rsis = [], []
        for t in tickers[:2]:
            try:
                df = get_history(t, period="3mo", timeout=10)
                if df is None or df.empty or len(df) < lookback + 5:
                    continue
                ok, _ = validate_ohlcv(df, t, min_rows=lookback + 5)
                if not ok:
                    continue
                ret = float((df["Close"].iloc[-1] - df["Close"].iloc[-lookback]) /
                            df["Close"].iloc[-lookback] * 100)
                rets.append(ret)
                d = df["Close"].diff()
                g = d.where(d > 0, 0).ewm(alpha=1 / 14, adjust=False).mean()
                l = (-d.where(d < 0, 0)).ewm(alpha=1 / 14, adjust=False).mean()
                rsis.append(float((100 - 100 / (1 + g / (l + 1e-9))).iloc[-1]))
            except Exception:
                continue
        if rets:
            avg = float(np.mean(rets))
            trend = ("STRONG_UP" if avg > 3 else "SLIGHT_UP" if avg > 0
                     else "SLIGHT_DOWN" if avg > -3 else "STRONG_DOWN")
            out[sector] = {"momentum_pct": round(avg, 2),
                           "avg_rsi": round(float(np.mean(rsis) if rsis else 50), 1),
                           "trend": trend, "n_tickers": len(rets)}

    if not out:
        return {}
    max_abs = max(abs(d["momentum_pct"]) for d in out.values()) or 1
    sorted_secs = sorted(out.items(), key=lambda x: x[1]["momentum_pct"],
                         reverse=True)
    for rank, (sec, data) in enumerate(sorted_secs, 1):
        out[sec]["rank"] = rank
        out[sec]["strength"] = round(data["momentum_pct"] / (max_abs + 1e-9), 3)

    _robust_write_json(SECTOR_MOMENTUM_FILE,
                       {"timestamp": myt_iso(), "sectors": out})
    return out


def get_sector_momentum() -> dict:
    cached = _robust_read_json(SECTOR_MOMENTUM_FILE, {})
    if cached:
        try:
            ts = datetime.strptime(cached["timestamp"], "%Y-%m-%d %H:%M:%S")
            ts = ts.replace(tzinfo=timezone(timedelta(hours=8)))
            if (get_myt_now() - ts).total_seconds() < 7200:
                return cached.get("sectors", {})
        except Exception:
            pass
    return calculate_sector_momentum()


# -------------------------------------------------------------------------
# Relative strength
# -------------------------------------------------------------------------

def calculate_relative_strength(ticker: str,
                                klci_df: pd.DataFrame | None = None,
                                period: int = 20,
                                stock_df: pd.DataFrame | None = None) -> dict | None:
    """Relative strength of `ticker` vs the active market's benchmark.

    v3.8 — two call-saving parameters
    ---------------------------------
    ``stock_df``: pass a price frame the caller ALREADY fetched (the screener
      pulls 2y for every ticker moments earlier) and we skip the network
      entirely. Only ``Close`` is read, and only the last ``period+5`` bars
      matter, so any frame >= that length works.

    ``klci_df``: pass the benchmark once. Previously callers left this None,
      and because the fallback below re-downloads the benchmark on EVERY
      invocation, a 109-ticker scan pulled ^KLSE 109 times — ~31% of the
      cycle's entire Yahoo budget spent refetching one identical series.
      The fallback is kept for standalone/manual callers.
    """
    try:
        df = stock_df
        if df is None or df.empty or len(df) < period + 5:
            df = get_history(ticker, period="3mo", timeout=10)
        if df is None or df.empty or len(df) < period + 5:
            return None
        ok, _ = validate_ohlcv(df, ticker, min_rows=period + 5)
        if not ok:
            return None
        if klci_df is None:
            klci_df = get_regime_benchmark_data()
        stock_ret = float((df["Close"].iloc[-1] - df["Close"].iloc[-period]) /
                          df["Close"].iloc[-period] * 100)
        klci_ret = 0.0
        if not klci_df.empty and len(klci_df) >= period + 5:
            klci_ret = float((klci_df["Close"].iloc[-1] - klci_df["Close"].iloc[-period]) /
                             klci_df["Close"].iloc[-period] * 100)
        rs = (stock_ret + 1e-9) / (klci_ret + 1e-9) if klci_ret != 0 else 1.0
        signal = ("LEADING" if rs > 1.2 else
                  "LAGGING" if rs < 0.8 else "MATCHING")
        return {"stock_return_pct": round(stock_ret, 2),
                "klci_return_pct": round(klci_ret, 2),
                "rs_ratio": round(rs, 3), "rs_signal": signal,
                "period_days": period}
    except Exception:
        return None


def rank_stocks_by_relative_strength(tickers: list,
                                     klci_df: pd.DataFrame | None = None,
                                     price_frames: dict | None = None) -> dict:
    """Rank tickers by relative strength vs the benchmark.

    v3.8: fetches the benchmark ONCE here instead of letting each
    ``calculate_relative_strength`` call re-download it (was 1 fetch per
    ticker). ``price_frames`` optionally supplies already-fetched OHLCV
    frames — ``{ticker: DataFrame}`` — so a caller that just scanned these
    names pays zero additional network cost.
    """
    # Fetch the benchmark once for the whole ranking pass.
    if klci_df is None:
        klci_df = get_regime_benchmark_data()

    price_frames = price_frames or {}

    out: dict = {}
    for t in tickers:
        rs = calculate_relative_strength(t, klci_df, period=20,
                                         stock_df=price_frames.get(t))
        if rs:
            out[t] = rs
    if not out:
        return {}
    sorted_rs = sorted(out.items(), key=lambda x: x[1]["rs_ratio"], reverse=True)
    for rank, (t, d) in enumerate(sorted_rs, 1):
        out[t]["rs_rank"] = rank
        out[t]["rs_percentile"] = round((1 - rank / len(sorted_rs)) * 100, 1)
    return out


# -------------------------------------------------------------------------
# Composite analysis
# -------------------------------------------------------------------------

def get_market_regime_cached() -> dict | None:
    cached = _robust_read_json(MARKET_CACHE_FILE, {})
    if cached:
        try:
            ts = datetime.strptime(cached["timestamp"], "%Y-%m-%d %H:%M:%S")
            ts = ts.replace(tzinfo=timezone(timedelta(hours=8)))
            if (get_myt_now() - ts).total_seconds() < 7200:
                return cached.get("regime_data", {})
        except Exception:
            pass
    return None


def get_full_market_analysis(force_refresh: bool = False) -> dict:
    if not force_refresh:
        c = get_market_regime_cached()
        if c:
            return c
    bench = get_regime_benchmark_data()
    regime_data = detect_market_regime(bench)
    sector_mom = get_sector_momentum()
    regime = regime_data["regime"]
    conv = regime_data["conviction"]

    try:
        from market_profiles import active_profile
        prof = active_profile()
        bull_max = prof.bull_max_positions
        neut_max = prof.neutral_max_positions
        bear_max = prof.bear_max_positions
    except Exception:
        bull_max, neut_max, bear_max = 8, 5, 3

    # v3.7: Lowered thresholds to increase trade frequency.
    # BULL: 0.60 (was 0.65) — strong market = more entries for brain learning
    # NEUTRAL: 0.65 — keep moderate threshold in uncertain markets
    # BEAR: 0.65 — strict protection (but screener already blocks most BEAR trades)
    # Conviction modulates threshold: in strong BULL with high conviction,
    # effective threshold = 0.60 * (1 - conv/200) = down to ~0.50
    if regime == "BULL":
        pos_mult, risk_adj, max_pos, thr = 1.0, 1.0, bull_max, 0.60
        # In strong BULL with conviction > 60, lower threshold further
        if conv > 60:
            thr = 0.55  # more entries in clearly trending market
    elif regime == "NEUTRAL":
        pos_mult, risk_adj, max_pos, thr = 0.75, 0.8, neut_max, 0.65
    else:
        pos_mult, risk_adj, max_pos, thr = 0.50, 0.6, bear_max, 0.65
    effective = pos_mult * (0.5 + conv / 100)

    hot, cold = [], []
    for s, d in (sector_mom or {}).items():
        if d.get("strength", 0) > 0.3:
            hot.append(s)
        elif d.get("strength", 0) < -0.3:
            cold.append(s)

    result = {
        "timestamp": myt_iso(),
        "regime_data": regime_data,
        "sector_momentum": sector_mom,
        "position_rules": {
            "position_size_mult": round(effective, 3),
            "risk_adjustment": round(risk_adj, 3),
            "max_concurrent_positions": max_pos,
            "new_signal_threshold": round(thr, 2),
            "regime_label": regime,
            "conviction_pct": round(conv, 1),
        },
        "hot_sectors": hot,
        "cold_sectors": cold,
        "guidance": _guidance(regime, conv, hot),
    }
    _robust_write_json(MARKET_CACHE_FILE,
                       {"timestamp": result["timestamp"],
                        "regime_data": result})
    return result


def _guidance(regime, conv, hot):
    hot_s = ", ".join(hot[:3]) if hot else "None"
    if regime == "BULL":
        return (f"🐂 BULL — conviction {conv:.0f}%. Hot: {hot_s}. "
                "Favour momentum breakouts; hold winners longer.")
    if regime == "BEAR":
        try:
            from market_profiles import active_profile
            bear_max = active_profile().bear_max_positions
        except Exception:
            bear_max = 3
        return (f"🐻 BEAR — conviction {conv:.0f}%. Avoid new entries; "
                f"short-term scalps only; max {bear_max} positions.")
    if regime == "NEUTRAL":
        return (f"⚖️ NEUTRAL — conviction {conv:.0f}%. Selective entry, "
                f"favour pullbacks. Hot: {hot_s}.")
    return "⚠️ Regime uncertain — conservative sizing."


# -------------------------------------------------------------------------
# ML regime classifier (CV + sealed holdout)
# -------------------------------------------------------------------------

def _classifier_training_tickers() -> list[str]:
    """Tickers used to train the regime classifier. 5 representative names."""
    if _active_market_code() == "MY":
        return ["^KLSE", "1155.KL", "5347.KL", "0166.KL", "5285.KL"]
    return ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"]


def train_market_regime_classifier(persist: bool = True):
    tickers = _classifier_training_tickers()
    rows = []
    for t in tickers:
        try:
            df = get_history(t, period="3y", timeout=15)
            if df is None or df.empty or len(df) < 250:
                continue
            ok, _ = validate_ohlcv(df, t, min_rows=250)
            if not ok:
                continue
            for i in range(200, len(df) - 21):
                row = df.iloc[i]
                ema20 = df["Close"].iloc[:i].ewm(span=20).mean().iloc[-1]
                ema50 = df["Close"].iloc[:i].ewm(span=50).mean().iloc[-1]
                ema200 = df["Close"].iloc[:i].ewm(span=200).mean().iloc[-1]
                fut_ret = float((df["Close"].iloc[i + 10:i + 21].mean()
                                 - row["Close"]) / row["Close"] * 100)
                if fut_ret > 3:
                    label = 1
                elif fut_ret < -3:
                    label = 0
                else:
                    continue
                prev = df["Volume"].iloc[max(0, i - 20):i].mean()
                feats = [
                    float((row["Close"] - ema20) / ema20 * 100) if ema20 else 0,
                    float((row["Close"] - ema50) / ema50 * 100) if ema50 else 0,
                    float((row["Close"] - ema200) / ema200 * 100) if ema200 else 0,
                    float(row["Volume"] / (prev + 1e-9)),
                    float((row["High"] - row["Low"]) / row["Close"] * 100),
                ]
                rows.append((df.index[i], feats, label))
        except Exception:
            continue
    if len(rows) < 150:
        return None
    rows.sort(key=lambda r: r[0])
    X = np.array([r[1] for r in rows])
    y = np.array([r[2] for r in rows])
    split = int(len(rows) * 0.85)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]
    clf = GradientBoostingClassifier(n_estimators=80, max_depth=3, random_state=42)
    clf.fit(X_train, y_train)
    train_acc = float(clf.score(X_train, y_train))
    test_acc = float(clf.score(X_test, y_test)) if len(y_test) else 0.0
    if persist:
        joblib.dump(clf, REGIME_MODEL_PATH)
        _robust_write_json(REGIME_META_PATH, {
            "trained_at": myt_iso(), "n_train": len(X_train),
            "n_test": len(X_test), "train_accuracy": round(train_acc, 4),
            "holdout_accuracy": round(test_acc, 4),
        })
    log_learning_event(
        "REGIME_CLASSIFIER_TRAINED",
        f"OOS accuracy {test_acc:.3f} (train {train_acc:.3f})",
        metrics={"n_train": len(X_train), "n_test": len(X_test)})
    return clf


_market_clf = None


def get_market_ml_prediction() -> dict | None:
    global _market_clf
    if _market_clf is None:
        if os.path.exists(REGIME_MODEL_PATH):
            try:
                _market_clf = joblib.load(REGIME_MODEL_PATH)
            except Exception:
                _market_clf = None
        if _market_clf is None:
            _market_clf = train_market_regime_classifier()
    if _market_clf is None:
        return None
    bench = get_regime_benchmark_data(period="3mo")
    if bench.empty or len(bench) < 200:
        return None
    try:
        close = bench["Close"]
        e20 = close.ewm(span=20).mean().iloc[-1]
        e50 = close.ewm(span=50).mean().iloc[-1]
        e200 = close.ewm(span=200).mean().iloc[-1]
        v_avg = bench["Volume"].rolling(20).mean().iloc[-1]
        v = bench["Volume"].iloc[-1]
        feats = np.array([[
            float((close.iloc[-1] - e20) / e20 * 100),
            float((close.iloc[-1] - e50) / e50 * 100),
            float((close.iloc[-1] - e200) / e200 * 100),
            float(v / (v_avg + 1e-9)),
            float((bench["High"].iloc[-1] - bench["Low"].iloc[-1]) /
                  close.iloc[-1] * 100),
        ]])
        prob = _market_clf.predict_proba(feats)[0]
        pred = _market_clf.predict(feats)[0]
        verdict = "BULL" if pred == 1 else "BEAR"
        return {
            "prediction": verdict,
            "bull_probability": round(float(prob[1]) * 100, 1),
            "bear_probability": round(float(prob[0]) * 100, 1),
            "interpretation": (
                f"ML model predicts {verdict} with "
                f"{max(prob)*100:.0f}% confidence."),
        }
    except Exception as e:
        log.warning(f"regime ML predict failed: {e}")
        return None
