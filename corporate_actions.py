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



# ===========================================================================
# Phase 4 — Orchestrator: process_corporate_actions
# ===========================================================================
#
# Called by the scheduler at the start of each cycle. Pulls active-trade
# tickers, scans the window since last_corp_action_scan_at for events, and
# for each event either:
#   - SPLIT/BONUS  → calls trading_engine.apply_split_to_trade (auto-adjust)
#   - DIVIDEND     → alert-only (no P&L adjustment in v3.5)
#
# All failures are caught per-event so one bad split doesn't abort the
# whole scan. Each event is recorded in corporate_actions_processed via
# record_processed() — idempotent if the same event is seen twice.
# ---------------------------------------------------------------------------

def _get_active_trade_tickers() -> list[str]:
    """Distinct list of tickers that currently have at least one ACTIVE trade."""
    from db import connect
    with connect(readonly=True) as c:
        rows = c.execute(
            "SELECT DISTINCT ticker FROM trades WHERE status='ACTIVE'"
        ).fetchall()
    return [r["ticker"] for r in rows]


def _get_active_trade_ids_for_ticker(ticker: str) -> list[int]:
    """All active trade ids for a given ticker (could be more than one)."""
    from db import connect
    with connect(readonly=True) as c:
        rows = c.execute(
            "SELECT id FROM trades WHERE ticker=? AND status='ACTIVE'",
            (ticker,),
        ).fetchall()
    return [r["id"] for r in rows]


def _send_alert(subject: str, body: str,
                ticker: Optional[str] = None) -> None:
    """
    Best-effort Telegram + Email alert via notifier.dispatch.
    Honors the user's live_trigger_config channel preferences but is NOT
    gated by the live_trigger 'enabled' flag — corporate actions are
    system-event alerts, separate from trade-signal alerts.

    Never raises.
    """
    try:
        from notifier import dispatch
        from live_trigger import load_config as _load_lt_config
        cfg = _load_lt_config()
        channels = {
            "telegram": bool(cfg.get("telegram_enabled", 1)),
            "email": bool(cfg.get("email_enabled", 0)),
            "dashboard": True,
        }
        recipients = []
        if channels["email"]:
            raw = (cfg.get("email_recipients") or "").strip()
            recipients = [r.strip() for r in raw.split(",") if r.strip()]

        # Plain-text body for Telegram, simple HTML for email.
        html_body = body.replace("\n", "<br/>")
        dispatch(
            event_type="CORPORATE_ACTION",
            message_text=f"{subject}\n\n{body}",
            message_html=f"<h3>{subject}</h3><p>{html_body}</p>",
            subject=subject,
            trade_id=None,
            ticker=ticker,
            channels=channels,
            recipients=recipients,
        )
    except Exception as e:
        # Alerts are best-effort. Log but never raise — must not abort
        # the corporate-action scan because of a notifier glitch.
        try:
            _log.warning("corporate_actions: alert dispatch failed: %s", e)
        except Exception:
            pass


def process_corporate_actions(
    *,
    autoadjust: bool = True,
    last_scan_iso: Optional[str] = None,
    actor: str = "SCHEDULER",
) -> dict:
    """
    Full cycle: detect events for all active-trade tickers, then apply
    splits / alert dividends. Returns a summary dict for logging.

    Parameters
    ----------
    autoadjust    : if False, splits are detected & alerted but NOT applied.
                    Useful for shadow-mode rollout (set via Settings toggle).
    last_scan_iso : ISO timestamp of last successful scan. None → 7-day lookback.
    actor         : string for audit logs (defaults to 'SCHEDULER').

    Returns
    -------
    {
        "tickers_scanned":   int,
        "events_detected":   int,
        "splits_adjusted":   int,
        "splits_alerted_only": int,   # autoadjust=False or already_processed
        "dividends_alerted": int,
        "failures":          int,
        "skipped_dupes":     int,
        "details":           list[dict],   # per-event outcome
        "scan_window":       (start_iso, end_iso),
    }
    """
    tickers = _get_active_trade_tickers()
    summary = {
        "tickers_scanned": len(tickers),
        "events_detected": 0,
        "splits_adjusted": 0,
        "splits_alerted_only": 0,
        "dividends_alerted": 0,
        "failures": 0,
        "skipped_dupes": 0,
        "details": [],
        "scan_window": None,
    }

    if not tickers:
        # No active trades → nothing to scan. Cheap exit.
        return summary

    start, end = get_scan_window(last_scan_iso)
    summary["scan_window"] = (str(start), str(end))

    try:
        events = detect_for_tickers(tickers, start, end)
    except Exception as e:
        _log.error("process_corporate_actions: detect_for_tickers failed: %s", e)
        return summary

    summary["events_detected"] = len(events)

    # Sort events: oldest first so multiple splits on same trade compose in order.
    events.sort(key=lambda e: e.ex_date)

    for ev in events:
        per_event = {
            "ticker": ev.ticker,
            "ex_date": ev.ex_date,
            "event_type": ev.event_type,
            "ratio": ev.ratio,
            "amount_per_share": ev.amount_per_share,
            "source": ev.source,
            "outcome": None,
            "trade_ids": [],
            "error": None,
        }

        # Idempotency: skip if we already processed this event.
        if already_processed(ev):
            per_event["outcome"] = "skipped_already_processed"
            summary["skipped_dupes"] += 1
            summary["details"].append(per_event)
            continue

        trade_ids = _get_active_trade_ids_for_ticker(ev.ticker)
        per_event["trade_ids"] = trade_ids

        if not trade_ids:
            # No active position on this ticker — just record & move on.
            record_processed(ev, action_taken="SKIPPED_NO_POSITION",
                             affected_trade_ids=[])
            per_event["outcome"] = "no_active_position"
            summary["details"].append(per_event)
            continue

        try:
            if ev.event_type == "DIVIDEND":
                # Alert-only — no P&L adjustment in v3.5
                _send_alert(
                    subject=f"💰 Bursa dividend: {ev.ticker}",
                    body=(
                        f"{ev.describe()}\n"
                        f"Affected active trades: {trade_ids}\n"
                        f"Source: {ev.source}\n"
                        f"v3.5 policy: no automatic P&L adjustment.\n"
                        f"Mirror this dividend manually in Moomoo if you trade live."
                    ),
                )
                record_processed(ev, action_taken="ALERTED_ONLY",
                                 affected_trade_ids=trade_ids)
                per_event["outcome"] = "dividend_alerted"
                summary["dividends_alerted"] += 1

            elif ev.event_type in ("SPLIT", "BONUS"):
                if not autoadjust:
                    # Shadow mode: detect + alert but don't touch trades.
                    _send_alert(
                        subject=f"⚠️ {ev.event_type} detected (auto-adjust OFF): {ev.ticker}",
                        body=(
                            f"{ev.describe()}\n"
                            f"Affected active trades: {trade_ids}\n"
                            f"Source: {ev.source}\n"
                            f"Auto-adjustment is disabled in Settings — "
                            f"you must manually adjust these trades."
                        ),
                    )
                    record_processed(ev, action_taken="ALERTED_ONLY",
                                     affected_trade_ids=trade_ids)
                    per_event["outcome"] = "alerted_only_autoadjust_off"
                    summary["splits_alerted_only"] += 1

                else:
                    # Live adjustment: apply split atomically to each trade.
                    from trading_engine import apply_split_to_trade
                    adjusted: list[int] = []
                    errors: list[str] = []
                    for tid in trade_ids:
                        try:
                            apply_split_to_trade(
                                tid, ratio=ev.ratio, ex_date=ev.ex_date,
                                note=f"auto via {ev.source}",
                            )
                            adjusted.append(tid)
                        except Exception as ee:
                            errors.append(f"trade_id={tid}: {ee}")

                    if errors and not adjusted:
                        # Total failure: don't mark as processed so next cycle retries.
                        per_event["outcome"] = "failed"
                        per_event["error"] = "; ".join(errors)
                        summary["failures"] += 1
                        _send_alert(
                            subject=f"🚨 {ev.event_type} ADJUSTMENT FAILED: {ev.ticker}",
                            body=(
                                f"{ev.describe()}\n"
                                f"All adjustments failed — trades left unchanged.\n"
                                f"Errors: {per_event['error']}\n"
                                f"Will retry next cycle. Fix manually if needed."
                            ),
                        )
                    else:
                        # Partial or full success → record it (won't retry).
                        # We record even partial success because the failed trades
                        # are now in an inconsistent state and need manual review.
                        record_processed(
                            ev,
                            action_taken="ADJUSTED",
                            affected_trade_ids=adjusted,
                            error_message="; ".join(errors) if errors else None,
                        )
                        summary["splits_adjusted"] += 1
                        per_event["outcome"] = (
                            "adjusted_partial" if errors else "adjusted"
                        )
                        per_event["error"] = "; ".join(errors) if errors else None

                        # Success alert
                        _send_alert(
                            subject=f"✅ {ev.event_type} auto-adjusted: {ev.ticker}",
                            body=(
                                f"{ev.describe()}\n"
                                f"Adjusted trades: {adjusted}\n"
                                f"Source: {ev.source}\n"
                                + (f"⚠️ Failed: {per_event['error']}\n" if errors else "")
                                + "Mirror this in Moomoo if you're trading live."
                            ),
                        )

        except Exception as e:
            # Catch-all so one bad event doesn't poison the rest of the batch
            per_event["outcome"] = "exception"
            per_event["error"] = repr(e)
            summary["failures"] += 1
            _log.error("process_corporate_actions: unhandled error on %s: %s",
                       ev.describe(), e)

        summary["details"].append(per_event)

    return summary


# Public alias for symmetry with scheduler imports
__all__ = [
    "CorporateAction", "EventType", "SourceType", "ActionTaken",
    "INITIAL_LOOKBACK_DAYS", "MAX_CONSECUTIVE_MOOMOO_FAILURES",
    "already_processed", "record_processed",
    "get_scan_window",
    "detect_for_ticker", "detect_for_tickers",
    "detection_health", "reset_detection_state",
    "process_corporate_actions",
]
