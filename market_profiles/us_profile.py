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
falls back to a small hardcoded NYSE set if the library is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date, time as dtime
from zoneinfo import ZoneInfo

from market_profiles.base import (
    MarketProfile,
    TickerSpec,
    TradingSession,
)


# ---------------------------------------------------------------------------
# Default US watchlist — leveraged ETFs + momentum mega-caps
# ~25 names; user can edit via Settings → Custom Watchlist
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# US holiday detection
# ---------------------------------------------------------------------------

# Cache the calendar object since pandas_market_calendars is somewhat heavy.
_NYSE_CAL = None
_HOLIDAY_CACHE: dict[int, set[date]] = {}


def _get_nyse_holidays_for_year(year: int) -> set[date]:
    """Return the set of NYSE holiday dates for the given year.

    Uses pandas_market_calendars if available; otherwise falls back to a
    minimal hardcoded set (warns once).
    """
    if year in _HOLIDAY_CACHE:
        return _HOLIDAY_CACHE[year]

    global _NYSE_CAL
    try:
        if _NYSE_CAL is None:
            import pandas_market_calendars as mcal  # type: ignore
            _NYSE_CAL = mcal.get_calendar("NYSE")
        # holidays() returns a tuple of numpy.datetime64. Normalise to date,
        # then filter by year.
        raw = _NYSE_CAL.holidays().holidays
        result: set[date] = set()
        for h in raw:
            # numpy.datetime64 -> ISO string -> date
            d = date.fromisoformat(str(h)[:10])
            if d.year == year:
                result.add(d)
    except Exception:
        # Minimal fallback — only fixed-date NYSE holidays.
        # Good enough to avoid trading on Christmas / New Year if the optional
        # dependency isn't installed; UI should warn.
        result = {
            date(year, 1,  1),    # New Year's Day
            date(year, 7,  4),    # Independence Day
            date(year, 12, 25),   # Christmas Day
        }
    _HOLIDAY_CACHE[year] = result
    return result


def _is_us_holiday(local_dt: datetime) -> bool:
    return local_dt.date() in _get_nyse_holidays_for_year(local_dt.year)


# ---------------------------------------------------------------------------
# US-specific slippage (much tighter than MY)
# ---------------------------------------------------------------------------

def _us_etf_slippage(price: float, qty: int, adv_value: float, side: str) -> float:
    """2 bps base + size penalty for orders consuming significant ADV.

    Leveraged ETFs and mega-caps have penny-tight spreads at retail size,
    so 2-3 bps is realistic for <1% ADV orders. Orders above 5% ADV get
    progressively worse.
    """
    bps_base = 2.0
    notional = price * qty

    if adv_value > 0:
        adv_consumed_pct = (notional / adv_value) * 100.0
        # 1 bp per 1% ADV, capped at 30 bps for the size component
        bps_size = min(adv_consumed_pct, 30.0)
    else:
        bps_size = 10.0  # unknown — assume moderate impact

    # No extra liquidity floor — US universe is all liquid by construction
    bps_total = min(bps_base + bps_size, 35.0)
    slip = price * (bps_total / 10_000.0)
    return slip if side == "BUY" else -slip


# ---------------------------------------------------------------------------
# The profile singleton
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _USProfile:
    # identity
    code: str = "US"
    display_name: str = "United States (NYSE/NASDAQ)"
    flag_emoji: str = "🇺🇸"

    # currency & units
    currency_iso: str = "USD"
    currency_symbol: str = "$"
    lot_size: int = 1
    default_capital: float = 5_000.0   # USD — roughly RM 20k equivalent

    # time & calendar
    timezone: ZoneInfo = ZoneInfo("America/New_York")
    sessions: tuple[TradingSession, ...] = (
        # Regular Trading Hours only. Extended hours intentionally excluded
        # from auto-entry; users can fire manual orders via broker_adapter
        # with fill_outside_rth=True when desired.
        TradingSession("RTH", dtime(9, 30), dtime(16, 0)),
    )
    pre_open_minutes: int = 0
    safe_entry_cutoff: dtime = dtime(15, 30)   # 30 min before close
    is_holiday: callable = staticmethod(_is_us_holiday)

    # universe
    regime_ticker_yf: str = "SPY"
    regime_ticker_moomoo: str = "US.SPY"
    default_watchlist: tuple[TickerSpec, ...] = _US_WATCHLIST

    # fees & slippage
    fee_rate: float = 0.0      # moomoo US is commission-free for stocks/ETFs
    min_fee: float = 0.0
    slippage_fn: callable = staticmethod(_us_etf_slippage)

    # broker / data integration
    moomoo_available: bool = True
    moomoo_market_enum: str = "TrdMarket.US"   # resolved at runtime in broker_adapter
    ticker_yf_template: str = "{symbol}"
    ticker_moomoo_template: str = "US.{symbol}"

    # risk defaults
    min_risk_per_trade: float = 20.0   # USD

    # learner / scheduler tuning
    # US daily ranges on leveraged ETFs are wider; keep concurrent caps
    # tighter to avoid overexposure on correlated moves.
    cycle_interval_sec: int = 3600
    bull_max_positions: int = 6
    neutral_max_positions: int = 4
    bear_max_positions: int = 2


US_PROFILE: MarketProfile = _USProfile()


__all__ = ["US_PROFILE"]
