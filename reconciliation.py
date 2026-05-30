# reconciliation.py
"""
Broker ↔ internal reconciliation (Block 6, v3.6).

The agent's internal `account` and `trades` tables are the source of truth
for the Bayesian brain. When the user has opted into SIMULATE or REAL
broker_mode, real orders are also being placed via `MoomooUSAdapter`.

These two ledgers can drift due to:
  - Partial fills on market orders
  - Slippage between our paper fill assumption and the broker's real fill
  - Manual trades the user placed directly in Moomoo
  - Failed mirror orders (broker REJECTED our place_order)
  - Corporate actions handled differently by broker vs our engine

This module computes the drift once per scheduler cycle and:
  - Logs RECONCILE_OK if drift is below threshold (default 0.5% of equity)
  - Logs RECONCILE_DRIFT (WARN) + sends Telegram alert if above threshold
  - Persists `last_reconcile_at` and `last_reconcile_drift` on scheduler_state
    so the Settings tab can show the current state.

Design constraints (per the project handbook §4.21):
  - NEVER raises into the scheduler cycle. Any error → log + return None.
  - NEVER mutates internal `account` or `trades` rows. Internal state is
    authoritative; reconciliation is a READ-ONLY observation that produces
    an alert when divergence is suspicious.
  - Cheap: one accinfo_query + one position_list_query per cycle.
  - Skipped entirely when broker_mode == NOOP (no broker to compare against).
  - Skipped entirely when MY market is active (no broker either).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

try:
    from logger import get_logger, log_scheduler_event
    log = get_logger("reconciliation")
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("reconciliation")
    def log_scheduler_event(*a, **kw):
        pass


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# Drift > this fraction of total equity → flagged as RECONCILE_DRIFT (alert)
# 0.005 = 0.5%. On a USD 5000 account that's USD 25 of drift before alerting.
DEFAULT_DRIFT_ALERT_THRESHOLD = 0.005

# Position quantity mismatch tolerance.
# If broker says we hold 100 shares of AAPL and internal says 95, abs(100-95)=5.
# Anything ≥ this many shares OR ≥ this fraction (whichever is more permissive)
# is considered a mismatch.
POSITION_QTY_TOLERANCE_ABS = 1            # 1 share
POSITION_QTY_TOLERANCE_PCT = 0.01         # 1%


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class PositionDiff:
    ticker: str
    internal_qty: int
    broker_qty: int

    @property
    def delta(self) -> int:
        return self.broker_qty - self.internal_qty


@dataclass
class ReconcileResult:
    """Outcome of one reconciliation cycle. Always returned, even on errors."""
    ran: bool                                      # False if skipped
    reason_skipped: Optional[str] = None
    # Cash / equity drift
    internal_cash: float = 0.0
    internal_equity: float = 0.0
    broker_cash: float = 0.0
    broker_total_assets: float = 0.0
    broker_market_value: float = 0.0
    cash_drift: float = 0.0                         # broker - internal
    equity_drift: float = 0.0
    equity_drift_pct: float = 0.0                  # relative to internal_equity
    drift_threshold_pct: float = DEFAULT_DRIFT_ALERT_THRESHOLD * 100
    # Position-level
    position_diffs: list[PositionDiff] = field(default_factory=list)
    positions_only_in_broker: list[str] = field(default_factory=list)
    positions_only_in_internal: list[str] = field(default_factory=list)
    # Verdict
    drift_flagged: bool = False
    error: Optional[str] = None

    def is_clean(self) -> bool:
        return self.ran and not self.drift_flagged and not self.error

    def to_dict(self) -> dict:
        return {
            "ran": self.ran,
            "reason_skipped": self.reason_skipped,
            "internal_cash": round(self.internal_cash, 2),
            "internal_equity": round(self.internal_equity, 2),
            "broker_cash": round(self.broker_cash, 2),
            "broker_total_assets": round(self.broker_total_assets, 2),
            "broker_market_value": round(self.broker_market_value, 2),
            "cash_drift": round(self.cash_drift, 2),
            "equity_drift": round(self.equity_drift, 2),
            "equity_drift_pct": round(self.equity_drift_pct, 4),
            "drift_threshold_pct": round(self.drift_threshold_pct, 4),
            "position_diffs": [
                {"ticker": d.ticker, "internal_qty": d.internal_qty,
                 "broker_qty": d.broker_qty, "delta": d.delta}
                for d in self.position_diffs
            ],
            "positions_only_in_broker": list(self.positions_only_in_broker),
            "positions_only_in_internal": list(self.positions_only_in_internal),
            "drift_flagged": self.drift_flagged,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Predicate helpers
# ---------------------------------------------------------------------------

def _should_run() -> tuple[bool, str]:
    """Decide whether to run reconciliation this cycle.

    Returns (run_bool, skip_reason). When run_bool is False, skip_reason
    explains why so the Logs tab can show it.
    """
    try:
        from broker_adapter import get_broker_mode
        mode = get_broker_mode()
    except Exception as e:
        return False, f"broker_adapter not importable: {e}"

    if mode == "NOOP":
        return False, "broker_mode=NOOP (notification-only; nothing to reconcile)"

    try:
        from market_profiles import active_profile
        prof = active_profile()
    except Exception as e:
        return False, f"market_profiles not importable: {e}"

    if not prof.moomoo_available:
        return False, (f"market={prof.code} has moomoo_available=False "
                       "(no broker to reconcile against)")
    return True, ""


def _position_mismatch(internal_qty: int, broker_qty: int) -> bool:
    """Returns True if the two quantities differ by more than the tolerance."""
    diff_abs = abs(broker_qty - internal_qty)
    if diff_abs < POSITION_QTY_TOLERANCE_ABS:
        return False
    base = max(abs(internal_qty), abs(broker_qty), 1)
    return (diff_abs / base) > POSITION_QTY_TOLERANCE_PCT


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_reconciliation(*,
                       drift_threshold_pct: float = DEFAULT_DRIFT_ALERT_THRESHOLD,
                       alert_on_drift: bool = True) -> ReconcileResult:
    """
    Compare internal account + positions to the broker.

    Args:
        drift_threshold_pct: fraction (0.005 = 0.5%) of internal equity above
            which we flag RECONCILE_DRIFT and (optionally) alert.
        alert_on_drift: when True, send a Telegram alert on drift.

    Returns:
        ReconcileResult — never raises. Always safe to call from the scheduler.
    """
    result = ReconcileResult(ran=False)
    result.drift_threshold_pct = drift_threshold_pct * 100

    # --- 1. Gate ---
    should, reason = _should_run()
    if not should:
        result.reason_skipped = reason
        _persist_summary(result)
        return result

    # --- 2. Pull internal state ---
    try:
        from repository import load_account, active_trades
        acc = load_account()
        internal_cash = float(acc.get("cash_balance", 0.0) or 0.0)
        internal_equity = float(acc.get("total_equity", 0.0) or 0.0)
        internal_positions: dict[str, int] = {}
        for t in active_trades():
            tk = (t.get("ticker") or "").strip().upper()
            qty = int(t.get("shares_remaining") or 0)
            if not tk or qty <= 0:
                continue
            internal_positions[tk] = internal_positions.get(tk, 0) + qty
    except Exception as e:
        result.error = f"internal-state read failed: {e}"
        log.warning(f"reconciliation: {result.error}")
        result.ran = True
        _persist_summary(result)
        return result

    result.internal_cash = internal_cash
    result.internal_equity = internal_equity

    # --- 3. Pull broker state ---
    try:
        from broker_adapter import get_broker_adapter
        adapter = get_broker_adapter()
        if not adapter.is_connected():
            ok = adapter.connect()
            if not ok:
                last_err = getattr(adapter, "last_error", lambda: None)() \
                    if callable(getattr(adapter, "last_error", None)) else None
                result.error = (f"broker not connectable: "
                                f"{last_err or 'unknown reason'}")
                log.warning(f"reconciliation: {result.error}")
                result.ran = True
                _persist_summary(result)
                return result
        snap = adapter.get_account_snapshot()
        broker_positions_list = adapter.list_positions()
    except Exception as e:
        result.error = f"broker fetch failed: {e}"
        log.warning(f"reconciliation: {result.error}")
        result.ran = True
        _persist_summary(result)
        return result

    result.broker_cash = float(snap.cash or 0.0)
    result.broker_total_assets = float(snap.total_assets or 0.0)
    result.broker_market_value = float(snap.market_value or 0.0)

    broker_positions: dict[str, int] = {}
    for p in broker_positions_list:
        tk = (p.ticker or "").strip().upper()
        if not tk:
            continue
        broker_positions[tk] = broker_positions.get(tk, 0) + int(p.quantity)

    # --- 4. Compute drift ---
    result.cash_drift = round(result.broker_cash - internal_cash, 2)
    result.equity_drift = round(result.broker_total_assets - internal_equity, 2)
    if internal_equity > 0:
        result.equity_drift_pct = round(
            abs(result.equity_drift) / internal_equity * 100, 4)
    else:
        result.equity_drift_pct = 0.0

    # --- 5. Position-level diff ---
    all_tickers = set(internal_positions) | set(broker_positions)
    for tk in sorted(all_tickers):
        i_qty = internal_positions.get(tk, 0)
        b_qty = broker_positions.get(tk, 0)
        if i_qty == 0 and b_qty > 0:
            result.positions_only_in_broker.append(tk)
            result.position_diffs.append(PositionDiff(tk, 0, b_qty))
        elif b_qty == 0 and i_qty > 0:
            result.positions_only_in_internal.append(tk)
            result.position_diffs.append(PositionDiff(tk, i_qty, 0))
        elif _position_mismatch(i_qty, b_qty):
            result.position_diffs.append(PositionDiff(tk, i_qty, b_qty))

    # --- 6. Verdict ---
    result.ran = True
    drift_too_big = (
        internal_equity > 0
        and (result.equity_drift_pct / 100) > drift_threshold_pct
    )
    positions_mismatched = bool(result.position_diffs)
    result.drift_flagged = drift_too_big or positions_mismatched

    # --- 7. Log + alert ---
    _emit_log_and_alerts(result, alert_on_drift=alert_on_drift)

    # --- 8. Persist summary for Settings UI ---
    _persist_summary(result)

    return result


# ---------------------------------------------------------------------------
# Logging + alerts
# ---------------------------------------------------------------------------

def _emit_log_and_alerts(result: ReconcileResult, *, alert_on_drift: bool) -> None:
    if not result.drift_flagged:
        log_scheduler_event(
            "RECONCILE_OK",
            f"Drift {result.equity_drift_pct:.3f}% "
            f"(threshold {result.drift_threshold_pct:.2f}%) — "
            f"{len(result.position_diffs)} position diffs",
            "INFO",
            payload=result.to_dict(),
        )
        return

    # Build a compact human summary
    bits = []
    if (result.equity_drift_pct / 100) > (result.drift_threshold_pct / 100):
        bits.append(
            f"equity drift {result.equity_drift:+,.2f} "
            f"({result.equity_drift_pct:.3f}% vs {result.drift_threshold_pct:.2f}% limit)"
        )
    if result.position_diffs:
        diff_strs = [
            f"{d.ticker} int={d.internal_qty} brk={d.broker_qty} Δ={d.delta:+d}"
            for d in result.position_diffs[:5]
        ]
        more = (f" + {len(result.position_diffs) - 5} more"
                if len(result.position_diffs) > 5 else "")
        bits.append("positions: " + "; ".join(diff_strs) + more)

    msg = " | ".join(bits) or "drift detected"
    log_scheduler_event("RECONCILE_DRIFT", msg, "WARN",
                          payload=result.to_dict())

    # Telegram alert (best-effort, never blocks the cycle)
    if alert_on_drift:
        try:
            from notifier import send_telegram, telegram_configured
            if telegram_configured():
                send_telegram(
                    "⚠️ Reconciliation drift detected\n"
                    f"Internal equity:   {result.internal_equity:,.2f}\n"
                    f"Broker assets:     {result.broker_total_assets:,.2f}\n"
                    f"Equity drift:      {result.equity_drift:+,.2f} "
                    f"({result.equity_drift_pct:.3f}%)\n"
                    f"Cash drift:        {result.cash_drift:+,.2f}\n"
                    f"Position mismatches: {len(result.position_diffs)}\n"
                    f"\nDetails: {msg}\n"
                    "Review the 📜 Logs tab for full breakdown."
                )
        except Exception as e:
            log.warning(f"reconciliation: telegram alert failed: {e}")


def _persist_summary(result: ReconcileResult) -> None:
    """Stamp last_reconcile_at + last_reconcile_drift on scheduler_state.

    Used by the Settings tab UI panel. Safe — never raises.
    """
    try:
        from db import connect, myt_iso
        with connect() as c:
            c.execute(
                "UPDATE scheduler_state "
                "SET last_reconcile_at=?, last_reconcile_drift=? "
                "WHERE id=1",
                (myt_iso(), float(result.equity_drift or 0.0)),
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Status getter for the UI
# ---------------------------------------------------------------------------

def get_reconciliation_status() -> dict:
    """Returns current status for the Settings tab panel."""
    try:
        from db import connect
        from broker_adapter import get_broker_mode, adapter_health
        with connect(readonly=True) as c:
            row = c.execute(
                "SELECT last_reconcile_at, last_reconcile_drift "
                "FROM scheduler_state WHERE id=1"
            ).fetchone()
        last_at = row["last_reconcile_at"] if row else None
        last_drift = row["last_reconcile_drift"] if row else None
        return {
            "broker_mode": get_broker_mode(),
            "last_reconcile_at": last_at,
            "last_reconcile_drift": last_drift,
            "adapter_health": adapter_health(),
            "drift_threshold_pct": DEFAULT_DRIFT_ALERT_THRESHOLD * 100,
        }
    except Exception as e:
        return {"error": str(e)}
