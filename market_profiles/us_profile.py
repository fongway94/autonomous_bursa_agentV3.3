"""
market_profiles.us_profile — United States (NYSE/NASDAQ) profile.

Design choices (derived from the Q&A with project owner, 2026-05-30):
    - Universe: ~25 curated leveraged ETFs + high-momentum mega-caps.
      Rationale: keeps Bayesian state space dense, matches reference
      bot (LookAtWallStreet) trading style, fast brain convergence.
    - Lot size: 1 (no board-lot rule in US for retail)
    - Fees: 0.0 (moomoo US is commission-free for stocks/ETFs)
    - Slippage: tighter than MY (~2-15 bps) because of NBBO and depth
    - Sessions: 09:30-16:00 ET regular trading hours only.
      Pre/post-market (04:00-09:30 / 16:00-20:00) excluded from auto-entry
      to match the reference bot's conservative posture (the `fill_outside_rth`
      flag will be exposed in broker_adapter for manual overrides).
    - Holidays: computed from `pandas_market_calendars` NYSE calendar,
      auto-extending — no annual maintenance like the MY public-holiday list.
    - Moomoo: AVAILABLE (TrdMarket.US, codes like "US.AAPL").

NB: The actual holiday lookup requires `pandas_market_calendars` at runtime.
To keep this profile importable without optional deps, the implementation
falls back to a comprehensive hardcoded NYSE set if the library is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date, time as dtime, timedelta
from zoneinfo import ZoneInfo

from market_profiles.base import (
    MarketProfile,
    TickerSpec,
    TradingSession,
)


# -------------------------------------------------------------------------
# Default US watchlist — leveraged ETFs + momentum mega-caps
# ~26 names; user can edit via Settings → Custom Watchlist
# -------------------------------------------------------------------------

_US_WATCHLIST: tuple[TickerSpec, ...] = tuple(
    TickerSpec(
        symbol=s,
        name=n,
        sector=sec,
        yf_symbol=s,
        moomoo_symbol=f"US.{s}",
    )
    for s, n, sec in [
        # --- 3x Leveraged Equity Index ETFs ---
        ("TQQQ", "ProShares UltraPro QQQ",          "Leveraged ETF"),
        ("SQQQ", "ProShares UltraPro Short QQQ",    "Leveraged ETF"),
        ("SPXL", "Direxion Daily S&P 500 Bull 3X",  "Leveraged ETF"),
        ("SPXS", "Direxion Daily S&P 500 Bear 3X",  "Leveraged ETF"),
        ("UPRO", "ProShares UltraPro S&P 500",      "Leveraged ETF"),
        # --- 3x Leveraged Sector ETFs ---
        ("SOXL", "Direxion Daily Semi Bull 3X",     "Leveraged Sector"),
        ("SOXS", "Direxion Daily Semi Bear 3X",     "Leveraged Sector"),
        ("FNGU", "MicroSectors FANG+ 3X",           "Leveraged Sector"),
        ("LABU", "Direxion Daily S&P Bio Bull 3X",  "Leveraged Sector"),
        ("NAIL", "Direxion Daily Homebuilders 3X",  "Leveraged Sector"),
        ("TNA",  "Direxion Daily Russell 2K Bull 3X","Leveraged Sector"),
        # --- Crypto-Linked ---
        ("IBIT", "iShares Bitcoin Trust",           "Crypto"),
        ("MSTR", "MicroStrategy",                   "Crypto"),
        ("COIN", "Coinbase Global",                 "Crypto"),
        ("MARA", "Marathon Digital",                "Crypto"),
        # --- High-Momentum Mega-Caps ---
        ("NVDA", "NVIDIA",                          "Technology"),
        ("TSLA", "Tesla",                           "Technology"),
        ("AMD",  "Advanced Micro Devices",          "Technology"),
        ("META", "Meta Platforms",                  "Technology"),
        ("AAPL", "Apple",                           "Technology"),
        ("MSFT", "Microsoft",                       "Technology"),
        ("GOOGL","Alphabet",                        "Technology"),
        ("AMZN", "Amazon",                          "Consumer Disc."),
        ("PLTR", "Palantir Technologies",           "Technology"),
        # --- Volatility Hedge ---
        ("UVXY", "ProShares Ultra VIX",             "Volatility"),
        ("VXX",  "iPath Series B VIX",              "Volatility"),
    ]
)


# -------------------------------------------------------------------------
# US holiday detection
# -------------------------------------------------------------------------

# Cache the calendar object since pandas_market_calendars is somewhat heavy.
_NYSE_CAL = None
_HOLIDAY_CACHE: dict[int, set[date]] = {}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the nth occurrence of weekday (0=Mon..6=Sun) in given month."""
    first = date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return date(year, month, 1 + delta + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of weekday (0=Mon..6=Sun) in given month."""
    last_day = date(year, month + 1, 1) - timedelta(days=1) if month < 12 \
        else date(year + 1, 1, 1) - timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _good_friday(year: int) -> date:
    """Return Good Friday for a given year via the anonymous Gregorian algorithm."""
    g = year % 19
    c = year // 100
    h = (c - c // 4 - (8 * c + 13) // 25 + 19 * g + 15) % 30
    i = h - (h // 28) * (1 - (29 // (h + 1)) * ((21 - g) // 11))
    j = (year + year // 4 + i + 2 - c + c // 4) % 7
    l = i - j
    month = 3 + (l + 40) // 44
    day = l + 28 - 31 * (month // 4)
    return date(year, month, day) - timedelta(days=2)


def _hardcoded_nyse_holidays(year: int) -> set[date]:
    """Complete hardcoded NYSE holiday set (no pandas_market_calendars needed)."""
    result = set()
    # Fixed-date
    result.add(date(year, 1,  1))
    result.add(date(year, 7,  4))
    result.add(date(year, 12, 25))
    # Floating
    result.add(_nth_weekday(year, 1, 0, 3))   # MLK Day
    result.add(_nth_weekday(year, 2, 0, 3))   # Presidents Day
    result.add(_good_friday(year))             # Good Friday
    result.add(_last_weekday(year, 5, 0))      # Memorial Day
    result.add(_nth_weekday(year, 9, 0, 1))    # Labor Day
    result.add(_nth_weekday(year, 11, 3, 4))   # Thanksgiving
    return result


def _get_nyse_holidays_for_year(year: int) -> set[date]:
    if year in _HOLIDAY_CACHE:
        return _HOLIDAY_CACHE[year]

    global _NYSE_CAL
    try:
        if _NYSE_CAL is None:
            import pandas_market_calendars as mcal
            _NYSE_CAL = mcal.get_calendar("NYSE")
        raw = _NYSE_CAL.holidays().holidays
        result: set[date] = set()
        for h in raw:
            d = date.fromisoformat(str(h)[:10])
            if d.year == year:
                result.add(d)
    except Exception:
        # FIX #3-9: Use complete hardcoded set (was only 3 holidays before).
        result = _hardcoded_nyse_holidays(year)

    _HOLIDAY_CACHE[year] = result
    return result


def _is_us_holiday(local_dt: datetime) -> bool:
    return local_dt.date() in _get_nyse_holidays_for_year(local_dt.year)


# -------------------------------------------------------------------------
# US-specific slippage
# -------------------------------------------------------------------------

def _us_etf_slippage(price: float, qty: int, adv_value: float, side: str) -> float:
    bps_base = 2.0
    notional = price * qty
    if adv_value > 0:
        adv_consumed_pct = (notional / adv_value) * 100.0
        bps_size = min(adv_consumed_pct, 30.0)
    else:
        bps_size = 10.0
    bps_total = min(bps_base + bps_size, 35.0)
    slip = price * (bps_total / 10_000.0)
    return slip if side == "BUY" else -slip


# -------------------------------------------------------------------------
# The profile singleton
# -------------------------------------------------------------------------

@dataclass(frozen=True)
class _USProfile:
    code: str = "US"
    display_name: str = "United States (NYSE/NASDAQ)"
    flag_emoji: str = "🇺🇸"

    currency_iso: str = "USD"
    currency_symbol: str = "$"
    lot_size: int = 1
    default_capital: float = 5_000.0

    timezone: ZoneInfo = ZoneInfo("America/New_York")
    sessions: tuple[TradingSession, ...] = (
        TradingSession("RTH", dtime(9, 30), dtime(16, 0)),
    )
    pre_open_minutes: int = 0
    safe_entry_cutoff: dtime = dtime(15, 30)
    is_holiday: callable = staticmethod(_is_us_holiday)

    # FIX #3-6: QQQ is a better regime proxy than SPY for this portfolio.
    # Watchlist is heavily QQQ/nasdaq/sector-leveraged (TQQQ, SOXL, etc.).
    regime_ticker_yf: str = "QQQ"
    regime_ticker_moomoo: str = "US.QQQ"
    default_watchlist: tuple[TickerSpec, ...] = _US_WATCHLIST

    fee_rate: float = 0.0
    min_fee: float = 0.0
    slippage_fn: callable = staticmethod(_us_etf_slippage)

    moomoo_available: bool = True
    moomoo_market_enum: str = "TrdMarket.US"
    ticker_yf_template: str = "{symbol}"
    ticker_moomoo_template: str = "US.{symbol}"

    min_risk_per_trade: float = 20.0

    cycle_interval_sec: int = 3600
    bull_max_positions: int = 6
    neutral_max_positions: int = 4
    bear_max_positions: int = 2

    # FIX #3-2: Climax exit threshold — US 3x ETFs stretch 25-40% above EMA-50.
    # 30% lets winners run to TP3 while protecting against unsustainable moves.
    climax_stretch_pct: float = 30.0

    supports_intraday: bool = True
    intraday_interval: str = "5m"
    intraday_flat_by: dtime = dtime(15, 55)
    intraday_cycle_sec: int = 300
    intraday_target_r_multiple: float = 2.0
    intraday_require_trend: bool = True
    intraday_ema_length: int = 200
    intraday_rel_vol_threshold: float = 1.2


US_PROFILE: MarketProfile = _USProfile()


__all__ = ["US_PROFILE"]
