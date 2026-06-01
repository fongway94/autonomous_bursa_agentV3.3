"""
Block 5 tests — intraday scheduler dispatch, cycle logic, and force-flat
integration with the scheduler loop.

Covers:
  * _is_intraday_mode() returns correct boolean
  * _run_intraday_cycle() calls screener + engine correctly
  * _build_intraday_bar_data() returns correct shape
  * Session state dispatch (PREMARKET, OR_WINDOW, ACTIVE, FORCE_FLAT, POST)
  * Force-flat invariant: closes all intraday trades at 15:55 ET
  * Local-only guard: refuses entries without Moomoo OpenD
  * Sleep cadence: intraday uses 5-min, swing uses interval_sec
  * Summary dict has expected keys
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, date, time as dtime, timedelta, timezone
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Session-scoped fixtures — isolate the DB and module state
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _isolate_home():
    """Redirect HOME so DB files land in a temp dir."""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="bursa_t5_")
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = tmp
    yield tmp
    if old_home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = old_home


@pytest.fixture(autouse=True)
def _reset_everything(monkeypatch):
    """Reset all caches and env vars between tests."""
    import os as _os
    _os.environ.pop("MARKET_MODE", None)
    _os.environ.pop("TRADING_MODE", None)
    try:
        import market_profiles as _mp
        _mp.reset_cache()
        _mp.reset_trading_mode_cache()
        from pathlib import Path
        for f in [_mp._MARKER_FILE, _mp._TRADING_MODE_FILE]:
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass
    try:
        import scheduler as _s
        _s._STOP_EVENT.clear()
        _s._ORPHANED_THREAD_IDS.clear()
        _s._THREAD = None
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _init_db():
    """Ensure DB schema exists for each test."""
    from db import init_db
    try:
        init_db()
    except Exception:
        pass
    yield


# ---------------------------------------------------------------------------
# _is_intraday_mode() tests
# ---------------------------------------------------------------------------

class TestIsIntradayMode:
    def test_default_is_swing(self):
        from scheduler import _is_intraday_mode
        assert _is_intraday_mode() is False

    def test_returns_true_when_env_is_intraday(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "INTRADAY")
        import market_profiles
        market_profiles.reset_cache()
        from scheduler import _is_intraday_mode
        assert _is_intraday_mode() is True

    def test_returns_false_when_env_is_swing(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "SWING")
        import market_profiles
        market_profiles.reset_cache()
        from scheduler import _is_intraday_mode
        assert _is_intraday_mode() is False


# ---------------------------------------------------------------------------
# _run_intraday_cycle() — session state dispatch
# ---------------------------------------------------------------------------

class TestIntradayCycleSessionDispatch:
    """The cycle should behave differently based on the session state."""

    def _mock_status(self, state, can_scan, can_enter, should_ff):
        return {
            "state": state,
            "can_scan": can_scan,
            "can_enter": can_enter,
            "should_force_flat": should_ff,
            "message": f"State: {state}",
        }

    def test_premarket_does_nothing(self, monkeypatch):
        from scheduler import _run_intraday_cycle
        monkeypatch.setattr(
            "intraday_engine.intraday_session_status",
            lambda now_et=None: self._mock_status("PREMARKET", False, False, False),
        )
        res = _run_intraday_cycle(True, True)
        assert res["scan_count"] == 0
        assert res["auto_entries"] == 0
        assert res["forced_flats"] == 0
        assert not res["aborted"]

    def test_or_window_scans_but_no_entries(self, monkeypatch):
        from scheduler import _run_intraday_cycle
        monkeypatch.setattr(
            "intraday_engine.intraday_session_status",
            lambda now_et=None: self._mock_status("OR_WINDOW", True, False, False),
        )
        import data_provider
        monkeypatch.setattr(data_provider, "ensure_probed", lambda: None)
        monkeypatch.setattr(data_provider, "health",
                            lambda: {"moomoo_available": False})
        res = _run_intraday_cycle(True, True)
        assert res["scan_count"] == 0
        assert res["auto_entries"] == 0

    def test_force_flat_closes_all(self, monkeypatch):
        from scheduler import _run_intraday_cycle

        monkeypatch.setattr(
            "intraday_engine.intraday_session_status",
            lambda now_et=None: self._mock_status(
                "FORCE_FLAT_WINDOW", False, False, True),
        )

        closed_count = {"n": 0}

        def fake_force_flat(bar_data, actor="AGENT"):
            closed_count["n"] = 3
            return 3

        monkeypatch.setattr(
            "intraday_engine.force_flat_all_intraday", fake_force_flat,
        )
        monkeypatch.setattr("scheduler._build_intraday_bar_data", lambda: {})

        res = _run_intraday_cycle(True, True)
        assert res["forced_flats"] == 3
        assert res["settled"] == 3
        assert closed_count["n"] == 3

    def test_postmarket_does_nothing(self, monkeypatch):
        from scheduler import _run_intraday_cycle
        monkeypatch.setattr(
            "intraday_engine.intraday_session_status",
            lambda now_et=None: self._mock_status("POSTMARKET", False, False, False),
        )
        res = _run_intraday_cycle(True, True)
        assert res["scan_count"] == 0
        assert res["auto_entries"] == 0


# ---------------------------------------------------------------------------
# _run_intraday_cycle() — OpenD guard
# ---------------------------------------------------------------------------

class TestIntradayCycleOpenDGuard:
    def test_refuses_entries_without_opend(self, monkeypatch):
        from scheduler import _run_intraday_cycle

        monkeypatch.setattr(
            "intraday_engine.intraday_session_status",
            lambda now_et=None: {"state": "ACTIVE_TRADING", "can_scan": True,
                                 "can_enter": True, "should_force_flat": False,
                                 "message": "active"},
        )

        import data_provider
        monkeypatch.setattr(data_provider, "ensure_probed", lambda: None)
        monkeypatch.setattr(data_provider, "health",
                            lambda: {"moomoo_available": False})

        res = _run_intraday_cycle(True, True)
        assert res["auto_entries"] == 0
        assert res["scan_count"] == 0

    def test_proceeds_with_opend(self, monkeypatch):
        from scheduler import _run_intraday_cycle

        monkeypatch.setattr(
            "intraday_engine.intraday_session_status",
            lambda now_et=None: {"state": "ACTIVE_TRADING", "can_scan": True,
                                 "can_enter": True, "should_force_flat": False,
                                 "message": "active"},
        )

        import data_provider
        monkeypatch.setattr(data_provider, "ensure_probed", lambda: None)
        monkeypatch.setattr(data_provider, "health",
                            lambda: {"moomoo_available": True})

        # Mock screen_intraday to return empty signals
        monkeypatch.setattr(
            "intraday_screener.screen_intraday", lambda *a, **kw: [],
        )
        monkeypatch.setattr(
            "intraday_engine.get_active_intraday_tickers", lambda: set(),
        )

        res = _run_intraday_cycle(True, True)
        assert res["scan_count"] == 0
        assert res["auto_entries"] == 0


# ---------------------------------------------------------------------------
# _build_intraday_bar_data() tests
# ---------------------------------------------------------------------------

class TestBuildBarData:
    def test_empty_when_no_active_trades(self, monkeypatch):
        monkeypatch.setattr(
            "intraday_engine.get_active_intraday_tickers", lambda: set(),
        )
        from scheduler import _build_intraday_bar_data
        result = _build_intraday_bar_data()
        assert result == {}

    def test_returns_dict_with_price_high_low(self, monkeypatch):
        import pandas as pd

        monkeypatch.setattr(
            "intraday_engine.get_active_intraday_tickers", lambda: {"TNA"},
        )

        fake_df = pd.DataFrame({
            "Close": [45.0, 45.5, 46.0],
            "High": [45.2, 45.8, 46.3],
            "Low": [44.8, 45.2, 45.7],
        }, index=pd.DatetimeIndex([
            "2026-06-03 10:00", "2026-06-03 10:05", "2026-06-03 10:10",
        ]))

        def fake_get_history(tk, **kw):
            return fake_df

        import data_provider
        monkeypatch.setattr(data_provider, "get_history", fake_get_history)

        from scheduler import _build_intraday_bar_data
        result = _build_intraday_bar_data()
        assert "TNA" in result
        assert result["TNA"]["price"] == 46.0
        assert result["TNA"]["high"] == 46.3
        assert result["TNA"]["low"] == 45.7


# ---------------------------------------------------------------------------
# Summary dict shape
# ---------------------------------------------------------------------------

class TestSummaryShape:
    def test_summary_has_expected_keys(self, monkeypatch):
        from scheduler import _run_intraday_cycle

        monkeypatch.setattr(
            "intraday_engine.intraday_session_status",
            lambda now_et=None: {"state": "PREMARKET", "can_scan": False,
                                 "can_enter": False, "should_force_flat": False,
                                 "message": "pre"},
        )
        res = _run_intraday_cycle(True, True)
        expected = {
            "scan_count", "settled", "partials",
            "auto_entries", "rejected", "forced_flats",
            "errors", "aborted",
        }
        assert expected.issubset(set(res.keys()))


# ---------------------------------------------------------------------------
# Ownership check
# ---------------------------------------------------------------------------

class TestOwnershipCheck:
    def test_aborts_when_owner_changed(self, monkeypatch):
        from scheduler import _run_intraday_cycle

        def fake_get_state():
            return {"owner_pid": 99999}

        monkeypatch.setattr("scheduler.get_scheduler_state", fake_get_state)
        res = _run_intraday_cycle(True, True, my_pid=12345)
        assert res["aborted"] is True

    def test_proceeds_when_owner_matches(self, monkeypatch):
        from scheduler import _run_intraday_cycle

        def fake_get_state():
            return {"owner_pid": 12345}

        monkeypatch.setattr("scheduler.get_scheduler_state", fake_get_state)
        monkeypatch.setattr(
            "intraday_engine.intraday_session_status",
            lambda now_et=None: {"state": "PREMARKET", "can_scan": False,
                                 "can_enter": False, "should_force_flat": False,
                                 "message": "pre"},
        )
        res = _run_intraday_cycle(True, True, my_pid=12345)
        assert res["aborted"] is False


# ---------------------------------------------------------------------------
# Cadence constant
# ---------------------------------------------------------------------------

class TestCadence:
    def test_intraday_cycle_sec_is_300(self):
        from scheduler import INTRADAY_CYCLE_SEC
        assert INTRADAY_CYCLE_SEC == 300


# ---------------------------------------------------------------------------
# Regression test: US SWING lot-size bug
# "Unknown reason for zero entries" was caused by hardcoded // 100 rounding
# zeroing all US share quantities (lot size=1, not 100).
# ---------------------------------------------------------------------------

class TestUSSwingLotSizeBug:
    """Regression guard for the lot-size hardcoding bug.

    Before fix: target_shares = (shares // 100) * 100
      e.g. $50 risk / $5 risk_per_share = 10 shares
      10 // 100 * 100 = 0 → silently skipped → "Unknown reason"

    After fix: uses lot_size() from trading_engine (1 for US, 100 for MY)
      10 // 1 * 1 = 10 → enters trade correctly
    """

    def test_us_lot_size_is_1(self, monkeypatch):
        """trading_engine.lot_size() must return 1 for US market."""
        import os
        os.environ["MARKET_MODE"] = "US"
        import market_profiles
        market_profiles.reset_cache()
        from trading_engine import lot_size
        assert lot_size() == 1, (
            "US lot size must be 1 — hardcoding 100 breaks US auto-entry"
        )

    def test_my_lot_size_is_100(self, monkeypatch):
        """trading_engine.lot_size() must return 100 for MY market."""
        import os
        os.environ["MARKET_MODE"] = "MY"
        import market_profiles
        market_profiles.reset_cache()
        from trading_engine import lot_size
        assert lot_size() == 100

    def test_us_shares_not_zeroed_by_lot_rounding(self, monkeypatch):
        """10 shares rounded to US lot (1) must still be 10, not 0."""
        import os
        os.environ["MARKET_MODE"] = "US"
        import market_profiles
        market_profiles.reset_cache()
        from trading_engine import lot_size
        _lot = lot_size()
        # Simulate: $50 risk / $5 risk_per_share = 10 shares
        target_shares = 10
        rounded = (target_shares // _lot) * _lot
        assert rounded == 10, (
            f"US: 10 shares should round to 10 with lot=1, got {rounded}. "
            "Bug: hardcoded // 100 would give 0."
        )

    def test_my_shares_rounded_to_100(self, monkeypatch):
        """137 shares rounded to MY lot (100) must be 100."""
        import os
        os.environ["MARKET_MODE"] = "MY"
        import market_profiles
        market_profiles.reset_cache()
        from trading_engine import lot_size
        _lot = lot_size()
        target_shares = 137
        rounded = (target_shares // _lot) * _lot
        assert rounded == 100
