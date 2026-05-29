# data_provider.py
"""
Unified market-data provider with Moomoo OpenD → yfinance auto-fallback.

Goals
-----
1. Drop-in replacement for `yf.Ticker(t).history(period=..., timeout=...)` and
   `yf.Ticker(t).history(start=..., end=..., timeout=...)` patterns used in
   screener.py, market_analyzer.py, evaluation.py, learner.py.
2. Auto-detect Moomoo OpenD on first use. If unavailable, *stickily* fall back
   to yfinance for the rest of the process — no per-call reconnect storms.
3. Per-call defensive fallback: if Moomoo is "up" but a single call raises or
   times out, serve that call from yfinance. After
   `MOOMOO_MAX_CONSECUTIVE_FAILURES` consecutive failures, demote Moomoo for
   the rest of the process.
4. Ticker conversion is internal: callers always pass yfinance-style tickers
   (e.g. `0166.KL`, `^KLSE`). The Moomoo path converts them to `MY.0166` /
   `MY.800000` style on the way out.
5. Output DataFrame is identical in shape to what yfinance returns:
   columns = ['Open','High','Low','Close','Volume', ...], tz-aware
   DatetimeIndex named 'Date'. Existing OHLCV validators and indicator code
   keep working unmodified.
6. Honors PROJECT_HANDBOOK rule #15: every network call has an explicit
   `timeout=`. Moomoo SDK has no native timeout kwarg on `request_history_kline`,
   so we run it in a helper thread and join with a deadline.

Env / Streamlit secrets
-----------------------
- `BURSA_DATA_PROVIDER` : `auto` (default) | `moomoo` | `yfinance`
- `MOOMOO_HOST`         : default `127.0.0.1`
- `MOOMOO_PORT`         : default `11111`

Public API
----------
- `get_history(ticker, period=None, start=None, end=None, timeout=15)`
- `provider_name()`  → 'moomoo' | 'yfinance' (whichever served the last call,
                       or whichever we'd serve next if no calls yet)
- `health()`         → dict for the Settings tab
- `reset()`          → forget detection state (mainly for tests)
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

# How long to allow a Moomoo SDK call to run before we abandon it.
# Caller can override per-call via the `timeout=` kwarg.
DEFAULT_TIMEOUT_S = 15


# ---------------------------------------------------------------------------
# Module state (thread-safe singleton)
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_quote_ctx = None              # cached OpenQuoteContext (or None)
_moomoo_available: Optional[bool] = None   # None = not yet probed
_moomoo_failures = 0
_last_served_by = "yfinance"   # for diagnostics
_init_error: Optional[str] = None


# ---------------------------------------------------------------------------
# Ticker conversion
# ---------------------------------------------------------------------------

def _to_moomoo_code(ticker: str) -> Optional[str]:
    """
    Convert a yfinance-style ticker to a Moomoo `MARKET.CODE` string.

    Returns None if we can't safely map it (e.g. an unknown index symbol).
    The caller should treat None as "Moomoo can't serve this; use yfinance".
    """
    if not ticker:
        return None
    t = ticker.strip().upper()

    # Bursa equities:  '0166.KL'  →  'MY.0166'
    if t.endswith(".KL"):
        return f"MY.{t[:-3]}"

    # KLCI index. yfinance uses '^KLSE'. Moomoo uses 'MY.800000' for FBMKLCI.
    # We don't fully trust this code across SDK versions, so let the caller's
    # try/except path catch it and fall back to yfinance.
    if t in ("^KLSE", "^FBMKLCI"):
        return "MY.800000"

    # Anything else (US, HK, indices we don't know) → don't try Moomoo.
    return None


# ---------------------------------------------------------------------------
# Moomoo connection lifecycle
# ---------------------------------------------------------------------------

def _try_init_moomoo() -> bool:
    """
    Probe whether Moomoo OpenD is reachable. Sets module state.

    This is called at most once per process (unless `reset()` is called).
    Returns True if Moomoo is usable, False otherwise.
    """
    global _quote_ctx, _moomoo_available, _init_error

    if _PROVIDER_ENV == "yfinance":
        _moomoo_available = False
        _init_error = "BURSA_DATA_PROVIDER=yfinance (forced)"
        log.info("data_provider: forced yfinance via env")
        return False

    try:
        # Local import so missing moomoo-api doesn't break yfinance-only deploys.
        from moomoo import OpenQuoteContext, RET_OK  # noqa: F401
    except ImportError as e:
        _moomoo_available = False
        _init_error = f"moomoo-api not installed: {e}"
        log.info("data_provider: moomoo-api not installed, using yfinance")
        return False

    try:
        ctx = OpenQuoteContext(host=_MOOMOO_HOST, port=_MOOMOO_PORT)
        # Cheap liveness check: get_global_state is documented & fast.
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


def _resolve_window(period: Optional[str],
                    start: Optional[str],
                    end: Optional[str]) -> Tuple[str, str]:
    """Return (start_str, end_str) as YYYY-MM-DD for Moomoo."""
    if start is not None or end is not None:
        # Normalise to strings.
        end_d = pd.to_datetime(end).date() if end is not None else datetime.now(timezone.utc).date()
        if start is not None:
            start_d = pd.to_datetime(start).date()
        else:
            start_d = end_d - timedelta(days=365)
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

def _moomoo_kline_normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Reshape Moomoo's kline DataFrame to match yfinance.history() output."""
    if df is None or df.empty:
        return pd.DataFrame()

    # Moomoo columns of interest: time_key, open, close, high, low, volume,
    # turnover (optional), pe_ratio (optional), turnover_rate (optional).
    rename = {
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    }
    out = df.rename(columns=rename).copy()

    if "time_key" not in df.columns and "time_key" not in out.columns:
        # Some SDK versions name it differently; bail to yfinance.
        return pd.DataFrame()

    ts = pd.to_datetime(out["time_key"])
    # yfinance returns tz-aware (Asia/Kuala_Lumpur-naive but tz-aware UTC-ish).
    # Localise to Asia/Kuala_Lumpur to mirror Bursa session timestamps.
    try:
        ts = ts.dt.tz_localize("Asia/Kuala_Lumpur", nonexistent="shift_forward",
                               ambiguous="NaT")
    except TypeError:
        # Already tz-aware
        pass
    out.index = pd.DatetimeIndex(ts, name="Date")

    keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in out.columns]
    out = out[keep]

    # Drop rows with all-NaN OHLC (mirrors yfinance behaviour)
    out = out.dropna(how="all", subset=[c for c in ["Open", "Close"] if c in out.columns])
    return out


def _fetch_moomoo(ticker: str,
                  period: Optional[str],
                  start: Optional[str],
                  end: Optional[str],
                  timeout: float) -> Optional[pd.DataFrame]:
    """
    Returns a yfinance-shaped DataFrame, or None if Moomoo can't / won't
    serve this request (caller should fall back to yfinance).
    """
    from moomoo import KLType, AuType, RET_OK

    code = _to_moomoo_code(ticker)
    if code is None:
        return None  # not a Moomoo-mappable symbol

    s_str, e_str = _resolve_window(period, start, end)

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
                    ktype=KLType.K_DAY,
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
        # Moomoo SDK hung. Don't try to cancel — just abandon the thread
        # and treat as failure. Watchdog (rule #15) is our safety net.
        return _moomoo_failure_signal(f"timeout after {timeout}s")

    if result["err"]:
        return _moomoo_failure_signal(result["err"])

    df = _moomoo_kline_normalise(result["df"])
    _moomoo_success_signal()
    return df


def _moomoo_failure_signal(reason: str) -> None:
    global _moomoo_failures
    with _state_lock:
        _moomoo_failures += 1
        failures = _moomoo_failures
    log.warning("data_provider: Moomoo call failed (%s) [%d/%d]",
                reason, failures, MOOMOO_MAX_CONSECUTIVE_FAILURES)
    if failures >= MOOMOO_MAX_CONSECUTIVE_FAILURES:
        _demote_moomoo(f"{failures} consecutive failures (last: {reason})")
    return None


def _moomoo_success_signal() -> None:
    global _moomoo_failures
    if _moomoo_failures != 0:
        with _state_lock:
            _moomoo_failures = 0


# ---------------------------------------------------------------------------
# yfinance fetch (fallback / forced)
# ---------------------------------------------------------------------------

def _fetch_yfinance(ticker: str,
                    period: Optional[str],
                    start: Optional[str],
                    end: Optional[str],
                    timeout: float) -> pd.DataFrame:
    try:
        if period is not None and start is None and end is None:
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
                timeout: float = DEFAULT_TIMEOUT_S) -> pd.DataFrame:
    """
    Drop-in replacement for `yf.Ticker(ticker).history(...)`.

    Tries Moomoo first if available, falls back to yfinance on any failure
    for that single call. After repeated failures Moomoo is demoted for the
    rest of the process.

    Parameters mirror yfinance:
      - period: '1d','5d','1mo','3mo','6mo','1y','2y','3y','5y','10y','ytd','max'
      - start / end: anything pd.to_datetime() accepts
      - timeout: seconds; passed to yfinance and enforced via thread-join on Moomoo.

    Returns a (possibly empty) DataFrame with Open/High/Low/Close/Volume.
    """
    global _last_served_by

    _ensure_provider_decided()

    if _moomoo_available:
        df = _fetch_moomoo(ticker, period, start, end, timeout)
        if df is not None and not df.empty:
            _last_served_by = "moomoo"
            return df
        # Moomoo couldn't serve this one — fall through to yfinance for *this* call.

    df = _fetch_yfinance(ticker, period, start, end, timeout)
    _last_served_by = "yfinance"
    return df


def provider_name() -> str:
    """
    Returns the provider that served the last call, or the provider we'd use
    for the next call if none have been served yet.
    """
    if _moomoo_available is None:
        # Haven't probed yet. Best guess from env.
        return "yfinance" if _PROVIDER_ENV == "yfinance" else "auto"
    return _last_served_by


def health() -> dict:
    """Diagnostic snapshot for the Settings tab."""
    return {
        "provider_env": _PROVIDER_ENV,
        "moomoo_available": bool(_moomoo_available),
        "moomoo_host": _MOOMOO_HOST,
        "moomoo_port": _MOOMOO_PORT,
        "moomoo_consecutive_failures": _moomoo_failures,
        "last_served_by": _last_served_by,
        "init_error": _init_error,
    }


def reset() -> None:
    """
    Forget detection state. Mainly for tests; also useful from the Settings
    tab if the user starts OpenD after the app already booted.
    """
    global _quote_ctx, _moomoo_available, _moomoo_failures, _init_error, _last_served_by
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
