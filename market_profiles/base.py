"""
market_profiles.base — MarketProfile Protocol and shared dataclasses.

A MarketProfile is the single source of truth for every market-specific
constant or callable. Business modules depend ONLY on this Protocol;
they never know whether they're trading Bursa or NYSE.

Design decisions:
    - dataclass + Protocol (structural typing) instead of inheritance.
      Lets us define profiles as module-level singletons without `class`
      boilerplate, while still getting type checking.
    - Callable fields (is_open_fn, slippage_fn) instead of methods.
      Keeps profiles declarative and easy to mock in tests.
    - All times are ZoneInfo-aware. No naive datetimes anywhere.
    - Currency stored as ISO 4217 (MYR / USD) for safety; display strings
      live in `currency_symbol` (RM / $).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# Shared value types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TickerSpec:
    """How a single ticker is represented across data sources."""
    symbol: str                # canonical short form, e.g. "AAPL" or "1155"
    name: str                  # human-readable, e.g. "Apple Inc."
    sector: str                # for sector-exposure risk gates
    yf_symbol: str             # yfinance symbol, e.g. "AAPL" or "1155.KL"
    moomoo_symbol: str         # moomoo OpenD code, e.g. "US.AAPL" or "MY.1155"
    shariah_compliant: bool = False   # only relevant for MY universe

    @property
    def display(self) -> str:
        return self.symbol


@dataclass(frozen=True)
class TradingSession:
    """One contiguous trading session within a day (no lunch break, etc.)."""
    name: str                  # e.g. "MORNING", "AFTERNOON", "RTH"
    start: dtime
    end: dtime


# ---------------------------------------------------------------------------
# Callable contracts (for the slippage / calendar functions)
# ---------------------------------------------------------------------------

SlippageFn = Callable[
    [
        float,  # base price
        int,    # quantity
        float,  # avg daily traded value (RM / USD)
        str,    # side: "BUY" or "SELL"
    ],
    float,  # slippage in absolute price terms
]


IsHolidayFn = Callable[[datetime], bool]
"""Given a market-local datetime, return True if that calendar day is a public holiday."""


# ---------------------------------------------------------------------------
# The MarketProfile Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class MarketProfile(Protocol):
    """Structural contract every market profile must satisfy.

    Profile authors should construct a frozen dataclass that matches this
    Protocol and export it as a module-level singleton.
    """
    # --- identity ---
    code: str                          # "MY" / "US" / future "HK" / "SG"
    display_name: str                  # "Bursa Malaysia" / "United States"
    flag_emoji: str                    # "🇲🇾" / "🇺🇸"

    # --- currency & units ---
    currency_iso: str                  # "MYR" / "USD"
    currency_symbol: str               # "RM" / "$"
    lot_size: int                      # 100 (Bursa board lot) / 1 (US)
    default_capital: float             # seed capital for fresh account

    # --- time & calendar ---
    timezone: ZoneInfo                 # market-local TZ
    sessions: tuple[TradingSession, ...]      # e.g. MORNING + AFTERNOON
    pre_open_minutes: int              # informational, e.g. 30 for Bursa, 0 for US RTH
    safe_entry_cutoff: dtime           # last allowed new-entry time (local)
    is_holiday: IsHolidayFn            # market-local datetime -> bool

    # --- universe ---
    regime_ticker_yf: str              # KLCI / SPY for regime detection
    regime_ticker_moomoo: str          # moomoo equivalent
    default_watchlist: tuple[TickerSpec, ...]

    # --- fees & slippage ---
    fee_rate: float                    # 0.0015 = 0.15% per side (Bursa), 0.0 (US no-comm)
    min_fee: float                     # absolute floor, e.g. RM 8.00 / USD 0.00
    slippage_fn: SlippageFn            # see SlippageFn above

    # --- broker / data integration ---
    moomoo_available: bool             # True if OpenD supports this market
    moomoo_market_enum: str            # "TrdMarket.US" / "TrdMarket.HK" / ""  (str to avoid moomoo import here)
    ticker_yf_template: str            # "{symbol}.KL" / "{symbol}"
    ticker_moomoo_template: str        # "MY.{symbol}" / "US.{symbol}"

    # --- risk defaults (can be overridden in risk_params table) ---
    min_risk_per_trade: float          # min absolute risk in currency (e.g. RM 50, USD 20)

    # --- learner / scheduler tuning (rarely overridden) ---
    cycle_interval_sec: int            # 3600 (1h) default
    bull_max_positions: int
    neutral_max_positions: int
    bear_max_positions: int


# ---------------------------------------------------------------------------
# Helper functions usable by every profile
# ---------------------------------------------------------------------------

def is_within_sessions(now_local: datetime, sessions: tuple[TradingSession, ...]) -> bool:
    """True if `now_local` time-of-day falls inside any session window."""
    t = now_local.time()
    for s in sessions:
        if s.start <= t < s.end:
            return True
    return False


def next_session_start(now_local: datetime, sessions: tuple[TradingSession, ...]) -> datetime:
    """Next session-start datetime ON OR AFTER now_local (same day if possible)."""
    today = now_local.date()
    for s in sessions:
        candidate = datetime.combine(today, s.start, tzinfo=now_local.tzinfo)
        if candidate > now_local:
            return candidate
    # next day, first session
    from datetime import timedelta
    tomorrow = today + timedelta(days=1)
    return datetime.combine(tomorrow, sessions[0].start, tzinfo=now_local.tzinfo)


__all__ = [
    "MarketProfile",
    "TickerSpec",
    "TradingSession",
    "SlippageFn",
    "IsHolidayFn",
    "is_within_sessions",
    "next_session_start",
]
