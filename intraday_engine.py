#!/usr/bin/env python3
# intraday_engine.py
"""
Intraday trading engine — v3.7 Block 4.

WHAT THIS DOES:
    1. execute_intraday_entry()  — position-size and execute an ORB signal
    2. auto_settle_intraday()    — check active intraday trades for SL/TP hits
    3. force_flat_all_intraday() — close ALL open intraday positions (THE INVARIANT)
    4. get_active_intraday_tickers() — tickers with open intraday positions

WHAT THIS REUSES:
    * trading_engine.execute_entry()    — DB write + cash debit (same as swing)
    * trading_engine.execute_full_exit() — DB write + cash credit (same as swing)
    * trading_engine.apply_buy_slippage() / apply_sell_slippage()

WHAT'S DIFFERENT FROM SWING:
    * Stop = OR_low (structural), not ATR-based. Always a hard stop.
    * Targets = pre-computed by the screener (1.5R / 2.0R / 2.5R × OR_range).
    * No trailing stop — ORB is a binary bet.
    * No time exit — everything MUST be same-day (enforced by force_flat).
    * Force-flat at 15:55 ET — zero overnight risk. Dedicated test guards this.
    * execution_type = "AGENT_INTRADAY" — distinguishes from swing trades.

DESIGN INVARIANTS (test-guarded):
    1. force_flat_all_intraday() must leave ZERO active intraday trades.
    2. Every intraday position must be closed before the next US session open.
    3. execute_intraday_entry() must refuse if force-flat time has passed.
    4. Cash conservation: total equity before = total equity after + slippage.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from trading_engine import (
    execute_entry,
    execute_full_exit,
    apply_buy_slippage,
    apply_sell_slippage,
    lot_size,
    fee_rate,
    round_to_lot,
)
from repository import active_trades, load_account, update_trade, save_account
from trading_engine import execute_partial_exit
from logger import get_logger

log = get_logger("intraday_engine")

US_EASTERN = ZoneInfo("America/New_York")
INTRADAY_FLAT_BY = dtime(15, 55)           # hard exit time (ET)
INTRADAY_SESSION_CLOSE = dtime(16, 0)      # market close (ET)
INTRADAY_SESSION_OPEN = dtime(9, 30)       # market open (ET)
INTRADAY_EXECUTION_TYPE = "AGENT_INTRADAY"  # distinguishes from swing/AGENT
INTRADAY_DEFAULT_RISK_PCT = 1.0            # same as swing default


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now_et() -> datetime:
    """Current US Eastern time."""
    return datetime.now(US_EASTERN)


def _is_past_flat_by(now_et: Optional[datetime] = None) -> bool:
    """True if we're at or past the force-flat time."""
    t = (now_et or _now_et()).time()
    return t >= INTRADAY_FLAT_BY


def _is_during_session(now_et: Optional[datetime] = None) -> bool:
    """True if we're within the US RTH window."""
    t = (now_et or _now_et()).time()
    return INTRADAY_SESSION_OPEN <= t < INTRADAY_SESSION_CLOSE


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

def intraday_position_size(
    entry_price: float,
    stop_loss: float,
    capital: Optional[float] = None,
    risk_pct: float = INTRADAY_DEFAULT_RISK_PCT,
) -> int:
    """Calculate shares for an intraday ORB trade.

    Same formula as swing: risk_amount = capital × risk_pct / 100.
    Shares = risk_amount / risk_per_share, rounded down to lot size.

    Args:
        entry_price: signal entry price
        stop_loss: OR_low (structural stop)
        capital: total equity. If None, loads from account DB.
        risk_pct: % of capital to risk (default 1.0).
    """
    if capital is None:
        acc = load_account()
        capital = acc.get("total_equity", acc.get("cash_balance", 5000.0))

    risk_per_share = entry_price - stop_loss
    if risk_per_share <= 0:
        return 0

    risk_amount = capital * (risk_pct / 100.0)
    raw_shares = int(risk_amount / risk_per_share)
    return round_to_lot(raw_shares)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def execute_intraday_entry(
    signal: dict,
    market_regime: Optional[dict] = None,
    actor: str = "AGENT",
    risk_pct: float = INTRADAY_DEFAULT_RISK_PCT,
    now_et: Optional[datetime] = None,
) -> tuple[bool, int | None, str]:
    """Execute an intraday ORB signal as a paper trade.

    Guards:
        * Must be during session AND before flat_by time.
        * Signal must have valid entry, stop_loss, tp1-tp3.
        * Position must be at least 1 lot after rounding.

    Args:
        signal: dict from intraday_screener.compute_intraday_signal().
        market_regime: optional regime dict (not used; may add later).
        actor: "AGENT" (default) or "USER".
        risk_pct: % of capital to risk per trade.
        now_et: simulated time for testing.

    Returns:
        (success, trade_id or None, message)
    """
    if _is_past_flat_by(now_et):
        return False, None, "Past force-flat time (15:55 ET). No new entries."

    if not _is_during_session(now_et):
        return False, None, "Outside US session. No entries."

    ticker = signal.get("ticker", "")
    entry_price = signal.get("entry", 0)
    stop_loss = signal.get("stop_loss", 0)
    tp1 = signal.get("tp1", 0)
    tp2 = signal.get("tp2", 0)
    tp3 = signal.get("tp3", 0)
    signal_type = signal.get("signal", "GOLD BUY (ORB)")
    confidence = signal.get("confidence", 50.0)
    name = signal.get("name", ticker)
    sector = signal.get("sector", "")

    if entry_price <= 0 or stop_loss <= 0 or entry_price <= stop_loss:
        return False, None, (
            f"Invalid prices: entry={entry_price}, stop={stop_loss}"
        )

    shares = intraday_position_size(entry_price, stop_loss,
                                    risk_pct=risk_pct)
    if shares <= 0:
        return False, None, (
            f"Position size is zero. risk_per_share={entry_price - stop_loss:.3f}"
        )

    if market_regime is None:
        market_regime = {"regime_data": {"regime": "NEUTRAL"}}

    return execute_entry(
        ticker=ticker,
        name=name,
        sector=sector,
        entry_price=entry_price,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        signal_type=signal_type,
        shares=shares,
        analysis_data=signal,
        market_regime=market_regime,
        confidence_score=confidence,
        execution_type=INTRADAY_EXECUTION_TYPE,
        actor=actor,
    )


# ---------------------------------------------------------------------------
# Exit settlement — intraday-specific
# ---------------------------------------------------------------------------

def _bar_data_for_ticker(
    ticker: str,
    intraday_data: dict[str, dict],
) -> Optional[dict]:
    """Lookup the latest price/high/low for a ticker from intraday data.

    The screener's data_provider serves 5m bars; the caller passes a dict
    of {ticker: {"price": float, "high": float, "low": float}} for the
    latest completed bar. This mirrors swing's `price_lookup` pattern.
    """
    return intraday_data.get(ticker)


def auto_settle_intraday(
    intraday_data: dict[str, dict],
    actor: str = "AGENT",
    now_et: Optional[datetime] = None,
) -> dict:
    """Check all active intraday trades for exit conditions.

    Exit priority (same as swing, simplified):
        1. TP3 hit  → full exit (WIN)
        2. Hard SL  → full exit (LOSS)
        3. TP2 hit  → partial 50% exit (no trailing for intraday)
        4. TP1 hit  → no action (targets are pre-set, no trailing for intraday)

    After flat_by time: ALL remaining trades are force-flat (separate call).

    Args:
        intraday_data: {ticker: {"price": float, "high": float, "low": float}}
        actor: "AGENT" (default).
        now_et: simulated time for testing.

    Returns:
        {"settled": [...], "partials": [...]} same shape as swing.
    """
    settled: list[dict] = []
    partials: list[dict] = []

    for t in active_trades():
        # Only settle intraday trades
        if t.get("execution_type") != INTRADAY_EXECUTION_TYPE:
            continue

        ticker = t["ticker"]
        bar = _bar_data_for_ticker(ticker, intraday_data)
        if bar is None:
            continue

        current_price = float(bar["price"])
        high_bar = float(bar.get("high", current_price))
        low_bar = float(bar.get("low", current_price))

        entry = float(t["entry_price"])
        sl = float(t["stop_loss"])
        tp1, tp2, tp3 = t["tp1"], t["tp2"], t["tp3"]

        # Track MAE/MFE
        highest = max(float(t.get("highest_price") or entry), high_bar)
        lowest = min(float(t.get("lowest_price") or entry), low_bar)
        mae_pct = (lowest - entry) / entry * 100
        mfe_pct = (highest - entry) / entry * 100

        update_trade(t["id"], {
            "highest_price": round(highest, 3),
            "lowest_price": round(lowest, 3),
            "mae_pct": round(mae_pct, 3),
            "mfe_pct": round(mfe_pct, 3),
            "unrealized_pnl": round(
                (current_price - entry) * t["shares_remaining"], 2),
        })

        # ----- Exit conditions -----

        # 1. TP3 — full win
        if high_bar >= tp3:
            ok, msg = execute_full_exit(
                t["id"], tp3, reason="TP3 hit (intraday ORB)",
                outcome="WIN", actor=actor,
            )
            if ok:
                settled.append({
                    "trade_id": t["id"], "type": "TP3", "msg": msg,
                    "ticker": ticker, "outcome": "WIN",
                })
            continue

        # 2. Hard SL — full loss
        if low_bar <= sl:
            ok, msg = execute_full_exit(
                t["id"], sl, reason="Hard SL hit (OR_low)",
                outcome="LOSS", actor=actor,
            )
            if ok:
                settled.append({
                    "trade_id": t["id"], "type": "SL", "msg": msg,
                    "ticker": ticker, "outcome": "LOSS",
                })
            continue

        # 3. TP2 — partial 50% exit
        if high_bar >= tp2 and t.get("phase") == "FULL":
            shares_part = round_to_lot(t["shares_remaining"] // 2)
            if shares_part > 0:
                ok, msg = execute_partial_exit(
                    t["id"], "TP2", tp2, shares_part,
                    reason="50% partial at TP2 (intraday ORB) — runner kept",
                    actor=actor,
                )
                if ok:
                    partials.append({
                        "trade_id": t["id"], "ticker": ticker,
                        "shares": shares_part, "msg": msg,
                    })

        # 4. TP1 — NO trailing stop for intraday.
        # In swing, reaching TP1 sets a trailing stop. In intraday ORB,
        # the stop stays at OR_low — the trade runs to TP2 or SL.
        # No action here.

    # Update equity
    acc = load_account()
    active_val = 0.0
    for t in active_trades():
        if t.get("execution_type") != INTRADAY_EXECUTION_TYPE:
            continue
        px = intraday_data.get(
            t["ticker"], {},
        ).get("price", t["entry_price"])
        active_val += float(px) * int(t["shares_remaining"])

    save_account(total_equity=acc["cash_balance"] + active_val)

    return {
        "settled": settled,
        "partials": partials,
        "cash_balance": round(acc["cash_balance"], 2),
        "total_equity": round(acc["cash_balance"] + active_val, 2),
    }


# ---------------------------------------------------------------------------
# FORCE-FLAT — THE INVARIANT
# ---------------------------------------------------------------------------

def force_flat_all_intraday(
    intraday_data: dict[str, dict],
    actor: str = "AGENT",
    now_et: Optional[datetime] = None,
) -> int:
    """Close ALL open intraday positions at current market price.

    This is THE invariant. Must be called at 15:55 ET. After this call,
    there must be ZERO active intraday trades. Test-guarded.

    Args:
        intraday_data: {ticker: {"price": float}} for current bar.
        actor: "AGENT" (default).
        now_et: simulated time for testing.

    Returns:
        Number of trades closed.
    """
    closed_count = 0

    for t in active_trades():
        if t.get("execution_type") != INTRADAY_EXECUTION_TYPE:
            continue
        if t.get("status") != "ACTIVE":
            continue

        ticker = t["ticker"]
        bar = _bar_data_for_ticker(ticker, intraday_data)
        if bar is None:
            # No price data — use last known price from the trade
            exit_price = float(t.get("highest_price", t["entry_price"]))
        else:
            exit_price = float(bar["price"])

        outcome = "WIN" if exit_price > t["entry_price"] else "LOSS"

        ok, msg = execute_full_exit(
            t["id"], exit_price,
            reason=f"FORCE FLAT (15:55 ET intraday close)",
            outcome=outcome, actor=actor,
        )
        if ok:
            closed_count += 1
            log.info(
                "intraday force-flat: trade #%d %s @ %.3f (%s)",
                t["id"], ticker, exit_price, outcome,
            )
        else:
            log.error(
                "intraday force-flat FAILED: trade #%d %s — %s",
                t["id"], ticker, msg,
            )

    # Verify the invariant
    remaining = [
        t["id"] for t in active_trades()
        if t.get("execution_type") == INTRADAY_EXECUTION_TYPE
        and t.get("status") == "ACTIVE"
    ]
    if remaining:
        log.error(
            "INVARIANT VIOLATION: %d intraday trades still ACTIVE "
            "after force_flat: %s", len(remaining), remaining,
        )

    return closed_count


# ---------------------------------------------------------------------------
# Active ticker tracking (for screener's already_triggered set)
# ---------------------------------------------------------------------------

def get_active_intraday_tickers() -> set[str]:
    """Return the set of tickers with open intraday positions.

    The screener uses this to skip tickers that already have a trade today
    (one signal per ticker per session).
    """
    return {
        t["ticker"] for t in active_trades()
        if t.get("execution_type") == INTRADAY_EXECUTION_TYPE
        and t.get("status") == "ACTIVE"
    }


# ---------------------------------------------------------------------------
# Session-status helper (for the scheduler / UI)
# ---------------------------------------------------------------------------

def intraday_session_status(now_et: Optional[datetime] = None) -> dict:
    """Return a status dict about the current intraday session.

    Used by the scheduler to decide what to do, and by the UI to show
    the current state.
    """
    now = now_et or _now_et()
    t = now.time()

    if t < INTRADAY_SESSION_OPEN:
        return {
            "state": "PREMARKET",
            "can_scan": False,
            "can_enter": False,
            "should_force_flat": False,
            "message": f"US market opens at 09:30 ET. "
                       f"Currently {t.strftime('%H:%M')} ET.",
        }
    elif t < (datetime.combine(now.date(), INTRADAY_SESSION_OPEN)
              + timedelta(minutes=15)).time():
        # Inside OR window (first 15 min) — scan but don't enter yet
        return {
            "state": "OR_WINDOW",
            "can_scan": True,
            "can_enter": False,
            "should_force_flat": False,
            "message": "Opening range forming (09:30–09:45 ET). "
                       "Scanning, entries after 09:45.",
        }
    elif t < INTRADAY_FLAT_BY:
        return {
            "state": "ACTIVE_TRADING",
            "can_scan": True,
            "can_enter": True,
            "should_force_flat": False,
            "message": f"Intraday active. Entries until 15:55 ET. "
                       f"Currently {t.strftime('%H:%M')} ET.",
        }
    elif t < INTRADAY_SESSION_CLOSE:
        return {
            "state": "FORCE_FLAT_WINDOW",
            "can_scan": False,
            "can_enter": False,
            "should_force_flat": True,
            "message": f"Force-flat window (15:55–16:00 ET). "
                       f"All positions must be closed.",
        }
    else:
        return {
            "state": "POSTMARKET",
            "can_scan": False,
            "can_enter": False,
            "should_force_flat": True,
            "message": "US market closed. No intraday activity.",
        }
