#!/usr/bin/env python3
# intraday_screener.py
"""
Intraday ORB (Opening Range Breakout) screener — v3.7 Block 3.

WHAT THIS DOES:
    1. For each ticker in the US intraday watchlist, fetches today's 5m bars
       (up to now) and daily bars (for the EMA-200 trend filter).
    2. After the opening-range window closes (15 min after session open),
       checks each ticker for an ORB breakout signal.
    3. Returns a list of signal dicts in the SAME SHAPE as the swing
       screener (screener.screen_all_stocks output) so the trading engine
       and UI consume them identically — plus a `source: "INTRADAY"` key.

WHAT THIS DOES NOT DO:
    * Place any orders (read-only signal generation).
    * Write to the DB (caller feeds signals into the engine).
    * Send any alerts (caller dispatches via live_trigger).

STRATEGY — ORB with daily EMA-200 trend filter (round-4 winner):
    1. Opening Range (OR) = high/low of the first 15 minutes (default).
    2. AFTER the OR window closes, scan each bar. A LONG entry triggers
       on the FIRST bar that:
         * closes ABOVE OR_high
         * volume >= rel_vol_threshold × session-avg-so-far
         * close > session VWAP (if require_vwap_support)
         * AND the prior daily close > EMA-200 (trend filter, longs-only)
    3. Stop = OR_low (structural invalidation point).
    4. Targets = entry + R × OR_range (1.5R / 2.0R / 2.5R).
    5. Only fires ONE signal per ticker per session. If already triggered
       this session (caller tracks active trades), skips.

SIGNAL GRADING:
    * GOLD BUY (ORB)  -> all filters pass (VWAP, rel-vol, trend, OR breakout)
    * SILVER BUY (ORB) -> most filters pass, one minor weakness
    * NO SIGNAL        -> no breakout or major filter block

PARAMETERS (locked in from round-4 360-day validation):
    * Universe: curated-6 default (TNA, GOOGL, TQQQ, MSTR, SOXL, PLTR)
    * OR: 15 min | Target: 2.0R | Rel-vol: 1.2x | EMA: 200 daily
    * VWAP support: True | Longs-only: True | Flat-by: 15:55 ET

USAGE:
    from intraday_screener import screen_intraday, DEFAULT_INTRADAY_WATCHLIST
    signals = screen_intraday(DEFAULT_INTRADAY_WATCHLIST)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date, time as dtime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from intraday_backtest import (
    ORBConfig,
    compute_session_vwap,
    compute_session_relative_volume,
    compute_opening_range,
)
from intraday_backtest_v2 import compute_daily_ema_trend
import data_provider


US_EASTERN = ZoneInfo("America/New_York")

INTRADAY_DEFAULTS = ORBConfig(
    interval="5m",
    opening_range_minutes=15,
    target_r_multiple=2.0,
    rel_vol_threshold=1.2,
    require_vwap_support=True,
    session_open=dtime(9, 30),
    session_close=dtime(16, 0),
    flat_by=dtime(15, 55),
)

DEFAULT_INTRADAY_WATCHLIST = [
    "TNA", "GOOGL", "TQQQ", "MSTR", "SOXL", "PLTR",
]

INTRADAY_EMA_LENGTH = 200
INTRADAY_LONGS_ONLY = True
INTRADAY_EXPLORER_TARGET = 100


# ---------------------------------------------------------------------------
# Signal computation (pure function — no I/O, fully testable)
# ---------------------------------------------------------------------------

def compute_intraday_signal(
    ticker: str,
    df_5m: pd.DataFrame,
    df_daily: pd.DataFrame,
    cfg: ORBConfig = INTRADAY_DEFAULTS,
    *,
    ema_length: int = INTRADAY_EMA_LENGTH,
    now_et: Optional[datetime] = None,
    already_triggered_today: bool = False,
) -> Optional[dict]:
    """Run ORB analysis on the CURRENT session's bars and return a signal dict
    in the same shape as the swing screener, or None if no valid signal.
    """
    if already_triggered_today:
        return None

    required = {"Open", "High", "Low", "Close", "Volume"}
    if df_5m is None or df_5m.empty or not required.issubset(df_5m.columns):
        return None

    if now_et is None:
        now_et = datetime.now(US_EASTERN)
    now_t = now_et.time()

    if now_t < cfg.session_open or now_t >= cfg.flat_by:
        return None

    or_end = (datetime.combine(date.today(), cfg.session_open)
              + timedelta(minutes=cfg.opening_range_minutes)).time()
    if now_t <= or_end:
        return None

    rth = df_5m.between_time(cfg.session_open, cfg.flat_by, inclusive="left")
    if rth.empty:
        return None

    or_high, or_low = compute_opening_range(rth, cfg.session_open,
                                            cfg.opening_range_minutes)
    if or_high is None or or_low is None:
        return None
    or_range = or_high - or_low
    if or_range <= 0:
        return None

    vwap_series = compute_session_vwap(rth)
    rvol_series = compute_session_relative_volume(rth)

    trend_direction = "UNKNOWN"
    if not df_daily.empty:
        trend = compute_daily_ema_trend(df_daily, ema_len=ema_length)
        session_date = rth.index[0].date() if hasattr(rth.index[0], 'date') \
                       else pd.Timestamp(rth.index[0]).date()
        # Try session_date first; if daily data doesn't include today's
        # partial bar, fall back to the prior trading day.
        try:
            is_uptrend = bool(trend.get(session_date, None))
            if is_uptrend is None:
                is_uptrend = bool(trend.get(session_date - timedelta(days=1), False))
        except Exception:
            is_uptrend = False
        trend_direction = "UP" if is_uptrend else "DOWN"
        if INTRADAY_LONGS_ONLY and not is_uptrend:
            return None

    post_or = rth.between_time(or_end, cfg.flat_by, inclusive="left")
    if post_or.empty:
        return None

    entry_ts = None
    entry_bar = None
    for ts, bar in post_or.iterrows():
        if float(bar["Close"]) <= or_high:
            continue
        rv = rvol_series.get(ts, float("nan"))
        if pd.isna(rv) or rv < cfg.rel_vol_threshold:
            continue
        if cfg.require_vwap_support:
            vw = vwap_series.get(ts, float("nan"))
            if pd.isna(vw) or float(bar["Close"]) <= vw:
                continue
        entry_ts = ts
        entry_bar = bar
        break

    if entry_bar is None:
        return None

    entry_price = float(entry_bar["Close"])
    stop_loss = float(or_low)
    risk_per_share = entry_price - stop_loss
    if risk_per_share <= 0:
        return None

    entry_volume = int(entry_bar["Volume"])
    rel_vol_val = round(float(rvol_series.get(entry_ts, 1.0)), 2)
    vwap_val = round(float(vwap_series.get(entry_ts, entry_price)), 2)

    tp1 = round(entry_price + 1.5 * or_range, 3)
    tp2 = round(entry_price + 2.0 * or_range, 3)
    tp3 = round(entry_price + 2.5 * or_range, 3)
    risk_pct = round((risk_per_share / entry_price) * 100, 1)

    confidence = 50.0
    reasoning_parts = [
        f"OR={cfg.opening_range_minutes}min breakout above {or_high:.2f}.",
        f"Rel-vol={rel_vol_val:.1f}x.",
        f"VWAP={vwap_val:.2f} support.",
        f"EMA-{ema_length} daily trend {trend_direction}.",
    ]

    if rel_vol_val >= 2.0:
        confidence += 15
        reasoning_parts.append("Strong volume confirmation.")
    elif rel_vol_val >= 1.5:
        confidence += 8
    if entry_price > vwap_val * 1.005:
        confidence += 5
    if trend_direction == "UP":
        confidence += 7
        reasoning_parts.append("Trend-aligned.")
    if or_range >= 1.0:
        confidence += 5
    if risk_pct <= 3.0:
        confidence += 5
    elif risk_pct <= 5.0:
        confidence += 2

    confidence = round(min(max(confidence, 5.0), 99.0), 1)

    if rel_vol_val >= cfg.rel_vol_threshold and trend_direction == "UP":
        signal_type = "GOLD BUY (ORB)"
        reasoning = " ".join(reasoning_parts)
    elif rel_vol_val >= cfg.rel_vol_threshold or trend_direction == "UP":
        signal_type = "SILVER BUY (ORB)"
        reasoning = " ".join(reasoning_parts)
    else:
        return None

    ts_str = entry_ts.isoformat() if hasattr(entry_ts, "isoformat") \
             else str(entry_ts)

    return {
        "ticker": ticker,
        "name": "",
        "sector": "",
        "source": "INTRADAY",
        "price": round(entry_price, 3),
        "prev_price": round(entry_price, 3),
        "change_pct": 0.0,
        "volume": entry_volume,
        "vol_ratio": rel_vol_val,
        "rsi": 50.0,
        "signal": signal_type,
        "reasoning": reasoning,
        "confidence": confidence,
        "entry": round(entry_price, 3),
        "stop_loss": round(stop_loss, 3),
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "risk_pct": risk_pct,
        "atr": round(or_range, 3),
        "support": round(or_low, 3),
        "resistance": round(or_high, 3),
        "ema_trend": 0.0,
        "ema_fast": 0.0,
        "ema_slow": 0.0,
        "macd_hist": 0.0,
        "bb_upper": 0.0,
        "bb_lower": 0.0,
        "market_regime": "",
        "rs_rank": None,
        "rs_signal": None,
        "rs_ratio": None,
        "q_action": None,
        "q_confidence": 0.0,
        "q_reasoning": None,
        "indicators": {
            "vwap": vwap_val,
            "rel_vol": rel_vol_val,
            "or_high": round(or_high, 3),
            "or_low": round(or_low, 3),
            "or_range": round(or_range, 3),
            "ema_trend_direction": trend_direction,
            "entry_timestamp": ts_str,
        },
    }


# ---------------------------------------------------------------------------
# Runner (fetches data, calls per-ticker signal function)
# ---------------------------------------------------------------------------

def _resolve_ticker_name(ticker: str) -> str:
    try:
        from watchlist import get_ticker_name
        return get_ticker_name(ticker) or ticker
    except Exception:
        return ticker


def _resolve_ticker_sector(ticker: str) -> str:
    try:
        from watchlist import get_ticker_sector
        return get_ticker_sector(ticker) or ""
    except Exception:
        return ""


def screen_intraday(
    tickers: Optional[list[str]] = None,
    cfg: ORBConfig = INTRADAY_DEFAULTS,
    *,
    ema_length: int = INTRADAY_EMA_LENGTH,
    now_et: Optional[datetime] = None,
    already_triggered: Optional[set[str]] = None,
) -> list[dict]:
    """Run the intraday ORB screener against a watchlist."""
    if tickers is None:
        tickers = list(DEFAULT_INTRADAY_WATCHLIST)
    if already_triggered is None:
        already_triggered = set()

    data_provider.ensure_probed()

    daily_data: dict[str, pd.DataFrame] = {}
    for tk in tickers:
        try:
            df_d = data_provider.get_history(tk, period="1y", timeout=30)
            if df_d is not None and not df_d.empty:
                daily_data[tk] = df_d
        except Exception:
            continue

    intraday_data: dict[str, pd.DataFrame] = {}
    for tk in tickers:
        try:
            df_5m = data_provider.get_history(
                tk, interval="5m", period="5d", timeout=30,
            )
            if df_5m is not None and not df_5m.empty:
                intraday_data[tk] = df_5m
        except Exception:
            continue

    signals: list[dict] = []
    for tk in tickers:
        df_5m = intraday_data.get(tk)
        df_d = daily_data.get(tk, pd.DataFrame())
        if df_5m is None or df_5m.empty:
            continue

        sig = compute_intraday_signal(
            tk, df_5m, df_d, cfg,
            ema_length=ema_length,
            now_et=now_et,
            already_triggered_today=(tk in already_triggered),
        )
        if sig is None:
            continue

        sig["name"] = _resolve_ticker_name(tk)
        sig["sector"] = _resolve_ticker_sector(tk)
        signals.append(sig)

    def _sort_key(s: dict) -> tuple:
        grade = 1 if "GOLD" in s["signal"] else 2
        return (grade, -s["confidence"])

    signals.sort(key=_sort_key)
    return signals


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys, json

    tickers = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_INTRADAY_WATCHLIST
    print(f"Scanning {len(tickers)} tickers for intraday ORB signals...")
    print(f"  Data source: {data_provider.provider_name()}")
    print()

    results = screen_intraday(tickers)

    if not results:
        print("  No signals generated.")
    else:
        print(f"  {len(results)} signal(s) generated:")
        print()
        for s in results:
            print(f"  {s['ticker']:<7} {s['signal']:<20} "
                  f"confidence={s['confidence']:.0f}  "
                  f"entry={s['entry']:.2f}  stop={s['stop_loss']:.2f}  "
                  f"or_range={s['indicators']['or_range']:.2f}")
        print()

    if "--json" in sys.argv:
        out_file = "intraday_signals.json"
        with open(out_file, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"  Written to {out_file}")
