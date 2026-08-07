# trading_engine.py
"""
Trading Engine — paper-trade execution with realistic market conventions.

v3.6 multi-market change
------------------------
* `LOT_SIZE`, `TRANSACTION_COST_PCT` are now COMPUTED from the active
  market profile, not module-level constants.
* Existing callers can still import the names; they're now small wrapper
  functions returning the active value.
* Currency symbol (`RM` vs `$`) for user-facing strings comes from profile.
* Slippage model dispatches to the profile's `slippage_fn` if the active
  market is non-MY; MY retains the v3.3 volume-aware bps model exactly.
* All cash-conservation tests pass unchanged because (a) MY math is
  identical, and (b) US uses lot_size=1 + fee_rate=0 which is a strict
  subset of the existing math.

Improvements vs v1 (still guarded by tests)
-------------------------------------------
* Cash accounting verified with property tests (see tests/).
* Board-lot enforcement (100 for Bursa, 1 for US — both honoured by round_to_lot).
* Configurable slippage model (linear in trade size / ADV).
* MAE/MFE tracking per active trade.
* v3.8: Trailing stop now RATCHETS upward as new highs form (chandelier
  style, volatility-scaled by entry ATR) — it never moves back down.
* Time-exit handled by regime (5/7/14 days).
* All state in SQLite via repository — no JSON race conditions.
* Every action emits a structured trade_log row.
"""

from __future__ import annotations
import pandas as pd

from db import myt_iso, get_myt_now
from repository import (
    insert_trade, update_trade, get_trade, active_trades,
    insert_partial_exit, load_account, save_account,
)
from logger import log_trade_event, get_logger

log = get_logger("trading_engine")

# -------------------------------------------------------------------------
# COST + SLIPPAGE — multi-market dispatch
# -------------------------------------------------------------------------

# These three constants exist for backwards compatibility (app.py imports
# LOT_SIZE). They reflect MY defaults; runtime values come from the active
# profile via the helpers below.
TRANSACTION_COST_PCT = 0.0015          # MY default (Bursa brokerage + stamp + clearing)
SLIPPAGE_BASE_BPS = 5                  # MY-flavoured legacy constant
SLIPPAGE_K_RM = 50_000                 # MY-flavoured legacy constant
SLIPPAGE_LIQUIDITY_CAP_BPS = 80        # MY hard cap, still applied when MY active
LOT_SIZE = 100                         # MY default (Bursa board lot)

# -------------------------------------------------------------------------
# v3.8 — Ratcheting (chandelier) trailing stop
# -------------------------------------------------------------------------
# Classic swing-trade management: once the trail is armed (at TP1, unchanged),
# it ratchets UP as new highs form — distance = k × entry ATR (Chuck LeBeau
# chandelier; 2.5xATR is the standard swing setting, tighter than position
# trading's 3x). Volatility-scaling means it self-adapts across MY small caps
# and US 3x ETFs. NEVER moves back down.
# The high-water mark lags one cycle (trails off *yesterday's* peak) so the
# same-bar high can't tighten the stop and trigger it intrabar — no lookahead.
TRAIL_ATR_MULT = 2.5          # chandelier distance multiplier (swing standard)
TRAIL_FALLBACK_PCT = 3.0      # % below high-water when entry ATR is unknown
TRAIL_MIN_MOVE_PCT = 0.10     # skip persists/log churn for <0.1% improvements


def _profile():
    """Active MarketProfile or a tiny shim with MY defaults."""
    try:
        from market_profiles import active_profile
        return active_profile()
    except Exception:
        # Defensive fallback so this module remains importable in fixtures
        class _Shim:
            code = "MY"
            currency_symbol = "RM"
            currency_iso = "MYR"
            lot_size = 100
            fee_rate = 0.0015
            min_fee = 0.0
            climax_stretch_pct = 20.0   # MY default per FIX #3-2
        return _Shim()


def _ccy() -> str:
    return _profile().currency_symbol


def lot_size() -> int:
    """Active market's board-lot size. MY=100, US=1."""
    return int(_profile().lot_size)


def fee_rate() -> float:
    """Active market's per-side fee rate (proportion, not bps)."""
    return float(_profile().fee_rate)


def estimate_slippage_bps(trade_value: float, avg_daily_value: float | None = None,
                          participation_ratio: float | None = None,
                          avg_daily_rm: float | None = None) -> float:
    """
    Volume-aware slippage estimate (v3, MY-tuned).

    Used by the MY codepath. US uses its own profile.slippage_fn directly
    (see apply_buy_slippage below).

    Args:
        trade_value     — order notional in active currency
        avg_daily_value — 20-day average traded value, same currency
        participation_ratio — alternative to avg_daily_value (0–1)
        avg_daily_rm    — legacy alias for avg_daily_value (kept for
                          backwards compat with v3.3 tests)

    Components
    ----------
    1. base               5 bps   — minimum market-impact + spread half.
    2. size-impact        trade_value / 50_000   — linear in order size.
    3. liquidity penalty  if ADV known, scale ↑ when order is
                          >1% of typical daily volume.

    Always capped at SLIPPAGE_LIQUIDITY_CAP_BPS (80 bps = 0.8%).
    """
    if avg_daily_value is None and avg_daily_rm is not None:
        avg_daily_value = avg_daily_rm

    base = SLIPPAGE_BASE_BPS + (trade_value / SLIPPAGE_K_RM)

    if avg_daily_value is not None and avg_daily_value > 0:
        pr = trade_value / avg_daily_value
    elif participation_ratio is not None:
        pr = max(participation_ratio, 0)
    else:
        pr = 0.0

    liq_bps = min(max(pr, 0.0), 0.10) * 500.0
    return min(base + liq_bps, SLIPPAGE_LIQUIDITY_CAP_BPS)


def _lookup_adv_value(ticker: str | None) -> float | None:
    """
    Trailing 20-day average daily traded value (in the active currency)
    from the most recent scan cache. None if unavailable.
    """
    if not ticker:
        return None
    try:
        from repository import load_scan_cache
        records, _, _ = load_scan_cache()
        for r in records:
            if r.get("ticker") == ticker:
                vol = r.get("volume") or 0
                price = r.get("price") or 0
                if vol and price:
                    return float(vol) * float(price)
        return None
    except Exception:
        return None


def apply_buy_slippage(price: float, shares: int,
                       ticker: str | None = None) -> tuple[float, float]:
    """Worsen the fill on a buy. Returns (filled_price, slippage_pct).

    Dispatches:
      MY → estimate_slippage_bps() (Bursa-tuned, capped 80 bps)
      US → profile.slippage_fn (tighter, ETF-tuned ~2-35 bps)
    """
    prof = _profile()
    adv = _lookup_adv_value(ticker)
    if prof.code == "MY":
        bps = estimate_slippage_bps(price * shares, avg_daily_value=adv)
        slip = bps / 10_000.0
        return price * (1 + slip), slip * 100
    # Non-MY: use profile slippage_fn (signed absolute price impact)
    slip_abs = float(prof.slippage_fn(price, shares,
                                      adv if adv is not None else 0.0, "BUY"))
    filled = price + slip_abs
    slip_pct = (filled / price - 1.0) * 100 if price > 0 else 0.0
    return filled, slip_pct


def apply_sell_slippage(price: float, shares: int,
                        ticker: str | None = None) -> tuple[float, float]:
    prof = _profile()
    adv = _lookup_adv_value(ticker)
    if prof.code == "MY":
        bps = estimate_slippage_bps(price * shares, avg_daily_value=adv)
        slip = bps / 10_000.0
        return price * (1 - slip), slip * 100
    slip_abs = float(prof.slippage_fn(price, shares,
                                      adv if adv is not None else 0.0, "SELL"))
    filled = price + slip_abs    # slippage_fn returns negative for SELL
    slip_pct = (1.0 - filled / price) * 100 if price > 0 else 0.0
    return filled, slip_pct


def calculate_trade_cost(shares: int, price: float) -> dict:
    gross = shares * price
    fee = gross * fee_rate()
    return {"gross": gross, "fee": fee, "total": gross + fee}


def round_to_lot(shares: int) -> int:
    """Floor shares down to nearest board-lot. Returns 0 if below lot_size."""
    lot = lot_size()
    if shares < lot:
        return 0
    return int((shares // lot) * lot)


# -------------------------------------------------------------------------
# ENTRY
# -------------------------------------------------------------------------

def execute_entry(ticker, name, sector, entry_price, stop_loss,
                  tp1, tp2, tp3, signal_type, shares, analysis_data,
                  market_regime, confidence_score,
                  execution_type: str = "MANUAL",
                  actor: str = "USER") -> tuple[bool, int | None, str]:
    """
    Place a paper trade.

    Validates:
      * Positive sane prices, SL < entry
      * Shares is a positive multiple of lot_size (auto-round down)
      * Sufficient cash for slippage-adjusted fill + fee
    """
    ccy = _ccy()
    lot = lot_size()

    if entry_price <= 0 or stop_loss <= 0 or entry_price <= stop_loss:
        return False, None, "Invalid entry/stop prices."

    shares = round_to_lot(int(shares))
    if shares <= 0:
        return False, None, f"Position too small (< {lot} share lot)."

    fill_price, slip_pct = apply_buy_slippage(entry_price, shares, ticker=ticker)
    gross = fill_price * shares
    fee = gross * fee_rate()
    total_outlay = gross + fee

    acc = load_account()
    cash = acc["cash_balance"]
    if total_outlay > cash + 0.01:
        return False, None, (f"Insufficient cash. Need {ccy} {total_outlay:,.2f} "
                             f"(incl. {ccy} {fee:.2f} fee + {slip_pct:.2f}% slip), "
                             f"have {ccy} {cash:,.2f}.")

    risk_per_share = round(fill_price - stop_loss, 4)
    trade = {
        "ticker": ticker, "name": name, "sector": sector,
        "signal_type": signal_type,
        "entry_price": round(fill_price, 3),
        "stop_loss": round(float(stop_loss), 3),
        "tp1": round(float(tp1), 3),
        "tp2": round(float(tp2), 3),
        "tp3": round(float(tp3), 3),
        "shares": int(shares), "lots": int(shares // lot) if lot > 0 else int(shares),
        "cost": round(gross, 2), "fee": round(fee, 2),
        "total_outlay": round(total_outlay, 2),
        "risk_per_share": risk_per_share,
        "actual_risk_pct": round((fill_price - stop_loss) / fill_price * 100, 2),
        "status": "ACTIVE", "phase": "FULL", "outcome": None,
        "logged_at": myt_iso(),
        "execution_type": execution_type,
        "market_regime": market_regime.get("regime_data", {}).get("regime", "UNKNOWN"),
        "regime_conviction": market_regime.get("position_rules", {}).get("conviction_pct", 0),
        "confidence_score": float(confidence_score),
        "entry_reasoning": analysis_data.get("reasoning", ""),
        "entry_indicators": {
            "rsi": analysis_data.get("rsi", 0),
            "vol_ratio": analysis_data.get("vol_ratio", 0),
            "atr": analysis_data.get("atr", 0),
            "support": analysis_data.get("support", 0),
            "resistance": analysis_data.get("resistance", 0),
            "macd_hist": analysis_data.get("macd_hist", 0),
            "ema_trend_distance": round(
                (fill_price - analysis_data.get("ema_trend", fill_price)) /
                analysis_data.get("ema_trend", fill_price) * 100, 2)
            if analysis_data.get("ema_trend") else 0,
        },
        "highest_price": round(fill_price, 3),
        "lowest_price": round(fill_price, 3),
        "mae_pct": 0.0, "mfe_pct": 0.0,
        "unrealized_pnl": 0.0, "realized_pnl": 0.0,
        "shares_remaining": int(shares),
        "slippage_pct": round(slip_pct, 4),
        "tags": [market_regime.get("regime_data", {}).get("regime", "UNKNOWN")],
    }
    trade_id = insert_trade(trade)
    save_account(cash_balance=cash - total_outlay)

    log_trade_event(
        "ENTRY_EXECUTED", trade_id=trade_id, ticker=ticker, actor=actor,
        payload={
            "fill_price": fill_price, "slippage_pct": slip_pct,
            "shares": shares, "gross": gross, "fee": fee,
            "cash_after": cash - total_outlay,
            "signal_type": signal_type, "confidence": confidence_score,
            "execution_type": execution_type,
        },
    )

    # v3.1: live trigger hook (safe — swallows all exceptions)
    try:
        from live_trigger import fire as _live_fire
        _live_fire("ENTRY", trade_id=trade_id, ticker=ticker,
                   actor=actor, payload={"fill_price": fill_price,
                                          "shares": shares})
    except Exception:
        pass

    # v3.7 fix: backup immediately on entry so open positions survive
    # app restarts / Streamlit Cloud redeployments.
    # Previously only closed trades triggered a backup — open trades
    # were only saved on the hourly heartbeat and lost if the app was
    # deleted/redeployed between heartbeats (as happened with NVIDIA).
    try:
        from persistence import backup as _pers_backup, is_configured
        if is_configured():
            _pers_backup(reason=f"entry trade #{trade_id} {ticker}")
    except Exception:
        pass

    # v3.6: real-broker mirror — fires only when broker_mode is SIMULATE/REAL.
    # NOOP returns immediately. Silent failure is OK; reconciliation will catch drift.
    try:
        from broker_adapter import mirror_entry_to_broker
        mirror_entry_to_broker(
            ticker=ticker, shares=shares, fill_price=fill_price,
            stop_loss=stop_loss, tp1=tp1, trade_id=trade_id,
        )
    except Exception as e:
        log.warning(f"broker mirror_entry failed (non-fatal): {e}")

    return True, trade_id, (
        f"Entry executed: {shares} shares of {ticker} @ {ccy} {fill_price:.3f} "
        f"(slip {slip_pct:.2f}%). Total outlay {ccy} {total_outlay:,.2f}. "
        f"SL {ccy} {stop_loss:.3f} | TP1 {ccy} {tp1:.3f} | TP2 {ccy} {tp2:.3f} | TP3 {ccy} {tp3:.3f}.")


# -------------------------------------------------------------------------
# EXITS
# -------------------------------------------------------------------------

def execute_partial_exit(trade_id: int, tp_level: str, current_price: float,
                         shares_to_close: int, reason: str = "Partial TP exit",
                         actor: str = "USER") -> tuple[bool, str]:
    ccy = _ccy()
    t = get_trade(trade_id)
    if t is None:
        return False, "Trade not found."
    if t["status"] != "ACTIVE":
        return False, f"Trade is {t['status']}, cannot partially exit."

    shares_to_close = round_to_lot(min(shares_to_close, t["shares_remaining"]))
    if shares_to_close <= 0:
        return False, "Nothing to close (lot-size rounding)."

    fill_price, slip_pct = apply_sell_slippage(current_price, shares_to_close, ticker=t['ticker'])
    gross = fill_price * shares_to_close
    fee = gross * fee_rate()
    net_proceeds = gross - fee
    entry = t["entry_price"]
    # Proportional entry fee already paid at open — must be netted off P&L
    entry_fee_per_share = (t.get("fee") or 0) / max(t.get("shares") or 1, 1)
    entry_fee_share = entry_fee_per_share * shares_to_close
    pnl = (fill_price - entry) * shares_to_close
    net_pnl = pnl - fee - entry_fee_share

    insert_partial_exit(trade_id, {
        "tp_level": tp_level, "shares_closed": shares_to_close,
        "exit_price": round(fill_price, 3),
        "pnl_rm": round(pnl, 2),
        "net_pnl_after_fees": round(net_pnl, 2),
        "exit_at": myt_iso(), "reason": reason,
    })

    new_remaining = t["shares_remaining"] - shares_to_close
    new_realized = (t.get("realized_pnl") or 0) + net_pnl
    new_phase = "PARTIAL" if new_remaining > 0 else "CLOSED"

    fields = {
        "shares_remaining": new_remaining,
        "realized_pnl": round(new_realized, 2),
        "phase": new_phase,
    }
    if new_remaining <= 0:
        fields.update({
            "status": "CLOSED",
            "outcome": "WIN" if pnl > 0 else "LOSS",
            "closed_pnl": round(new_realized, 2),
            "exit_price": round(fill_price, 3),
            "closed_at": myt_iso(),
        })
    update_trade(trade_id, fields)

    # Cash: we receive net_proceeds (gross − fee)
    acc = load_account()
    save_account(cash_balance=acc["cash_balance"] + net_proceeds)

    log_trade_event(
        "PARTIAL_EXIT", trade_id=trade_id, ticker=t["ticker"], actor=actor,
        payload={"tp_level": tp_level, "shares_closed": shares_to_close,
                 "fill_price": fill_price, "slippage_pct": slip_pct,
                 "pnl": pnl, "net_pnl": net_pnl,
                 "shares_remaining": new_remaining,
                 "reason": reason},
    )

    # v3.1: live trigger hook (off by default — user opts in)
    try:
        from live_trigger import fire as _live_fire
        _live_fire("PARTIAL_EXIT", trade_id=trade_id, ticker=t["ticker"],
                   actor=actor,
                   payload={"fill_price": fill_price,
                            "net_pnl": net_pnl, "tp_level": tp_level})
    except Exception:
        pass

    # v3.6: broker mirror (NOOP unless SIMULATE/REAL)
    try:
        from broker_adapter import mirror_exit_to_broker
        mirror_exit_to_broker(
            ticker=t["ticker"], shares=shares_to_close,
            fill_price=fill_price, trade_id=trade_id, kind="PARTIAL",
        )
    except Exception as e:
        log.warning(f"broker mirror_exit (partial) failed (non-fatal): {e}")

    return True, (f"Partial {tp_level}: {shares_to_close} shares @ {ccy} "
                  f"{fill_price:.3f}. Net P&L {ccy} {net_pnl:+.2f}. "
                  f"{new_remaining} shares remaining.")


def execute_full_exit(trade_id: int, current_price: float,
                      reason: str = "Manual close",
                      outcome: str | None = None,
                      actor: str = "USER") -> tuple[bool, str]:
    ccy = _ccy()
    t = get_trade(trade_id)
    if t is None:
        return False, "Trade not found."
    if t["status"] != "ACTIVE":
        return False, f"Trade is {t['status']}."

    shares_to_close = t["shares_remaining"]
    if shares_to_close <= 0:
        return False, "No shares remaining."

    fill_price, slip_pct = apply_sell_slippage(current_price, shares_to_close, ticker=t['ticker'])
    gross = fill_price * shares_to_close
    fee = gross * fee_rate()
    net_proceeds = gross - fee
    entry = t["entry_price"]
    # Proportional entry fee already paid at open — must be netted off P&L
    entry_fee_per_share = (t.get("fee") or 0) / max(t.get("shares") or 1, 1)
    entry_fee_share = entry_fee_per_share * shares_to_close
    pnl = (fill_price - entry) * shares_to_close
    net_pnl = pnl - fee - entry_fee_share

    new_realized = (t.get("realized_pnl") or 0) + net_pnl
    if outcome is None:
        if pnl > 0:
            outcome = "WIN"
        elif pnl < -(t.get("cost", 1) * 0.01):
            outcome = "LOSS"
        else:
            outcome = "BREAKEVEN"

    # v3.7: determine exit_type from reason string for learning quality analysis
    reason_upper = (reason or "").upper()
    if "STOP LOSS" in reason_upper or "SL HIT" in reason_upper:
        exit_type = "SL"
    elif "TARGET 3" in reason_upper or " TP3" in reason_upper:
        exit_type = "TP3"
    elif "TARGET 2" in reason_upper or " TP2" in reason_upper:
        exit_type = "TP2"
    elif "TARGET 1" in reason_upper or " TP1" in reason_upper:
        exit_type = "TP1"
    elif "CLIMAX" in reason_upper:
        exit_type = "CLIMAX"
    elif "FORCE-FLAT" in reason_upper or "FLAT BY" in reason_upper:
        exit_type = "TIME"
    else:
        exit_type = "MANUAL"

    update_trade(trade_id, {
        "shares_remaining": 0, "phase": "CLOSED",
        "status": "CLOSED", "outcome": outcome,
        "realized_pnl": round(new_realized, 2),
        "closed_pnl": round(new_realized, 2),
        "exit_price": round(fill_price, 3),
        "exit_type": exit_type,           # v3.7: exit quality tracking
        "closed_at": myt_iso(),
        "notes": (t.get("notes", "") or "") + f" | Exit: {reason} [{exit_type}]",
    })

    acc = load_account()
    save_account(cash_balance=acc["cash_balance"] + net_proceeds)

    log_trade_event(
        "FULL_EXIT", trade_id=trade_id, ticker=t["ticker"], actor=actor,
        payload={"fill_price": fill_price, "slippage_pct": slip_pct,
                 "pnl": pnl, "net_pnl": net_pnl,
                 "outcome": outcome, "reason": reason},
    )

    # v3.1: live trigger hook — map reason -> event type
    try:
        from live_trigger import fire as _live_fire
        reason_upper = (reason or "").upper()
        if "STOP LOSS" in reason_upper or "SL HIT" in reason_upper:
            ev = "STOP_LOSS"
        elif "TRAILING" in reason_upper:
            ev = "TRAILING_STOP"
        else:
            ev = "FULL_EXIT"
        _live_fire(ev, trade_id=trade_id, ticker=t["ticker"],
                   actor=actor,
                   payload={"fill_price": fill_price,
                            "net_pnl": net_pnl,
                            "outcome": outcome, "reason": reason})
    except Exception:
        pass

    # v3.6: broker mirror (NOOP unless SIMULATE/REAL)
    try:
        from broker_adapter import mirror_exit_to_broker
        mirror_exit_to_broker(
            ticker=t["ticker"], shares=shares_to_close,
            fill_price=fill_price, trade_id=trade_id, kind="FULL",
        )
    except Exception as e:
        log.warning(f"broker mirror_exit (full) failed (non-fatal): {e}")

    # v3.1.5: backup DB after every full exit (manual or auto)
    try:
        from persistence import backup as _pers_backup, is_configured
        if is_configured():
            _pers_backup(reason=f"full exit trade #{trade_id} {outcome}")
    except Exception:
        pass

    return True, (f"Closed {t['ticker']} @ {ccy} {fill_price:.3f}. "
                  f"Net P&L {ccy} {net_pnl:+.2f} ({outcome}).")


# -------------------------------------------------------------------------
# AUTO-SETTLE LOOP (called by scheduler)
# -------------------------------------------------------------------------

def auto_settle_trades(price_lookup: dict, market_regime: dict,
                       actor: str = "AGENT") -> dict:
    """
    Idempotent settlement.

    price_lookup: {ticker: {"price": float, "high": float, "low": float}}
    """
    regime = market_regime.get("regime_data", {}).get("regime", "NEUTRAL")
    max_hold_days = {"BULL": 14, "NEUTRAL": 7, "BEAR": 5}.get(regime, 7)

    settled, partials = [], []

    for t in active_trades():
        ticker = t["ticker"]
        if ticker not in price_lookup:
            continue
        px = price_lookup[ticker]
        current_price = float(px["price"])
        high_today = float(px.get("high", current_price))
        low_today = float(px.get("low", current_price))

        entry = t["entry_price"]
        sl = t["stop_loss"]
        tp1, tp2, tp3 = t["tp1"], t["tp2"], t["tp3"]
        trailing = t.get("trailing_stop")
        # High-water mark EXCLUDING today (trails off yesterday's peak).
        prior_highest = float(t.get("highest_price") or entry)

        # Track MAE/MFE
        highest = max(t.get("highest_price") or entry, high_today)
        lowest = min(t.get("lowest_price") or entry, low_today)
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

        # ----- v3.8: Ratchet the trailing stop (never moves down) -----
        # Distance scales with the stock's own volatility (entry ATR), the
        # same measure used to size SL/TPs at entry. Runs BEFORE today's
        # exit checks so a give-back today fills at the tightened level.
        if trailing is not None:
            try:
                atr = float(
                    (t.get("entry_indicators") or {}).get("atr") or 0)
            except (TypeError, ValueError):
                atr = 0.0
            if atr > 0:
                dist, basis = TRAIL_ATR_MULT * atr, f"{TRAIL_ATR_MULT}xATR"
            else:
                dist = prior_highest * TRAIL_FALLBACK_PCT / 100.0
                basis = f"{TRAIL_FALLBACK_PCT}% fixed"
            candidate = round(prior_highest - dist, 3)
            min_move = trailing * TRAIL_MIN_MOVE_PCT / 100.0
            if candidate >= trailing + min_move:
                update_trade(t["id"], {"trailing_stop": candidate})
                log_trade_event(
                    "TRAIL_RATCHET", trade_id=t["id"], ticker=ticker,
                    actor=actor,
                    payload={"trailing_was": trailing,
                             "trailing_stop": candidate,
                             "high_water": prior_highest, "basis": basis})
                trailing = candidate  # exit checks below use tightened level

        # ----- Exit conditions (priority order) -----

        # Climax Run: price stretches too far above 50-day EMA
        # FIX #3-2: Threshold now comes from active profile (US=30%, MY=20%).
        # US 3x ETFs regularly stretch 25-40% in strong trends — 20% was too tight.
        ema50 = px.get("ema50")
        if ema50 is not None:
            stretch_pct = (current_price - ema50) / ema50 * 100
            climax_thresh = float(getattr(_profile(), "climax_stretch_pct", 20.0))
            if stretch_pct >= climax_thresh:
                # Fix: compute outcome from actual price vs entry (same as time exit),
                # not hardcoded "WIN" — a climax at a lower price is a loss.
                climax_outcome = "WIN" if current_price > entry else \
                                 ("BREAKEVEN" if abs(current_price - entry) / entry < 0.005 else "LOSS")
                ok, msg = execute_full_exit(
                    t["id"], current_price,
                    reason=f"Climax Run Exit: Price stretched {stretch_pct:.1f}% above 50-day EMA",
                    outcome=climax_outcome, actor=actor)
                if ok:
                    settled.append({"trade_id": t["id"], "type": "CLIMAX", "msg": msg,
                                    "ticker": ticker, "outcome": climax_outcome})
                continue

        # 1. TP3
        if high_today >= tp3:
            ok, msg = execute_full_exit(t["id"], tp3,
                                        reason="TP3 hit", outcome="WIN",
                                        actor=actor)
            if ok:
                settled.append({"trade_id": t["id"], "type": "TP3", "msg": msg,
                                "ticker": ticker, "outcome": "WIN"})
            continue

        # 2. Trailing stop
        if trailing is not None and low_today <= trailing:
            outcome = "WIN" if trailing > entry else \
                      ("BREAKEVEN" if abs(trailing - entry) / entry < 0.005
                       else "LOSS")
            ok, msg = execute_full_exit(
                t["id"], trailing,
                reason=f"Trailing stop hit @ {trailing:.3f}",
                outcome=outcome, actor=actor)
            if ok:
                settled.append({"trade_id": t["id"], "type": "TRAIL", "msg": msg,
                                "ticker": ticker, "outcome": outcome})
            continue

        # 3. Hard stop
        if low_today <= sl:
            ok, msg = execute_full_exit(t["id"], sl,
                                        reason="Hard SL hit", outcome="LOSS",
                                        actor=actor)
            if ok:
                settled.append({"trade_id": t["id"], "type": "SL", "msg": msg,
                                "ticker": ticker, "outcome": "LOSS"})
            continue

        # 4. TP2 — partial 50%, set trailing if not yet set
        if high_today >= tp2 and t.get("phase") == "FULL":
            shares_part = round_to_lot(t["shares_remaining"] // 2)
            if shares_part > 0:
                ok, msg = execute_partial_exit(
                    t["id"], "TP2", tp2, shares_part,
                    reason="50% partial at TP2 — runner kept",
                    actor=actor)
                if ok:
                    partials.append({"trade_id": t["id"], "ticker": ticker,
                                     "shares": shares_part, "msg": msg})
                t = get_trade(t["id"])

        # 5. TP1 — set trailing stop (once)
        if high_today >= tp1 and t.get("trailing_stop") is None:
            # v3.8: honour the risk-manager param (was hardcoded 0.5)
            try:
                from risk_manager import load_risk_params
                buffer_pct = float(
                    load_risk_params().get("trailing_stop_buffer_pct", 0.5))
            except Exception:
                buffer_pct = 0.5
            new_trail = max(entry * (1 + buffer_pct / 100),
                            current_price * (1 - buffer_pct / 100))
            update_trade(t["id"], {"trailing_stop": round(new_trail, 3)})
            log_trade_event(
                "TRAIL_SET", trade_id=t["id"], ticker=ticker, actor=actor,
                payload={"trailing_stop": new_trail, "tp1": tp1},
            )

        # 6. Time exit
        try:
            logged = pd.to_datetime(t["logged_at"])
            if logged.tzinfo:
                logged = logged.tz_localize(None)
            days_held = (get_myt_now().replace(tzinfo=None) - logged).days
            if days_held >= max_hold_days and t.get("phase") == "FULL":
                outcome = "WIN" if current_price > entry else "LOSS"
                ok, msg = execute_full_exit(
                    t["id"], current_price,
                    reason=f"Max hold {max_hold_days}d reached",
                    outcome=outcome, actor=actor)
                if ok:
                    settled.append({"trade_id": t["id"], "type": "TIME",
                                    "msg": msg, "ticker": ticker,
                                    "outcome": outcome})
        except Exception as e:
            log.warning(f"time-exit calc failed for {t['id']}: {e}")

    # Update equity = cash + sum(active position market values)
    acc = load_account()
    active_val = 0.0
    for t in active_trades():
        px = price_lookup.get(t["ticker"], {}).get("price", t["entry_price"])
        active_val += float(px) * t["shares_remaining"]
    save_account(total_equity=acc["cash_balance"] + active_val)

    return {
        "settled": settled, "partials": partials,
        "cash_balance": round(acc["cash_balance"], 2),
        "total_equity": round(acc["cash_balance"] + active_val, 2),
    }


# -------------------------------------------------------------------------
# Convenience helpers
# -------------------------------------------------------------------------

def add_trade_note(trade_id: int, note: str) -> bool:
    t = get_trade(trade_id)
    if not t:
        return False
    new = (t.get("notes") or "") + f"\n[{myt_iso()}] {note}"
    update_trade(trade_id, {"notes": new})
    return True


def tag_trade(trade_id: int, tag: str) -> bool:
    t = get_trade(trade_id)
    if not t:
        return False
    tags = t.get("tags") or []
    if tag not in tags:
        tags.append(tag)
        update_trade(trade_id, {"tags": tags})
    return True


# ===========================================================================
# v3.5 — Corporate-action adjustment (splits & bonus issues)
# ===========================================================================
# (Unchanged from v3.5 — purely numeric, market-agnostic.)

from typing import Optional


_PRICE_FIELDS_INVERSE = (
    "entry_price",
    "stop_loss",
    "tp1", "tp2", "tp3",
    "trailing_stop",
    "highest_price",
    "lowest_price",
    "exit_price",
    "risk_per_share",
)

_SHARE_FIELDS_FORWARD = (
    "shares",
    "shares_remaining",
    "lots",
)


def apply_split_to_trade(trade_id: int, ratio: float,
                         ex_date: str,
                         note: Optional[str] = None) -> dict:
    """
    Atomically adjust one trade for a stock split (or bonus issue).
    See PROJECT_HANDBOOK §4.19 for rationale.
    """
    if ratio is None or ratio <= 0:
        raise ValueError(f"ratio must be positive, got {ratio!r}")
    if abs(ratio - 1.0) < 1e-9:
        raise ValueError(f"ratio=1.0 is a no-op, refusing to apply")

    from db import connect, myt_iso

    with connect() as c:
        row = c.execute(
            "SELECT * FROM trades WHERE id=?", (trade_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"trade_id={trade_id} not found")

        before = dict(row)

        if before["status"] != "ACTIVE":
            raise ValueError(
                f"trade_id={trade_id} status={before['status']!r}; "
                f"can only adjust ACTIVE trades"
            )

        after: dict = {}

        for f in _PRICE_FIELDS_INVERSE:
            v = before.get(f)
            if v is None:
                after[f] = None
                continue
            try:
                after[f] = round(float(v) / ratio, 4)
            except (TypeError, ValueError):
                after[f] = v

        for f in _SHARE_FIELDS_FORWARD:
            v = before.get(f)
            if v is None:
                after[f] = None
                continue
            try:
                after[f] = int(round(float(v) * ratio))
            except (TypeError, ValueError):
                after[f] = v

        prev_factor = float(before.get("cumulative_split_factor") or 1.0)
        after["cumulative_split_factor"] = round(prev_factor * ratio, 6)

        # v3.8: price-unit entry indicators must scale with the split too —
        # the chandelier trail distances itself by entry ATR, which is an
        # absolute price quantity (RM). Ratio-adjusted prices with an
        # unadjusted ATR would mis-size the trail by the split ratio.
        try:
            import json as _json
            inds = _json.loads(before.get("entry_indicators_json") or "{}")
            if isinstance(inds, dict):
                for key in ("atr", "support", "resistance", "ema_trend"):
                    v = inds.get(key)
                    try:
                        if v is not None and float(v) != 0.0:
                            inds[key] = round(float(v) / ratio, 6)
                    except (TypeError, ValueError):
                        pass
                after["entry_indicators_json"] = _json.dumps(inds, default=str)
        except Exception:
            pass  # never let indicator cosmetics block a real adjustment

        old_notes = before.get("notes") or ""
        audit_note = (
            f"[{myt_iso()}] v3.5 SPLIT applied: ratio={ratio:g} ex_date={ex_date} "
            f"(shares {before['shares']} → {after['shares']}, "
            f"entry_price {before['entry_price']} → {after['entry_price']})"
        )
        if note:
            audit_note += f" | {note}"
        after["notes"] = (old_notes + "\n" + audit_note).lstrip("\n")

        before_basis = float(before["entry_price"]) * float(before["shares"])
        after_basis = float(after["entry_price"]) * float(after["shares"])
        cash_invariant_delta = round(after_basis - before_basis, 2)
        if abs(cash_invariant_delta) > 1.00:
            raise ValueError(
                f"cash-invariant violation: basis changed by "
                f"{cash_invariant_delta:.2f} (before={before_basis:.2f}, "
                f"after={after_basis:.2f}). Refusing to apply split."
            )

        set_clauses = []
        values = []
        for f, v in after.items():
            set_clauses.append(f"{f}=?")
            values.append(v)
        values.append(trade_id)
        sql = f"UPDATE trades SET {', '.join(set_clauses)} WHERE id=? AND status='ACTIVE'"

        cur = c.execute(sql, values)
        if cur.rowcount == 0:
            raise ValueError(
                f"trade_id={trade_id} no longer ACTIVE (closed mid-adjustment); "
                f"rolled back"
            )

    return {
        "trade_id": trade_id,
        "ratio": ratio,
        "ex_date": ex_date,
        "before": {f: before.get(f) for f in _PRICE_FIELDS_INVERSE + _SHARE_FIELDS_FORWARD},
        "after": {f: after.get(f) for f in _PRICE_FIELDS_INVERSE + _SHARE_FIELDS_FORWARD},
        "cumulative_split_factor": after["cumulative_split_factor"],
        "cash_invariant_delta_rm": cash_invariant_delta,
    }
