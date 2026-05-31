"""
Integration smoke tests for v3.6 multi-market dispatch.

Confirms the Block-3 wiring across:
    * db.py — separate DB file per market
    * market_calendar.py — sessions/holidays dispatched on active profile
    * watchlist.py — tickers/sectors come from the right profile
    * trading_engine.py — lot_size() / fee_rate() respect active profile
    * risk_manager.py — currency-aware messages + per-profile seed values

Each test isolates state via a tmp_path home directory so the real
~/.bursa_agent_data/ is never touched.
"""

from __future__ import annotations

import os
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Just isolate market_profiles state + ensure no MARKET_MODE or TRADING_MODE leak.

    NB: we deliberately do NOT change $HOME — the session-scoped
    `_isolate_data_dir` fixture in conftest.py already redirects DATA_DIR
    and pre-imports the business modules against it. Re-pointing HOME here
    would invalidate those module-level captures and break subsequent
    tests' DB access. Per-market DB-file isolation comes from db.py's
    own multi-market path resolution.
    """
    import market_profiles
    # Direct both marker files at a per-test tempdir so MY/US or SWING/INTRADAY flips don't leak
    monkeypatch.setattr(market_profiles, "_MARKER_FILE",
                        tmp_path / ".active_market")
    monkeypatch.setattr(market_profiles, "_TRADING_MODE_FILE",
                        tmp_path / ".trading_mode")
    market_profiles.reset_cache()
    market_profiles.reset_trading_mode_cache()
    monkeypatch.delenv("MARKET_MODE", raising=False)
    monkeypatch.delenv("TRADING_MODE", raising=False)
    yield tmp_path


def _reimport(modnames: list[str]):
    """Reload modules so they re-resolve per-market paths after MARKET_MODE
    changes.

    IMPORTANT (test-isolation fix): we reload *in place* with
    importlib.reload() instead of `del sys.modules[m]; import_module(m)`.

    The delete-then-reimport approach created a BRAND-NEW module object in
    sys.modules. Modules imported earlier (persistence, repository, …) kept
    referencing the OLD `db` module, while any later `from db import …`
    picked up the NEW one — a split-brain where writes went to one db
    module's WAL connection and reads came from another's. This leaked
    across files and made the full-suite run fail (`no such table: account`,
    `get_meta` returning None) even though each file passed alone.

    importlib.reload() keeps the SAME module object (mutates its __dict__),
    so every cross-module reference stays coherent. Since db.current_db_path()
    is already resolved dynamically, reloading is only needed to refresh
    module-level constants captured at import time.
    """
    import sys
    out = []
    for m in modnames:
        mod = sys.modules.get(m)
        out.append(importlib.reload(mod) if mod is not None
                   else importlib.import_module(m))
    return out


# ---------------------------------------------------------------------------
# db.py — separate files per market
# ---------------------------------------------------------------------------

def test_db_path_dispatches_on_active_market_my(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "MY")
    monkeypatch.setenv("TRADING_MODE", "SWING")
    db, = _reimport(["db"])
    # v3.7: DB path splits on (market, mode) -> bursa_agent_<CODE>_<MODE>.db
    assert db.current_db_path().endswith("bursa_agent_MY_SWING.db")


def test_db_path_dispatches_on_active_market_us(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "US")
    monkeypatch.setenv("TRADING_MODE", "SWING")
    db, = _reimport(["db"])
    assert db.current_db_path().endswith("bursa_agent_US_SWING.db")


def test_db_files_are_distinct_per_market(isolated_home, monkeypatch):
    """Switching markets must NEVER point at the same file (cross-contamination)."""
    monkeypatch.setenv("MARKET_MODE", "MY")
    db, = _reimport(["db"])
    my_path = db.current_db_path()
    monkeypatch.setenv("MARKET_MODE", "US")
    import market_profiles
    market_profiles.reset_cache()
    us_path = db.current_db_path()
    assert my_path != us_path
    assert "MY" in my_path and "US" in us_path


# ---------------------------------------------------------------------------
# market_calendar.py — dispatch
# ---------------------------------------------------------------------------

def test_my_session_handling_preserved(isolated_home, monkeypatch):
    """MY-mode behaviour MUST match v3.3 byte-for-byte for the session API."""
    monkeypatch.setenv("MARKET_MODE", "MY")
    _reimport(["db"])
    mc, = _reimport(["market_calendar"])
    from datetime import datetime, time
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Kuala_Lumpur")

    # Tuesday 10:00 MYT — morning session
    now = datetime(2026, 6, 2, 10, 0, tzinfo=tz)
    assert mc.is_market_open(now)
    s = mc.current_session(now)
    assert s.name == "MORNING"

    # Tuesday 13:00 MYT — lunch
    now = datetime(2026, 6, 2, 13, 0, tzinfo=tz)
    assert not mc.is_market_open(now)


def test_us_session_handling_via_dispatch(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "US")
    _reimport(["db"])
    mc, = _reimport(["market_calendar"])
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/New_York")

    # Tuesday 10:00 ET — RTH open
    now = datetime(2026, 6, 2, 10, 0, tzinfo=tz)
    assert mc.is_market_open(now)
    s = mc.current_session(now)
    assert s.name == "RTH"

    # Tuesday 07:00 ET — pre-market (excluded from RTH for our purposes)
    now = datetime(2026, 6, 2, 7, 0, tzinfo=tz)
    assert not mc.is_market_open(now)


def test_us_safe_entry_window_cutoff_at_1530(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "US")
    _reimport(["db"])
    mc, = _reimport(["market_calendar"])
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/New_York")

    assert mc.is_safe_entry_window(datetime(2026, 6, 2, 10, 0, tzinfo=tz))
    # 15:45 is after the cutoff but market is still open
    assert not mc.is_safe_entry_window(datetime(2026, 6, 2, 15, 45, tzinfo=tz))


# ---------------------------------------------------------------------------
# watchlist.py
# ---------------------------------------------------------------------------

def test_watchlist_us_contains_leveraged_etfs(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "US")
    _reimport(["db"])
    wl, = _reimport(["watchlist"])
    tickers = wl.get_all_tickers()
    assert "TQQQ" in tickers
    assert "SOXL" in tickers
    # Should NOT contain any .KL suffix tickers
    assert not any(t.endswith(".KL") for t in tickers)


def test_watchlist_my_contains_full_bursa_list(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "MY")
    _reimport(["db"])
    wl, = _reimport(["watchlist"])
    tickers = wl.get_all_tickers()
    # spot-check a few canonical ones
    assert "1155.KL" in tickers   # Maybank
    assert "0166.KL" in tickers   # Inari
    # All MY tickers must end with .KL
    assert all(t.endswith(".KL") for t in tickers if not t.startswith("^"))


def test_watchlist_us_normalises_added_ticker_without_kl(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "US")
    _reimport(["db"])
    wl, = _reimport(["watchlist"])
    # Even if user accidentally typed AAPL.KL, the US helper strips it
    result = wl.add_custom_ticker("AAPL.KL", "Apple Inc", "Technology")
    assert result == "AAPL"
    wl.remove_custom_ticker("AAPL")


def test_shariah_filter_is_noop_outside_my(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "US")
    _reimport(["db"])
    wl, = _reimport(["watchlist"])
    # In US, the Shariah filter is a no-op — TQQQ is "compliant" by passthrough
    assert wl.is_shariah_compliant("TQQQ") is True


# ---------------------------------------------------------------------------
# trading_engine.py — lot_size / fee_rate dispatch
# ---------------------------------------------------------------------------

def test_trading_engine_lot_size_my_is_100(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "MY")
    _reimport(["db"])
    te, = _reimport(["trading_engine"])
    assert te.lot_size() == 100
    assert te.round_to_lot(137) == 100   # rounds down to 100 lot
    assert te.round_to_lot(99) == 0       # below 1 lot


def test_trading_engine_lot_size_us_is_1(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "US")
    _reimport(["db"])
    te, = _reimport(["trading_engine"])
    assert te.lot_size() == 1
    assert te.round_to_lot(137) == 137   # no rounding needed in US
    assert te.round_to_lot(1) == 1


def test_trading_engine_fee_rate_my_is_15bps(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "MY")
    _reimport(["db"])
    te, = _reimport(["trading_engine"])
    assert te.fee_rate() == pytest.approx(0.0015)


def test_trading_engine_fee_rate_us_is_zero(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "US")
    _reimport(["db"])
    te, = _reimport(["trading_engine"])
    assert te.fee_rate() == 0.0


def test_trading_engine_buy_slippage_buy_is_higher_sell_is_lower(isolated_home, monkeypatch):
    """Sanity: slippage moves the price the right direction for both markets."""
    for market in ("MY", "US"):
        monkeypatch.setenv("MARKET_MODE", market)
        import market_profiles
        market_profiles.reset_cache()
        _reimport(["db"])
        te, = _reimport(["trading_engine"])
        buy_fill, _ = te.apply_buy_slippage(10.0, 100, ticker=None)
        sell_fill, _ = te.apply_sell_slippage(10.0, 100, ticker=None)
        assert buy_fill > 10.0, f"{market}: BUY fill should be ≥ mid"
        assert sell_fill < 10.0, f"{market}: SELL fill should be ≤ mid"


# ---------------------------------------------------------------------------
# risk_manager.py — currency-aware messages + per-profile seed values
# ---------------------------------------------------------------------------

def test_risk_manager_us_seeds_min_risk_usd_20(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "US")
    _reimport(["db"])
    rm, = _reimport(["risk_manager"])
    p = rm.load_risk_params()
    assert p["min_risk_per_trade_rm"] == 20.0   # USD 20 (legacy key name)


def test_risk_manager_my_seeds_min_risk_rm_50(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "MY")
    _reimport(["db"])
    rm, = _reimport(["risk_manager"])
    p = rm.load_risk_params()
    assert p["min_risk_per_trade_rm"] == 50.0


def test_risk_manager_currency_in_reject_message(isolated_home, monkeypatch):
    """A risk-amount rejection should use the active market's currency symbol."""
    monkeypatch.setenv("MARKET_MODE", "US")
    _reimport(["db"])
    rm, = _reimport(["risk_manager"])
    # Risk of $5 is below the US min ($20). Should reject with "$".
    result = rm.check_risk_amount(5.0, capital=5000.0)
    assert result["allowed"] is False
    assert "$" in result["reason"]
    assert "RM" not in result["reason"]


def test_risk_manager_my_uses_rm_in_message(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "MY")
    _reimport(["db"])
    rm, = _reimport(["risk_manager"])
    result = rm.check_risk_amount(10.0, capital=20000.0)
    assert result["allowed"] is False
    assert "RM" in result["reason"]


# ---------------------------------------------------------------------------
# broker_adapter.py — mode dispatch
# ---------------------------------------------------------------------------

def test_broker_adapter_my_always_returns_noop(isolated_home, monkeypatch):
    """No matter what mode is set, MY MUST resolve to NoopAdapter today."""
    monkeypatch.setenv("MARKET_MODE", "MY")
    _reimport(["db"])
    ba, = _reimport(["broker_adapter"])
    for mode in ("NOOP", "SIMULATE", "REAL"):
        ba.reset_adapter_cache()
        a = ba.get_broker_adapter(mode=mode)
        assert a.name == "noop", f"MY+{mode} must be Noop, got {a.name}"


def test_broker_adapter_us_noop_default(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "US")
    _reimport(["db"])
    ba, = _reimport(["broker_adapter"])
    ba.reset_adapter_cache()
    a = ba.get_broker_adapter(mode="NOOP")
    assert a.name == "noop"


def test_broker_adapter_us_simulate_returns_moomoo_us(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "US")
    _reimport(["db"])
    ba, = _reimport(["broker_adapter"])
    ba.reset_adapter_cache()
    a = ba.get_broker_adapter(mode="SIMULATE")
    assert a.name == "moomoo_us"
    # v3.6 Block 5: MoomooUSAdapter.connect() is now implemented.
    # In tests, no OpenD is running so connect() returns False (does NOT raise).
    # Full happy-path connection coverage lives in tests/test_moomoo_us_adapter.py.
    result = a.connect()
    assert result is False, "connect must return False when OpenD is not reachable"
    assert "not listening" in (a.last_error() or "")


def test_broker_adapter_mirror_hooks_are_noop_in_noop_mode(isolated_home, monkeypatch):
    """In NOOP mode the mirror functions must NEVER raise, regardless of args."""
    monkeypatch.setenv("MARKET_MODE", "US")
    _reimport(["db"])
    ba, = _reimport(["broker_adapter"])
    ba.set_broker_mode("NOOP")
    # Should silently do nothing
    ba.mirror_entry_to_broker(ticker="AAPL", shares=10, fill_price=150.0)
    ba.mirror_exit_to_broker(ticker="AAPL", shares=10, fill_price=160.0)


# ---------------------------------------------------------------------------
# data_provider.py — symmetric moomoo dispatch
# ---------------------------------------------------------------------------

def test_data_provider_to_moomoo_code_us(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "US")
    dp, = _reimport(["data_provider"])
    assert dp._to_moomoo_code("AAPL") == "US.AAPL"
    assert dp._to_moomoo_code("TQQQ") == "US.TQQQ"


def test_data_provider_to_moomoo_code_my(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "MY")
    dp, = _reimport(["data_provider"])
    assert dp._to_moomoo_code("1155.KL") == "MY.1155"
    assert dp._to_moomoo_code("^KLSE") == "MY.800000"


def test_data_provider_my_market_unsupported_by_moomoo(isolated_home, monkeypatch):
    """Until Bursa is wired into OpenD, MY tickers must be flagged unsupported."""
    monkeypatch.setenv("MARKET_MODE", "MY")
    dp, = _reimport(["data_provider"])
    assert dp._market_supports_moomoo("1155.KL") is False


def test_data_provider_us_market_supported_by_moomoo(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "US")
    dp, = _reimport(["data_provider"])
    assert dp._market_supports_moomoo("AAPL") is True


def test_data_provider_health_surfaces_active_market(isolated_home, monkeypatch):
    monkeypatch.setenv("MARKET_MODE", "US")
    dp, = _reimport(["data_provider"])
    h = dp.health()
    assert h["active_market"] == "US"
    assert h["moomoo_supports_active_market"] is True


# ---------------------------------------------------------------------------
# v3.7 trading mode API (SWING / INTRADAY)
# ---------------------------------------------------------------------------

def test_trading_mode_defaults_to_swings(isolated_home, monkeypatch):
    """Fresh module state must default to SWING without any marker file."""
    import market_profiles
    monkeypatch.delenv("TRADING_MODE", raising=False)
    market_profiles.reset_trading_mode_cache()
    # Remove any lingering marker file
    try:
        if market_profiles._TRADING_MODE_FILE.exists():
            market_profiles._TRADING_MODE_FILE.unlink()
    except Exception:
        pass
    assert market_profiles.active_trading_mode() == "SWING"


def test_trading_mode_switch_via_env(isolated_home, monkeypatch):
    """TRADING_MODE env var must be respected before any marker file."""
    import market_profiles
    monkeypatch.setenv("TRADING_MODE", "INTRADAY")
    market_profiles.reset_trading_mode_cache()
    try:
        if market_profiles._TRADING_MODE_FILE.exists():
            market_profiles._TRADING_MODE_FILE.unlink()
    except Exception:
        pass
    assert market_profiles.active_trading_mode() == "INTRADAY"


def test_set_trading_mode_persists_to_marker(isolated_home, monkeypatch):
    """set_trading_mode must write the marker file."""
    import market_profiles
    monkeypatch.setenv("TRADING_MODE", "SWING")  # start clean
    market_profiles.reset_trading_mode_cache()
    market_profiles.set_trading_mode("INTRADAY", persist=True)
    assert market_profiles.active_trading_mode() == "INTRADAY"
    # Marker file must exist and contain INTRADAY
    assert market_profiles._TRADING_MODE_FILE.exists()
    assert market_profiles._TRADING_MODE_FILE.read_text().strip() == "INTRADAY"


def test_is_intraday_true_when_mode_intraday(isolated_home, monkeypatch):
    import market_profiles
    monkeypatch.setenv("TRADING_MODE", "INTRADAY")
    market_profiles.reset_trading_mode_cache()
    assert market_profiles.is_intraday() is True


def test_is_intraday_false_when_mode_swing(isolated_home, monkeypatch):
    import market_profiles
    monkeypatch.setenv("TRADING_MODE", "SWING")
    market_profiles.reset_trading_mode_cache()
    assert market_profiles.is_intraday() is False


def test_db_path_includes_trading_mode_suffix(isolated_home, monkeypatch):
    """DB path must reflect both market AND mode."""
    monkeypatch.setenv("MARKET_MODE", "US")
    monkeypatch.setenv("TRADING_MODE", "INTRADAY")
    db, = _reimport(["db"])
    assert db.current_db_path().endswith("bursa_agent_US_INTRADAY.db")


def test_my_profile_supports_intraday_is_false(isolated_home, monkeypatch):
    """MY has no intraday today — Moomoo OpenAPI doesn't support Bursa."""
    monkeypatch.setenv("MARKET_MODE", "MY")
    import market_profiles
    market_profiles.reset_cache()
    assert market_profiles.active_profile().supports_intraday is False


def test_us_profile_supports_intraday_is_true(isolated_home, monkeypatch):
    """US has intraday via Moomoo OpenD."""
    monkeypatch.setenv("MARKET_MODE", "US")
    import market_profiles
    market_profiles.reset_cache()
    p = market_profiles.active_profile()
    assert p.supports_intraday is True
    # Intraday params must be present and sane
    assert p.intraday_interval == "5m"
    assert p.intraday_flat_by.hour == 15 and p.intraday_flat_by.minute == 55
    assert p.intraday_cycle_sec == 300
    assert p.intraday_target_r_multiple == 2.0
    assert p.intraday_require_trend is True
    assert p.intraday_ema_length == 200
    assert p.intraday_rel_vol_threshold == 1.2


def test_trading_mode_reset_clears_cache(isolated_home, monkeypatch):
    """reset_trading_mode_cache must force re-detection from env/marker."""
    import market_profiles
    monkeypatch.setenv("TRADING_MODE", "INTRADAY")
    market_profiles.reset_trading_mode_cache()
    assert market_profiles.active_trading_mode() == "INTRADAY"
    # Swap env and reset cache — must pick up new value
    monkeypatch.setenv("TRADING_MODE", "SWING")
    market_profiles.reset_trading_mode_cache()
    assert market_profiles.active_trading_mode() == "SWING"
