#!/usr/bin/env python3
# validate_intraday_edge.py
"""
OpenD-backed multi-year intraday-edge validator -- v3.7 round 3.

WHAT THIS DOES (and ONLY this):
    1. Connects to your local Moomoo OpenD (127.0.0.1:11111 by default).
    2. Pulls 5-minute history for the bull-leveraged + crypto + megacap US
       universe over a configurable look-back (default 365 days).
    3. Runs ORB with the round-2 winning params: OR=15min, target=2.0R,
       longs-only, VWAP support, rel-vol>=1.2, EMA-50 daily trend filter.
    4. Aggregates per-window walk-forward + per-month results so you can
       see whether the edge holds across bull, chop, and bear regimes.
    5. Prints a markdown-formatted report to stdout AND a JSON dump.

WHAT THIS DOES NOT DO:
    * Place any orders (read-only).
    * Touch your live DB or scheduler.
    * Modify any file outside the workspace (just writes a results file).
    * Send any Telegram/email alerts.

HOW TO RUN:
    1. Make sure Moomoo OpenD is RUNNING on 127.0.0.1:11111.
       Verify: `python3 verify_moomoo.py` should say "Moomoo OpenD connected".
    2. From the repo root:
         python3 validate_intraday_edge.py
       (Or with overrides:)
         python3 validate_intraday_edge.py --days 730 --json out.json
         python3 validate_intraday_edge.py --tickers TQQQ,SPY,NVDA

OUTPUT:
    A text report on stdout (paste it back to the AI), plus
    `intraday_validation_results.json` in the current directory with every
    trade so the AI can drill deeper if needed.

WHY IT MATTERS:
    yfinance gave us 60d. That covered Mar-May 2026, which was bullish, so
    longs-only + trend filter naturally outperforms. We need to see whether
    the edge survives across multiple regime types before committing to
    building a full intraday engine (Blocks 3-7 of the v3.7 plan).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

import data_provider
from intraday_backtest import (
    ORBConfig,
    Trade,
    BacktestSummary,
    DEFAULT_US_TICKERS,
    format_text_report,
)
from intraday_backtest_v2 import (
    run_v2,
    verdict,
    backtest_ticker_v2,
    simulate_orb_v2,
    compute_daily_ema_trend,
)


# ---------------------------------------------------------------------------
# ASCII-only output helpers (Windows cp1252 safe)
# ---------------------------------------------------------------------------
PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"
EDGE = "[EDGE]"
NO_EDGE = "[NO EDGE]"


# ---------------------------------------------------------------------------
# Universe (round-2 winner) + parameters (round-2 winner)
# ---------------------------------------------------------------------------

BULL20 = [
    # bull leveraged ETFs
    "TQQQ", "SPXL", "SOXL", "UPRO", "FNGU", "TNA",
    # crypto-linked
    "IBIT", "MSTR", "COIN", "MARA",
    # high-vol megacaps
    "NVDA", "TSLA", "AMD", "META", "AAPL", "MSFT", "GOOGL", "AMZN", "PLTR",
    # vol hedge (works both directions, kept in)
    "UVXY",
]

WINNING_CFG = ORBConfig(
    interval="5m",
    opening_range_minutes=15,
    target_r_multiple=2.0,
    rel_vol_threshold=1.2,
    require_vwap_support=True,
)


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def summarise(trades: list[Trade], label: str) -> str:
    if not trades:
        return f"  {label:<35}  n=0  no trades"
    n = len(trades)
    wins = sum(1 for t in trades if t.r_multiple > 0)
    tot = sum(t.r_multiple for t in trades)
    avg = tot / n
    worst = run = 0
    for t in trades:
        if t.r_multiple <= 0:
            run += 1
            worst = max(worst, run)
        else:
            run = 0
    win_rate = wins / n
    if avg >= 0.10 and win_rate >= 0.40 and worst <= 8:
        v = f"{PASS} EDGE"
    elif avg >= 0.10 and win_rate >= 0.40:
        v = f"{WARN} rough DD"
    elif avg > 0:
        v = f"{WARN} marginal"
    else:
        v = f"{FAIL} no edge"
    return (f"  {label:<35}  n={n:>4}  win={win_rate*100:>3.0f}%  "
            f"avg={avg:>+.3f}R  tot={tot:>+7.2f}R  cl={worst:>2}  {v}")


def fetch_universe_intraday(tickers: list[str],
                            days: int,
                            verbose: bool = True) -> dict[str, pd.DataFrame]:
    """Pull 5m history for each ticker, going back `days` calendar days.

    Uses data_provider, which auto-routes to Moomoo OpenD if connected.
    Returns dict[ticker -> DataFrame]. Skips tickers with no data.
    """
    end = datetime.utcnow().date()
    start = end - timedelta(days=days)
    out: dict[str, pd.DataFrame] = {}
    for tk in tickers:
        try:
            df = data_provider.get_history(
                tk,
                start=start.isoformat(),
                end=end.isoformat(),
                interval="5m",
                timeout=60,
            )
        except Exception as e:
            if verbose:
                print(f"  [skip] {tk}: fetch failed ({e})", file=sys.stderr)
            continue
        if df is None or df.empty:
            if verbose:
                print(f"  [skip] {tk}: no 5m data returned", file=sys.stderr)
            continue
        if not {"Open", "High", "Low", "Close", "Volume"}.issubset(df.columns):
            if verbose:
                print(f"  [skip] {tk}: missing OHLCV cols", file=sys.stderr)
            continue
        out[tk] = df
        if verbose:
            n_bars = len(df)
            n_days = df.index.normalize().nunique()
            print(f"  {tk:6s}  bars={n_bars:>6}  days={n_days:>4}  "
                  f"first={df.index[0].date()}  last={df.index[-1].date()}")
    return out


def fetch_universe_daily(tickers: list[str],
                         verbose: bool = True) -> dict[str, pd.DataFrame]:
    """Pull daily history (1y) for each ticker -- used by the EMA-50 trend filter."""
    out: dict[str, pd.DataFrame] = {}
    for tk in tickers:
        try:
            df = data_provider.get_history(tk, period="1y", timeout=30)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        out[tk] = df
    if verbose:
        print(f"  Daily history fetched for {len(out)}/{len(tickers)} tickers.")
    return out


# ---------------------------------------------------------------------------
# Main validator
# ---------------------------------------------------------------------------

def run_validation(tickers: list[str],
                   days: int,
                   cfg: ORBConfig,
                   verbose: bool = True) -> dict:
    """Pull data, run ORB, return rich dict for printing + JSON dump."""
    print("=" * 90)
    print(f"VALIDATION RUN -- {len(tickers)} tickers x {days} days x OR={cfg.opening_range_minutes}min R={cfg.target_r_multiple}")
    print("=" * 90)
    print()

    # Surface which provider we're actually using.
    data_provider.ensure_probed()
    h = data_provider.health()
    moomoo_status = "Moomoo OpenD [CONNECTED]" if h["moomoo_available"] else "yfinance fallback (no OpenD)"
    print(f"  Data source       : {moomoo_status}")
    print(f"  Provider env      : {h['provider_env']}")
    if h["init_error"]:
        print(f"  (note: {h['init_error']})")
    print()

    # Pull intraday + daily history.
    print("Fetching intraday 5m history...")
    intraday = fetch_universe_intraday(tickers, days, verbose=verbose)
    if not intraday:
        print("  ERROR: no intraday data fetched. Aborting.")
        return {}
    print()
    print("Fetching daily history for EMA-50 trend filter...")
    daily = fetch_universe_daily(list(intraday.keys()), verbose=verbose)
    print()

    # Run ORB per ticker, accumulate trades.
    all_trades: list[Trade] = []
    for tk, df_5m in intraday.items():
        df_d = daily.get(tk, pd.DataFrame())
        if df_d.empty:
            if verbose:
                print(f"  [skip] {tk}: no daily history for trend filter")
            continue
        trades = backtest_ticker_v2(
            tk, df_5m, df_d, cfg,
            use_trend_filter=True,
            allow_longs=True,
            allow_shorts=False,
        )
        all_trades.extend(trades)
        if verbose and trades:
            wins = sum(1 for t in trades if t.r_multiple > 0)
            tot = sum(t.r_multiple for t in trades)
            print(f"  {tk:6s}  n={len(trades):>3}  win={wins:>3}  "
                  f"tot={tot:>+6.2f}R  avg={tot/len(trades):>+.3f}R")
    print()

    if not all_trades:
        print("  No trades generated. Aborting.")
        return {}

    all_trades.sort(key=lambda t: t.entry_time)

    # ---- Aggregates ----
    summary = BacktestSummary(config=cfg, tickers=list(intraday.keys()))
    summary.trades = all_trades

    print("=" * 90)
    print("AGGREGATE")
    print("=" * 90)
    print(summarise(all_trades, "FULL PERIOD"))
    print()

    # ---- Walk-forward: split into 6 equal windows ----
    print("=" * 90)
    print("WALK-FORWARD -- 6 equal windows (to test regime stability)")
    print("=" * 90)
    first = all_trades[0].entry_time.date()
    last  = all_trades[-1].entry_time.date()
    span_days = (last - first).days
    print(f"  Trade span: {first} --> {last}  ({span_days} days)")
    print()
    n_windows = 6
    chunk = span_days // n_windows or 1
    windows_data = []
    for i in range(n_windows):
        lo = first + timedelta(days=i * chunk)
        hi = first + timedelta(days=(i + 1) * chunk) if i < n_windows - 1 else last + timedelta(days=1)
        sub = [t for t in all_trades if lo <= t.entry_time.date() < hi]
        line = summarise(sub, f"W{i+1}: {lo} --> {hi-timedelta(days=1)}")
        print(line)
        windows_data.append({
            "window": i + 1,
            "from": lo.isoformat(),
            "to": (hi - timedelta(days=1)).isoformat(),
            "n_trades": len(sub),
            "win_rate": (sum(1 for t in sub if t.r_multiple > 0) / len(sub)) if sub else 0,
            "avg_r": (sum(t.r_multiple for t in sub) / len(sub)) if sub else 0,
            "total_r": sum(t.r_multiple for t in sub),
        })
    print()

    # ---- Monthly breakdown ----
    print("=" * 90)
    print("PER-MONTH BREAKDOWN")
    print("=" * 90)
    months: dict[str, list[Trade]] = defaultdict(list)
    for t in all_trades:
        months[t.entry_time.strftime("%Y-%m")].append(t)
    months_data = []
    for m in sorted(months.keys()):
        line = summarise(months[m], m)
        print(line)
        sub = months[m]
        months_data.append({
            "month": m,
            "n_trades": len(sub),
            "win_rate": sum(1 for t in sub if t.r_multiple > 0) / len(sub),
            "avg_r": sum(t.r_multiple for t in sub) / len(sub),
            "total_r": sum(t.r_multiple for t in sub),
        })
    pos_months = sum(1 for m in months_data if m["total_r"] > 0)
    print(f"\n  Monthly hit rate: {pos_months}/{len(months_data)} = {pos_months/len(months_data)*100:.0f}% net-positive")
    print()

    # ---- Per-ticker breakdown ----
    print("=" * 90)
    print("PER-TICKER BREAKDOWN (sorted by total R)")
    print("=" * 90)
    per_ticker = summary.per_ticker_breakdown()
    print(f"  {'TICKER':<7} {'N':>4} {'WIN%':>6} {'AVG R':>8} {'TOTAL R':>9}")
    for row in per_ticker:
        print(f"  {row['ticker']:<7} {row['n_trades']:>4} {row['win_rate']*100:>5.0f}% "
              f"{row['avg_r']:>+8.3f} {row['total_r']:>+9.2f}")
    print()

    # ---- R distribution ----
    print("=" * 90)
    print("R DISTRIBUTION")
    print("=" * 90)
    rs = sorted(t.r_multiple for t in all_trades)
    n = len(rs)
    print(f"  n trades         : {n}")
    print(f"  worst trade      : {rs[0]:+.3f}R")
    print(f"  best  trade      : {rs[-1]:+.3f}R")
    print(f"  10th percentile  : {rs[n//10]:+.3f}R")
    print(f"  median           : {rs[n//2]:+.3f}R")
    print(f"  90th percentile  : {rs[n*9//10]:+.3f}R")
    print()
    buckets = [
        (-99,    -1.001, "<-1R (slipped past stop)"),
        (-1.001, -0.5,   "-1R to -0.5R"),
        (-0.5,    0,     "-0.5R to 0R (small loss)"),
        (0,       0.5,   "0R to +0.5R (small win)"),
        (0.5,     1.0,   "+0.5R to +1R"),
        (1.0,     2.0,   "+1R to +2R"),
        (2.0,     3.0,   "+2R to +3R"),
        (3.0,     99,    "+3R+ (outsized winner)"),
    ]
    for lo, hi, label in buckets:
        cnt = sum(1 for r in rs if lo <= r < hi)
        bar = "#" * (cnt * 40 // max(1, n))
        print(f"  {label:<30}  n={cnt:>4}  {bar}")
    print()
    top10n = max(1, n // 10)
    top10_r = sum(rs[-top10n:])
    total_r = sum(rs)
    print(f"  Top-10% trades contribute {top10_r:+.2f}R of total {total_r:+.2f}R "
          f"= {top10_r/total_r*100:.0f}% from {top10n}/{n} trades")
    print()

    # ---- Final verdict ----
    print("=" * 90)
    print("FINAL VERDICT")
    print("=" * 90)
    overall_avg = total_r / n
    overall_win = sum(1 for r in rs if r > 0) / n
    worst_streak = 0; run = 0
    for t in all_trades:
        if t.r_multiple <= 0:
            run += 1; worst_streak = max(worst_streak, run)
        else:
            run = 0
    monthly_hit_rate = pos_months / len(months_data)

    pass_count = 0
    avg_pass = overall_avg >= 0.10
    win_pass = overall_win >= 0.40
    cl_pass = worst_streak <= 8
    mhr_pass = monthly_hit_rate >= 0.65
    if avg_pass: pass_count += 1
    if win_pass: pass_count += 1
    if cl_pass: pass_count += 1
    if mhr_pass: pass_count += 1

    print(f"  [{PASS if avg_pass else FAIL}] Expectancy >= +0.10R     : {overall_avg:+.3f}R")
    print(f"  [{PASS if win_pass else FAIL}] Win rate >= 40%          : {overall_win*100:.0f}%")
    print(f"  [{PASS if cl_pass else FAIL}] Max consec losers <= 8   : {worst_streak}")
    print(f"  [{PASS if mhr_pass else FAIL}] Monthly hit rate >= 65% : {monthly_hit_rate*100:.0f}%")
    print()

    if pass_count == 4:
        recommend = f"{PASS} {EDGE} BUILD ENGINE -- edge holds across regimes. Proceed to Block 2."
    elif pass_count == 3:
        recommend = f"{WARN} MOSTLY VALID -- edge real but one weakness. Consider building Blocks 2-3 only, validate live before 4-7."
    elif pass_count == 2:
        recommend = f"{WARN} MIXED -- needs more tuning or only build plumbing (Block 2). Don't ship full engine."
    else:
        recommend = f"{FAIL} {NO_EDGE} EDGE DOES NOT GENERALIZE -- DO NOT build engine. Shelve or pick different strategy."
    print(f"  RECOMMENDATION: {recommend}")
    print()

    return {
        "config": {
            "tickers": list(intraday.keys()),
            "days": days,
            "opening_range_minutes": cfg.opening_range_minutes,
            "target_r_multiple": cfg.target_r_multiple,
            "rel_vol_threshold": cfg.rel_vol_threshold,
            "require_vwap_support": cfg.require_vwap_support,
            "data_source": "moomoo" if h["moomoo_available"] else "yfinance",
            "interval": cfg.interval,
        },
        "aggregate": {
            "n_trades": n,
            "win_rate": round(overall_win, 4),
            "avg_r": round(overall_avg, 4),
            "total_r": round(total_r, 3),
            "max_consec_losers": worst_streak,
            "monthly_hit_rate": round(monthly_hit_rate, 4),
        },
        "walk_forward_windows": windows_data,
        "monthly_breakdown": months_data,
        "per_ticker": per_ticker,
        "verdict": {
            "passes": pass_count,
            "of": 4,
            "recommend": recommend,
        },
        "trades": [t.to_dict() for t in all_trades],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="OpenD-backed intraday-edge validator (round 3)"
    )
    p.add_argument("--tickers", type=str, default=None,
                   help="comma-separated tickers (default: bull-20 universe)")
    p.add_argument("--days", type=int, default=365,
                   help="calendar days of 5m history to pull (default: 365)")
    p.add_argument("--or-minutes", type=int, default=15,
                   help="opening-range minutes (default: 15, the round-2 winner)")
    p.add_argument("--target-r", type=float, default=2.0,
                   help="target as multiple of OR range (default: 2.0)")
    p.add_argument("--rel-vol", type=float, default=1.2,
                   help="rel-vol threshold (default: 1.2)")
    p.add_argument("--no-vwap", action="store_true",
                   help="disable VWAP-support filter")
    p.add_argument("--json", type=str, default="intraday_validation_results.json",
                   help="path to write JSON dump (default: intraday_validation_results.json)")
    args = p.parse_args(argv)

    cfg = ORBConfig(
        interval="5m",
        opening_range_minutes=args.or_minutes,
        target_r_multiple=args.target_r,
        rel_vol_threshold=args.rel_vol,
        require_vwap_support=not args.no_vwap,
    )
    tickers = args.tickers.split(",") if args.tickers else list(BULL20)

    result = run_validation(tickers, args.days, cfg, verbose=True)
    if not result:
        return 1

    with open(args.json, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"  Full results (every trade) written to: {args.json}")
    print()
    print("=" * 90)
    print("NEXT STEP: paste the AGGREGATE + WALK-FORWARD + FINAL VERDICT sections")
    print("           back to the AI so we can decide on Blocks 2-7.")
    print("=" * 90)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
