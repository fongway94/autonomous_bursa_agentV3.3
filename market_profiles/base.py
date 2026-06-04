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


# -------------------------------------------------------------------------
# Shared value types
# -------------------------------------------------------------------------

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


# -------------------------------------------------------------------------
# Callable contracts (for the slippage / calendar functions)
# -------------------------------------------------------------------------

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


# -------------------------------------------------------------------------
# The MarketProfile Protocol
# -------------------------------------------------------------------------

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

    # --- exit sizing ---
    climax_stretch_pct: float          # FIX 3: price stretch above 50-day EMA to trigger
                                        # climax-run profit exit. Market-specific (US ETFs wider
                                        # than MY stocks). Default 20.0%.

    # --- learner / scheduler tuning (rarely overridden) ---
    cycle_interval_sec: int            # 3600 (1h) default
    bull_max_positions: int
    neutral_max_positions: int
    bear_max_positions: int

    # --- v3.7 intraday (see PROJECT_HANDBOOK §15) ---
    # Intraday support is US-only today. MY flips `supports_intraday=True`
    # the day Moomoo OpenAPI adds Bursa coverage.
    supports_intraday: bool            # True = this market can run intraday mode
    intraday_interval: str             # "5m" — candle size for intraday scanning
    intraday_flat_by: dtime            # hard exit time (market-local), e.g. 15:55 ET
    intraday_cycle_sec: int            # scheduler tick rate for intraday mode, e.g. 300 (5 min)
    intraday_target_r_multiple: float  # ORB target, e.g. 2.0R
    intraday_require_trend: bool       # require daily close > EMA for longs
    intraday_ema_length: int           # EMA length for the daily trend filter
    intraday_rel_vol_threshold: float  # breakout-bar volume vs session-avg, e.g. 1.2


# -------------------------------------------------------------------------
# Helper functions usable by every profile
# -------------------------------------------------------------------------

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


# -------------------------------------------------------------------------
# Display helpers (v3.6) — used by the Settings UI so it adapts per market
# -------------------------------------------------------------------------

# The user runs the app from Malaysia, so for non-MY markets we ALSO render
# the equivalent wall-clock time in MYT. This lets a Malaysia-based trader
# know "when do I need to be watching" without doing timezone math.
USER_LOCAL_TZ = ZoneInfo("Asia/Kuala_Lumpur")


def _tz_abbrev(tz: ZoneInfo) -> str:
    """Short label for a timezone, e.g. 'MYT' / 'ET'. Falls back to the
    tzdata abbreviation for the current date if not one we special-case."""
    key = str(tz)
    if key == "Asia/Kuala_Lumpur":
        return "MYT"
    if key == "America/New_York":
        return "ET"
    # Generic fallback — current abbreviation (e.g. 'EDT'/'EST').
    try:
        return datetime.now(tz).strftime("%Z") or key
    except Exception:
        return key


def _to_user_local(t: dtime, market_tz: ZoneInfo, on_date: datetime | None = None) -> dtime:
    """Convert a market-local time-of-day into the user's local (MYT) time-of-day.

    We anchor on a concrete date (today by default) so DST is handled
    correctly — the US↔MY offset shifts between 12h and 13h across the year.
    """
    base_date = (on_date or datetime.now(market_tz)).date()
    market_dt = datetime.combine(base_date, t, tzinfo=market_tz)
    return market_dt.astimezone(USER_LOCAL_TZ).time()


def format_session_window(profile: "MarketProfile", with_user_local: bool = True) -> str:
    """Human-readable session string for the Settings panel.

    MY example:
        '09:00–12:30 and 14:30–17:00 MYT'
    US example (shown to a Malaysia-based user):
        '09:30–16:00 ET  (21:30–04:00 MYT)'

    The MYT mirror is only appended when the market is NOT MY and
    `with_user_local` is True.
    """
    mkt_tz = profile.timezone
    mkt_abbr = _tz_abbrev(mkt_tz)

    def _fmt(sessions, tz_for_value):
        parts = []
        for s in sessions:
            if tz_for_value is None:
                a, b = s.start, s.end
            else:
                a = _to_user_local(s.start, mkt_tz)
                b = _to_user_local(s.end, mkt_tz)
            parts.append(f"{a.strftime('%H:%M')}–{b.strftime('%H:%M')}")
        return " and ".join(parts)

    native = f"{_fmt(profile.sessions, None)} {mkt_abbr}"

    if with_user_local and str(mkt_tz) != str(USER_LOCAL_TZ):
        local = f"{_fmt(profile.sessions, USER_LOCAL_TZ)} {_tz_abbrev(USER_LOCAL_TZ)}"
        return f"{native}  ({local})"
    return native


def format_time_with_user_local(t: dtime, profile: "MarketProfile") -> str:
    """Format a single market-local time, with the MYT equivalent in parens
    for non-MY markets. e.g. '15:30 ET (04:30 MYT)' or '16:00 MYT'."""
    mkt_tz = profile.timezone
    mkt_abbr = _tz_abbrev(mkt_tz)
    native = f"{t.strftime('%H:%M')} {mkt_abbr}"
    if str(mkt_tz) != str(USER_LOCAL_TZ):
        local_t = _to_user_local(t, mkt_tz)
        return f"{native} ({local_t.strftime('%H:%M')} {_tz_abbrev(USER_LOCAL_TZ)})"
    return native


__all__ = [
    "MarketProfile",
    "TickerSpec",
    "TradingSession",
    "SlippageFn",
    "IsHolidayFn",
    "is_within_sessions",
    "next_session_start",
    "format_session_window",
    "format_time_with_user_local",
    "USER_LOCAL_TZ",
]
