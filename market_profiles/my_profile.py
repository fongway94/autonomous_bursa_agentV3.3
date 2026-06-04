"""
market_profiles.my_profile — Bursa Malaysia profile (KLSE).

This profile encodes EXACTLY the constants used by v3.3 of the agent so
that switching to the profile system is a behavioural no-op for MY.

Sources of truth (cross-referenced with PROJECT_HANDBOOK.md §4 & §6):
    - Sessions: 09:00-12:30 morning, 14:30-17:00 afternoon (lunch break)
    - Lot size: 100 shares (Bursa board lot)
    - Fees: 0.15% per side
    - Safe-entry cutoff: 16:00 MYT (≥1h to develop)
    - Currency: MYR (RM)
    - Regime ticker: ^KLSE (yfinance)
    - Moomoo: NOT yet available for MY market (broker_adapter stays stubbed)

NB: The exhaustive 2024-2027 public holiday set already lives in
market_calendar.MY_PUBLIC_HOLIDAYS. We delegate to it here to keep a single
source of truth; this module deliberately does NOT re-list every date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from market_profiles.base import (
    MarketProfile,
    TickerSpec,
    TradingSession,
)


# -------------------------------------------------------------------------
# Watchlist — mirrors watchlist.BURSA_TICKERS in v3.3.
# Kept compact here (~30 representative names). The full ~74-ticker list
# continues to live in watchlist.py for backward compatibility; that module
# will be migrated to delegate to active_profile().default_watchlist in
# a later block.
# -------------------------------------------------------------------------

_MY_WATCHLIST: tuple[TickerSpec, ...] = tuple(
    TickerSpec(
        symbol=s,
        name=n,
        sector=sec,
        yf_symbol=f"{s}.KL",
        moomoo_symbol=f"MY.{s}",          # placeholder — OpenD has no MY support yet
        shariah_compliant=shariah,
    )
    for s, n, sec, shariah in [
        # --- Banking & Financial Services ---
        ("1155", "Malayan Banking (Maybank)", "Banking", False),
        ("1066", "RHB Bank",                  "Banking", False),
        ("5347", "CIMB Group",                "Banking", False),
        ("1015", "AMMB Holdings",             "Banking", False),
        ("1295", "Public Bank",               "Banking", False),
        ("1023", "CIMB Group Holdings",       "Banking", False),
        # --- Telecommunications ---
        ("6947", "DiGi.Com",                  "Telco",   True),
        ("4863", "Telekom Malaysia",          "Telco",   True),
        ("6012", "Maxis",                     "Telco",   True),
        ("6888", "Axiata Group",              "Telco",   True),
        # --- Plantation ---
        ("4065", "PPB Group",                 "Plantation", True),
        ("2445", "Kuala Lumpur Kepong",       "Plantation", True),
        ("1961", "IOI Corp",                  "Plantation", True),
        ("5285", "Sime Darby Plantation",     "Plantation", True),
        # --- Consumer & Retail ---
        ("3034", "Hap Seng Consolidated",     "Consumer", False),
        ("4707", "Nestle (Malaysia)",         "Consumer", True),
        ("4197", "Sime Darby",                "Consumer", True),
        ("5183", "Petronas Chemicals",        "Chemicals", True),
        # --- Utilities ---
        ("5347", "Tenaga Nasional",           "Utilities", True),
        ("4677", "YTL Corp",                  "Utilities", False),
        ("6033", "Petronas Gas",              "Utilities", True),
        # --- Technology ---
        ("0166", "Inari Amertron",            "Technology", True),
        ("5005", "Globetronics Tech",         "Technology", True),
        ("7022", "ViTrox Corp",               "Technology", True),
        # --- Healthcare ---
        ("5168", "Hartalega Holdings",        "Healthcare", True),
        ("7113", "Top Glove",                 "Healthcare", True),
        ("5878", "KPJ Healthcare",            "Healthcare", True),
        ("5225", "IHH Healthcare",            "Healthcare", True),
        # --- Property & Construction ---
        ("5347", "Gamuda",                    "Construction", True),
        ("5202", "Sunway",                    "Construction", True),
    ]
)


# -------------------------------------------------------------------------
# Bursa-specific calendar helpers
# -------------------------------------------------------------------------

def _is_my_holiday(local_dt: datetime) -> bool:
    """Delegate to market_calendar.MY_PUBLIC_HOLIDAYS (single source of truth).

    During v3.3, market_calendar may not yet import this profile. To avoid
    circular imports, we lazy-import here.
    """
    try:
        import market_calendar  # type: ignore
        date_str = local_dt.date().isoformat()
        return date_str in market_calendar.MY_PUBLIC_HOLIDAYS
    except Exception:
        # in tests without market_calendar wired in, default to "no holiday"
        return False


# -------------------------------------------------------------------------
# Bursa-specific slippage (mirrors trading_engine.py volume-aware model)
# -------------------------------------------------------------------------

def _bursa_slippage(price: float, qty: int, adv_value: float, side: str) -> float:
    """5 bps base + size-linear + liquidity penalty, capped 80 bps.

    Mirrors the v3.3 trading_engine slippage model. Kept here so that the
    profile is self-contained; trading_engine.py will be migrated to call
    `active_profile().slippage_fn(...)` in Block 3.
    """
    bps_base = 5.0
    notional = price * qty

    # size penalty: 1 bp per 1% of ADV consumed (capped)
    if adv_value > 0:
        adv_consumed_pct = (notional / adv_value) * 100.0
        bps_size = min(adv_consumed_pct, 50.0)
    else:
        bps_size = 25.0  # unknown liquidity → assume thin

    # liquidity floor for very thin names
    bps_liquidity = 10.0 if adv_value < 50_000 else 0.0

    bps_total = min(bps_base + bps_size + bps_liquidity, 80.0)
    slip = price * (bps_total / 10_000.0)
    return slip if side == "BUY" else -slip


# -------------------------------------------------------------------------
# The profile singleton
# -------------------------------------------------------------------------

@dataclass(frozen=True)
class _MYProfile:
    # identity
    code: str = "MY"
    display_name: str = "Bursa Malaysia"
    flag_emoji: str = "🇲🇾"

    # currency & units
    currency_iso: str = "MYR"
    currency_symbol: str = "RM"
    lot_size: int = 100
    default_capital: float = 20_000.0

    # time & calendar
    timezone: ZoneInfo = ZoneInfo("Asia/Kuala_Lumpur")
    sessions: tuple[TradingSession, ...] = (
        TradingSession("MORNING",   dtime(9,  0), dtime(12, 30)),
        TradingSession("AFTERNOON", dtime(14, 30), dtime(17,  0)),
    )
    pre_open_minutes: int = 30
    safe_entry_cutoff: dtime = dtime(16, 0)
    is_holiday: callable = staticmethod(_is_my_holiday)

    # universe
    regime_ticker_yf: str = "^KLSE"
    regime_ticker_moomoo: str = "MY.000001"   # placeholder
    default_watchlist: tuple[TickerSpec, ...] = _MY_WATCHLIST

    # fees & slippage
    fee_rate: float = 0.0015
    min_fee: float = 8.00            # typical Bursa minimum
    slippage_fn: callable = staticmethod(_bursa_slippage)

    # broker / data integration
    moomoo_available: bool = False    # OpenD does not yet support MY
    moomoo_market_enum: str = ""      # n/a
    ticker_yf_template: str = "{symbol}.KL"
    ticker_moomoo_template: str = "MY.{symbol}"

    # risk defaults
    min_risk_per_trade: float = 50.0  # RM

    # --- FIX 3: Exit sizing ---
    # Bursa stocks move more slowly than US ETFs. A 25% stretch above the
    # 50-day EMA captures genuine climax runs without exiting positions
    # that are simply in a healthy long-term uptrend.
    climax_stretch_pct: float = 25.0

    # learner / scheduler tuning
    cycle_interval_sec: int = 3600
    bull_max_positions: int = 8
    neutral_max_positions: int = 5
    bear_max_positions: int = 3

    # v3.7 intraday (MY not yet supported by Moomoo OpenAPI)
    supports_intraday: bool = False
    intraday_interval: str = "5m"
    intraday_flat_by: dtime = dtime(16, 0)    # not used; MY has no intraday today
    intraday_cycle_sec: int = 300
    intraday_target_r_multiple: float = 2.0
    intraday_require_trend: bool = True
    intraday_ema_length: int = 200
    intraday_rel_vol_threshold: float = 1.2


MY_PROFILE: MarketProfile = _MYProfile()


__all__ = ["MY_PROFILE"]
