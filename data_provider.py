# data_provider.py
"""
Unified market-data provider — multi-market (v3.6).

What this module does
---------------------
1. Drop-in replacement for `yf.Ticker(t).history(...)` patterns used in
   screener.py, market_analyzer.py, evaluation.py, learner.py.
2. Auto-detect Moomoo OpenD on first use. If unavailable, stickily fall
   back to yfinance for the rest of the process — no per-call reconnect storms.
3. Per-call defensive fallback: if Moomoo is "up" but a single call raises
   or times out, serve that call from yfinance. After
   MOOMOO_MAX_CONSECUTIVE_FAILURES consecutive failures, demote Moomoo for
   the rest of the process.
4. **NEW in v3.6:** the ticker→moomoo-code conversion respects the ACTIVE
   market profile. The same code path serves MY (`MY.0166`) and US
   (`US.AAPL`) symbol mapping. If `active_profile().moomoo_available` is
   False (MY today — Bursa not yet on OpenD), we never try moomoo for
   that market's tickers — saves a TCP probe + SDK init.

Output DataFrame shape is identical to yfinance.history() for both
markets. Existing OHLCV validators and indicator code keep working.

Honors PROJECT_HANDBOOK rule #15: every network call has an explicit
`timeout=`. Moomoo SDK has no native timeout kwarg on
`request_history_kline`, so we run it in a helper thread and join with
a deadline.

Env / Streamlit secrets
-----------------------
- `BURSA_DATA_PROVIDER` : `auto` (default) | `moomoo` | `yfinance`
  (Name kept for backwards compatibility — applies to both markets.)
- `MOOMOO_HOST`         : default `127.0.0.1`
- `MOOMOO_PORT`         : default `11111`

Public API
----------
- `get_history(ticker, period=None, start=None, end=None, timeout=15, interval="1d")`
- `provider_name()`  → 'moomoo' | 'yfinance' (last call, or next-call guess)
- `health()`         → dict for the Settings tab
- `reset()`          → forget detection state (mainly for tests)

v3.7 — Intraday support
-----------------------
`get_history` now accepts an `interval` argument:
    - "1d" (default)           → byte-identical to the pre-v3.7 daily path
    - "1m"/"5m"/"15m"/"30m"/"60m"/"1h" → intraday candles

Data-source reality (do not pretend otherwise):
    - Moomoo OpenD serves real intraday for US (local PC only).
    - yfinance intraday is limited: 1m ≤ 7 days back, 5m/15m ≤ 60 days back.
    - MY (Bursa) has NO reliable intraday feed → intraday is US-only today,
      gated by MarketProfile.supports_intraday (flip the day Moomoo adds MY).
"""

from __future__ import annotations

import os
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import pandas as pd

try:
    from logger import get_logger
    log = get_logger("data_provider")
except Exception:  # pragma: no cover — keeps the module importable in tests
    import logging
    log = logging.getLogger("data_provider")

# yfinance is always available (it's our fallback)
import yfinance as yf


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_PROVIDER_ENV = os.getenv("BURSA_DATA_PROVIDER", "auto").strip().lower()
_MOOMOO_HOST = os.getenv("MOOMOO_HOST", "127.0.0.1")
_MOOMOO_PORT = int(os.getenv("MOOMOO_PORT", "11111"))

# After this many consecutive Moomoo failures, demote to yfinance for the
# rest of the process. Each successful call resets the counter.
MOOMOO_MAX_CONSECUTIVE_FAILURES = 5

DEFAULT_TIMEOUT_S = 15


# ---------------------------------------------------------------------------
# Module state (thread-safe singleton)
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_quote_ctx = None                         # cached OpenQuoteContext (or None)
_moomoo_available: Optional[bool] = None  # None = not yet probed
_moomoo_failures = 0
_last_served_by = "yfinance"              # for diagnostics
_init_error: Optional[str] = None
_last_moomoo_error: Optional[str] = None  # per-call failure surface for health()


# ---------------------------------------------------------------------------
# Ticker conversion (multi-market aware)
# ---------------------------------------------------------------------------

def _to_moomoo_code(ticker: str) -> Optional[str]:
    """
    Convert a yfinance-style ticker to a Moomoo `MARKET.CODE` string.

    Dispatch logic:
        '*.KL'  → MY.<base>      (Bursa equity)
        '^KLSE' → MY.800000      (FBMKLCI index; may fail per SDK version)
        bare    → US.<symbol>    (NYSE/NASDAQ)
        '^...'  → US index — we don't map these; caller falls back to yfinance.

    Returns None for symbols we can't safely map; the caller should treat
    None as "moomoo can't serve this; use yfinance".
    """
    if not ticker:
        return None
    t = ticker.strip().upper()

    # Bursa equities
    if t.endswith(".KL"):
        return f"MY.{t[:-3]}"

    # KLCI index
    if t in ("^KLSE", "^FBMKLCI"):
        return "MY.800000"

    # US bare symbol (1-5 letters, optional dot like BRK.B → we keep it simple)
    if t.replace(".", "").isalpha() and len(t.replace(".", "")) <= 6:
        # Bare symbol that looks like a US ticker. SPY, AAPL, NVDA, BRK.B…
        return f"US.{t}"

    # Other indices (^SPX, ^IXIC, ^VIX, etc.) — fall back to yfinance.
    return None


def _moomoo_market_for(ticker: str) -> str:
    """Return the MARKET prefix (MY/US/HK/…) for a yfinance ticker."""
    code = _to_moomoo_code(ticker)
    if code is None:
        return "UNKNOWN"
    return code.split(".", 1)[0]


def _market_supports_moomoo(ticker: str) -> bool:
    """True if the active OR per-ticker market profile claims moomoo OpenD support."""
    mkt = _moomoo_market_for(ticker)
    if mkt == "MY":
        # Static fact today: OpenD doesn't yet support MY.
        # We honour the MY profile's moomoo_available flag so the day
        # Moomoo enables MY, flipping that flag in my_profile.py turns this on.
        try:
            from market_profiles.my_profile import MY_PROFILE
            return bool(MY_PROFILE.moomoo_available)
        except Exception:
            return False
    if mkt == "US":
        try:
            from market_profiles.us_profile import US_PROFILE
            return bool(US_PROFILE.moomoo_available)
        except Exception:
            return True   # US is the marquee supported market
    # Future markets: assume supported. If wrong, the per-call try/except
    # demotes moomoo for this process.
    return True


# ---------------------------------------------------------------------------
# Moomoo connection lifecycle
# ---------------------------------------------------------------------------

def _is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """
    Raw TCP probe: is anything listening on host:port?

    We do this *before* instantiating OpenQuoteContext because the moomoo
    SDK spawns a background reconnect thread in its constructor that will
    spam ECONNREFUSED forever on environments without OpenD (Streamlit Cloud,
    VPS without Moomoo Desktop, etc.) — even if we close the context.
    """
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _try_init_moomoo() -> bool:
    """
    Probe whether Moomoo OpenD is reachable. Sets module state.
    Called at most once per process (unless `reset()` is called).
    """
    global _quote_ctx, _moomoo_available, _init_error

    if _PROVIDER_ENV == "yfinance":
        _moomoo_available = False
        _init_error = "BURSA_DATA_PROVIDER=yfinance (forced)"
        log.info("data_provider: forced yfinance via env")
        return False

    try:
        from moomoo import OpenQuoteContext, RET_OK  # noqa: F401
    except ImportError as e:
        _moomoo_available = False
        _init_error = f"moomoo-api not installed: {e}"
        log.info("data_provider: moomoo-api not installed, using yfinance")
        return False

    if not _is_port_open(_MOOMOO_HOST, _MOOMOO_PORT, timeout=1.0):
        _moomoo_available = False
        _init_error = f"OpenD port {_MOOMOO_HOST}:{_MOOMOO_PORT} not open (no listener)"
        log.info("data_provider: %s — using yfinance", _init_error)
        return False

    try:
        ctx = OpenQuoteContext(host=_MOOMOO_HOST, port=_MOOMOO_PORT)
        ret, _data = ctx.get_global_state()
        if ret != 0:  # RET_OK == 0
            try:
                ctx.close()
            except Exception:
                pass
            _moomoo_available = False
            _init_error = f"OpenD reachable but get_global_state ret={ret}"
            log.warning("data_provider: %s — falling back to yfinance", _init_error)
            return False

        _quote_ctx = ctx
        _moomoo_available = True
        _init_error = None
        log.info("data_provider: Moomoo OpenD connected at %s:%s",
                 _MOOMOO_HOST, _MOOMOO_PORT)
        return True

    except Exception as e:
        _moomoo_available = False
        _init_error = f"OpenD connect failed: {e}"
        log.info("data_provider: %s — using yfinance", _init_error)
        return False


def _ensure_provider_decided() -> None:
    """Probe Moomoo at most once. Thread-safe."""
    global _moomoo_available
    if _moomoo_available is not None:
        return
    with _state_lock:
        if _moomoo_available is None:
            _try_init_moomoo()


def _demote_moomoo(reason: str) -> None:
    """Permanently switch to yfinance for the rest of the process."""
    global _moomoo_available, _quote_ctx, _init_error
    with _state_lock:
        if _moomoo_available is False:
            return
        _moomoo_available = False
        _init_error = f"demoted: {reason}"
        log.warning("data_provider: demoting Moomoo → yfinance (%s)", reason)
        if _quote_ctx is not None:
            try:
                _quote_ctx.close()
            except Exception:
                pass
            _quote_ctx = None


# ---------------------------------------------------------------------------
# Date-range resolution
# ---------------------------------------------------------------------------

_PERIOD_TO_DAYS = {
    "1d": 1, "5d": 5, "1mo": 31, "3mo": 93, "6mo": 186,
    "1y": 366, "2y": 732, "3y": 1098, "5y": 1830, "10y": 3660,
    "ytd": None, "max": 3660,
}

# ---------------------------------------------------------------------------
# Intraday support (v3.7) — interval handling
# ---------------------------------------------------------------------------
# The whole pre-v3.7 stack is daily-bar only. Intraday (ORB on 5m) needs
# sub-daily candles. We thread an `interval` arg through get_history():
#   - interval="1d" (default)  → byte-identical to the old daily behaviour
#   - interval="5m"/"15m"/"1m" → intraday candles

# yfinance interval strings we support → max look-back days yfinance allows.
_INTRADAY_INTERVALS = {
    "1m": 7,      # yfinance hard cap: ~7 days of 1-minute data
    "2m": 60,
    "5m": 60,     # ~60 days of 5-minute data (enough for a walk-forward)
    "15m": 60,
    "30m": 60,
    "60m": 730,
    "1h": 730,
}


def _is_intraday(interval: Optional[str]) -> bool:
    return bool(interval) and interval.strip().lower() != "1d"


def _moomoo_ktype_for_interval(interval: Optional[str]):
    """Map a yfinance-style interval string to a moomoo KLType enum.

    Returns None if the interval isn't representable in moomoo (caller
    should fall back to yfinance).
    """
    try:
        from moomoo import KLType
    except Exception:
        return None
    iv = (interval or "1d").strip().lower()
    table = {
        "1d":  getattr(KLType, "K_DAY", None),
        "1m":  getattr(KLType, "K_1M",  None),
        "3m":  getattr(KLType, "K_3M",  None),
        "5m":  getattr(KLType, "K_5M",  None),
        "15m": getattr(KLType, "K_15M", None),
        "30m": getattr(KLType, "K_30M", None),
        "60m": getattr(KLType, "K_60M", None),
        "1h":  getattr(KLType, "K_60M", None),
    }
    return table.get(iv)


def _default_intraday_lookback_days(interval: str) -> int:
    """How many calendar days back to request for an intraday interval by
    default (when no explicit period/start/end is given). Capped to what
    yfinance allows so the same default works for both providers."""
    return _INTRADAY_INTERVALS.get((interval or "").strip().lower(), 60)


def _resolve_window(period: Optional[str],
                    start: Optional[str],
                    end: Optional[str],
                    interval: Optional[str] = None) -> Tuple[str, str]:
    """Return (start_str, end_str) as YYYY-MM-DD for Moomoo.

    `interval` only affects the *default* window when no period/start/end is
    given: intraday intervals default to a recent-days look-back (capped to
    what's actually available) instead of 1 year of (nonexistent) intraday.
    """
    if start is not None or end is not None:
        end_d = pd.to_datetime(end).date() if end is not None else datetime.now(timezone.utc).date()
        if start is not None:
            start_d = pd.to_datetime(start).date()
        else:
            default_back = (_default_intraday_lookback_days(interval)
                            if _is_intraday(interval) else 365)
            start_d = end_d - timedelta(days=default_back)
        return str(start_d), str(end_d)

    if _is_intraday(interval):
        end_d = datetime.now(timezone.utc).date()
        start_d = end_d - timedelta(days=_default_intraday_lookback_days(interval))
        return str(start_d), str(end_d)

    p = (period or "1y").lower()
    if p == "ytd":
        end_d = datetime.now(timezone.utc).date()
        start_d = datetime(end_d.year, 1, 1).date()
        return str(start_d), str(end_d)

    days = _PERIOD_TO_DAYS.get(p, 366)
    end_d = datetime.now(timezone.utc).date()
    start_d = end_d - timedelta(days=days)
    return str(start_d), str(end_d)


# ---------------------------------------------------------------------------
# Moomoo fetch (with thread-based timeout)
# ---------------------------------------------------------------------------

def _moomoo_kline_normalise(df: pd.DataFrame, market: str) -> pd.DataFrame:
    """Reshape Moomoo's kline DataFrame to match yfinance.history() output.

    `market` ('MY'/'US'/...) picks the timezone we localise to so the
    DatetimeIndex semantics match what yfinance would have returned.
    """
    if df is None or df.empty:
        return pd.DataFrame()

    rename = {
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    }
    out = df.rename(columns=rename).copy()

    if "time_key" not in df.columns and "time_key" not in out.columns:
        return pd.DataFrame()

    ts = pd.to_datetime(out["time_key"])

    tz_for_market = {
        "MY": "Asia/Kuala_Lumpur",
        "US": "America/New_York",
        "HK": "Asia/Hong_Kong",
        "SG": "Asia/Singapore",
    }.get(market, "Asia/Kuala_Lumpur")

    try:
        ts = ts.dt.tz_localize(tz_for_market, nonexistent="shift_forward",
                               ambiguous="NaT")
    except TypeError:
        # Already tz-aware
        pass
    out.index = pd.DatetimeIndex(ts, name="Date")

    keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in out.columns]
    out = out[keep]

    out = out.dropna(how="all", subset=[c for c in ["Open", "Close"] if c in out.columns])
    return out


def _fetch_moomoo(ticker: str,
                  period: Optional[str],
                  start: Optional[str],
                  end: Optional[str],
                  timeout: float,
                  interval: Optional[str] = None) -> Optional[pd.DataFrame]:
    """
    Returns a yfinance-shaped DataFrame, or None if Moomoo can't / won't
    serve this request (caller should fall back to yfinance).
    """
    from moomoo import AuType, RET_OK

    code = _to_moomoo_code(ticker)
    if code is None:
        return None
    market = code.split(".", 1)[0]

    if not _market_supports_moomoo(ticker):
        # MY today: OpenD doesn't serve it; skip silently rather than spam.
        return None

    ktype = _moomoo_ktype_for_interval(interval)
    if ktype is None:
        # This moomoo SDK build can't represent the requested interval —
        # let the caller fall back to yfinance.
        return None

    s_str, e_str = _resolve_window(period, start, end, interval)

    result: dict = {"df": None, "err": None}

    def _worker():
        try:
            all_rows = []
            page_key = None
            ctx = _quote_ctx
            if ctx is None:
                result["err"] = "no quote_ctx"
                return
            while True:
                ret, data, page_key = ctx.request_history_kline(
                    code,
                    start=s_str,
                    end=e_str,
                    ktype=ktype,
                    autype=AuType.QFQ,
                    max_count=1000,
                    page_req_key=page_key,
                )
                if ret != RET_OK:
                    result["err"] = f"ret={ret} data={data}"
                    return
                if data is not None and not data.empty:
                    all_rows.append(data)
                if page_key is None:
                    break
            if not all_rows:
                result["df"] = pd.DataFrame()
            else:
                result["df"] = pd.concat(all_rows, ignore_index=True)
        except Exception as e:
            result["err"] = repr(e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=max(1.0, float(timeout)))

    if t.is_alive():
        return _moomoo_failure_signal(f"timeout after {timeout}s")

    if result["err"]:
        return _moomoo_failure_signal(result["err"])

    df = _moomoo_kline_normalise(result["df"], market=market)
    _moomoo_success_signal()
    return df


def _moomoo_failure_signal(reason: str) -> None:
    global _moomoo_failures, _last_moomoo_error
    with _state_lock:
        _moomoo_failures += 1
        _last_moomoo_error = reason
        failures = _moomoo_failures
    log.warning("data_provider: Moomoo call failed (%s) [%d/%d]",
                reason, failures, MOOMOO_MAX_CONSECUTIVE_FAILURES)
    if failures >= MOOMOO_MAX_CONSECUTIVE_FAILURES:
        _demote_moomoo(f"{failures} consecutive failures (last: {reason})")
    return None


def _moomoo_success_signal() -> None:
    global _moomoo_failures, _last_moomoo_error
    if _moomoo_failures != 0 or _last_moomoo_error is not None:
        with _state_lock:
            _moomoo_failures = 0
            _last_moomoo_error = None


# ---------------------------------------------------------------------------
# yfinance fetch (fallback / forced)
# ---------------------------------------------------------------------------

def _fetch_yfinance(ticker: str,
                    period: Optional[str],
                    start: Optional[str],
                    end: Optional[str],
                    timeout: float,
                    interval: Optional[str] = None) -> pd.DataFrame:
    iv = (interval or "1d").strip().lower()
    try:
        if _is_intraday(iv):
            # yfinance intraday: must pass interval=, and respect the
            # provider's look-back cap. If the caller gave explicit
            # start/end (backtest), honour them; else use a recent window.
            if start is not None or end is not None:
                df = yf.Ticker(ticker).history(
                    start=start, end=end, interval=iv, timeout=timeout)
            else:
                cap = _INTRADAY_INTERVALS.get(iv, 60)
                # yfinance accepts e.g. period="60d" with interval="5m"
                df = yf.Ticker(ticker).history(
                    period=f"{cap}d", interval=iv, timeout=timeout)
        elif period is not None and start is None and end is None:
            df = yf.Ticker(ticker).history(period=period, timeout=timeout)
        else:
            df = yf.Ticker(ticker).history(start=start, end=end, timeout=timeout)
    except Exception as e:
        log.warning("data_provider: yfinance fetch failed for %s: %s", ticker, e)
        return pd.DataFrame()
    if df is None:
        return pd.DataFrame()
    return df


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_history(ticker: str,
                period: Optional[str] = None,
                start=None,
                end=None,
                timeout: float = DEFAULT_TIMEOUT_S,
                interval: str = "1d") -> pd.DataFrame:
    """
    Drop-in replacement for `yf.Ticker(ticker).history(...)`.

    Tries Moomoo first if available AND the ticker's market is on the
    moomoo supported list. Otherwise (or on any failure) falls back to
    yfinance for that single call. After repeated failures Moomoo is
    demoted for the rest of the process.

    `interval` (v3.7): "1d" (default, daily — unchanged behaviour) or an
    intraday string ("1m"/"5m"/"15m"/"30m"/"60m"/"1h"). Intraday is only
    meaningful for markets with a real intraday feed (US via Moomoo OpenD;
    yfinance intraday is limited and gappy). Daily callers are 100%
    unaffected — the default keeps the exact pre-v3.7 code path.
    """
    global _last_served_by

    _ensure_provider_decided()

    if _moomoo_available and _market_supports_moomoo(ticker):
        df = _fetch_moomoo(ticker, period, start, end, timeout, interval=interval)
        if df is not None and not df.empty:
            _last_served_by = "moomoo"
            return df
        # Fall through to yfinance for this single call.

    df = _fetch_yfinance(ticker, period, start, end, timeout, interval=interval)
    _last_served_by = "yfinance"
    return df


def provider_name() -> str:
    """Provider that served the last call, or our guess for the next call."""
    if _moomoo_available is None:
        return "yfinance" if _PROVIDER_ENV == "yfinance" else "auto"
    return _last_served_by


def ensure_probed() -> None:
    """Public alias for the internal probe — safe to call from the Settings tab."""
    _ensure_provider_decided()


def health() -> dict:
    """Diagnostic snapshot for the Settings tab."""
    # Active market context, so the Settings panel can show e.g.
    # "US market active; Moomoo available; serving from moomoo".
    try:
        from market_profiles import active_market_code
        market = active_market_code()
    except Exception:
        market = "MY"
    return {
        "active_market": market,
        "provider_env": _PROVIDER_ENV,
        "moomoo_available": bool(_moomoo_available),
        "moomoo_host": _MOOMOO_HOST,
        "moomoo_port": _MOOMOO_PORT,
        "moomoo_consecutive_failures": _moomoo_failures,
        "last_served_by": _last_served_by,
        "init_error": _init_error,
        "last_moomoo_error": _last_moomoo_error,
        # v3.6: explicit per-market support flag so the UI can render
        # "📡 OpenD not yet available for MY market" without re-probing.
        "moomoo_supports_active_market": (
            _market_supports_moomoo("AAPL") if market == "US"
            else _market_supports_moomoo("1155.KL")
        ),
    }


def reset() -> None:
    """Forget detection state. Mainly for tests; also useful if OpenD was started after boot."""
    global _quote_ctx, _moomoo_available, _moomoo_failures, _init_error
    global _last_served_by, _last_moomoo_error
    with _state_lock:
        if _quote_ctx is not None:
            try:
                _quote_ctx.close()
            except Exception:
                pass
        _quote_ctx = None
        _moomoo_available = None
        _moomoo_failures = 0
        _init_error = None
        _last_served_by = "yfinance"
        _last_moomoo_error = None
