#!/usr/bin/env python3
# intraday_backtest.py
"""
Minimal Opening-Range Breakout (ORB) backtest harness — v3.7 prove-the-edge.

Purpose
-------
Before we build the full intraday engine (Blocks 3–7 of the v3.7 plan),
we need to know whether ORB has an actual edge on our US universe. This
script answers exactly that question and nothing else:

    "If we had been running 5m ORB on this watchlist for the last ~60
     days, how many R would we have made/lost, what's the win rate, and
     are the results consistent enough to bother building the full engine?"

The functions in this file are intentionally pure and self-contained so
that `intraday_screener.py` (Block 3) and `intraday_engine.py` (Block 4)
can import them directly — no rewriting.

Strategy: Opening-Range Breakout (long-only v1)
-----------------------------------------------
1. Opening range (OR) = high/low of the first N minutes of the session
   (default 30 min = six 5-minute candles, 09:30–10:00 ET).
2. After 10:00 ET, a long entry triggers on the FIRST candle that closes
   ABOVE OR_high, with confirmation:
     - close > session VWAP (don't fight VWAP)
     - bar volume > rel_vol_threshold * avg volume of the session so far
3. Stop = OR_low (the structural invalidation point).
4. Target = entry + (OR_range * R_target_multiple), default 1.0R.
5. Force-flat at flat_by (default 15:55 ET). No overnight risk.
6. One trade per ticker per day. If stopped or targeted, that's the day.

Reward
------
For each trade:
    R = (exit_price - entry_price) / (entry_price - stop_loss)
This is the SAME reward shape the daily swing learner uses, so the same
Bayesian Beta(alpha, beta) machinery can be reused on intraday in Block 3.

Usage (locally, on your PC)
---------------------------
    python intraday_backtest.py                  # default US watchlist
    python intraday_backtest.py --tickers SPY,QQQ,TQQQ,NVDA
    python intraday_backtest.py --or-minutes 15 --target-r 1.5
    python intraday_backtest.py --json results.json

It uses data_provider.get_history(..., interval='5m'), which transparently
uses Moomoo OpenD if running, else yfinance (≤60 days of 5m history).

This file MUST NOT be imported by the live runtime (scheduler/screener/
engine). It is a research tool only.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Iterable, Optional

import pandas as pd


# ---------------------------------------------------------------------------
# Default backtest configuration
# ---------------------------------------------------------------------------

# US-only first cut. Mirrors the live US watchlist (us_profile.py) — a mix
# of leveraged ETFs (intraday vol darlings) and high-momentum mega-caps.
DEFAULT_US_TICKERS = [
    # Leveraged index ETFs — intraday workhorses
    "TQQQ", "SQQQ", "SPXL", "SOXL", "SOXS", "UPRO",
    # Sector / themed leveraged
    "FNGU", "LABU", "TNA",
    # Crypto-linked
    "IBIT", "MSTR", "COIN", "MARA",
    # High-vol mega-caps
    "NVDA", "TSLA", "AMD", "META", "AAPL", "MSFT", "GOOGL", "AMZN", "PLTR",
    # Vol hedge
    "UVXY",
]

US_OPEN  = dtime(9, 30)
US_CLOSE = dtime(16, 0)
US_FLAT_BY = dtime(15, 55)


@dataclass(frozen=True)
class ORBConfig:
    """Tunable knobs for the ORB strategy. Frozen so they're explicit per run."""
    interval: str = "5m"                # candle size
    opening_range_minutes: int = 30     # OR length, e.g. 30 = 6 × 5m bars
    rel_vol_threshold: float = 1.2      # breakout bar's vol vs session avg
    require_vwap_support: bool = True   # close > VWAP confirmation
    target_r_multiple: float = 1.0      # target = entry + R × range
    session_open: dtime = US_OPEN
    session_close: dtime = US_CLOSE
    flat_by: dtime = US_FLAT_BY


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    ticker: str
    session_date: date
    entry_time: datetime
    entry_price: float
    stop_loss: float
    target: float
    exit_time: datetime
    exit_price: float
    exit_reason: str           # 'TARGET' | 'STOP' | 'FORCE_FLAT'
    or_high: float
    or_low: float
    or_range: float
    r_multiple: float          # signed R; >0 = win, <0 = loss

    def to_dict(self) -> dict:
        d = asdict(self)
        d["session_date"]  = self.session_date.isoformat()
        d["entry_time"]    = self.entry_time.isoformat()
        d["exit_time"]     = self.exit_time.isoformat()
        return d


@dataclass
class BacktestSummary:
    config: ORBConfig
    tickers: list[str]
    trades: list[Trade] = field(default_factory=list)

    # ---- aggregates ----
    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def n_winners(self) -> int:
        return sum(1 for t in self.trades if t.r_multiple > 0)

    @property
    def n_losers(self) -> int:
        return sum(1 for t in self.trades if t.r_multiple <= 0)

    @property
    def win_rate(self) -> float:
        return (self.n_winners / self.n_trades) if self.n_trades else 0.0

    @property
    def total_r(self) -> float:
        return sum(t.r_multiple for t in self.trades)

    @property
    def avg_r(self) -> float:
        return (self.total_r / self.n_trades) if self.n_trades else 0.0

    @property
    def median_r(self) -> float:
        if not self.trades:
            return 0.0
        rs = sorted(t.r_multiple for t in self.trades)
        m = len(rs) // 2
        return rs[m] if len(rs) % 2 else 0.5 * (rs[m - 1] + rs[m])

    @property
    def max_consecutive_losers(self) -> int:
        worst = run = 0
        for t in self.trades:
            if t.r_multiple <= 0:
                run += 1
                worst = max(worst, run)
            else:
                run = 0
        return worst

    @property
    def expectancy_r(self) -> float:
        """Per-trade expectancy in R. Same as avg_r but spelled out so
        humans reading the summary recognise the term."""
        return self.avg_r

    def per_ticker_breakdown(self) -> list[dict]:
        rows: dict[str, dict] = {}
        for t in self.trades:
            row = rows.setdefault(t.ticker, {
                "ticker": t.ticker, "n_trades": 0, "wins": 0, "total_r": 0.0,
            })
            row["n_trades"] += 1
            if t.r_multiple > 0:
                row["wins"] += 1
            row["total_r"] += t.r_multiple
        for row in rows.values():
            row["win_rate"] = row["wins"] / row["n_trades"] if row["n_trades"] else 0.0
            row["avg_r"]    = row["total_r"] / row["n_trades"] if row["n_trades"] else 0.0
        return sorted(rows.values(), key=lambda r: -r["total_r"])

    def to_dict(self) -> dict:
        return {
            "config": asdict(self.config),
            "tickers": list(self.tickers),
            "aggregate": {
                "n_trades": self.n_trades,
                "win_rate": round(self.win_rate, 4),
                "total_r": round(self.total_r, 3),
                "avg_r": round(self.avg_r, 3),
                "median_r": round(self.median_r, 3),
                "max_consecutive_losers": self.max_consecutive_losers,
                "n_winners": self.n_winners,
                "n_losers": self.n_losers,
            },
            "per_ticker": self.per_ticker_breakdown(),
            "trades": [t.to_dict() for t in self.trades],
        }


# ---------------------------------------------------------------------------
# Pure indicator functions (re-used by Block 3's intraday_screener.py)
# ---------------------------------------------------------------------------

def compute_session_vwap(df: pd.DataFrame) -> pd.Series:
    """Volume-weighted average price, RESET each calendar day.

    df must have a DatetimeIndex and OHLCV columns. The typical price
    (H+L+C)/3 is used (the textbook VWAP definition). Output is a Series
    aligned to df.index.
    """
    if df.empty:
        return pd.Series(dtype=float, index=df.index)
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    pv = tp * df["Volume"]
    day_key = df.index.normalize()
    # Cumulative within each day:
    cum_pv  = pv.groupby(day_key).cumsum()
    cum_vol = df["Volume"].groupby(day_key).cumsum().replace(0, pd.NA)
    vwap = (cum_pv / cum_vol).astype(float)
    return vwap.ffill()


def compute_session_relative_volume(df: pd.DataFrame) -> pd.Series:
    """Each bar's volume divided by the rolling AVERAGE of session volume
    so far (same day). 1.0 = average; >1.0 = above-average pressure."""
    if df.empty:
        return pd.Series(dtype=float, index=df.index)
    day_key = df.index.normalize()
    # Expanding mean within each day. shift(1) so the current bar isn't
    # part of "average so far" — fair comparison.
    avg_so_far = df["Volume"].groupby(day_key).expanding().mean().shift(1)
    # Drop the outer (day) index level from groupby.expanding() so we can
    # align back to df.index.
    if isinstance(avg_so_far.index, pd.MultiIndex):
        avg_so_far = avg_so_far.droplevel(0)
    avg_so_far = avg_so_far.reindex(df.index)
    rel = df["Volume"] / avg_so_far.replace(0, pd.NA)
    return rel.astype(float)


def compute_opening_range(day_df: pd.DataFrame,
                          session_open: dtime,
                          or_minutes: int) -> tuple[Optional[float], Optional[float]]:
    """Return (OR_high, OR_low) for a single session, or (None, None) if
    the session doesn't have enough bars in the OR window yet."""
    if day_df.empty:
        return None, None
    or_end = (datetime.combine(date.today(), session_open)
              + timedelta(minutes=or_minutes)).time()
    in_or = day_df.between_time(session_open, or_end, inclusive="left")
    if in_or.empty:
        return None, None
    return float(in_or["High"].max()), float(in_or["Low"].min())


# ---------------------------------------------------------------------------
# Single-session simulator
# ---------------------------------------------------------------------------

def _split_into_sessions(df: pd.DataFrame,
                         session_open: dtime,
                         session_close: dtime) -> Iterable[tuple[date, pd.DataFrame]]:
    """Yield (session_date, df_for_that_day_in_RTH) for each trading day."""
    if df.empty:
        return
    rth = df.between_time(session_open, session_close, inclusive="left")
    for day, g in rth.groupby(rth.index.normalize()):
        if g.empty:
            continue
        yield day.date(), g


def simulate_orb_session(ticker: str,
                         day_df: pd.DataFrame,
                         cfg: ORBConfig) -> Optional[Trade]:
    """Run the ORB strategy on a single day's RTH bars.

    Returns at most one Trade (one entry per day). Returns None if no
    valid breakout signal occurred that day.

    day_df must be:
        * index = DatetimeIndex (tz-aware fine), all bars on a single date
        * columns = Open, High, Low, Close, Volume
        * already filtered to RTH (caller does this)
    """
    if day_df.empty:
        return None

    or_high, or_low = compute_opening_range(
        day_df, cfg.session_open, cfg.opening_range_minutes
    )
    if or_high is None or or_low is None:
        return None
    or_range = or_high - or_low
    if or_range <= 0:
        return None  # degenerate (flat OR), skip

    # Bars eligible to trigger an entry: AFTER the OR window, BEFORE flat_by.
    or_end_time = (datetime.combine(date.today(), cfg.session_open)
                   + timedelta(minutes=cfg.opening_range_minutes)).time()
    post_or = day_df.between_time(or_end_time, cfg.flat_by, inclusive="left")
    if post_or.empty:
        return None

    # Compute session-aware indicators on the FULL day_df (so VWAP & rel-vol
    # carry the OR-window context), then align back to the post-OR slice.
    vwap = compute_session_vwap(day_df)
    rvol = compute_session_relative_volume(day_df)

    entry_idx = None
    for ts, bar in post_or.iterrows():
        if bar["Close"] <= or_high:
            continue
        if cfg.require_vwap_support and not (bar["Close"] > vwap.loc[ts]):
            continue
        rv = rvol.loc[ts]
        # rv may be NaN on the very first bar of the day; skip if so.
        if pd.isna(rv) or rv < cfg.rel_vol_threshold:
            continue
        entry_idx = ts
        break

    if entry_idx is None:
        return None

    entry_price = float(post_or.loc[entry_idx, "Close"])
    stop_loss   = float(or_low)
    risk_per_share = entry_price - stop_loss
    if risk_per_share <= 0:
        # Pathological: breakout candle closed at/below stop — skip.
        return None
    target = entry_price + cfg.target_r_multiple * or_range

    # Walk forward bar-by-bar to determine the exit. We use the high/low
    # of each bar to detect stop/target hits (more realistic than close-only).
    after_entry = day_df.loc[day_df.index > entry_idx]
    after_entry = after_entry.between_time(
        cfg.session_open, cfg.flat_by, inclusive="left"
    )

    exit_time = None
    exit_price = None
    exit_reason = None

    for ts, bar in after_entry.iterrows():
        # Conservative tiebreaker: if a bar's range straddles BOTH stop
        # and target, assume the stop hit first (worst case for the trade).
        hit_stop   = bar["Low"]  <= stop_loss
        hit_target = bar["High"] >= target
        if hit_stop and hit_target:
            exit_time, exit_price, exit_reason = ts, stop_loss, "STOP"
            break
        if hit_stop:
            exit_time, exit_price, exit_reason = ts, stop_loss, "STOP"
            break
        if hit_target:
            exit_time, exit_price, exit_reason = ts, target, "TARGET"
            break

    if exit_time is None:
        # Force-flat at the last bar of the post-OR window.
        last_ts = after_entry.index[-1] if len(after_entry) else entry_idx
        exit_time   = last_ts
        exit_price  = float(after_entry["Close"].iloc[-1]) if len(after_entry) \
                       else entry_price
        exit_reason = "FORCE_FLAT"

    r = (exit_price - entry_price) / risk_per_share

    return Trade(
        ticker=ticker,
        session_date=entry_idx.date(),
        entry_time=entry_idx.to_pydatetime(),
        entry_price=entry_price,
        stop_loss=stop_loss,
        target=target,
        exit_time=exit_time.to_pydatetime() if hasattr(exit_time, "to_pydatetime")
                                            else exit_time,
        exit_price=float(exit_price),
        exit_reason=exit_reason,
        or_high=float(or_high),
        or_low=float(or_low),
        or_range=float(or_range),
        r_multiple=float(r),
    )


def backtest_ticker(ticker: str,
                    df: pd.DataFrame,
                    cfg: ORBConfig) -> list[Trade]:
    """Run ORB over every session in df. Returns chronological list of trades."""
    trades: list[Trade] = []
    for _day, day_df in _split_into_sessions(df, cfg.session_open, cfg.session_close):
        t = simulate_orb_session(ticker, day_df, cfg)
        if t is not None:
            trades.append(t)
    return trades


# ---------------------------------------------------------------------------
# Top-level harness
# ---------------------------------------------------------------------------

def run_backtest(tickers: list[str],
                 cfg: ORBConfig = ORBConfig(),
                 get_history_fn=None,
                 verbose: bool = True) -> BacktestSummary:
    """Pull 5m history per ticker via data_provider and aggregate ORB results."""
    if get_history_fn is None:
        from data_provider import get_history as get_history_fn  # type: ignore

    summary = BacktestSummary(config=cfg, tickers=list(tickers))
    for tk in tickers:
        try:
            df = get_history_fn(tk, interval=cfg.interval)
        except Exception as e:
            if verbose:
                print(f"  [skip] {tk}: fetch failed ({e})", file=sys.stderr)
            continue
        if df is None or df.empty:
            if verbose:
                print(f"  [skip] {tk}: no data", file=sys.stderr)
            continue
        # Need all 5 OHLCV cols.
        required = {"Open", "High", "Low", "Close", "Volume"}
        if not required.issubset(df.columns):
            if verbose:
                print(f"  [skip] {tk}: missing cols (have {list(df.columns)})",
                      file=sys.stderr)
            continue
        trades = backtest_ticker(tk, df, cfg)
        summary.trades.extend(trades)
        if verbose:
            wins = sum(1 for t in trades if t.r_multiple > 0)
            ttl  = sum(t.r_multiple for t in trades)
            print(f"  {tk:6s}  n={len(trades):3d}  wins={wins:3d}  "
                  f"total={ttl:+.2f}R  avg={ttl/len(trades) if trades else 0:+.2f}R")
    return summary


# ---------------------------------------------------------------------------
# CLI / text report
# ---------------------------------------------------------------------------

def format_text_report(summary: BacktestSummary) -> str:
    cfg = summary.config
    lines = []
    lines.append("=" * 70)
    lines.append("ORB Backtest Report — v3.7 prove-the-edge")
    lines.append("=" * 70)
    lines.append(f"  interval         : {cfg.interval}")
    lines.append(f"  opening range    : {cfg.opening_range_minutes} min")
    lines.append(f"  rel-vol threshold: {cfg.rel_vol_threshold:.2f}x")
    lines.append(f"  vwap support req : {cfg.require_vwap_support}")
    lines.append(f"  target           : {cfg.target_r_multiple:.2f}R")
    lines.append(f"  session          : {cfg.session_open}-{cfg.session_close}, "
                 f"flat by {cfg.flat_by}")
    lines.append(f"  tickers          : {', '.join(summary.tickers)}")
    lines.append("-" * 70)
    if not summary.n_trades:
        lines.append("  NO TRADES — check that the data provider returned 5m bars.")
        lines.append("=" * 70)
        return "\n".join(lines)
    lines.append(f"  n trades         : {summary.n_trades}")
    lines.append(f"  winners / losers : {summary.n_winners} / {summary.n_losers}")
    lines.append(f"  win rate         : {summary.win_rate:.1%}")
    lines.append(f"  total R          : {summary.total_r:+.2f}R")
    lines.append(f"  avg R / trade    : {summary.avg_r:+.3f}R  "
                 f"(expectancy)")
    lines.append(f"  median R         : {summary.median_r:+.3f}R")
    lines.append(f"  max consec losers: {summary.max_consecutive_losers}")
    lines.append("-" * 70)
    lines.append(" Per-ticker breakdown (sorted by total R):")
    lines.append(f"  {'TICKER':<7} {'N':>4} {'WIN%':>6} {'AVG R':>8} {'TOTAL R':>9}")
    for row in summary.per_ticker_breakdown():
        lines.append(f"  {row['ticker']:<7} "
                     f"{row['n_trades']:>4} "
                     f"{row['win_rate']:>5.0%} "
                     f"{row['avg_r']:>+8.3f} "
                     f"{row['total_r']:>+9.2f}")
    lines.append("=" * 70)
    # Verdict heuristic (trader's rule-of-thumb, not gospel):
    #   - need ≥ 30 trades for any opinion
    #   - want expectancy ≥ +0.10R with win rate ≥ 40% to call it an edge
    #   - want consec losers ≤ 8 for psychological survivability
    verdict_lines = []
    if summary.n_trades < 30:
        verdict_lines.append(
            f"  Verdict   : INSUFFICIENT SAMPLE ({summary.n_trades} trades < 30 min)"
        )
    else:
        edge_ok   = summary.expectancy_r >= 0.10
        winrate_ok= summary.win_rate     >= 0.40
        dd_ok     = summary.max_consecutive_losers <= 8
        if edge_ok and winrate_ok and dd_ok:
            verdict_lines.append("  Verdict   : ✅ EDGE LOOKS REAL — proceed to Block 3.")
        elif edge_ok and winrate_ok:
            verdict_lines.append("  Verdict   : ⚠️ Edge present but drawdown is rough — proceed with caution.")
        elif summary.expectancy_r > 0:
            verdict_lines.append("  Verdict   : ⚠️ Marginally positive — try tuning OR length / rel-vol before building engine.")
        else:
            verdict_lines.append("  Verdict   : ❌ NO EDGE on these params — DO NOT build engine yet. Re-tune or pick a different intraday setup.")
    lines.extend(verdict_lines)
    lines.append("=" * 70)
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="ORB backtest harness — v3.7 prove-the-edge"
    )
    p.add_argument("--tickers", type=str, default=None,
                   help="comma-separated tickers (default: built-in US watchlist)")
    p.add_argument("--or-minutes", type=int, default=30,
                   help="opening range length in minutes (default: 30)")
    p.add_argument("--target-r", type=float, default=1.0,
                   help="target as multiple of OR range (default: 1.0)")
    p.add_argument("--rel-vol", type=float, default=1.2,
                   help="relative-volume threshold on breakout bar (default: 1.2)")
    p.add_argument("--no-vwap", action="store_true",
                   help="disable VWAP-support filter")
    p.add_argument("--json", type=str, default=None,
                   help="write full results (incl. every trade) to this JSON file")
    args = p.parse_args(argv)

    cfg = ORBConfig(
        opening_range_minutes=args.or_minutes,
        target_r_multiple=args.target_r,
        rel_vol_threshold=args.rel_vol,
        require_vwap_support=not args.no_vwap,
    )
    tickers = (args.tickers.split(",") if args.tickers
               else list(DEFAULT_US_TICKERS))

    print(f"Fetching 5m data for {len(tickers)} tickers via data_provider...")
    summary = run_backtest(tickers, cfg)
    print(format_text_report(summary))

    if args.json:
        with open(args.json, "w") as f:
            json.dump(summary.to_dict(), f, indent=2, default=str)
        print(f"\nFull results (incl. every trade) written to: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
