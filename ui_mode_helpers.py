"""UI helpers for trading-mode aware rendering (v3.7 Block 6).

Pure functions only. These keep `app.py` readable and are easy to unit-test.
"""

from __future__ import annotations

from typing import Iterable


def available_trading_modes_for_profile(supports_intraday: bool) -> list[str]:
    """Return the mode options the UI should expose for a market profile."""
    return ["SWING", "INTRADAY"] if supports_intraday else ["SWING"]


def trading_mode_label(mode: str) -> str:
    """Human-friendly label for the sidebar mode switcher."""
    m = (mode or "SWING").upper().strip()
    if m == "INTRADAY":
        return "⚡ INTRADAY — 5m ORB"
    return "📈 SWING — hourly scanner"


def effective_scheduler_interval_sec(
    *,
    supports_intraday: bool,
    trading_mode: str,
    swing_cycle_sec: int = 3600,
    intraday_cycle_sec: int = 300,
) -> int:
    """Scheduler cadence for the currently-selected mode/profile."""
    m = (trading_mode or "SWING").upper().strip()
    if m == "INTRADAY" and supports_intraday:
        return int(intraday_cycle_sec)
    return int(swing_cycle_sec)


def intraday_unavailable_message(
    *,
    market_code: str,
    supports_intraday: bool,
    moomoo_available: bool,
) -> str | None:
    """Return a user-facing intraday warning banner, or None.

    Rules:
      * MY / unsupported markets: explain the market-level gate.
      * US + no OpenD: explain the local-only requirement.
      * Otherwise: None.
    """
    market = (market_code or "").upper().strip()
    if not supports_intraday:
        if market == "MY":
            return (
                "Intraday mode is not available for MY yet. Bursa will stay in "
                "SWING mode until Moomoo OpenAPI adds Bursa coverage."
            )
        return "Intraday mode is not available for the active market profile."
    if not moomoo_available:
        return (
            "Intraday unavailable, data source insufficient. Moomoo OpenD must "
            "be connected locally for US intraday trading."
        )
    return None


def mode_specific_scanner_columns(trading_mode: str) -> list[str]:
    """Columns for the Scanner dataframe per mode.

    Intraday keeps the same core keys but drops swing-only clutter so the UI is
    easier to understand.
    """
    m = (trading_mode or "SWING").upper().strip()
    if m == "INTRADAY":
        return [
            "ticker", "name", "sector", "signal", "confidence",
            "price", "volume", "vol_ratio",
            "entry", "stop_loss", "tp1", "tp2", "tp3", "risk_pct",
        ]
    return [
        "ticker", "name", "sector", "signal", "confidence",
        "price", "change_pct", "vol_ratio", "rsi",
        "entry", "stop_loss", "tp1", "tp2", "tp3",
        "risk_pct", "rs_signal",
    ]


def intraday_settings_rows(watchlist: Iterable[str]) -> list[tuple[str, str]]:
    """Read-only settings rows shown when INTRADAY mode is active."""
    return [
        ("Universe", ", ".join(watchlist)),
        ("Opening range", "15 min"),
        ("Target", "2.0R"),
        ("Rel-volume", "1.2×"),
        ("Trend filter", "EMA-200 daily"),
        ("VWAP support", "Required"),
        ("Direction", "Longs only"),
        ("Force-flat", "15:55 ET"),
        ("Explorer target", "100 trades"),
    ]
