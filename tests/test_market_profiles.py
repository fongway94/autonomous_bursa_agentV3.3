"""
Tests for the market_profiles package.

Confirms:
    - Both profiles satisfy the MarketProfile Protocol (structural typing).
    - Profile resolution honours env var > meta table > default.
    - set_active_market() switches cleanly and is reversible.
    - Critical invariants: lot size, fee, currency, regime ticker, etc.
    - Helper functions (is_within_sessions, next_session_start) work for both.
    - Slippage functions return reasonable, side-correct values.

These tests are pure-Python; they DO NOT require yfinance, moomoo, sqlite,
streamlit, or any business-module import. They run in < 1 second.
"""

from __future__ import annotations

import os
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pytest

import market_profiles
from market_profiles import (
    active_profile,
    set_active_market,
    available_markets,
    reset_cache,
)
from market_profiles.base import (
    MarketProfile,
    TickerSpec,
    TradingSession,
    is_within_sessions,
    next_session_start,
    format_session_window,
    format_time_with_user_local,
)
from market_profiles.my_profile import MY_PROFILE
from market_profiles.us_profile import US_PROFILE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_profile_cache(tmp_path, monkeypatch):
    """Clear active-profile cache, isolate marker file, scrub env var."""
    old_env = os.environ.pop("MARKET_MODE", None)
    # Point the marker file at a temp dir so tests don't touch real ~/.bursa_agent_data/
    monkeypatch.setattr(market_profiles, "_MARKER_FILE",
                        tmp_path / ".active_market")
    monkeypatch.setattr(market_profiles, "_DATA_DIR", tmp_path)
    reset_cache()
    yield
    reset_cache()
    if old_env is not None:
        os.environ["MARKET_MODE"] = old_env


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("profile", [MY_PROFILE, US_PROFILE])
def test_profile_satisfies_protocol(profile):
    assert isinstance(profile, MarketProfile)


@pytest.mark.parametrize("profile", [MY_PROFILE, US_PROFILE])
def test_profile_has_all_required_fields(profile):
    required = [
        "code", "display_name", "flag_emoji",
        "currency_iso", "currency_symbol", "lot_size", "default_capital",
        "timezone", "sessions", "pre_open_minutes",
        "safe_entry_cutoff", "is_holiday",
        "regime_ticker_yf", "regime_ticker_moomoo", "default_watchlist",
        "fee_rate", "min_fee", "slippage_fn",
        "moomoo_available", "moomoo_market_enum",
        "ticker_yf_template", "ticker_moomoo_template",
        "min_risk_per_trade",
        "cycle_interval_sec",
        "bull_max_positions", "neutral_max_positions", "bear_max_positions",
    ]
    for field in required:
        assert hasattr(profile, field), f"{profile.code} missing {field}"


# ---------------------------------------------------------------------------
# MY-specific invariants (must not regress v3.3 behaviour)
# ---------------------------------------------------------------------------

def test_my_profile_currency_and_lot():
    assert MY_PROFILE.code == "MY"
    assert MY_PROFILE.currency_iso == "MYR"
    assert MY_PROFILE.currency_symbol == "RM"
    assert MY_PROFILE.lot_size == 100, "Bursa board lot must be 100"
    assert MY_PROFILE.default_capital == 20_000.0


def test_my_profile_sessions_match_v33():
    sessions = MY_PROFILE.sessions
    assert len(sessions) == 2
    assert sessions[0].start == dtime(9, 0) and sessions[0].end == dtime(12, 30)
    assert sessions[1].start == dtime(14, 30) and sessions[1].end == dtime(17, 0)


def test_my_profile_safe_entry_cutoff_at_1600():
    assert MY_PROFILE.safe_entry_cutoff == dtime(16, 0)


def test_my_profile_regime_is_klci():
    assert MY_PROFILE.regime_ticker_yf == "^KLSE"


def test_my_profile_fees_at_15bps():
    assert MY_PROFILE.fee_rate == 0.0015


def test_my_profile_moomoo_still_unavailable():
    """MY OpenAPI not yet released — broker stays in NOOP mode."""
    assert MY_PROFILE.moomoo_available is False


def test_my_profile_yf_template_appends_kl():
    formatted = MY_PROFILE.ticker_yf_template.format(symbol="1155")
    assert formatted == "1155.KL"


# ---------------------------------------------------------------------------
# US-specific invariants
# ---------------------------------------------------------------------------

def test_us_profile_currency_and_lot():
    assert US_PROFILE.code == "US"
    assert US_PROFILE.currency_iso == "USD"
    assert US_PROFILE.currency_symbol == "$"
    assert US_PROFILE.lot_size == 1, "US retail has no board-lot rule"


def test_us_profile_single_rth_session():
    sessions = US_PROFILE.sessions
    assert len(sessions) == 1
    assert sessions[0].start == dtime(9, 30) and sessions[0].end == dtime(16, 0)


def test_us_profile_no_fees_for_moomoo_us():
    assert US_PROFILE.fee_rate == 0.0


def test_us_profile_moomoo_available():
    assert US_PROFILE.moomoo_available is True
    assert "US" in US_PROFILE.moomoo_market_enum


def test_us_profile_yf_template_is_bare_symbol():
    assert US_PROFILE.ticker_yf_template.format(symbol="AAPL") == "AAPL"


def test_us_profile_moomoo_template_prefixes_us():
    assert US_PROFILE.ticker_moomoo_template.format(symbol="AAPL") == "US.AAPL"


def test_us_profile_regime_is_spy():
    assert US_PROFILE.regime_ticker_yf == "QQQ"


def test_us_profile_safe_entry_cutoff_30min_before_close():
    assert US_PROFILE.safe_entry_cutoff == dtime(15, 30)


def test_us_profile_watchlist_contains_core_leveraged_etfs():
    symbols = {t.symbol for t in US_PROFILE.default_watchlist}
    for core in ["TQQQ", "SOXL", "SPXL", "IBIT", "NVDA", "TSLA"]:
        assert core in symbols, f"{core} missing from US default watchlist"


def test_us_watchlist_has_no_duplicates():
    symbols = [t.symbol for t in US_PROFILE.default_watchlist]
    assert len(symbols) == len(set(symbols)), f"duplicates in US watchlist: {symbols}"


# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------

def test_default_profile_is_my_when_no_env_no_meta():
    p = active_profile()
    assert p.code == "MY", "default must remain MY to preserve v3.3 behaviour"


def test_env_var_overrides_default():
    os.environ["MARKET_MODE"] = "US"
    reset_cache()
    p = active_profile()
    assert p.code == "US"


def test_env_var_case_insensitive():
    os.environ["MARKET_MODE"] = "us"
    reset_cache()
    p = active_profile()
    assert p.code == "US"


def test_set_active_market_switches():
    set_active_market("US", persist=False)
    assert active_profile().code == "US"
    set_active_market("MY", persist=False)
    assert active_profile().code == "MY"


def test_set_active_market_rejects_unknown():
    with pytest.raises(ValueError):
        set_active_market("XX", persist=False)


def test_available_markets_lists_both():
    assert set(available_markets()) == {"MY", "US"}


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def test_is_within_sessions_my_morning():
    tz = MY_PROFILE.timezone
    now = datetime(2026, 6, 2, 10, 0, tzinfo=tz)   # Tue 10am MYT
    assert is_within_sessions(now, MY_PROFILE.sessions) is True


def test_is_within_sessions_my_lunch_break():
    tz = MY_PROFILE.timezone
    now = datetime(2026, 6, 2, 13, 0, tzinfo=tz)   # Tue 1pm MYT — lunch
    assert is_within_sessions(now, MY_PROFILE.sessions) is False


def test_is_within_sessions_us_rth():
    tz = US_PROFILE.timezone
    now = datetime(2026, 6, 2, 10, 0, tzinfo=tz)   # Tue 10am ET
    assert is_within_sessions(now, US_PROFILE.sessions) is True


def test_is_within_sessions_us_premarket():
    tz = US_PROFILE.timezone
    now = datetime(2026, 6, 2, 7, 0, tzinfo=tz)    # Tue 7am ET — premarket
    assert is_within_sessions(now, US_PROFILE.sessions) is False


def test_next_session_start_us_after_close():
    tz = US_PROFILE.timezone
    now = datetime(2026, 6, 2, 17, 0, tzinfo=tz)   # Tue 5pm ET — after close
    nxt = next_session_start(now, US_PROFILE.sessions)
    assert nxt.date() == (now + timedelta(days=1)).date()
    assert nxt.time() == dtime(9, 30)


def test_next_session_start_my_during_lunch_returns_afternoon():
    tz = MY_PROFILE.timezone
    now = datetime(2026, 6, 2, 13, 0, tzinfo=tz)   # lunch break
    nxt = next_session_start(now, MY_PROFILE.sessions)
    assert nxt.date() == now.date()
    assert nxt.time() == dtime(14, 30)


# ---------------------------------------------------------------------------
# Slippage functions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("profile", [MY_PROFILE, US_PROFILE])
def test_slippage_buy_is_positive_sell_is_negative(profile):
    slip_buy = profile.slippage_fn(10.0, 1000, 1_000_000.0, "BUY")
    slip_sell = profile.slippage_fn(10.0, 1000, 1_000_000.0, "SELL")
    assert slip_buy > 0, f"{profile.code} BUY slippage should add to price"
    assert slip_sell < 0, f"{profile.code} SELL slippage should subtract from price"
    assert abs(slip_buy) == abs(slip_sell), "magnitude must be symmetric"


def test_my_slippage_capped_at_80bps():
    # Huge order on thin name
    slip = MY_PROFILE.slippage_fn(1.0, 1_000_000, 10_000.0, "BUY")
    assert slip <= 1.0 * 0.0080 + 1e-9, "MY slippage cap violated"


def test_us_slippage_tighter_than_my_for_same_inputs():
    args = (10.0, 1000, 1_000_000.0, "BUY")
    assert US_PROFILE.slippage_fn(*args) < MY_PROFILE.slippage_fn(*args)


# ---------------------------------------------------------------------------
# Ticker formatting
# ---------------------------------------------------------------------------

def test_my_tickers_use_kl_suffix():
    for t in MY_PROFILE.default_watchlist:
        assert t.yf_symbol.endswith(".KL"), f"{t.symbol} bad yf format"
        assert t.moomoo_symbol.startswith("MY."), f"{t.symbol} bad moomoo format"


def test_us_tickers_use_us_prefix_for_moomoo():
    for t in US_PROFILE.default_watchlist:
        assert t.moomoo_symbol.startswith("US."), f"{t.symbol} bad moomoo format"
        assert "." not in t.yf_symbol, f"{t.symbol} yf format must be bare"


# ---------------------------------------------------------------------------
# Holiday detection (light smoke tests — exhaustive coverage in market_calendar tests)
# ---------------------------------------------------------------------------

def test_us_holiday_new_years_day():
    tz = US_PROFILE.timezone
    nyd = datetime(2026, 1, 1, 12, 0, tzinfo=tz)
    assert US_PROFILE.is_holiday(nyd) is True


def test_us_holiday_regular_weekday_is_not_holiday():
    tz = US_PROFILE.timezone
    normal = datetime(2026, 6, 2, 12, 0, tzinfo=tz)  # Tuesday
    assert US_PROFILE.is_holiday(normal) is False


# ---------------------------------------------------------------------------
# Display helpers (v3.6) — Settings panel adapts per market.
# The user runs from Malaysia, so non-MY markets also show the MYT mirror.
# ---------------------------------------------------------------------------

def test_format_session_window_my_is_native_only():
    """MY market shows native MYT sessions with NO redundant mirror."""
    s = format_session_window(MY_PROFILE)
    assert "09:00–12:30" in s
    assert "14:30–17:00" in s
    assert "MYT" in s
    # No bracketed mirror because market TZ == user TZ.
    assert "(" not in s


def test_format_session_window_us_shows_et_and_myt():
    """US market shows ET sessions PLUS the Malaysia-local (MYT) equivalent."""
    s = format_session_window(US_PROFILE)
    assert "09:30–16:00" in s
    assert "ET" in s
    # Mirror in brackets, labelled MYT.
    assert "MYT" in s
    assert "(" in s and ")" in s


def test_format_session_window_us_can_suppress_mirror():
    s = format_session_window(US_PROFILE, with_user_local=False)
    assert "ET" in s
    assert "MYT" not in s
    assert "(" not in s


def test_format_time_with_user_local_my():
    s = format_time_with_user_local(MY_PROFILE.safe_entry_cutoff, MY_PROFILE)
    assert s == "16:00 MYT"


def test_format_time_with_user_local_us_has_myt_mirror():
    s = format_time_with_user_local(US_PROFILE.safe_entry_cutoff, US_PROFILE)
    assert s.startswith("15:30 ET")
    assert "MYT" in s
    assert "(" in s and ")" in s
