# corporate_actions.py
"""
Corporate-action detection + safe trade adjustment.

v3.5 — handles Bursa stock splits / bonus issues / cash dividends that occur
while the agent holds an open position.

Why this exists
---------------
Before v3.5 the agent had a silent failure mode: a stock doing a 1-for-5 split
mid-position would make stored entry_price / stop_loss / qty all wrong relative
to post-split market price. yfinance auto-adjusts historical bars but the
trade record in SQLite was never adjusted, so:

  - Stop-loss would trigger at "wrong" price (looks like an 80% crash)
  - Realized P&L on close would be wildly wrong
  - The learner would train on garbage data

This module fixes that by:

  1. Scanning all active trades for split / bonus / dividend events that
     occurred since the last scan.
  2. For splits/bonuses: atomically adjusting the trade (qty × ratio,
     entry_price ÷ ratio, stop_loss ÷ ratio, tp1/tp2/tp3 ÷ ratio).
     Cash-conservation invariant is preserved (no cash moves on a split).
  3. For cash dividends: alert-only (no P&L adjustment in v3.5 — deferred to v5).
  4. Idempotent via the `corporate_actions_processed` table.

Detection layer (Phase 2) is symmetric with data_provider.py:
  - Moomoo OpenAPI `request_rehab()` preferred when available
  - yfinance `Stock Splits` / `Dividends` columns as fallback

Adjustment layer (Phase 3) lives in trading_engine.apply_split_to_trade()
and is fully transaction-wrapped: any error rolls back AND fires a Telegram
alert so the user can fix manually.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional, Literal


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

EventType = Literal["SPLIT", "BONUS", "DIVIDEND"]
SourceType = Literal["moomoo", "yfinance"]
ActionTaken = Literal["ADJUSTED", "ALERTED_ONLY", "SKIPPED_NO_POSITION", "FAILED"]


@dataclass(frozen=True)
class CorporateAction:
    """
    Normalised representation of a corporate action, source-agnostic.

    Fields
    ------
    ticker          yfinance-style ticker (e.g. '0166.KL')
    ex_date         ISO date (YYYY-MM-DD) — the first day the stock trades
                    EX (i.e. after) the corporate event. This is the date
                    used for idempotency.
    event_type      'SPLIT' | 'BONUS' | 'DIVIDEND'
    ratio           For SPLIT/BONUS: new_shares / old_shares. Examples:
                    - 1-for-5 forward split (you get 5 new for each 1 old) → 5.0
                    - 5-for-1 reverse split (1 new for each 5 old)          → 0.2
                    - 1-for-2 bonus issue (1 free new for each 2 held)      → 1.5
                    For DIVIDEND: None.
    amount_per_share  For DIVIDEND: cash per share in RM. For SPLIT/BONUS: None.
    source          Which detector produced this event ('moomoo' | 'yfinance').
    raw             Optional dict carrying provider-specific fields for
                    auditing/debugging (not persisted).
    """
    ticker: str
    ex_date: str
    event_type: EventType
    ratio: Optional[float] = None
    amount_per_share: Optional[float] = None
    source: SourceType = "yfinance"
    raw: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Validation — fail loud on bad inputs so the scheduler doesn't
        # silently process garbage.
        if not self.ticker:
            raise ValueError("ticker is required")
        try:
            datetime.strptime(self.ex_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"ex_date must be YYYY-MM-DD, got {self.ex_date!r}")
        if self.event_type not in ("SPLIT", "BONUS", "DIVIDEND"):
            raise ValueError(f"event_type must be SPLIT|BONUS|DIVIDEND, got {self.event_type!r}")
        if self.event_type in ("SPLIT", "BONUS"):
            if self.ratio is None or self.ratio <= 0:
                raise ValueError(f"{self.event_type} requires positive ratio, got {self.ratio}")
            if self.amount_per_share is not None:
                raise ValueError(f"{self.event_type} must not carry amount_per_share")
        elif self.event_type == "DIVIDEND":
            if self.amount_per_share is None or self.amount_per_share <= 0:
                raise ValueError(f"DIVIDEND requires positive amount_per_share, got {self.amount_per_share}")
            if self.ratio is not None:
                raise ValueError("DIVIDEND must not carry ratio")

    @property
    def idempotency_key(self) -> tuple[str, str, str]:
        """Used to dedupe across multiple cycles via corporate_actions_processed table."""
        return (self.ticker, self.ex_date, self.event_type)

    def describe(self) -> str:
        """Human-readable summary for log lines and Telegram alerts."""
        if self.event_type == "SPLIT":
            return f"{self.ticker} SPLIT ex-{self.ex_date} ratio={self.ratio:.4f} (1:{self.ratio:g})"
        if self.event_type == "BONUS":
            return f"{self.ticker} BONUS ex-{self.ex_date} ratio={self.ratio:.4f}"
        if self.event_type == "DIVIDEND":
            return f"{self.ticker} DIVIDEND ex-{self.ex_date} RM{self.amount_per_share:.4f}/share"
        return f"{self.ticker} {self.event_type} ex-{self.ex_date}"


# ---------------------------------------------------------------------------
# Idempotency helpers (Phase 1 — lightweight DB layer)
# ---------------------------------------------------------------------------

def already_processed(action: CorporateAction) -> bool:
    """
    Returns True if this action has already been recorded in
    corporate_actions_processed (idempotent). Safe to call from the
    scheduler before any DB writes.
    """
    from db import connect
    with connect(readonly=True) as c:
        row = c.execute(
            "SELECT 1 FROM corporate_actions_processed "
            "WHERE ticker=? AND ex_date=? AND event_type=? LIMIT 1",
            action.idempotency_key,
        ).fetchone()
    return row is not None


def record_processed(
    action: CorporateAction,
    action_taken: ActionTaken,
    affected_trade_ids: Optional[list[int]] = None,
    error_message: Optional[str] = None,
) -> None:
    """
    Mark this corporate action as processed. UNIQUE constraint on
    (ticker, ex_date, event_type) means a second insert is a no-op.
    """
    import json
    from db import connect, myt_iso
    with connect() as c:
        c.execute(
            "INSERT OR IGNORE INTO corporate_actions_processed "
            "(ticker, ex_date, event_type, ratio, amount_per_share, source, "
            " detected_at, action_taken, affected_trade_ids_json, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                action.ticker,
                action.ex_date,
                action.event_type,
                action.ratio,
                action.amount_per_share,
                action.source,
                myt_iso(),
                action_taken,
                json.dumps(affected_trade_ids or []),
                error_message,
            ),
        )


# ---------------------------------------------------------------------------
# Scan window helpers
# ---------------------------------------------------------------------------

# Default lookback if we've never scanned before. Keeps the first run cheap
# (we don't pull years of dividend history just to find we never had a position).
INITIAL_LOOKBACK_DAYS = 7


def get_scan_window(last_scan_iso: Optional[str]) -> tuple[date, date]:
    """
    Return (start_date, end_date) for the next corporate-action scan.

    If last_scan_iso is None (first run ever) → start = today - 7 days.
    Otherwise                                  → start = last_scan_date - 1 day
                                                 (1-day overlap for safety;
                                                  idempotency table catches dupes)
    end_date is always today.
    """
    today = datetime.now().date()
    if last_scan_iso is None:
        return today - timedelta(days=INITIAL_LOOKBACK_DAYS), today
    try:
        last_d = datetime.strptime(last_scan_iso[:10], "%Y-%m-%d").date()
        return last_d - timedelta(days=1), today
    except (ValueError, TypeError):
        # Corrupt timestamp → behave like first-run.
        return today - timedelta(days=INITIAL_LOOKBACK_DAYS), today


# ---------------------------------------------------------------------------
# Phase 2 and 3 below will add:
#   - detect_corporate_actions(tickers, start, end) -> list[CorporateAction]
#       (Moomoo path + yfinance path with auto-fallback)
#   - process_corporate_actions(actions) -> dict with summary stats
#       (calls trading_engine.apply_split_to_trade for each affected position)
# ---------------------------------------------------------------------------



# ===========================================================================
# Phase 2 — Detection layer
# ===========================================================================
#
# Two backends, same output:
#   1. Moomoo OpenD via request_rehab() — preferred when available
#   2. yfinance — fallback (always available, works on Streamlit Cloud)
#
# Both share the data_provider port-pre-check / sticky-demote pattern: if
# Moomoo OpenD isn't reachable (no listener on 11111), we don't even try
# the SDK call. After MAX_CONSECUTIVE_MOOMOO_FAILURES consecutive failures
# we permanently fall back to yfinance for the rest of the process.
# ---------------------------------------------------------------------------

import threading
from typing import Optional

try:
    from logger import get_logger
    _log = get_logger("corporate_actions")
except Exception:  # pragma: no cover
    import logging
    _log = logging.getLogger("corporate_actions")


# State for the sticky-demote pattern (mirrors data_provider.py).
_moomoo_lock = threading.Lock()
_moomoo_state: dict = {
    "available": None,        # None = not probed; True/False = probed
    "consecutive_failures": 0,
    "init_error": None,
}
MAX_CONSECUTIVE_MOOMOO_FAILURES = 3


# ---------------------------------------------------------------------------
# Moomoo path
# ---------------------------------------------------------------------------

def _moomoo_available() -> bool:
    """Cheap probe: is the data_provider's Moomoo path live?

    We piggyback on data_provider's port-pre-check + connection state so we
    don't double-probe OpenD or spawn duplicate retry threads."""
    with _moomoo_lock:
        cached = _moomoo_state["available"]
        if cached is not None:
            return cached
        try:
            import data_provider as _dp
            _dp.ensure_probed()
            cached = bool(_dp._moomoo_available)
            _moomoo_state["available"] = cached
            if not cached:
                _moomoo_state["init_error"] = _dp._init_error
            return cached
        except Exception as e:
            _moomoo_state["available"] = False
            _moomoo_state["init_error"] = f"data_provider probe failed: {e}"
            return False


def _moomoo_demote(reason: str) -> None:
    with _moomoo_lock:
        _moomoo_state["consecutive_failures"] += 1
        n = _moomoo_state["consecutive_failures"]
    _log.warning("corporate_actions: Moomoo failed (%s) [%d/%d]",
                 reason, n, MAX_CONSECUTIVE_MOOMOO_FAILURES)
    if n >= MAX_CONSECUTIVE_MOOMOO_FAILURES:
        with _moomoo_lock:
            _moomoo_state["available"] = False
            _moomoo_state["init_error"] = f"demoted: {reason}"
        _log.warning("corporate_actions: Moomoo demoted — using yfinance for rest of process")


def _moomoo_success() -> None:
    with _moomoo_lock:
        if _moomoo_state["consecutive_failures"] > 0:
            _moomoo_state["consecutive_failures"] = 0


def _to_moomoo_code(ticker: str) -> Optional[str]:
    """0166.KL → MY.0166 (mirrors data_provider._to_moomoo_code)."""
    if not ticker:
        return None
    t = ticker.strip().upper()
    if t.endswith(".KL"):
        return f"MY.{t[:-3]}"
    return None  # Non-Bursa: skip Moomoo, use yfinance


def _detect_moomoo(ticker: str,
                   start: date,
                   end: date,
                   timeout: float = 10.0) -> Optional[list[CorporateAction]]:
    """
    Returns a list of CorporateActions detected via Moomoo request_rehab.
    Returns None on failure (caller should fall back to yfinance).
    Returns [] if Moomoo succeeded but found no events in the window.
    """
    code = _to_moomoo_code(ticker)
    if code is None:
        return None

    if not _moomoo_available():
        return None

    # Run the SDK call in a thread with a join timeout (data_provider pattern).
    result: dict = {"df": None, "err": None}

    def _worker():
        try:
            import data_provider as _dp
            ctx = _dp._quote_ctx
            if ctx is None:
                result["err"] = "no quote_ctx"
                return
            ret, data = ctx.request_rehab(code)
            if ret != 0:
                result["err"] = f"ret={ret} data={data}"
                return
            result["df"] = data
        except Exception as e:
            result["err"] = repr(e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=max(1.0, float(timeout)))

    if t.is_alive():
        _moomoo_demote(f"request_rehab timeout {timeout}s")
        return None
    if result["err"]:
        _moomoo_demote(result["err"])
        return None

    df = result["df"]
    if df is None or len(df) == 0:
        _moomoo_success()
        return []

    events: list[CorporateAction] = []
    # Moomoo request_rehab returns columns including:
    #   ex_div_date, split_ratio, bonus_div_ratio, per_cash_div,
    #   forward_adj_factorA, forward_adj_factorB
    # See the protobuf Qot_RequestRehab spec.
    start_s, end_s = str(start), str(end)
    for _, row in df.iterrows():
        try:
            ex_date = str(row.get("ex_div_date") or row.get("ex_date") or "")[:10]
            if not ex_date or ex_date < start_s or ex_date > end_s:
                continue

            split_ratio = float(row.get("split_ratio") or 1.0)
            bonus_ratio = float(row.get("bonus_div_ratio") or 0.0)
            cash_div = float(row.get("per_cash_div") or 0.0)

            # Forward split: split_ratio > 1.0 means "1 old → N new"
            if split_ratio and split_ratio > 1.0001:
                events.append(CorporateAction(
                    ticker=ticker, ex_date=ex_date,
                    event_type="SPLIT", ratio=split_ratio,
                    source="moomoo", raw=dict(row),
                ))
            # Reverse split: split_ratio < 1.0
            elif split_ratio and split_ratio < 0.9999:
                events.append(CorporateAction(
                    ticker=ticker, ex_date=ex_date,
                    event_type="SPLIT", ratio=split_ratio,
                    source="moomoo", raw=dict(row),
                ))

            # Bonus issue: bonus_div_ratio > 0 means "N free new for each held"
            # We convert to the same ratio convention as SPLIT.
            # bonus_div_ratio=0.5 means 1 free for every 2 → ratio = 1.5
            if bonus_ratio and bonus_ratio > 0:
                events.append(CorporateAction(
                    ticker=ticker, ex_date=ex_date,
                    event_type="BONUS", ratio=1.0 + bonus_ratio,
                    source="moomoo", raw=dict(row),
                ))

            # Cash dividend
            if cash_div and cash_div > 0:
                events.append(CorporateAction(
                    ticker=ticker, ex_date=ex_date,
                    event_type="DIVIDEND", amount_per_share=cash_div,
                    source="moomoo", raw=dict(row),
                ))
        except (ValueError, TypeError, KeyError) as e:
            _log.warning("corporate_actions: skipping malformed Moomoo row for %s: %s",
                         ticker, e)
            continue

    _moomoo_success()
    return events


# ---------------------------------------------------------------------------
# yfinance path (always-available fallback)
# ---------------------------------------------------------------------------

def _detect_yfinance(ticker: str,
                     start: date,
                     end: date,
                     timeout: float = 15.0) -> list[CorporateAction]:
    """
    Returns CorporateActions from yfinance's Stock Splits and Dividends
    columns. Always returns a list (possibly empty); never returns None.
    """
    try:
        import yfinance as yf
        # Pull enough history to cover the window. We use period that covers
        # the window plus a 5-day buffer for safety. For windows > 1y we just
        # use period='max'.
        days_window = (end - start).days
        if days_window <= 30:
            period = "1mo"
        elif days_window <= 90:
            period = "3mo"
        elif days_window <= 180:
            period = "6mo"
        elif days_window <= 365:
            period = "1y"
        elif days_window <= 730:
            period = "2y"
        else:
            period = "max"

        df = yf.Ticker(ticker).history(period=period, timeout=timeout)
    except Exception as e:
        _log.warning("corporate_actions: yfinance fetch failed for %s: %s", ticker, e)
        return []

    if df is None or df.empty:
        return []

    events: list[CorporateAction] = []
    start_s, end_s = str(start), str(end)

    # Splits column
    if "Stock Splits" in df.columns:
        splits = df[df["Stock Splits"] != 0]["Stock Splits"]
        for ts, ratio in splits.items():
            ex_date = ts.strftime("%Y-%m-%d")
            if ex_date < start_s or ex_date > end_s:
                continue
            try:
                events.append(CorporateAction(
                    ticker=ticker, ex_date=ex_date,
                    event_type="SPLIT", ratio=float(ratio),
                    source="yfinance",
                ))
            except ValueError as e:
                _log.warning("corporate_actions: bad split for %s on %s: %s",
                             ticker, ex_date, e)

    # Dividends column
    if "Dividends" in df.columns:
        divs = df[df["Dividends"] > 0]["Dividends"]
        for ts, amt in divs.items():
            ex_date = ts.strftime("%Y-%m-%d")
            if ex_date < start_s or ex_date > end_s:
                continue
            try:
                events.append(CorporateAction(
                    ticker=ticker, ex_date=ex_date,
                    event_type="DIVIDEND", amount_per_share=float(amt),
                    source="yfinance",
                ))
            except ValueError as e:
                _log.warning("corporate_actions: bad dividend for %s on %s: %s",
                             ticker, ex_date, e)

    return events


# ---------------------------------------------------------------------------
# Public API — provider-agnostic detection with auto-fallback
# ---------------------------------------------------------------------------

def detect_for_ticker(ticker: str,
                      start: date,
                      end: date,
                      timeout: float = 15.0) -> list[CorporateAction]:
    """
    Detect all corporate actions for a single ticker between [start, end].

    Strategy:
      1. Try Moomoo's request_rehab if available.
      2. If Moomoo returns None (failure / unreachable), fall back to yfinance.
      3. If Moomoo returns [] (success, no events), trust it — don't double-check.

    Returns a list of CorporateActions (possibly empty).
    """
    moomoo_events = _detect_moomoo(ticker, start, end, timeout=timeout)
    if moomoo_events is not None:
        return moomoo_events  # success — even if empty, Moomoo confirmed nothing happened
    # Moomoo failed for this call (or unavailable) — fall back
    return _detect_yfinance(ticker, start, end, timeout=timeout)


def detect_for_tickers(tickers: list[str],
                       start: date,
                       end: date,
                       timeout_per_ticker: float = 15.0) -> list[CorporateAction]:
    """
    Detect corporate actions across multiple tickers. Used by the scheduler
    to scan all active-trade tickers in one pass.

    Returns the flattened list of all CorporateActions across tickers, in
    arbitrary order. Caller is responsible for deduplication / sorting.
    """
    all_events: list[CorporateAction] = []
    for t in tickers:
        try:
            evs = detect_for_ticker(t, start, end, timeout=timeout_per_ticker)
            all_events.extend(evs)
        except Exception as e:
            # One ticker's failure must not poison the whole scan.
            _log.warning("corporate_actions: detect_for_ticker(%s) raised: %s", t, e)
            continue
    return all_events


# ---------------------------------------------------------------------------
# Diagnostics (for the Settings tab — Phase 5)
# ---------------------------------------------------------------------------

def detection_health() -> dict:
    """Snapshot of the Moomoo detection state. Used by the UI."""
    with _moomoo_lock:
        return {
            "moomoo_available": _moomoo_state["available"],
            "moomoo_consecutive_failures": _moomoo_state["consecutive_failures"],
            "moomoo_init_error": _moomoo_state["init_error"],
            "max_consecutive_failures_before_demote": MAX_CONSECUTIVE_MOOMOO_FAILURES,
        }


def reset_detection_state() -> None:
    """Forget Moomoo probe state. For tests + UI re-probe button."""
    with _moomoo_lock:
        _moomoo_state["available"] = None
        _moomoo_state["consecutive_failures"] = 0
        _moomoo_state["init_error"] = None
