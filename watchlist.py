# watchlist.py
"""
Curated default watchlist + user-added custom tickers.

v3.6 multi-market change
------------------------
The full ~74-ticker Bursa universe still lives in BURSA_WATCHLIST below as
the single source of truth for MY. The US universe is sourced from
`market_profiles.us_profile.US_PROFILE.default_watchlist`.

All public helpers (`get_all_tickers`, `get_ticker_sector`,
`get_ticker_name`, `is_shariah_compliant`, etc.) DISPATCH on the active
market profile. Callers (screener.py, market_analyzer.py, evaluation.py)
need no changes.

Custom tickers are stored in SQLite (`custom_watchlist` table) per-market
(each market has its own DB file as of v3.6, so custom tickers are
naturally isolated).
"""

import os
import json
from db import connect, myt_iso, DATA_DIR

CUSTOM_WATCHLIST_FILE = os.path.join(DATA_DIR, "custom_watchlist.json")


# ---------------------------------------------------------------------------
# MY universe — full 74-ticker Bursa list (unchanged from v3.3)
# ---------------------------------------------------------------------------

BURSA_WATCHLIST = {
    "Construction": {
        "1651.KL": "Malaysian Resources Corporation Berhad (MRCB)",
        "3336.KL": "IJM Corporation Berhad",
        "5184.KL": "Cypark Resources Berhad",
        "5253.KL": "Econpile Holdings Berhad",
        "5263.KL": "Sunway Construction Group Berhad",
        "5398.KL": "Gamuda Berhad",
        "7161.KL": "Kerjaya Prospek Group Berhad",
        "9679.KL": "WCT Holdings Berhad",
    },
    "Consumer Products": {
        "0157.KL": "Focus Point Holdings Berhad",
        "2836.KL": "Carlsberg Brewery Malaysia Berhad",
        "3182.KL": "Genting Berhad",
        "3255.KL": "Heineken Malaysia Berhad",
        "3689.KL": "Fraser & Neave Holdings Bhd",
        "4065.KL": "PPB Group Berhad",
        "4197.KL": "Sime Darby Berhad",
        "4707.KL": "Nestle (Malaysia) Berhad",
        "4715.KL": "Genting Malaysia Berhad",
        "5296.KL": "Mr D.I.Y. Group (M) Berhad",
        "5326.KL": "99 Speed Mart Retail Holdings Berhad",
        "6599.KL": "AEON Co. (M) Berhad",
        "7052.KL": "Padini Holdings Berhad",
        "7084.KL": "QL Resources Berhad",
    },
    "Energy": {
        "5132.KL": "Deleum Berhad",
        "5141.KL": "Dayang Enterprise Holdings Bhd",
        "5199.KL": "Hibiscus Petroleum Berhad",
        "5210.KL": "Bumi Armada Berhad",
        "5243.KL": "Velesto Energy Berhad",
        "5244.KL": "Wasco Berhad",
        "7108.KL": "Dialog Group Berhad",
        "7277.KL": "Yinson Holdings Berhad",
    },
    "Financial Services": {
        "1015.KL": "AMMB Holdings Berhad (AmBank)",
        "1023.KL": "CIMB Group Holdings Berhad",
        "1066.KL": "RHB Bank Berhad",
        "1155.KL": "Malayan Banking Berhad (Maybank)",
        "1163.KL": "Allianz Malaysia Berhad",
        "1295.KL": "Public Bank Berhad",
        "1818.KL": "Bursa Malaysia Berhad",
        "5230.KL": "Tune Protect Group Berhad",
        "5258.KL": "Bank Islam Malaysia Berhad",
        "5819.KL": "Hong Leong Bank Berhad",
        "6459.KL": "MNRB Holdings Berhad",
    },
    "Healthcare": {
        "0179.KL": "Bioalpha Holdings Berhad",
        "5168.KL": "Hartalega Holdings Berhad",
        "5225.KL": "IHH Healthcare Berhad",
        "5301.KL": "CTOS Digital Berhad",
        "5878.KL": "KPJ Healthcare Berhad",
        "7081.KL": "Pharmaniaga Berhad",
        "7106.KL": "Supermax Corporation Berhad",
        "7113.KL": "Top Glove Corporation Berhad",
        "7153.KL": "Kossan Rubber Industries Berhad",
    },
    "Plantation": {
        "1961.KL": "IOI Corporation Berhad",
        "2089.KL": "United Plantations Berhad",
        "2291.KL": "Genting Plantations Berhad",
        "2445.KL": "Kuala Lumpur Kepong Berhad (KLK)",
        "5138.KL": "Hap Seng Plantations Holdings Berhad",
        "5222.KL": "FGV Holdings Berhad",
        "5245.KL": "Sarawak Oil Palms Berhad",
        "5285.KL": "SD Guthrie Berhad (Sime Darby Plantation)",
        "9059.KL": "TSH Resources Berhad",
    },
    "Property & REITs": {
        "5106.KL": "Axis Real Estate Investment Trust",
        "5148.KL": "UEM Sunrise Berhad",
        "5176.KL": "Sunway Real Estate Investment Trust",
        "5180.KL": "CapitaLand Malaysia Trust",
        "5211.KL": "Sunway Berhad",
        "5212.KL": "Pavilion Real Estate Investment Trust",
        "5227.KL": "IGB Real Estate Investment Trust",
        "5235SS.KL": "KLCC Property Holdings Berhad",
        "5249.KL": "IOI Properties Group Berhad",
        "5269.KL": "Al-Salam Real Estate Investment Trust",
        "5288.KL": "Sime Darby Property Berhad",
        "8206.KL": "Eco World Development Group Berhad",
        "8583.KL": "Mah Sing Group Berhad",
        "8664.KL": "S P Setia Berhad",
    },
    "Technology": {
        "0040.KL": "OpenSys (M) Berhad",
        "0097.KL": "ViTrox Corporation Berhad",
        "0127.KL": "JCY International Berhad",
        "0128.KL": "Frontken Corporation Berhad",
        "0138.KL": "MYEG Services Berhad",
        "0146.KL": "JF Technology Berhad",
        "0166.KL": "Inari Amertron Berhad",
        "0217.KL": "Powerwell Holdings Berhad",
        "0270.KL": "NationGate Holdings Berhad",
        "3867.KL": "Malaysian Pacific Industries Berhad",
        "5005.KL": "Unisem (M) Berhad",
        "5216.KL": "NEXG Berhad",
        "5292.KL": "UWC Berhad",
        "7022.KL": "Globetronics Technology Berhad",
        "7095.KL": "P.I.E. Industrial Berhad",
        "7204.KL": "D&O Green Technologies Berhad",
    },
    "Telecommunications": {
        "0172.KL": "OCK Group Berhad",
        "4863.KL": "Telekom Malaysia Berhad (TM)",
        "6012.KL": "Maxis Berhad",
        "6888.KL": "Axiata Group Berhad",
        "6947.KL": "CelcomDigi Berhad",
    },
    "Utilities": {
        "4677.KL": "YTL Corporation Berhad",
        "5209.KL": "Gas Malaysia Berhad",
        "5347.KL": "Tenaga Nasional Berhad (TNB)",
        "6033.KL": "Petronas Gas Berhad",
        "6742.KL": "YTL Power International Berhad",
    },
    "Industrial Products": {
        "3034.KL": "Hap Seng Consolidated Berhad",
        "5183.KL": "PETRONAS Chemicals Group Berhad",
        "5681.KL": "PETRONAS Dagangan Berhad",
        "7087.KL": "Magni-Tech Industries Berhad",
        "7100.KL": "Uchi Technologies Berhad",
        "8869.KL": "Press Metal Aluminium Holdings Berhad",
    },
    "Transportation & Logistics": {
        "0078.KL": "GDEX Berhad",
        "3816.KL": "MISC Berhad",
        "5099.KL": "Capital A Berhad",
        "5238.KL": "AirAsia X Berhad",
        "5246.KL": "Westports Holdings Berhad",
    },
}


# ---------------------------------------------------------------------------
# Multi-market dispatch
# ---------------------------------------------------------------------------

def _active_code() -> str:
    try:
        from market_profiles import active_market_code
        return active_market_code()
    except Exception:
        return "MY"


def _us_curated_by_sector() -> dict[str, dict[str, str]]:
    """Build a {sector: {yf_symbol: name}} map from us_profile for parity with BURSA_WATCHLIST."""
    out: dict[str, dict[str, str]] = {}
    try:
        from market_profiles.us_profile import US_PROFILE
        for t in US_PROFILE.default_watchlist:
            out.setdefault(t.sector, {})[t.yf_symbol] = t.name
    except Exception:
        pass
    return out


def get_default_watchlist_by_sector() -> dict[str, dict[str, str]]:
    """For UI display — {sector: {ticker: name}} for the active market.

    MY → returns BURSA_WATCHLIST directly.
    US → returns the leveraged-ETF + mega-cap basket from us_profile.
    """
    return BURSA_WATCHLIST if _active_code() == "MY" else _us_curated_by_sector()


# ---------------------------------------------------------------------------
# Custom tickers — SQLite-backed with JSON fallback
# (table is per-market because each market has its own DB file in v3.6)
# ---------------------------------------------------------------------------

def _ensure_custom_table():
    with connect() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS custom_watchlist ("
            " ticker TEXT PRIMARY KEY, name TEXT, sector TEXT, added_at TEXT)"
        )
    # Migrate from JSON file if present (MY-only legacy path)
    if _active_code() == "MY" and os.path.exists(CUSTOM_WATCHLIST_FILE):
        try:
            with open(CUSTOM_WATCHLIST_FILE) as f:
                items = json.load(f)
            for ticker, val in (items or {}).items():
                if isinstance(val, dict):
                    name, sector = val.get("name", ticker), val.get("sector", "Custom")
                else:
                    name, sector = str(val), "Custom"
                with connect() as c:
                    c.execute(
                        "INSERT OR IGNORE INTO custom_watchlist "
                        "(ticker, name, sector, added_at) VALUES (?,?,?,?)",
                        (ticker, name, sector, myt_iso()),
                    )
        except Exception:
            pass


_ensure_custom_table()


def load_custom_watchlist_tickers() -> dict:
    with connect(readonly=True) as c:
        rows = c.execute(
            "SELECT ticker, name, sector FROM custom_watchlist"
        ).fetchall()
    return {r["ticker"]: {"name": r["name"], "sector": r["sector"]} for r in rows}


def _normalise_ticker_for_active_market(ticker: str) -> str:
    """Apply the active market's yf_template if the user typed a bare symbol."""
    t = ticker.strip().upper()
    if _active_code() == "MY":
        if not t.endswith(".KL"):
            t += ".KL"
        return t
    # US: bare symbol (no suffix). Strip a misplaced .KL if any.
    if t.endswith(".KL"):
        t = t[:-3]
    return t


def add_custom_ticker(ticker: str, name: str, sector: str = "Custom") -> str:
    ticker = _normalise_ticker_for_active_market(ticker)
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO custom_watchlist "
            "(ticker, name, sector, added_at) VALUES (?,?,?,?)",
            (ticker, name.strip(), sector.strip(), myt_iso()),
        )
    return ticker


def remove_custom_ticker(ticker: str) -> None:
    with connect() as c:
        c.execute("DELETE FROM custom_watchlist WHERE ticker=?", (ticker,))


def get_all_tickers() -> list[str]:
    tickers = []
    default = get_default_watchlist_by_sector()
    for items in default.values():
        tickers.extend(items.keys())
    tickers.extend(load_custom_watchlist_tickers().keys())
    return sorted(set(tickers))


def get_ticker_sector(ticker: str) -> str:
    default = get_default_watchlist_by_sector()
    for sector, items in default.items():
        if ticker in items:
            return sector
    custom = load_custom_watchlist_tickers().get(ticker)
    return custom["sector"] if custom else "Unknown"


def get_ticker_name(ticker: str) -> str:
    default = get_default_watchlist_by_sector()
    for items in default.values():
        if ticker in items:
            return items[ticker]
    custom = load_custom_watchlist_tickers().get(ticker)
    return custom["name"] if custom else ticker


# ---------------------------------------------------------------------------
# Shariah-compliant filter (MY-only; in US it's a no-op pass-through)
# ---------------------------------------------------------------------------

SHARIAH_NON_COMPLIANT = {
    # Conventional banks
    "1155.KL", "1295.KL", "1023.KL", "1015.KL", "1066.KL", "5819.KL",
    "1818.KL",  # Bursa MY itself (mixed activities)
    # Brewers
    "3255.KL", "2836.KL",
    # Gaming
    "3182.KL", "4715.KL",
    # Conventional insurance (mixed model)
    "1163.KL",
}


def is_shariah_compliant(ticker: str) -> bool:
    """Best-effort check; user can override in Settings.

    For non-MY markets the concept doesn't apply, so we return True
    (treating the filter as a no-op).
    """
    if _active_code() != "MY":
        return True
    return ticker not in SHARIAH_NON_COMPLIANT


def get_all_tickers_shariah_only() -> list[str]:
    return [t for t in get_all_tickers() if is_shariah_compliant(t)]
