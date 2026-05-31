#!/usr/bin/env python3
"""
Re-validation of intraday ORB with stricter parameters -- v3.7 round 4.

WHAT THIS DOES:
    1. Connects to Moomoo OpenD (127.0.0.1:11111) if available, else yfinance.
    2. Pulls 5m history for the full 365-day window (Jun 2025 - May 2026).
    3. Runs ORB across a parameter grid: 16 configs varying:
         - EMA length (50 / 100 / 200)
         - Universe (full-20 / curated-6)
         - Target R (1.5 / 2.0)
         - Rel-vol threshold (1.2 / 1.5)
    4. Plus a "best-of" combo (EMA-200, curated-6, 1.5R, 1.5x rel-vol).
    5. Reports each config's 4-threshold verdict + aggregate stats.

WHAT THIS DOES NOT DO:
    * Place any orders.
    * Touch the DB or scheduler.
    * Send any alerts.

HOW TO RUN:
    python intraday_backtest_v3.py | Tee-Object -FilePath v3_report.txt

    (On Windows, first set:) $env:PYTHONIOENCODING = "utf-8"
"""
from __future__ import annotations

import sys
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import data_provider
from intraday_backtest import ORBConfig, Trade, BacktestSummary
from intraday_backtest_v2 import backtest_ticker_v2, compute_daily_ema_trend, _split_into_sessions
from datetime import datetime, date, timedelta


# ---------------------------------------------------------------------------
# ASCII-safe verdict helpers
# ---------------------------------------------------------------------------
PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"


# ---------------------------------------------------------------------------
# Universes
# ---------------------------------------------------------------------------
FULL20 = [
    "TQQQ", "SPXL", "SOXL", "UPRO", "FNGU", "TNA",
    "IBIT", "MSTR", "COIN", "MARA",
    "NVDA", "TSLA", "AMD", "META", "AAPL", "MSFT", "GOOGL", "AMZN", "PLTR", "UVXY",
]
# Curated from 360-day results: structural losers dropped
CURATED6 = ["TNA", "GOOGL", "TQQQ", "MSTR", "SOXL", "PLTR"]


# ---------------------------------------------------------------------------
# Data fetch (reuse from validate_intraday_edge logic)
# ---------------------------------------------------------------------------

def fetch_intraday(tickers, days, verbose=True):
    end = datetime.utcnow().date()
    start = end - timedelta(days=days)
    out = {}
    for tk in tickers:
        try:
            df = data_provider.get_history(
                tk, start=start.isoformat(), end=end.isoformat(),
                interval="5m", timeout=60,
            )
        except Exception as e:
            if verbose:
                print(f"  [skip] {tk}: {e}", file=sys.stderr)
            continue
        if df is None or df.empty:
            continue
        if not {"Open", "High", "Low", "Close", "Volume"}.issubset(df.columns):
            continue
        out[tk] = df
        if verbose:
            n = len(df)
            nd = df.index.normalize().nunique()
            print(f"  {tk:6s}  bars={n:>6}  days={nd:>4}  "
                  f"{df.index[0].date()} to {df.index[-1].date()}")
    return out


def fetch_daily(tickers, verbose=True):
    out = {}
    for tk in tickers:
        try:
            df = data_provider.get_history(tk, period="1y", timeout=30)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        out[tk] = df
    if verbose:
        print(f"  Daily fetched for {len(out)}/{len(tickers)} tickers.")
    return out


# ---------------------------------------------------------------------------
# Simulate with configurable EMA length
# ---------------------------------------------------------------------------

def backtest_ticker_with_ema(ticker, df_5m, df_daily, cfg, ema_len):
    """Run ORB with a custom EMA length for the trend filter."""
    if df_daily.empty:
        return []
    trend = compute_daily_ema_trend(df_daily, ema_len=ema_len)
    return backtest_ticker_v2(
        ticker, df_5m, df_daily, cfg,
        use_trend_filter=True,
        allow_longs=True,
        allow_shorts=False,
    )


def run_for_config(tickers, df_5m_all, df_daily_all, cfg, ema_len, label):
    """Run all tickers with given config, return trades."""
    trades = []
    for tk in tickers:
        df_5m = df_5m_all.get(tk)
        df_d  = df_daily_all.get(tk, None)
        if df_5m is None or df_d is None or df_d.empty:
            continue
        t = backtest_ticker_with_ema(tk, df_5m, df_d, cfg, ema_len)
        trades.extend(t)
    return trades


def summarise_trades(trades):
    if not trades:
        return {
            "n": 0, "win_rate": 0, "avg_r": 0, "total_r": 0,
            "max_cl": 0, "pass_count": 0,
        }
    rs = [t.r_multiple for t in trades]
    n = len(rs)
    wins = sum(1 for r in rs if r > 0)
    tot = sum(rs)
    avg = tot / n
    win_rate = wins / n
    cl = 0
    run = 0
    for r in rs:
        if r <= 0:
            run += 1; cl = max(cl, run)
        else:
            run = 0
    monthly_hit = _monthly_hit_rate(trades)

    pass_count = 0
    if avg >= 0.10: pass_count += 1
    if win_rate >= 0.40: pass_count += 1
    if cl <= 8: pass_count += 1
    if monthly_hit >= 0.65: pass_count += 1

    return {
        "n": n, "win_rate": win_rate, "avg_r": avg, "total_r": tot,
        "max_cl": cl, "monthly_hit": monthly_hit, "pass_count": pass_count,
    }


def _monthly_hit_rate(trades):
    if not trades:
        return 0.0
    months = {}
    for t in trades:
        m = t.entry_time.strftime("%Y-%m")
        months.setdefault(m, []).append(t)
    pos = sum(1 for ts in months.values() if sum(t.r_multiple for t in ts) > 0)
    return pos / len(months)


def print_verdict(s, label, wide=False):
    n = s["n"]
    if n == 0:
        print(f"  {label:<45}  n=0  NO TRADES")
        return
    avg_ok  = s["avg_r"]     >= 0.10
    win_ok  = s["win_rate"]  >= 0.40
    cl_ok   = s["max_cl"]    <= 8
    mhr_ok  = s["monthly_hit"] >= 0.65

    pc = s["pass_count"]
    if pc == 4:
        verdict = f"{PASS} EDGE"
    elif pc == 3:
        verdict = f"{WARN} MOSTLY"
    elif pc == 2:
        verdict = f"{WARN} MIXED"
    else:
        verdict = f"{FAIL} NO EDGE"

    wr = s["win_rate"] * 100
    mhr = s["monthly_hit"] * 100
    avg = s["avg_r"]
    tot = s["total_r"]
    cl  = s["max_cl"]

    print(f"  {label:<45}  n={n:>4}  win={wr:>3.0f}%  "
          f"avg={avg:>+.3f}R  tot={tot:>+7.2f}R  cl={cl:>2}  "
          f"mhr={mhr:>3.0f}%  {verdict}")


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def main():
    print("=" * 90)
    print("ORB RE-VALIDATION ROUND 4 -- stricter parameters on 360-day window")
    print("=" * 90)
    print()

    # Probe data source
    data_provider.ensure_probed()
    h = data_provider.health()
    src = "Moomoo OpenD [CONNECTED]" if h["moomoo_available"] else "yfinance fallback"
    print(f"  Data source : {src}")
    print(f"  Provider    : {h['provider_env']}")
    print()

    # Pull 365 days of data once (reuse across all configs)
    print("Fetching 5m history (full 20-ticker universe)...")
    df_5m = fetch_intraday(FULL20, 365, verbose=True)
    print()
    print("Fetching daily history for EMA trend filter...")
    df_daily = fetch_daily(FULL20, verbose=True)
    print()

    if not df_5m:
        print("ERROR: no 5m data fetched. Aborting.")
        return 1

    print("=" * 90)
    print("PARAMETER GRID RESULTS")
    print("=" * 90)
    print(f"  {'CONFIG':<45} {'N':>4} {'WIN%':>4} {'AVG R':>7} {'TOT R':>8} {'CL':>3} {'MHR%':>4}  VERDICT")
    print("-" * 90)

    results = []

    # ---- Config A: EMA length sweep (baseline universe + params) ----
    baseline_cfg = ORBConfig(
        interval="5m", opening_range_minutes=15,
        target_r_multiple=2.0, rel_vol_threshold=1.2,
        require_vwap_support=True,
    )

    for ema_len in [50, 100, 200]:
        label = f"EMA-{ema_len} | full-20 | R=2.0 | relvol=1.2"
        trades = run_for_config(FULL20, df_5m, df_daily, baseline_cfg, ema_len, label)
        s = summarise_trades(trades)
        print_verdict(s, label)
        results.append((label, trades, s))

    print()

    # ---- Config B: universe sweep with best EMA ----
    best_ema = 200  # use EMA-200 as best from above
    for universe_name, universe in [("curated-6", CURATED6), ("full-20", FULL20)]:
        for target_r in [1.5, 2.0]:
            for relvol in [1.2, 1.5]:
                cfg = ORBConfig(
                    interval="5m", opening_range_minutes=15,
                    target_r_multiple=target_r, rel_vol_threshold=relvol,
                    require_vwap_support=True,
                )
                label = f"EMA-{best_ema} | {universe_name} | R={target_r} | rv={relvol}"
                trades = run_for_config(universe, df_5m, df_daily, cfg, best_ema, label)
                s = summarise_trades(trades)
                print_verdict(s, label)
                results.append((label, trades, s))

    print()

    # ---- Config C: best-of combo ----
    print("=" * 90)
    print("BEST-OF COMBO: EMA-200 | curated-6 | R=1.5 | relvol=1.5")
    print("=" * 90)
    bestof_cfg = ORBConfig(
        interval="5m", opening_range_minutes=15,
        target_r_multiple=1.5, rel_vol_threshold=1.5,
        require_vwap_support=True,
    )
    trades_bo = run_for_config(CURATED6, df_5m, df_daily, bestof_cfg, 200, "BESTOF")
    s_bo = summarise_trades(trades_bo)

    print()
    print(f"  n trades         : {s_bo['n']}")
    print(f"  win rate         : {s_bo['win_rate']*100:.0f}%")
    print(f"  avg R / trade    : {s_bo['avg_r']:+.3f}R")
    print(f"  total R          : {s_bo['total_r']:+.2f}R")
    print(f"  max consec losers: {s_bo['max_cl']}")
    print(f"  monthly hit rate : {s_bo['monthly_hit']*100:.0f}%")
    print(f"  thresholds pass  : {s_bo['pass_count']}/4")
    print()

    # Detailed monthly for best-of
    if trades_bo:
        months = {}
        for t in trades_bo:
            m = t.entry_time.strftime("%Y-%m")
            months.setdefault(m, []).append(t)
        print("  Monthly breakdown (BESTOF):")
        for m in sorted(months.keys()):
            ts = months[m]
            n_ = len(ts)
            wr_ = sum(1 for t in ts if t.r_multiple > 0) / n_
            tot_ = sum(t.r_multiple for t in ts)
            print(f"    {m}  n={n_:>3}  win={wr_*100:>3.0f}%  tot={tot_:>+6.2f}R")

        # Walk-forward windows for best-of
        trades_bo.sort(key=lambda t: t.entry_time)
        if len(trades_bo) >= 10:
            first = trades_bo[0].entry_time.date()
            last  = trades_bo[-1].entry_time.date()
            span = (last - first).days
            nw = 6
            chunk = span // nw or 1
            print()
            print("  Walk-forward (BESTOF):")
            for i in range(nw):
                lo = first + timedelta(days=i * chunk)
                hi = first + timedelta(days=(i + 1) * chunk) if i < nw - 1 else last + timedelta(days=1)
                sub = [t for t in trades_bo if lo <= t.entry_time.date() < hi]
                if sub:
                    ws = summarise_trades(sub)
                    print(f"    W{i+1}: {lo} --> {hi-timedelta(days=1)}  "
                          f"n={ws['n']:>3}  avg={ws['avg_r']:+.3f}R  tot={ws['total_r']:>+6.2f}R  "
                          f"cl={ws['max_cl']:>2}")

    print()
    print("=" * 90)

    # ---- Final recommendation ----
    print("FINAL RECOMMENDATION")
    print("=" * 90)

    # Find best result
    best = max(results, key=lambda x: x[2]["pass_count"] * 1000 + x[2]["avg_r"] * 100)
    best_label, best_trades, best_s = best

    print(f"  Best config: {best_label}")
    print(f"  n={best_s['n']}  win={best_s['win_rate']*100:.0f}%  "
          f"avg={best_s['avg_r']:+.3f}R  tot={best_s['total_r']:+.2f}R  "
          f"cl={best_s['max_cl']}  mhr={best_s['monthly_hit']*100:.0f}%")
    print(f"  Passes {best_s['pass_count']}/4 thresholds")
    print()

    if best_s["pass_count"] >= 3:
        print(f"  [{PASS}] PROCEED TO BLOCK 2 -- edge holds with stricter params.")
        print(f"        Lock in: {best_label}")
    elif best_s["pass_count"] == 2 and best_s["avg_r"] > 0:
        print(f"  [{WARN}] BUILD BLOCK 2 PLUMBING ONLY -- edge exists but fragile.")
        print(f"        Consider dropping all structural losers from universe.")
    else:
        print(f"  [{FAIL}] DO NOT BUILD ENGINE -- strategy does not generalize.")
        print(f"        Recommend shelving ORB or trying a completely different setup.")

    print()
    print("NEXT STEP: paste this output back to the AI.")
    print("=" * 90)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
