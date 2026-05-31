"""
market_profiles — Multi-market abstraction layer for autonomous_bursa_agent.

Each market (MY / US / future: HK / SG) is described by a single MarketProfile
implementation exposing a fixed surface (lot size, calendar, watchlist,
broker adapter class, ticker formats, fee/slippage model, etc.).

Business modules (screener.py, trading_engine.py, scheduler.py, etc.)
import `active_profile()` instead of hard-coding Bursa constants.

Adding a new market = drop in a new `<market>_profile.py` that satisfies
the MarketProfile Protocol — zero changes elsewhere.

Active profile selection priority (first match wins):
    1. Environment variable `MARKET_MODE` (one of: MY, US)
    2. Marker file `~/.bursa_agent_data/.active_market` (set by Settings tab)
    3. Default = MY (preserves v3.3 behaviour for existing deployments)

Active trading mode selection priority (first match wins):
    1. Environment variable `TRADING_MODE` (one of: SWING, INTRADAY)
    2. Marker file `~/.bursa_agent_data/.trading_mode` (set by Settings tab)
    3. Default = SWING (preserves v3.3/v3.6 daily-only behaviour)

DB isolation: each (market, mode) pair has its own SQLite file so the
daily Bayesian brain and the intraday Bayesian brain never cross-contaminate.

NB: We deliberately do NOT read the market from the SQLite `meta` table —
that would create a chicken-and-egg with `db.py` (whose DB_PATH depends on
the active market). The text-file marker breaks the cycle cleanly.

This module deliberately has NO imports from any business module to avoid
circular imports. Profiles import their own dependencies.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

from market_profiles.base import MarketProfile

# Lazy-loaded, cached profile. Reset via set_active_market() on switch.
_ACTIVE_PROFILE: Optional[MarketProfile] = None
_LOCK = threading.RLock()

# Marker file location — kept under DATA_DIR but managed here directly
# (no `from db import DATA_DIR` to avoid circular import).
_DATA_DIR = Path(os.path.expanduser("~")) / ".bursa_agent_data"
_MARKER_FILE = _DATA_DIR / ".active_market"
_TRADING_MODE_FILE = _DATA_DIR / ".trading_mode"

# Valid trading modes.
TRADING_MODES = ("SWING", "INTRADAY")
_DEFAULT_TRADING_MODE = "SWING"

# Cached trading mode (reset via set_trading_mode).
_CACHED_TRADING_MODE: Optional[str] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def active_profile() -> MarketProfile:
    """Return the currently active MarketProfile (thread-safe, cached).

    Resolution order:
        1. env var MARKET_MODE
        2. ~/.bursa_agent_data/.active_market marker file
        3. default 'MY'
    """
    global _ACTIVE_PROFILE
    with _LOCK:
        if _ACTIVE_PROFILE is None:
            _ACTIVE_PROFILE = _resolve_profile(_detect_market_code())
        return _ACTIVE_PROFILE


def active_market_code() -> str:
    """Short form, e.g. 'MY' or 'US'. Cheaper than active_profile() in hot paths."""
    return active_profile().code


def set_active_market(market_code: str, persist: bool = True) -> MarketProfile:
    """Switch the active market profile at runtime.

    Args:
        market_code: 'MY' or 'US' (case-insensitive)
        persist: if True, write to marker file so next boot picks it up

    Returns:
        The newly-activated MarketProfile.

    Raises:
        ValueError: if market_code is unknown.
    """
    global _ACTIVE_PROFILE
    code = market_code.upper().strip()
    new_profile = _resolve_profile(code)
    with _LOCK:
        _ACTIVE_PROFILE = new_profile
    if persist:
        _persist_market_to_marker(code)

    # v3.6 hotfix: ensure the newly-active market's DB has its schema +
    # singleton rows seeded. Without this, the very first switch to a fresh
    # market crashes with `sqlite3.OperationalError: no such table: account`
    # because db.py only ran init_db() at module load time against the
    # market that was active then.
    try:
        from db import init_db
        init_db()
    except Exception:
        # If db.py isn't importable (e.g. very early bootstrap), the next
        # app.py rerun will catch it via its own init_db() call.
        pass

    return new_profile


def available_markets() -> list[str]:
    """All market codes this build supports."""
    return ["MY", "US"]


# ---------------------------------------------------------------------------
# Trading mode API (v3.7)
# ---------------------------------------------------------------------------

def active_trading_mode() -> str:
    """Current trading mode: 'SWING' (default, daily) or 'INTRADAY'.

    Resolution order:
        1. env var TRADING_MODE
        2. ~/.bursa_agent_data/.trading_mode marker file
        3. default 'SWING'

    Intraday is only available on markets that have `supports_intraday=True`
    in their profile. Calling code must check `active_profile().supports_intraday`
    before running intraday logic.
    """
    global _CACHED_TRADING_MODE
    if _CACHED_TRADING_MODE is not None:
        return _CACHED_TRADING_MODE
    mode = _detect_trading_mode()
    _CACHED_TRADING_MODE = mode
    return mode


def is_intraday() -> bool:
    """True if the active mode is INTRADAY."""
    return active_trading_mode() == "INTRADAY"


def set_trading_mode(mode: str, persist: bool = True) -> str:
    """Switch trading mode at runtime.

    Args:
        mode: 'SWING' or 'INTRADAY' (case-insensitive)
        persist: if True, write to marker file so next boot picks it up

    Returns:
        The newly-set mode string.

    Raises:
        ValueError: if mode is not SWING or INTRADAY.
    """
    global _CACHED_TRADING_MODE
    m = mode.upper().strip()
    if m not in TRADING_MODES:
        raise ValueError(
            f"Unknown trading mode {m!r}. Available: {TRADING_MODES}"
        )
    _CACHED_TRADING_MODE = m
    if persist:
        _persist_trading_mode_to_marker(m)
    return m


# ---------------------------------------------------------------------------
# Reset helpers (for tests)
# ---------------------------------------------------------------------------

def reset_cache() -> None:
    """Force re-resolution of active profile on next active_profile() call. For tests."""
    global _ACTIVE_PROFILE
    with _LOCK:
        _ACTIVE_PROFILE = None


def reset_trading_mode_cache() -> None:
    """Force re-resolution of trading mode on next active_trading_mode() call. For tests."""
    global _CACHED_TRADING_MODE
    _CACHED_TRADING_MODE = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _detect_market_code() -> str:
    # 1. env var wins
    env = os.environ.get("MARKET_MODE", "").upper().strip()
    if env in available_markets():
        return env

    # 2. marker file
    try:
        if _MARKER_FILE.exists():
            v = _MARKER_FILE.read_text(encoding="utf-8").strip().upper()
            if v in available_markets():
                return v
    except Exception:
        pass

    # 3. default
    return "MY"


def _detect_trading_mode() -> str:
    """Detect the active trading mode (SWING or INTRADAY)."""
    # 1. env var wins
    env = os.environ.get("TRADING_MODE", "").upper().strip()
    if env in TRADING_MODES:
        return env

    # 2. marker file
    try:
        if _TRADING_MODE_FILE.exists():
            v = _TRADING_MODE_FILE.read_text(encoding="utf-8").strip().upper()
            if v in TRADING_MODES:
                return v
    except Exception:
        pass

    # 3. default
    return _DEFAULT_TRADING_MODE


def _resolve_profile(code: str) -> MarketProfile:
    if code == "MY":
        from market_profiles.my_profile import MY_PROFILE
        return MY_PROFILE
    if code == "US":
        from market_profiles.us_profile import US_PROFILE
        return US_PROFILE
    raise ValueError(
        f"Unknown market code {code!r}. Available: {available_markets()}"
    )


def _persist_market_to_marker(code: str) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _MARKER_FILE.write_text(code, encoding="utf-8")
    except Exception:
        # best-effort — UI layer should surface failures if needed
        pass


def _persist_trading_mode_to_marker(mode: str) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _TRADING_MODE_FILE.write_text(mode, encoding="utf-8")
    except Exception:
        pass


__all__ = [
    "MarketProfile",
    "active_profile",
    "active_market_code",
    "set_active_market",
    "available_markets",
    "reset_cache",
    # v3.7 trading mode API
    "active_trading_mode",
    "is_intraday",
    "set_trading_mode",
    "reset_trading_mode_cache",
    "TRADING_MODES",
]
