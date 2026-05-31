# tests/test_intraday_backtest.py
"""
Unit tests for intraday_backtest.py (v3.7 prove-the-edge harness).

These exercise the pure functions on hand-crafted 5m bar fixtures with
known outcomes so we can prove:

  * VWAP resets each session
  * Session relative volume is computed against avg-so-far
  * Opening range is the first N minutes only
  * ORB triggers a LONG only after OR, with VWAP support + rel-vol confirm
  * Stops, targets, and force-flat fire correctly
  * R-multiple math is exact
  * Aggregates (win rate, expectancy, max consecutive losers) are correct
  * The harness skips empty / unparsable data gracefully
"""
from __future__ import annotations

from datetime import datetime, date, time as dtime, timedelta

import numpy as np
import pandas as pd
import pytest

from intraday_backtest import (
    ORBConfig,
    Trade,
    BacktestSummary,
    compute_session_vwap,
    compute_session_relative_volume,
    compute_opening_range,
    simulate_orb_session,
    backtest_ticker,
    run_backtest,
    format_text_report,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_session(d: date, bars: list[tuple]) -> pd.DataFrame:
    """Build a session DataFrame.

    bars = list of (HH, MM, open, high, low, close, volume)
    """
    idx = pd.DatetimeIndex(
        [datetime.combine(d, dtime(h, m)) for (h, m, *_rest) in bars],
        name="Date",
    )
    rows = {
        "Open":   [b[2] for b in bars],
        "High":   [b[3] for b in bars],
        "Low":    [b[4] for b in bars],
        "Close":  [b[5] for b in bars],
        "Volume": [b[6] for b in bars],
    }
    return pd.DataFrame(rows, index=idx)


# A canonical "good breakout" session:
#   OR window 09:30–10:00 = first 6 bars; high=101, low=99, range=2
#   At 10:05 a bar closes at 102 with above-avg volume → entry.
#   Target = 102 + 1*2 = 104; stop = 99 (OR low).
#   At 10:25 a bar reaches high=104 → TARGET hit, exit at 104.
#   R = (104 - 102) / (102 - 99) = +0.6667R
GOOD_BREAKOUT_BARS = [
    # h,  m,  o,    h,    l,    c,    vol
    (9, 30, 100.0, 100.5, 99.5, 100.0, 1000),
    (9, 35, 100.0, 100.8, 99.8, 100.5, 1000),
    (9, 40, 100.5, 101.0, 100.0, 100.5, 1000),
    (9, 45, 100.5, 100.8, 99.0, 99.5, 1000),   # OR low = 99.0
    (9, 50, 99.5, 100.0, 99.2, 99.8, 1000),
    (9, 55, 99.8, 101.0, 99.5, 100.5, 1000),   # OR high = 101.0
    # ---- Post-OR ----
    (10, 0,  100.5, 101.5, 100.0, 101.0, 1000), # close=101 NOT > OR_high=101
    (10, 5,  101.0, 102.5, 100.5, 102.0, 5000), # close=102 > OR_high, big vol → ENTRY
    (10, 10, 102.0, 103.0, 101.5, 102.5, 2000),
    (10, 15, 102.5, 103.5, 102.0, 103.0, 2000),
    (10, 20, 103.0, 103.8, 102.5, 103.5, 2000),
    (10, 25, 103.5, 104.5, 103.0, 104.0, 2000), # high=104.5 ≥ target 104 → TARGET
    (10, 30, 104.0, 104.2, 103.8, 104.0, 1500),
]


def good_breakout_df(d: date = date(2025, 1, 6)) -> pd.DataFrame:
    return _make_session(d, GOOD_BREAKOUT_BARS)


# ---------------------------------------------------------------------------
# compute_session_vwap
# ---------------------------------------------------------------------------

class TestVWAP:
    def test_vwap_is_typical_price_when_one_bar(self):
        d = date(2025, 1, 6)
        df = _make_session(d, [(9, 30, 100.0, 102.0, 98.0, 101.0, 1000)])
        v = compute_session_vwap(df)
        # typical price = (102 + 98 + 101) / 3 = 100.333…
        assert v.iloc[0] == pytest.approx(100.333, abs=0.01)

    def test_vwap_resets_each_day(self):
        d1, d2 = date(2025, 1, 6), date(2025, 1, 7)
        df = pd.concat([
            _make_session(d1, [(9, 30, 100, 102, 98, 100, 1000),
                               (9, 35, 100, 101, 99, 100, 1000)]),
            # Day 2 with prices in a DIFFERENT regime
            _make_session(d2, [(9, 30, 200, 202, 198, 200, 1000)]),
        ])
        v = compute_session_vwap(df)
        # Day 2's first bar VWAP should reflect day 2 alone, not the mix.
        v_day2 = v[v.index.normalize() == pd.Timestamp(d2)].iloc[0]
        assert v_day2 == pytest.approx(200, abs=0.5)

    def test_vwap_weighted_by_volume(self):
        d = date(2025, 1, 6)
        df = _make_session(d, [
            (9, 30, 100, 100, 100, 100, 1),     # tp=100, vol=1
            (9, 35, 200, 200, 200, 200, 100),   # tp=200, vol=100
        ])
        v = compute_session_vwap(df)
        # Cumulative: (100*1 + 200*100) / (1+100) = 20100/101 ≈ 199.01
        assert v.iloc[1] == pytest.approx(199.01, abs=0.05)


# ---------------------------------------------------------------------------
# compute_session_relative_volume
# ---------------------------------------------------------------------------

class TestRelativeVolume:
    def test_first_bar_relvol_is_nan(self):
        d = date(2025, 1, 6)
        df = _make_session(d, [(9, 30, 100, 100, 100, 100, 1000)])
        rv = compute_session_relative_volume(df)
        assert pd.isna(rv.iloc[0]), "first bar has no prior avg → NaN"

    def test_second_bar_relvol_uses_first_as_baseline(self):
        d = date(2025, 1, 6)
        df = _make_session(d, [
            (9, 30, 100, 100, 100, 100, 1000),
            (9, 35, 100, 100, 100, 100, 2000),
        ])
        rv = compute_session_relative_volume(df)
        # bar 2: vol=2000, avg_so_far=1000 → rel = 2.0
        assert rv.iloc[1] == pytest.approx(2.0, abs=0.01)

    def test_relvol_resets_per_day(self):
        d1, d2 = date(2025, 1, 6), date(2025, 1, 7)
        df = pd.concat([
            _make_session(d1, [
                (9, 30, 100, 100, 100, 100, 1000),
                (9, 35, 100, 100, 100, 100, 10000),  # huge spike day 1
            ]),
            _make_session(d2, [
                (9, 30, 100, 100, 100, 100, 500),
                (9, 35, 100, 100, 100, 100, 1000),   # day 2's relvol = 2.0
            ]),
        ])
        rv = compute_session_relative_volume(df)
        d2_rows = rv[rv.index.normalize() == pd.Timestamp(d2)]
        assert d2_rows.iloc[1] == pytest.approx(2.0, abs=0.01), \
            "day 2 relvol must NOT be polluted by day 1's volume"


# ---------------------------------------------------------------------------
# compute_opening_range
# ---------------------------------------------------------------------------

class TestOpeningRange:
    def test_or_uses_only_first_n_minutes(self):
        df = good_breakout_df()
        hi, lo = compute_opening_range(df, dtime(9, 30), 30)
        assert hi == 101.0
        assert lo == 99.0

    def test_or_15min(self):
        df = good_breakout_df()
        # First 15 min = bars at 09:30, 09:35, 09:40 only.
        hi, lo = compute_opening_range(df, dtime(9, 30), 15)
        assert hi == 101.0
        assert lo == 99.5

    def test_empty_session_returns_none(self):
        empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        empty.index = pd.DatetimeIndex([], name="Date")
        hi, lo = compute_opening_range(empty, dtime(9, 30), 30)
        assert hi is None and lo is None


# ---------------------------------------------------------------------------
# simulate_orb_session
# ---------------------------------------------------------------------------

class TestSimulateORBSession:
    def test_target_hit_returns_positive_r(self):
        df = good_breakout_df()
        cfg = ORBConfig()
        trade = simulate_orb_session("FOO", df, cfg)
        assert trade is not None
        assert trade.exit_reason == "TARGET"
        assert trade.entry_price == 102.0
        assert trade.stop_loss == 99.0
        assert trade.target == 104.0
        # R = (104 - 102) / (102 - 99) = 0.6667
        assert trade.r_multiple == pytest.approx(2 / 3, abs=0.001)

    def test_stop_hit_returns_negative_r(self):
        # Same setup but after entry the next bar tanks below stop.
        d = date(2025, 1, 6)
        bars = list(GOOD_BREAKOUT_BARS[:8])  # OR + 2 post-OR up through entry
        # Now add a bar that wipes through stop.
        bars.append((10, 10, 102.0, 102.0, 98.0, 99.5, 2000))
        df = _make_session(d, bars)
        trade = simulate_orb_session("FOO", df, ORBConfig())
        assert trade is not None
        assert trade.exit_reason == "STOP"
        assert trade.exit_price == 99.0
        # R = (99 - 102) / (102 - 99) = -1.0R exact
        assert trade.r_multiple == pytest.approx(-1.0, abs=0.001)

    def test_no_breakout_no_trade(self):
        # OR high=101, but no post-OR bar closes above 101.
        d = date(2025, 1, 6)
        bars = list(GOOD_BREAKOUT_BARS[:6])  # OR only
        # post-OR: drifting sideways, no breakout
        for i in range(7):
            bars.append((10, i * 5, 100, 100.5, 99.5, 100.0, 2000))
        df = _make_session(d, bars)
        trade = simulate_orb_session("FOO", df, ORBConfig())
        assert trade is None

    def test_breakout_below_vwap_blocked_when_required(self):
        d = date(2025, 1, 6)
        # Goal: OR_high=110, then a post-OR pump bar inflates VWAP well
        # above 111, then a breakout bar closes at 111 (> OR_high but
        # < VWAP). The pump bar itself must NOT qualify as a breakout, so
        # its CLOSE must stay ≤ OR_high (110). We achieve that by giving
        # the pump bar a tall wick: high=200, low=110, close=110, huge vol.
        # Typical price = (200+110+110)/3 = 140 → drags VWAP up.
        bars = [
            # ---- OR window (first 15 min, three 5-min bars) ----
            (9, 30, 107, 110, 105, 108, 500),
            (9, 35, 108, 110, 105, 108, 500),
            (9, 40, 108, 110, 105, 108, 500),       # OR high=110, low=105
            # ---- Post-OR pump bar: wick to 200, close back at 110 (no
            #      breakout because close ≤ OR_high), moderate volume to
            #      dominate VWAP via typical-price without poisoning the
            #      session-relative-volume baseline so much that no
            #      breakout bar could ever pass rel-vol later.
            (9, 45, 108, 200, 108, 110, 5_000),     # tp=139.3
            (9, 50, 110, 110, 108, 109, 500),
            (9, 55, 109, 110, 108, 110, 500),
            # ---- Breakout candidate at 10:00: close=111 > OR_high 110.
            #      Volume 5000 vs avg-so-far ≈ 1083 → rel-vol ≈ 4.6 → passes.
            #      But VWAP is still well above 111 thanks to the pump →
            #      VWAP filter must reject.
            (10, 0,  110, 112, 110, 111, 5000),
        ]
        df = _make_session(d, bars)
        cfg_strict = ORBConfig(require_vwap_support=True,
                               opening_range_minutes=15)
        assert simulate_orb_session("FOO", df, cfg_strict) is None, \
            "VWAP-support filter should block sub-VWAP breakouts"
        cfg_lax = ORBConfig(require_vwap_support=False,
                            opening_range_minutes=15)
        # Without the VWAP filter the trade triggers (close > OR_high,
        # rel-vol > 1.2x avg).
        trade = simulate_orb_session("FOO", df, cfg_lax)
        assert trade is not None
        assert trade.entry_price == 111.0

    def test_breakout_with_low_relvol_blocked(self):
        d = date(2025, 1, 6)
        bars = list(GOOD_BREAKOUT_BARS[:6])
        # Post-OR breakout but with TINY volume (well under 1.2x avg of 1000).
        bars.append((10, 5, 101, 102.5, 100.5, 102.0, 100))
        bars.append((10, 10, 102, 102.5, 101.5, 102.0, 100))
        df = _make_session(d, bars)
        trade = simulate_orb_session("FOO", df, ORBConfig(rel_vol_threshold=1.2))
        assert trade is None

    def test_force_flat_when_no_exit_triggered(self):
        d = date(2025, 1, 6)
        bars = list(GOOD_BREAKOUT_BARS[:8])  # enters at 10:05 at 102
        # Drift sideways the rest of the day, never hitting stop (99) or
        # target (104). Last bar at 15:50 closes at 102.5.
        for hh in range(10, 16):
            for mm in (15, 30, 45):
                if (hh, mm) == (15, 50):
                    continue
                bars.append((hh, mm, 102.0, 102.8, 101.5, 102.5, 1500))
        bars.append((15, 50, 102.5, 102.8, 101.5, 102.7, 1500))
        df = _make_session(d, bars)
        trade = simulate_orb_session("FOO", df, ORBConfig())
        assert trade is not None
        assert trade.exit_reason == "FORCE_FLAT"
        # R should be positive but small (closed at 102.7, entered at 102).
        # R = (102.7 - 102) / 3 = 0.233
        assert trade.r_multiple == pytest.approx(0.233, abs=0.05)

    def test_only_one_trade_per_session(self):
        """Even with multiple breakouts in a day, we only take the first."""
        df = good_breakout_df()
        cfg = ORBConfig()
        trades = backtest_ticker("FOO", df, cfg)
        assert len(trades) == 1


# ---------------------------------------------------------------------------
# Multi-session aggregation
# ---------------------------------------------------------------------------

class TestAggregation:
    def test_two_winning_days(self):
        d1, d2 = date(2025, 1, 6), date(2025, 1, 7)
        df = pd.concat([
            good_breakout_df(d1),
            good_breakout_df(d2),
        ])
        trades = backtest_ticker("FOO", df, ORBConfig())
        assert len(trades) == 2
        assert all(t.exit_reason == "TARGET" for t in trades)

    def test_summary_metrics(self):
        # Three trades: +1R, -1R, +2R → win rate 2/3, total +2R, avg +0.667R
        cfg = ORBConfig()
        s = BacktestSummary(config=cfg, tickers=["FOO"])
        now = datetime(2025, 1, 6, 10, 5)
        s.trades = [
            Trade("FOO", now.date(), now, 100, 99, 101, now, 101, "TARGET",
                  101, 99, 2, +1.0),
            Trade("FOO", now.date(), now, 100, 99, 101, now, 99, "STOP",
                  101, 99, 2, -1.0),
            Trade("FOO", now.date(), now, 100, 99, 101, now, 103, "TARGET",
                  101, 99, 2, +2.0),
        ]
        assert s.n_trades == 3
        assert s.n_winners == 2
        assert s.n_losers == 1
        assert s.win_rate == pytest.approx(2 / 3, abs=0.001)
        assert s.total_r == pytest.approx(2.0)
        assert s.avg_r == pytest.approx(2 / 3, abs=0.001)
        assert s.median_r == pytest.approx(1.0)
        # losing streak of length 1 (the middle trade)
        assert s.max_consecutive_losers == 1

    def test_max_consecutive_losers(self):
        cfg = ORBConfig()
        s = BacktestSummary(config=cfg, tickers=["X"])
        now = datetime(2025, 1, 6, 10, 5)
        rs = [-1, -1, +1, -1, -1, -1, +1, -1]   # worst run = 3
        s.trades = [
            Trade("X", now.date(), now, 100, 99, 101, now, 100, "STOP",
                  101, 99, 2, float(r))
            for r in rs
        ]
        assert s.max_consecutive_losers == 3

    def test_per_ticker_breakdown(self):
        cfg = ORBConfig()
        s = BacktestSummary(config=cfg, tickers=["A", "B"])
        now = datetime(2025, 1, 6, 10, 5)
        s.trades = [
            Trade("A", now.date(), now, 100, 99, 101, now, 101, "TARGET",
                  101, 99, 2, +1.0),
            Trade("A", now.date(), now, 100, 99, 101, now, 99, "STOP",
                  101, 99, 2, -1.0),
            Trade("B", now.date(), now, 100, 99, 101, now, 102, "TARGET",
                  101, 99, 2, +2.0),
        ]
        rows = {r["ticker"]: r for r in s.per_ticker_breakdown()}
        assert rows["A"]["n_trades"] == 2
        assert rows["A"]["total_r"] == pytest.approx(0.0)
        assert rows["B"]["n_trades"] == 1
        assert rows["B"]["total_r"] == pytest.approx(2.0)
        # Sorted descending by total_r → B first
        assert s.per_ticker_breakdown()[0]["ticker"] == "B"


# ---------------------------------------------------------------------------
# run_backtest (mocked data provider)
# ---------------------------------------------------------------------------

class TestRunBacktest:
    def test_calls_data_provider_with_5m_interval(self):
        captured = {"calls": []}

        def fake_get_history(ticker, **kw):
            captured["calls"].append((ticker, kw))
            return good_breakout_df()

        s = run_backtest(["FOO", "BAR"], cfg=ORBConfig(),
                         get_history_fn=fake_get_history, verbose=False)
        assert len(captured["calls"]) == 2
        for ticker, kw in captured["calls"]:
            assert kw.get("interval") == "5m"
        assert s.n_trades == 2  # one TARGET per ticker

    def test_handles_empty_data(self):
        def fake_get_history(ticker, **kw):
            return pd.DataFrame()

        s = run_backtest(["FOO"], get_history_fn=fake_get_history, verbose=False)
        assert s.n_trades == 0

    def test_handles_missing_columns(self):
        def fake_get_history(ticker, **kw):
            return pd.DataFrame({"Wrong": [1, 2, 3]})

        s = run_backtest(["FOO"], get_history_fn=fake_get_history, verbose=False)
        assert s.n_trades == 0

    def test_handles_fetch_exception(self):
        def fake_get_history(ticker, **kw):
            raise RuntimeError("network down")

        s = run_backtest(["FOO"], get_history_fn=fake_get_history, verbose=False)
        assert s.n_trades == 0


# ---------------------------------------------------------------------------
# Report formatter sanity
# ---------------------------------------------------------------------------

class TestTextReport:
    def test_report_with_no_trades_says_so(self):
        s = BacktestSummary(config=ORBConfig(), tickers=["FOO"])
        out = format_text_report(s)
        assert "NO TRADES" in out

    def test_report_with_trades_includes_metrics(self):
        s = BacktestSummary(config=ORBConfig(), tickers=["FOO"])
        now = datetime(2025, 1, 6, 10, 5)
        s.trades = [
            Trade("FOO", now.date(), now, 100, 99, 101, now, 101, "TARGET",
                  101, 99, 2, +1.0)
        ] * 30  # enough to trigger the verdict heuristic
        out = format_text_report(s)
        assert "n trades" in out
        assert "win rate" in out
        assert "Verdict" in out
        # 30 winners at +1R each → strong edge → ✅
        assert "EDGE LOOKS REAL" in out

    def test_verdict_insufficient_sample(self):
        s = BacktestSummary(config=ORBConfig(), tickers=["FOO"])
        now = datetime(2025, 1, 6, 10, 5)
        s.trades = [
            Trade("FOO", now.date(), now, 100, 99, 101, now, 101, "TARGET",
                  101, 99, 2, +1.0)
        ] * 5
        out = format_text_report(s)
        assert "INSUFFICIENT SAMPLE" in out
