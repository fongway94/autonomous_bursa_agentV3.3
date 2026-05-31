#!/usr/bin/env python3
# tests/test_intraday_screener.py
"""Unit tests for intraday_screener.py (v3.7 Block 3)."""

from __future__ import annotations

from datetime import datetime, date, time as dtime, timedelta

import numpy as np
import pandas as pd
import pytest

from intraday_screener import (
    compute_intraday_signal,
    screen_intraday,
    INTRADAY_DEFAULTS,
    DEFAULT_INTRADAY_WATCHLIST,
    INTRADAY_EMA_LENGTH,
    INTRADAY_LONGS_ONLY,
)
from intraday_backtest import ORBConfig


US_ET = "America/New_York"
SESSION_OPEN = dtime(9, 30)
FLAT_BY = dtime(15, 55)


def _make_bars(d: date, bars: list[tuple], tz: str = US_ET) -> pd.DataFrame:
    idx = pd.DatetimeIndex(
        [pd.Timestamp(datetime.combine(d, dtime(h, m)), tz=tz)
         for (h, m, *_rest) in bars],
        name="Date",
    )
    rows = {
        "Open": [b[2] for b in bars],
        "High": [b[3] for b in bars],
        "Low": [b[4] for b in bars],
        "Close": [b[5] for b in bars],
        "Volume": [b[6] for b in bars],
    }
    return pd.DataFrame(rows, index=idx)


def _make_daily(dates: list[date], closes: list[float], tz: str = US_ET) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz=tz) for d in dates], name="Date")
    return pd.DataFrame({
        "Open": [c * 0.99 for c in closes],
        "High": [c * 1.02 for c in closes],
        "Low": [c * 0.98 for c in closes],
        "Close": closes,
        "Volume": [1_000_000] * len(closes),
    }, index=idx)


def _good_breakout_session(d: date = date(2026, 6, 3)) -> pd.DataFrame:
    return _make_bars(d, [
        (9, 30, 100.0, 100.5, 99.5, 100.0, 1000),
        (9, 35, 100.0, 100.8, 99.8, 100.5, 1000),
        (9, 40, 100.5, 101.0, 99.0, 100.0, 1000),  # OR high=101, low=99
        (9, 45, 100.0, 100.5, 99.5, 100.0, 1000),
        (9, 50, 100.5, 102.5, 100.0, 102.0, 5000),  # BREAKOUT
        (9, 55, 102.0, 103.0, 101.5, 102.5, 2000),
        (10, 0,  102.5, 103.0, 102.0, 102.5, 2000),
    ])


def _uptrend_daily(d: date = date(2026, 6, 3)) -> pd.DataFrame:
    dates_list = [d - timedelta(days=i) for i in range(200, -1, -1)]
    closes = [90.0 + i * 0.05 for i in range(201)]
    return _make_daily(dates_list, closes)


def _downtrend_daily(d: date = date(2026, 6, 3)) -> pd.DataFrame:
    dates_list = [d - timedelta(days=i) for i in range(200, -1, -1)]
    closes = [110.0 - i * 0.05 for i in range(201)]
    return _make_daily(dates_list, closes)


# ---------------------------------------------------------------------------
# Signal shape
# ---------------------------------------------------------------------------

class TestSignalShape:
    SWING_KEYS = {
        "ticker", "name", "sector",
        "price", "prev_price", "change_pct",
        "volume", "vol_ratio", "rsi",
        "signal", "reasoning", "confidence",
        "entry", "stop_loss", "tp1", "tp2", "tp3",
        "risk_pct", "atr", "support", "resistance",
        "ema_trend", "ema_fast", "ema_slow",
        "macd_hist", "bb_upper", "bb_lower",
        "market_regime", "rs_rank", "rs_signal", "rs_ratio",
        "q_action", "q_confidence", "q_reasoning",
        "indicators",
    }

    def test_signal_has_all_swing_keys(self):
        d = date(2026, 6, 3)
        df_5m = _good_breakout_session(d)
        df_daily = _uptrend_daily(d)
        now = datetime.combine(d, dtime(10, 5))
        sig = compute_intraday_signal("TNA", df_5m, df_daily, now_et=now)
        assert sig is not None, "Should fire GOLD BUY"
        assert set(sig.keys()) >= self.SWING_KEYS
        assert sig["source"] == "INTRADAY"

    def test_signal_has_intraday_indicators(self):
        d = date(2026, 6, 3)
        df_5m = _good_breakout_session(d)
        df_daily = _uptrend_daily(d)
        now = datetime.combine(d, dtime(10, 5))
        sig = compute_intraday_signal("TNA", df_5m, df_daily, now_et=now)
        assert sig is not None
        ind = sig["indicators"]
        for k in ("vwap", "rel_vol", "or_high", "or_low", "or_range",
                  "ema_trend_direction", "entry_timestamp"):
            assert k in ind


# ---------------------------------------------------------------------------
# Signal classification
# ---------------------------------------------------------------------------

class TestSignalClassification:
    def test_gold_buy_on_perfect_breakout(self):
        d = date(2026, 6, 3)
        df_5m = _good_breakout_session(d)
        df_daily = _uptrend_daily(d)
        now = datetime.combine(d, dtime(10, 5))
        sig = compute_intraday_signal("TNA", df_5m, df_daily, now_et=now)
        assert sig is not None
        assert "GOLD BUY" in sig["signal"]
        assert sig["entry"] == 102.0
        assert sig["stop_loss"] == 99.0
        assert sig["confidence"] >= 50.0

    def test_no_signal_before_or_window_closes(self):
        d = date(2026, 6, 3)
        df_5m = _good_breakout_session(d)
        df_daily = _uptrend_daily(d)
        now = datetime.combine(d, dtime(9, 40))
        sig = compute_intraday_signal("TNA", df_5m, df_daily, now_et=now)
        assert sig is None

    def test_no_signal_before_session_open(self):
        d = date(2026, 6, 3)
        df_5m = _good_breakout_session(d)
        df_daily = _uptrend_daily(d)
        now = datetime.combine(d, dtime(8, 0))
        sig = compute_intraday_signal("TNA", df_5m, df_daily, now_et=now)
        assert sig is None

    def test_no_signal_after_flat_by(self):
        d = date(2026, 6, 3)
        df_5m = _good_breakout_session(d)
        df_daily = _uptrend_daily(d)
        now = datetime.combine(d, dtime(15, 56))
        sig = compute_intraday_signal("TNA", df_5m, df_daily, now_et=now)
        assert sig is None

    def test_no_breakout_no_signal(self):
        d = date(2026, 6, 3)
        bars = [
            (9, 30, 100, 101, 99, 100, 1000),
            (9, 35, 100, 101, 99, 100, 1000),
            (9, 40, 100, 101, 99, 100, 1000),
            (9, 45, 100, 100.5, 99.5, 100, 2000),
            (9, 50, 100, 100.8, 99.8, 100.5, 2000),
        ]
        df_5m = _make_bars(d, bars)
        df_daily = _uptrend_daily(d)
        now = datetime.combine(d, dtime(10, 0))
        sig = compute_intraday_signal("TNA", df_5m, df_daily, now_et=now)
        assert sig is None


# ---------------------------------------------------------------------------
# Filter tests
# ---------------------------------------------------------------------------

class TestFilters:
    def test_vwap_filter_blocks_sub_vwap_breakout(self):
        d = date(2026, 6, 3)
        bars = [
            (9, 30, 107, 110, 105, 108, 500),
            (9, 35, 108, 110, 105, 108, 500),
            (9, 40, 108, 110, 105, 108, 500),
            (9, 45, 108, 200, 108, 110, 5_000),
            (9, 50, 110, 110, 108, 109, 500),
            (9, 55, 109, 110, 108, 110, 500),
            (10, 0,  110, 112, 110, 111, 5000),
        ]
        df_5m = _make_bars(d, bars)
        df_daily = _uptrend_daily(d)
        now = datetime.combine(d, dtime(10, 5))
        cfg = ORBConfig(interval="5m", opening_range_minutes=15,
                        target_r_multiple=2.0, rel_vol_threshold=1.2,
                        require_vwap_support=True)
        sig = compute_intraday_signal("TNA", df_5m, df_daily, cfg, now_et=now)
        assert sig is None, "VWAP filter should block sub-VWAP breakout"

    def test_vwap_disabled_allows_breakout(self):
        d = date(2026, 6, 3)
        bars = [
            (9, 30, 107, 110, 105, 108, 500),
            (9, 35, 108, 110, 105, 108, 500),
            (9, 40, 108, 110, 105, 108, 500),
            (9, 45, 108, 200, 108, 110, 5_000),
            (9, 50, 110, 110, 108, 109, 500),
            (9, 55, 109, 110, 108, 110, 500),
            (10, 0,  110, 112, 110, 111, 5000),
        ]
        df_5m = _make_bars(d, bars)
        df_daily = _uptrend_daily(d)
        now = datetime.combine(d, dtime(10, 5))
        cfg = ORBConfig(interval="5m", opening_range_minutes=15,
                        target_r_multiple=2.0, rel_vol_threshold=1.2,
                        require_vwap_support=False)
        sig = compute_intraday_signal("TNA", df_5m, df_daily, cfg, now_et=now)
        assert sig is not None

    def test_rel_vol_filter_blocks_low_volume_breakout(self):
        d = date(2026, 6, 3)
        bars = [
            (9, 30, 100, 101, 99, 100, 1000),
            (9, 35, 100, 101, 99, 100, 1000),
            (9, 40, 100, 101, 99, 100, 1000),
            (9, 45, 100, 102.5, 100, 102, 100),
        ]
        df_5m = _make_bars(d, bars)
        df_daily = _uptrend_daily(d)
        now = datetime.combine(d, dtime(10, 0))
        sig = compute_intraday_signal("TNA", df_5m, df_daily, now_et=now)
        assert sig is None

    def test_trend_filter_blocks_downtrend(self):
        d = date(2026, 6, 3)
        df_5m = _good_breakout_session(d)
        df_daily = _downtrend_daily(d)
        now = datetime.combine(d, dtime(10, 5))
        sig = compute_intraday_signal("TNA", df_5m, df_daily, now_et=now)
        assert sig is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_already_triggered_today(self):
        d = date(2026, 6, 3)
        df_5m = _good_breakout_session(d)
        df_daily = _uptrend_daily(d)
        now = datetime.combine(d, dtime(10, 5))
        sig = compute_intraday_signal("TNA", df_5m, df_daily, now_et=now,
                                      already_triggered_today=True)
        assert sig is None

    def test_empty_df(self):
        empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        empty.index = pd.DatetimeIndex([], name="Date")
        now = datetime.combine(date(2026, 6, 3), dtime(10, 5))
        sig = compute_intraday_signal("TNA", empty, pd.DataFrame(), now_et=now)
        assert sig is None

    def test_missing_columns(self):
        bad = pd.DataFrame({"X": [1, 2, 3]})
        bad.index = pd.DatetimeIndex(
            [pd.Timestamp("2026-06-03 09:30"), pd.Timestamp("2026-06-03 09:35"),
             pd.Timestamp("2026-06-03 09:40")], name="Date")
        now = datetime.combine(date(2026, 6, 3), dtime(10, 5))
        sig = compute_intraday_signal("TNA", bad, pd.DataFrame(), now_et=now)
        assert sig is None

    def test_confidence_bounded(self):
        d = date(2026, 6, 3)
        df_5m = _good_breakout_session(d)
        df_daily = _uptrend_daily(d)
        now = datetime.combine(d, dtime(10, 5))
        sig = compute_intraday_signal("TNA", df_5m, df_daily, now_et=now)
        assert sig is not None
        assert 5.0 <= sig["confidence"] <= 99.0

    def test_targets_ascending(self):
        d = date(2026, 6, 3)
        df_5m = _good_breakout_session(d)
        df_daily = _uptrend_daily(d)
        now = datetime.combine(d, dtime(10, 5))
        sig = compute_intraday_signal("TNA", df_5m, df_daily, now_et=now)
        assert sig is not None
        assert sig["tp1"] < sig["tp2"] < sig["tp3"]
        assert sig["tp1"] == pytest.approx(105.0)
        assert sig["tp2"] == pytest.approx(106.0)
        assert sig["tp3"] == pytest.approx(107.0)

    def test_risk_pct_positive(self):
        d = date(2026, 6, 3)
        df_5m = _good_breakout_session(d)
        df_daily = _uptrend_daily(d)
        now = datetime.combine(d, dtime(10, 5))
        sig = compute_intraday_signal("TNA", df_5m, df_daily, now_et=now)
        assert sig is not None
        assert sig["risk_pct"] > 0

    def test_zero_or_range_no_signal(self):
        d = date(2026, 6, 3)
        bars = [
            (9, 30, 100, 100, 100, 100, 500),
            (9, 35, 100, 100, 100, 100, 500),
            (9, 40, 100, 100, 100, 100, 500),
            (9, 45, 100, 101, 100, 101, 5000),
        ]
        df_5m = _make_bars(d, bars)
        df_daily = _uptrend_daily(d)
        now = datetime.combine(d, dtime(10, 0))
        sig = compute_intraday_signal("TNA", df_5m, df_daily, now_et=now)
        assert sig is None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class TestScreenIntraday:
    def test_returns_signals_for_valid_breakouts(self, monkeypatch):
        d = date(2026, 6, 3)
        now = datetime.combine(d, dtime(10, 5))
        df_5m = _good_breakout_session(d)
        df_d = _uptrend_daily(d)

        def fake_get_history(ticker, **kw):
            interval = kw.get("interval", "1d")
            return df_5m.copy() if interval == "5m" else df_d.copy()

        monkeypatch.setattr("data_provider.get_history", fake_get_history)
        monkeypatch.setattr("data_provider.ensure_probed", lambda: None)
        monkeypatch.setattr("data_provider.health",
                            lambda: {"moomoo_available": False})

        results = screen_intraday(["TNA", "GOOGL"], now_et=now)
        assert len(results) == 2
        assert all(r["source"] == "INTRADAY" for r in results)

    def test_respects_already_triggered_set(self, monkeypatch):
        d = date(2026, 6, 3)
        now = datetime.combine(d, dtime(10, 5))
        df_5m = _good_breakout_session(d)
        df_d = _uptrend_daily(d)

        def fake_get_history(ticker, **kw):
            interval = kw.get("interval", "1d")
            return df_5m.copy() if interval == "5m" else df_d.copy()

        monkeypatch.setattr("data_provider.get_history", fake_get_history)
        monkeypatch.setattr("data_provider.ensure_probed", lambda: None)
        monkeypatch.setattr("data_provider.health",
                            lambda: {"moomoo_available": False})

        results = screen_intraday(["TNA", "GOOGL"], now_et=now,
                                  already_triggered={"TNA"})
        assert len(results) == 1
        assert results[0]["ticker"] == "GOOGL"

    def test_empty_when_no_data(self, monkeypatch):
        def fake_get_history(ticker, **kw):
            return pd.DataFrame()
        monkeypatch.setattr("data_provider.get_history", fake_get_history)
        monkeypatch.setattr("data_provider.ensure_probed", lambda: None)
        monkeypatch.setattr("data_provider.health",
                            lambda: {"moomoo_available": False})
        results = screen_intraday(["TNA"], now_et=datetime.now())
        assert results == []

    def test_handles_fetch_exceptions(self, monkeypatch):
        def fake_get_history(ticker, **kw):
            raise RuntimeError("network down")
        monkeypatch.setattr("data_provider.get_history", fake_get_history)
        monkeypatch.setattr("data_provider.ensure_probed", lambda: None)
        monkeypatch.setattr("data_provider.health",
                            lambda: {"moomoo_available": False})
        results = screen_intraday(["TNA"], now_et=datetime.now())
        assert results == []

    def test_default_watchlist_is_curated_6(self):
        assert len(DEFAULT_INTRADAY_WATCHLIST) == 6
        assert "TNA" in DEFAULT_INTRADAY_WATCHLIST
        assert "GOOGL" in DEFAULT_INTRADAY_WATCHLIST
        assert "TQQQ" in DEFAULT_INTRADAY_WATCHLIST
        assert "MSTR" in DEFAULT_INTRADAY_WATCHLIST
        assert "SOXL" in DEFAULT_INTRADAY_WATCHLIST
        assert "PLTR" in DEFAULT_INTRADAY_WATCHLIST


# ---------------------------------------------------------------------------
# Locked parameters (round-4 spec)
# ---------------------------------------------------------------------------

class TestLockedParameters:
    def test_or_minutes_is_15(self):
        assert INTRADAY_DEFAULTS.opening_range_minutes == 15
    def test_target_r_is_2(self):
        assert INTRADAY_DEFAULTS.target_r_multiple == 2.0
    def test_rel_vol_is_1_2(self):
        assert INTRADAY_DEFAULTS.rel_vol_threshold == 1.2
    def test_vwap_support_is_true(self):
        assert INTRADAY_DEFAULTS.require_vwap_support is True
    def test_interval_is_5m(self):
        assert INTRADAY_DEFAULTS.interval == "5m"
    def test_ema_length_is_200(self):
        assert INTRADAY_EMA_LENGTH == 200
    def test_longs_only_is_true(self):
        assert INTRADAY_LONGS_ONLY is True
    def test_flat_by_is_1555(self):
        assert INTRADAY_DEFAULTS.flat_by == dtime(15, 55)
    def test_explorer_target_is_100(self):
        from intraday_screener import INTRADAY_EXPLORER_TARGET
        assert INTRADAY_EXPLORER_TARGET == 100
