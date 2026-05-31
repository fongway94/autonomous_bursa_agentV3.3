#!/usr/bin/env python3
# intraday_backtest_v2.py
"""
Tuning iteration of intraday_backtest.py — v3.7 prove-the-edge round 2.

What's new vs v1:
    1. **Daily EMA-50 trend filter** (long-only): only take longs when
       prior-day's close > EMA-50 of the daily chart. Symmetric for shorts.
    2. **Short ORB**: mirror of the long rule. Triggers on the first 5m
       candle that closes BELOW OR_low with rel-vol confirm and (optional)
       price < VWAP. Only enabled in down-trending names.
    3. **Side-aware bookkeeping**: separate `side='LONG'/'SHORT'`. R math
       is signed: long R = (exit-entry)/(entry-stop); short R = (entry-exit)/(stop-entry).

Everything else (force-flat, one-trade-per-day, rel-vol, target = R × range)
is identical to v1.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Optional

import pandas as pd

# Re-use everything pure from v1 so we don't drift.
from intraday_backtest import (
    ORBConfig,
    Trade,
    BacktestSummary,
    DEFAULT_US_TICKERS,
    compute_session_vwap,
    compute_session_relative_volume,
    compute_opening_range,
    _split_into_sessions,
    format_text_report,
)
import data_provider


# ---------------------------------------------------------------------------
# Daily trend filter
# ---------------------------------------------------------------------------

def compute_daily_ema_trend(daily_df: pd.DataFrame, ema_len: int = 50) -> pd.Series:
    """Boolean Series indexed by date: True if close > EMA(ema_len) on that
    day. Calling code should look up `trend[session_date]` and only take
    a LONG when True (and a SHORT when False)."""
    if daily_df.empty:
        return pd.Series(dtype=bool)
    ema = daily_df["Close"].ewm(span=ema_len, adjust=False).mean()
    above = daily_df["Close"] > ema
    # Use date as index for easy lookup. Use prior-day's value to avoid
    # look-ahead (we decide direction BEFORE the session opens).
    above.index = pd.to_datetime(above.index).date
    return above.shift(1).fillna(False)


# ---------------------------------------------------------------------------
# v2 simulator: long + short with trend filter
# ---------------------------------------------------------------------------

def simulate_orb_v2(ticker: str,
                    day_df: pd.DataFrame,
                    cfg: ORBConfig,
                    *,
                    allow_long: bool,
                    allow_short: bool) -> Optional[Trade]:
    """One trade per day, the FIRST signal on either side that qualifies.

    Long trigger:  close > OR_high  AND  rel_vol ≥ thr  AND  (no VWAP filter OR close > VWAP)
    Short trigger: close < OR_low   AND  rel_vol ≥ thr  AND  (no VWAP filter OR close < VWAP)
    """
    if day_df.empty:
        return None

    or_high, or_low = compute_opening_range(day_df, cfg.session_open, cfg.opening_range_minutes)
    if or_high is None or or_low is None:
        return None
    or_range = or_high - or_low
    if or_range <= 0:
        return None

    or_end_time = (datetime.combine(date.today(), cfg.session_open)
                   + timedelta(minutes=cfg.opening_range_minutes)).time()
    post_or = day_df.between_time(or_end_time, cfg.flat_by, inclusive="left")
    if post_or.empty:
        return None

    vwap = compute_session_vwap(day_df)
    rvol = compute_session_relative_volume(day_df)

    entry_idx = None
    side = None
    for ts, bar in post_or.iterrows():
        rv = rvol.loc[ts]
        if pd.isna(rv) or rv < cfg.rel_vol_threshold:
            continue
        # Long check
        if allow_long and bar["Close"] > or_high:
            if cfg.require_vwap_support and not (bar["Close"] > vwap.loc[ts]):
                pass
            else:
                entry_idx = ts; side = "LONG"; break
        # Short check
        if allow_short and bar["Close"] < or_low:
            if cfg.require_vwap_support and not (bar["Close"] < vwap.loc[ts]):
                pass
            else:
                entry_idx = ts; side = "SHORT"; break

    if entry_idx is None:
        return None

    entry_price = float(post_or.loc[entry_idx, "Close"])
    if side == "LONG":
        stop_loss = float(or_low)
        rps = entry_price - stop_loss
        if rps <= 0:
            return None
        target = entry_price + cfg.target_r_multiple * or_range
    else:  # SHORT
        stop_loss = float(or_high)
        rps = stop_loss - entry_price
        if rps <= 0:
            return None
        target = entry_price - cfg.target_r_multiple * or_range

    after = day_df.loc[day_df.index > entry_idx].between_time(
        cfg.session_open, cfg.flat_by, inclusive="left"
    )

    exit_time = exit_price = exit_reason = None
    for ts, bar in after.iterrows():
        if side == "LONG":
            hit_stop = bar["Low"] <= stop_loss
            hit_target = bar["High"] >= target
        else:
            hit_stop = bar["High"] >= stop_loss
            hit_target = bar["Low"] <= target
        if hit_stop and hit_target:
            exit_time, exit_price, exit_reason = ts, stop_loss, "STOP"; break
        if hit_stop:
            exit_time, exit_price, exit_reason = ts, stop_loss, "STOP"; break
        if hit_target:
            exit_time, exit_price, exit_reason = ts, target, "TARGET"; break

    if exit_time is None:
        last_ts = after.index[-1] if len(after) else entry_idx
        exit_time = last_ts
        exit_price = float(after["Close"].iloc[-1]) if len(after) else entry_price
        exit_reason = "FORCE_FLAT"

    # Signed R
    if side == "LONG":
        r = (exit_price - entry_price) / rps
    else:
        r = (entry_price - exit_price) / rps

    t = Trade(
        ticker=ticker,
        session_date=entry_idx.date(),
        entry_time=entry_idx.to_pydatetime(),
        entry_price=entry_price,
        stop_loss=stop_loss,
        target=target,
        exit_time=exit_time.to_pydatetime() if hasattr(exit_time, "to_pydatetime") else exit_time,
        exit_price=float(exit_price),
        exit_reason=exit_reason,
        or_high=float(or_high),
        or_low=float(or_low),
        or_range=float(or_range),
        r_multiple=float(r),
    )
    # Stash side in exit_reason suffix so it shows up in reports.
    t.exit_reason = f"{exit_reason}_{side}"
    return t


def backtest_ticker_v2(ticker: str,
                       df_intraday: pd.DataFrame,
                       df_daily: pd.DataFrame,
                       cfg: ORBConfig,
                       *,
                       use_trend_filter: bool,
                       allow_longs: bool = True,
                       allow_shorts: bool = True) -> list[Trade]:
    """Run v2 ORB across every session, optionally gated by daily-EMA-50 trend."""
    trades: list[Trade] = []
    trend = (compute_daily_ema_trend(df_daily) if use_trend_filter
             else pd.Series(dtype=bool))
    for d, day_df in _split_into_sessions(df_intraday, cfg.session_open, cfg.session_close):
        if use_trend_filter:
            is_up = bool(trend.get(d, False))
            allow_long  = allow_longs  and is_up
            allow_short = allow_shorts and (not is_up)
        else:
            allow_long  = allow_longs
            allow_short = allow_shorts
        if not (allow_long or allow_short):
            continue
        t = simulate_orb_v2(ticker, day_df, cfg,
                            allow_long=allow_long, allow_short=allow_short)
        if t is not None:
            trades.append(t)
    return trades


def run_v2(tickers: list[str],
           cfg: ORBConfig,
           *,
           use_trend_filter: bool,
           allow_longs: bool = True,
           allow_shorts: bool = True,
           verbose: bool = False) -> BacktestSummary:
    summary = BacktestSummary(config=cfg, tickers=list(tickers))
    for tk in tickers:
        try:
            df_5m = data_provider.get_history(tk, interval=cfg.interval)
        except Exception:
            continue
        if df_5m is None or df_5m.empty:
            continue
        if not {"Open", "High", "Low", "Close", "Volume"}.issubset(df_5m.columns):
            continue
        # Need daily history if we're using the trend filter.
        df_daily = pd.DataFrame()
        if use_trend_filter:
            try:
                df_daily = data_provider.get_history(tk, period="6mo")
            except Exception:
                df_daily = pd.DataFrame()
            if df_daily is None or df_daily.empty:
                # Skip ticker rather than fall through (can't apply filter).
                continue
        trades = backtest_ticker_v2(
            tk, df_5m, df_daily, cfg,
            use_trend_filter=use_trend_filter,
            allow_longs=allow_longs,
            allow_shorts=allow_shorts,
        )
        summary.trades.extend(trades)
        if verbose:
            wins = sum(1 for t in trades if t.r_multiple > 0)
            ttl  = sum(t.r_multiple for t in trades)
            longs  = sum(1 for t in trades if "_LONG"  in t.exit_reason)
            shorts = sum(1 for t in trades if "_SHORT" in t.exit_reason)
            print(f"  {tk:6s}  n={len(trades):3d}  L/S={longs}/{shorts}  "
                  f"wins={wins:3d}  total={ttl:+.2f}R  avg={ttl/len(trades) if trades else 0:+.2f}R")
    return summary


def verdict(s: BacktestSummary) -> str:
    if s.n_trades < 30:
        return f"INSUFFICIENT ({s.n_trades})"
    if s.expectancy_r >= 0.10 and s.win_rate >= 0.40 and s.max_consecutive_losers <= 8:
        return "✅ EDGE"
    if s.expectancy_r >= 0.10 and s.win_rate >= 0.40:
        return "⚠️ rough DD"
    if s.expectancy_r > 0:
        return "⚠️ marginal"
    return "❌ no edge"


if __name__ == "__main__":
    bull20 = ["TQQQ","SPXL","SOXL","UPRO","FNGU","TNA","IBIT","MSTR","COIN","MARA",
              "NVDA","TSLA","AMD","META","AAPL","MSFT","GOOGL","AMZN","PLTR","UVXY"]
    all23 = list(DEFAULT_US_TICKERS)

    cfg30_2 = ORBConfig(opening_range_minutes=30, target_r_multiple=2.0)
    cfg15_2 = ORBConfig(opening_range_minutes=15, target_r_multiple=2.0)
    cfg30_15 = ORBConfig(opening_range_minutes=30, target_r_multiple=1.5)

    print(f"{'CONFIG':<55} {'N':>4} {'WIN%':>5} {'AVG R':>7} {'TOTAL R':>8} {'CL':>3}  VERDICT")
    print("-" * 105)
    for label, tickers, cfg, kw in [
        # Baseline (v1, longs only, no trend filter) — for comparison
        ("BASELINE v1: all23 OR=30 R=2.0 longs-only no-trend",
            all23, cfg30_2,
            dict(use_trend_filter=False, allow_shorts=False)),

        # +Trend filter, longs only
        ("v2: all23 OR=30 R=2.0 longs-only +EMA-50 trend",
            all23, cfg30_2,
            dict(use_trend_filter=True, allow_shorts=False)),
        ("v2: bull20 OR=30 R=2.0 longs-only +EMA-50 trend",
            bull20, cfg30_2,
            dict(use_trend_filter=True, allow_shorts=False)),
        ("v2: bull20 OR=15 R=2.0 longs-only +EMA-50 trend",
            bull20, cfg15_2,
            dict(use_trend_filter=True, allow_shorts=False)),

        # +Shorts, no trend filter (just to see if shorts add value)
        ("v2: all23 OR=30 R=2.0 LONGS+SHORTS no-trend",
            all23, cfg30_2,
            dict(use_trend_filter=False, allow_shorts=True)),
        ("v2: bull20 OR=30 R=2.0 LONGS+SHORTS no-trend",
            bull20, cfg30_2,
            dict(use_trend_filter=False, allow_shorts=True)),

        # +Trend filter + shorts (the full v2)
        ("v2 FULL: all23 OR=30 R=2.0 L+S +EMA-50 trend",
            all23, cfg30_2,
            dict(use_trend_filter=True, allow_shorts=True)),
        ("v2 FULL: bull20 OR=30 R=2.0 L+S +EMA-50 trend",
            bull20, cfg30_2,
            dict(use_trend_filter=True, allow_shorts=True)),
        ("v2 FULL: bull20 OR=15 R=2.0 L+S +EMA-50 trend",
            bull20, cfg15_2,
            dict(use_trend_filter=True, allow_shorts=True)),
        ("v2 FULL: bull20 OR=30 R=1.5 L+S +EMA-50 trend",
            bull20, cfg30_15,
            dict(use_trend_filter=True, allow_shorts=True)),
    ]:
        data_provider.reset()
        s = run_v2(tickers, cfg, **kw)
        v = verdict(s)
        print(f"{label:<55} {s.n_trades:>4} {s.win_rate*100:>4.0f}% {s.avg_r:>+7.3f} {s.total_r:>+8.2f} {s.max_consecutive_losers:>3}  {v}")
