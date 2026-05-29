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
