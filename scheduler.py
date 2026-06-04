# scheduler.py
"""
Background scheduler — Robo-Trader.

Design
------
Spawns a daemon thread inside the Streamlit process when the app boots.
The thread:
  1. Sleeps until the next cadence boundary (hourly).
  2. Wakes during market hours, runs scan → auto-settle → bookkeeping.
  3. Records HEARTBEAT every cycle into scheduler_log.
  4. Honours the `kill_switch` flag in scheduler_state.
  5. Exits cleanly when `running = 0` is set.

Thread-safety: any one Python process should run at most ONE scheduler
thread. We enforce this with a module-global handle + an atomic
INSERT-OR-UPDATE on scheduler_state.

If the user wants a real production setup, they can run
`python -m scheduler --daemon` from cron / Task Scheduler — the same
loop code is reused.

v3.2 — Simplified scheduler lifecycle
--------------------------------------
Previous versions (v3.1.8–v3.1.11) accumulated layers of patches for
ghost-thread / zombie-thread issues. Each patch addressed one symptom
but introduced new edge cases:

  - v3.1.8: silent ghost exit + conservative ensure_started
  - v3.1.9: crash-recoverable start guards
  - v3.1.10: orphan registry + watchdog
  - v3.1.11: ensure_watchdog_running from ADOPT_THREAD path

Root cause found: the ADOPT_THREAD path in start() adopted a still-alive
thread but never wrote `running=1, kill_switch=0` to the DB. The adopted
thread would read `kill_switch=1` (set by the preceding stop()) and
self-terminate, leaving the scheduler permanently STOPPED.

v3.2 simplifies the entire lifecycle:

  - start(): orphan ALL stale threads, then spawn fresh. No ADOPT_THREAD.
  - stop(): does NOT set kill_switch (only the Kill-Switch button does).
  - ensure_started(): just `if not is_running(): start()`. No 5-case tree.
  - force_restart(): just `stop(); start()`.
  - is_running(): _THREAD alive + not orphaned + DB running=1.

The watchdog and _loop internals are unchanged — they were correct.
"""

from __future__ import annotations
import os
import sys
import time
import threading
import traceback
from datetime import datetime, timezone, timedelta

import pandas as pd

from db import get_myt_now, myt_iso
from repository import (
    get_scheduler_state, update_scheduler_state, save_scan_cache,
    active_trades, load_account, save_account,
)
from logger import (
    log_scheduler_event, log_learning_event, get_logger, prune_logs,
)
from risk_manager import check_trading_time_window, run_full_risk_check

log = get_logger("scheduler")

# Module-global thread handle (single-process invariant)
_THREAD: threading.Thread | None = None
_LOCK = threading.RLock()
_STOP_EVENT = threading.Event()

# Registry of thread idents that stop() has asked to exit but that may
# still be alive (stuck in a sleep / blocking I/O). start() must skip
# these to avoid adopting dying threads.
_ORPHANED_THREAD_IDS: set[int] = set()

# Watchdog thread — detects runaway cycles and forces clean handoff.
_WATCHDOG_THREAD: threading.Thread | None = None
_WATCHDOG_STOP_EVENT = threading.Event()

# Tunable knobs
WATCHDOG_TICK_SEC = 60                  # how often the watchdog checks
WATCHDOG_CYCLE_TIMEOUT_SEC = 600        # 10 min — cycle is "runaway"
WATCHDOG_TIMEOUT_OWNER_SENTINEL = -1    # forces owner_pid mismatch
CYCLE_DURATION_WARN_SEC = 300           # 5 min — soft warn, no action


def _gc_orphaned_thread_ids() -> None:
    """Remove idents from the orphan set whose thread has actually died."""
    if not _ORPHANED_THREAD_IDS:
        return
    alive_idents = {t.ident for t in threading.enumerate() if t.is_alive()}
    _ORPHANED_THREAD_IDS.intersection_update(alive_idents)


# -------------------------------------------------------------------------
# Cadence helpers
# -------------------------------------------------------------------------

def _next_run_at(interval_sec: int) -> datetime:
    """Next top-of-hour (if interval = 3600) or now + interval."""
    now = get_myt_now()
    if interval_sec == 3600:
        nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        nxt = now + timedelta(seconds=interval_sec)
    return nxt


def _is_market_hours() -> bool:
    """
    True when the exchange is open for trading (scans + exits should run).
    Uses is_market_open() from market_calendar — NOT check_trading_time_window(),
    which also blocks on the safe-entry window (16:00+). The safe-entry check
    is applied separately inside _run_one_cycle for new entries only.
    """
    from market_calendar import is_market_open
    return is_market_open()


def _run_corporate_actions_step(summary: dict) -> None:
    """
    v3.5 — Run the corporate-actions scan/adjust step at the start of a cycle.

    Mutates `summary` in-place to add corp_actions_* counts. Catches all
    exceptions internally — this step MUST NOT abort the trading cycle.
    """
    try:
        from corporate_actions import process_corporate_actions
        state_now = get_scheduler_state()
        autoadjust = bool(state_now.get("corp_action_autoadjust", 1))
        last_scan = state_now.get("last_corp_action_scan_at")

        ca_summary = process_corporate_actions(
            autoadjust=autoadjust,
            last_scan_iso=last_scan,
            actor="SCHEDULER",
        )

        summary["corp_actions_detected"] = ca_summary["events_detected"]
        summary["corp_actions_adjusted"] = ca_summary["splits_adjusted"]
        summary["corp_actions_failed"] = ca_summary["failures"]

        # Persist the scan timestamp so next cycle's window rolls forward.
        update_scheduler_state(last_corp_action_scan_at=myt_iso())

        if ca_summary["events_detected"] > 0:
            log_scheduler_event(
                "CORP_ACTIONS",
                (f"detected={ca_summary['events_detected']} "
                 f"adjusted={ca_summary['splits_adjusted']} "
                 f"alerted={ca_summary['splits_alerted_only']} "
                 f"divs={ca_summary['dividends_alerted']} "
                 f"dupes={ca_summary['skipped_dupes']} "
                 f"failed={ca_summary['failures']}"),
                payload={
                    "window": ca_summary["scan_window"],
                    "autoadjust": autoadjust,
                    # Cap details to keep scheduler_log row size sane.
                    "details": ca_summary["details"][:20],
                },
            )
    except Exception as e:
        # Corporate-action failure must NEVER abort the trading cycle.
        log_scheduler_event(
            "CORP_ACTIONS_ERROR",
            f"process_corporate_actions raised: {e}",
            "ERROR",
            payload={"traceback": traceback.format_exc()},
        )


def _run_reconciliation_step(summary: dict) -> None:
    """
    v3.6 Block 6 — Compare internal state to broker state, log drift.

    Runs at the END of each cycle (after all settles + entries) so the
    snapshot reflects the cycle's actual outcome. Skipped automatically
    when broker_mode == NOOP or the market has no broker support.

    Mutates `summary` to add reconcile_* keys. Catches all exceptions
    internally — reconciliation MUST NOT abort the trading cycle.
    """
    try:
        from reconciliation import run_reconciliation
        rec = run_reconciliation(alert_on_drift=True)
        summary["reconcile_ran"] = rec.ran
        summary["reconcile_drift_flagged"] = rec.drift_flagged
        summary["reconcile_equity_drift_pct"] = rec.equity_drift_pct
        summary["reconcile_position_diffs"] = len(rec.position_diffs)
        if not rec.ran:
            summary["reconcile_skip_reason"] = rec.reason_skipped
    except Exception as e:
        log_scheduler_event(
            "RECONCILE_ERROR",
            f"run_reconciliation raised: {e}",
            "ERROR",
            payload={"traceback": traceback.format_exc()},
        )


def _explain_cycle_outcome(summary: dict, df, regime: dict,
                            threshold: float, active_count: int,
                            max_positions: int,
                            autotrade_enabled: bool) -> str:
    """
    Build a human-readable explanation of why a cycle ended with zero
    new entries. Returns a short sentence ready to log.

    Order of checks matches the actual cycle flow so the reason
    reported is the FIRST blocking condition encountered.
    """
    regime_name = (regime or {}).get("regime_data", {}).get("regime", "?")
    conv = (regime or {}).get("position_rules", {}).get("conviction_pct", 0)

    # 1. Auto-trade disabled
    if not autotrade_enabled:
        return ("Auto-entry is OFF. Toggle it on in 🤖 Robo-Trader tab "
                "to let the agent open positions.")

    # 2. Scan returned nothing
    if df is None or len(df) == 0:
        return ("Scanner returned 0 results — likely a data-source outage. "
                "Check 📜 Logs → Data Quality.")

    # 3. Count GOLD BUYs at all
    gold_buys_all = df[df["signal"].astype(str).str.contains(
        "GOLD BUY", regex=False)]
    if len(gold_buys_all) == 0:
        return (f"No GOLD BUY signals found across {len(df)} scanned stocks "
                f"in {regime_name} regime. Market may be in distribution / "
                "no breakout or pullback setups today.")

    # 4. Above-threshold qualifiers
    qualifiers = gold_buys_all[gold_buys_all["confidence"] >= threshold]
    if len(qualifiers) == 0:
        best_conf = float(gold_buys_all["confidence"].max())

        trend_note = ""
        try:
            from repository import get_regime_trend
            trend = get_regime_trend(lookback_hours=24)
            if trend["samples"] >= 2:
                if regime_name == "BEAR":
                    if trend["direction"] == "WEAKENING":
                        feel = "weakening (good — entries may resume soon)"
                    elif trend["direction"] == "STRENGTHENING":
                        feel = "strengthening (BEAR getting deeper)"
                    else:
                        feel = "stable"
                elif regime_name == "BULL":
                    feel = trend["direction"].lower()
                else:
                    feel = trend["direction"].lower()

                trend_note = (
                    f" Regime conviction: {trend['current_conviction']:.0f}% "
                    f"(was {trend['avg_recent_conviction']:.0f}% avg over last "
                    f"24h → {feel})."
                )
                if trend["ema_200_distance_pct"] is not None:
                    dist = trend["ema_200_distance_pct"]
                    if dist < 0:
                        trend_note += (
                            f" KLCI is {abs(dist):.1f}% BELOW its 200-EMA "
                            "— watch for it to cross ABOVE for regime flip."
                        )
                    else:
                        trend_note += (
                            f" KLCI is {dist:.1f}% above its 200-EMA — "
                            "regime should normalize as trend confirms."
                        )
        except Exception:
            pass

        return (f"{len(gold_buys_all)} GOLD BUY signal(s) found, but the "
                f"highest confidence was {best_conf:.0f}/100 — below the "
                f"signal threshold of {threshold:.0f}. "
                f"Agent stays defensive.{trend_note}")

    # 5. Position cap
    if active_count >= max_positions:
        return (f"At max concurrent positions ({active_count}/{max_positions} "
                f"in {regime_name} regime). Wait for existing positions to "
                "close before opening new ones.")

    # 6. All qualifiers already held
    active_tickers = set()
    try:
        from repository import active_trades as _at
        active_tickers = {t["ticker"] for t in _at()}
    except Exception:
        pass
    available = qualifiers[~qualifiers["ticker"].isin(active_tickers)]
    if len(available) == 0:
        return (f"{len(qualifiers)} qualifier(s) found but all already held "
                "in portfolio. No duplicates allowed.")

    # 7. Risk checks blocked them
    if summary.get("rejected", 0) > 0:
        return (f"{summary['rejected']} candidate(s) rejected by risk checks. "
                "Check trade_log for details.")

    return "Unknown reason for zero entries."


# -------------------------------------------------------------------------
# One cycle
# -------------------------------------------------------------------------

def _run_one_cycle(autotrade: bool, autoexit: bool,
                     my_pid: int | None = None) -> dict:
    """One full scan + settle cycle. Returns summary dict."""
    # Ownership check at the very top — if another thread has
    # claimed the scheduler while we were mid-cycle, abort immediately.
    if my_pid is not None:
        state = get_scheduler_state()
        current_owner = state.get("owner_pid", 0) or 0
        if current_owner and current_owner != my_pid:
            log_scheduler_event(
                "CYCLE_ABORT",
                f"PID {my_pid} aborting cycle — owner_pid now {current_owner}",
                "WARN",
            )
            return {"scan_count": 0, "settled": 0, "partials": 0,
                    "auto_entries": 0, "rejected": 0, "errors": [],
                    "aborted": True}

    from screener import screen_all_stocks
    from market_analyzer import get_full_market_analysis
    from trading_engine import auto_settle_trades, execute_entry
    from learner import learn_from_trade_outcome
    from repository import get_trade

    summary = {"scan_count": 0, "settled": 0, "partials": 0,
               "auto_entries": 0, "rejected": 0, "errors": [],
               # v3.5: corporate-action stats added to summary
               "corp_actions_detected": 0, "corp_actions_adjusted": 0,
               "corp_actions_failed": 0}

    # v3.5 Step 0 — Corporate actions (splits/bonuses/dividends).
    # Runs BEFORE regime/scan/settle so that stop-loss math uses post-split
    # prices instead of triggering a false 80%-crash exit on the next cycle.
    _run_corporate_actions_step(summary)

    t0 = time.time()
    log_scheduler_event("SCAN_START", "Starting market scan")

    try:
        regime = get_full_market_analysis(force_refresh=True)
        try:
            from repository import record_regime_snapshot
            rd = regime.get("regime_data", {})
            details = rd.get("details", {})
            record_regime_snapshot(
                regime=rd.get("regime", "UNKNOWN"),
                conviction=rd.get("conviction", 0),
                trend_score=details.get("trend_score"),
                ema_200_vs_price=details.get("ema_200_vs_price"),
                klci_rsi=details.get("klci_rsi"),
            )
        except Exception:
            pass
    except Exception as e:
        regime = {"regime_data": {"regime": "UNCERTAIN"}, "position_rules": {}}
        log_scheduler_event("ERROR", f"Regime fetch failed: {e}", "ERROR")

    try:
        df = screen_all_stocks(market_regime=regime)
        summary["scan_count"] = len(df)
        save_scan_cache(df.to_dict("records") if not df.empty else [], regime)
    except Exception as e:
        log_scheduler_event("ERROR", f"Scan failed: {e}\n{traceback.format_exc()}",
                            "ERROR")
        return summary

    duration = time.time() - t0
    log_scheduler_event("SCAN_END", f"{len(df)} signals", duration_sec=duration,
                        payload={"regime": regime.get("regime_data", {}).get("regime")})

    # ---- Auto-settle existing positions ----
    if autoexit:
        log_scheduler_event("SETTLE_START", "Auto-settling active trades")
        price_lookup = {}
        
        # Populate price_lookup with default prices from scan cache
        if not df.empty:
            for _, row in df.iterrows():
                price_lookup[row["ticker"]] = {
                    "price": float(row["price"]),
                    "high": float(row["price"]),
                    "low": float(row["price"]),
                }
                
        # Fetch true live daily High/Low for active trades to prevent missing intraday moves
        from repository import active_trades
        from data_provider import get_history
        for t in active_trades():
            ticker = t["ticker"]
            try:
                # Fetch 3 months of daily bars to compute the 50-day EMA
                df_t = get_history(ticker, period="3mo", timeout=15)
                if df_t is not None and not df_t.empty:
                    # Calculate 50-day EMA
                    ema50 = None
                    if len(df_t) >= 50:
                        try:
                            ema50 = df_t["Close"].ewm(span=50, adjust=False).mean().iloc[-1]
                        except Exception:
                            pass
                    last_row = df_t.iloc[-1]
                    price_lookup[ticker] = {
                        "price": float(last_row["Close"]),
                        "high": float(last_row["High"]),
                        "low": float(last_row["Low"]),
                        "ema50": float(ema50) if ema50 is not None else None,
                    }
            except Exception as e:
                log.warning(f"Failed to fetch live exit price for {ticker}: {e}")

        try:
            settle_res = auto_settle_trades(price_lookup, regime, actor="AGENT")
            summary["settled"] = len(settle_res.get("settled", []))
            summary["partials"] = len(settle_res.get("partials", []))
            log_scheduler_event(
                "SETTLE_END",
                f"{summary['settled']} settled, {summary['partials']} partials",
                payload=settle_res,
            )

            for ev in settle_res.get("settled", []):
                t = get_trade(ev["trade_id"])
                if t and t.get("status") == "CLOSED":
                    try:
                        learn_from_trade_outcome(t)
                    except Exception as e:
                        log.error(f"learning failed for trade {t['id']}: {e}")
            if settle_res.get("settled"):
                try:
                    from persistence import backup as _pers_backup, is_configured
                    if is_configured():
                        _pers_backup(reason=f"{len(settle_res['settled'])} trade(s) closed")
                except Exception:
                    pass
        except Exception as e:
            log_scheduler_event("ERROR",
                                f"Auto-settle failed: {e}", "ERROR")

    # ---- Auto-entry on best GOLD BUYs (only if enabled) ----
    if autotrade and not df.empty:
        from market_calendar import is_safe_entry_window, current_session
        if not is_safe_entry_window():
            sess = current_session()
            sess_name = sess.name if sess else "outside-session"
            from market_calendar import market_status_text
            ms = market_status_text()
            # v3.6: describe the active market's safe-entry window instead of
            # hardcoding Bursa hours. Shown to a Malaysia-based user, so the
            # helper appends the MYT equivalent for non-MY markets.
            try:
                from market_profiles import active_profile
                from market_profiles.base import format_session_window
                _win = format_session_window(active_profile(), with_user_local=True)
            except Exception:
                _win = "the configured session window"
            log_scheduler_event(
                "AUTO_ENTRY_SKIP",
                f"0 entries — In {sess_name} session "
                f"({ms.get('reason', '')}). "
                f"New auto-entries only fire during {_win} "
                f"(up to the safe-entry cutoff). "
                f"Next opportunity: {ms.get('next_event', '?')}",
                "INFO",
                payload={"reason": "outside_safe_entry_window",
                         "session": sess_name})
            return summary
        from repository import load_trades
        log_scheduler_event("AUTO_ENTRY_START", "Evaluating new entries")
        trades = load_trades()
        active = active_trades()
        active_tickers = {t["ticker"] for t in active}
        acc = load_account()
        cash = acc["cash_balance"]
        # v3.7 fix: use total_equity (cash + market value of positions) for
        # drawdown check, not cash_balance alone. Using cash_balance causes a
        # false drawdown trigger when positions are open — cash drops by the
        # position cost but equity is unchanged (cash + position value = same).
        try:
            _active_mv = sum(
                float(t.get("entry_price", 0)) * int(t.get("shares_remaining", 0))
                for t in active
            )
        except Exception:
            _active_mv = 0.0
        total_equity = cash + _active_mv
        threshold = regime.get("position_rules", {}).get("new_signal_threshold", 0.70) * 100

        gold_buys = df[df["signal"].astype(str).str.contains("GOLD BUY", regex=False)]
        gold_buys = gold_buys[gold_buys["confidence"] >= threshold]
        gold_buys = gold_buys.head(regime.get("position_rules", {})
                                   .get("max_concurrent_positions", 5))

        risk_per_trade_rm = 0.01 * acc["initial_capital"]

        # v3.7 fix: use profile lot size (MY=100, US=1) not hardcoded 100.
        # Hardcoded 100 silently zeroed all US positions ($50 risk / $5 per share
        # = 10 shares → 10//100*100 = 0) causing "Unknown reason for zero entries".
        try:
            from trading_engine import lot_size as _lot_size
            _lot = _lot_size()
        except Exception:
            _lot = 100  # safe fallback for MY

        for _, row in gold_buys.iterrows():
            if row["ticker"] in active_tickers:
                continue
            entry = row["entry"]; sl = row["stop_loss"]
            
            # --- ATR-Based Position Sizing (Turtles & O'Neil Standard) ---
            # Instead of raw entry - sl (which can become dangerously small on tight support),
            # we use ATR * Multiplier to determine the natural volatility-based share count.
            atr = float(row.get("atr", entry * 0.03))
            try:
                from risk_manager import load_risk_params
                atr_mult = float(load_risk_params().get("atr_multiplier_stop", 1.5))
            except Exception:
                atr_mult = 1.5
            vol_risk_per_share = max(atr * atr_mult, 0.001)
            
            target_shares = int(risk_per_trade_rm / vol_risk_per_share)
            target_shares = (target_shares // _lot) * _lot
            if target_shares < max(_lot, 1):
                continue
            actual_cost = target_shares * entry
            risk_check = run_full_risk_check(
                trades, {"ticker": row["ticker"], "sector": row["sector"],
                         "entry": entry, "stop_loss": sl,
                         "cost": actual_cost,
                         "risk_amount": vol_risk_per_share * target_shares},
                total_equity, acc["initial_capital"])
            if not risk_check["pass"]:
                summary["rejected"] += 1
                from logger import log_trade_event
                log_trade_event(
                    "RISK_REJECTED", trade_id=None, ticker=row["ticker"],
                    actor="AGENT",
                    payload={"verdict": risk_check["final_verdict"]})
                try:
                    from live_trigger import fire as _live_fire
                    _live_fire("RISK_REJECTED", trade_id=None,
                               ticker=row["ticker"], actor="AGENT",
                               payload={"verdict": risk_check["final_verdict"]})
                except Exception:
                    pass
                continue
            # Apply regime position-size multiplier (BEAR=0.5, NEUTRAL=0.75, BULL=1.0)
            regime_size_mult = regime.get("position_rules", {}).get(
                "position_size_mult", 1.0)
            sized_shares = int(target_shares * risk_check["size_multiplier"]
                               * regime_size_mult)
            sized_shares = (sized_shares // _lot) * _lot
            if sized_shares < max(_lot, 1):
                continue
            try:
                ok, tid, msg = execute_entry(
                    row["ticker"], row["name"], row["sector"],
                    entry, sl, row["tp1"], row["tp2"], row["tp3"],
                    row["signal"], sized_shares,
                    {"reasoning": row.get("reasoning", ""),
                     "rsi": row.get("rsi"), "vol_ratio": row.get("vol_ratio"),
                     "atr": row.get("atr"), "support": row.get("support"),
                     "resistance": row.get("resistance"),
                     "macd_hist": row.get("macd_hist"),
                     "ema_trend": row.get("ema_trend", entry)},
                    regime, row["confidence"],
                    execution_type="AUTO", actor="AGENT")
                if ok:
                    summary["auto_entries"] += 1
                    cash -= sized_shares * entry * 1.0015
            except Exception as e:
                summary["errors"].append(f"{row['ticker']}: {e}")

        if summary["auto_entries"] == 0:
            reason = _explain_cycle_outcome(
                summary, df, regime, threshold,
                active_count=len(active),
                max_positions=regime.get("position_rules", {})
                                   .get("max_concurrent_positions", 5),
                autotrade_enabled=True,
            )
            log_scheduler_event(
                "AUTO_ENTRY_END",
                f"0 entries — {reason}",
                payload={"reason": reason,
                         "regime": regime.get("regime_data", {}).get("regime"),
                         "threshold": threshold,
                         "scan_count": summary["scan_count"]})
        else:
            log_scheduler_event(
                "AUTO_ENTRY_END",
                f"{summary['auto_entries']} entries, "
                f"{summary['rejected']} rejected")

    elif autotrade:
        log_scheduler_event(
            "AUTO_ENTRY_END",
            "0 entries — Scanner returned 0 results. Likely a data-source "
            "outage; check 📜 Logs → Data Quality.",
            payload={"reason": "empty_scan"})
    else:
        log_scheduler_event(
            "AUTO_ENTRY_END",
            "0 entries — Auto-entry is OFF. Toggle it on in "
            "🤖 Robo-Trader tab to let the agent open positions.",
            payload={"reason": "autotrade_disabled"})

    # v3.6 Block 6 — broker ↔ internal reconciliation (no-op in NOOP mode).
    # Runs at the very end so settlements and entries are already reflected.
    _run_reconciliation_step(summary)

    return summary


# -------------------------------------------------------------------------
# Watchdog
# -------------------------------------------------------------------------

def _watchdog_loop(my_pid: int):
    """
    Wakes every WATCHDOG_TICK_SEC. If a cycle has been running longer than
    WATCHDOG_CYCLE_TIMEOUT_SEC, forces a clean handoff.

    Python ``threading`` cannot interrupt blocking I/O. The watchdog
    only ensures the system *recovers* within WATCHDOG_CYCLE_TIMEOUT_SEC;
    per-call HTTP timeouts are the first line of defence.
    """
    log_scheduler_event(
        "WATCHDOG_STARTED",
        f"Watchdog started (PID {my_pid}, "
        f"timeout={WATCHDOG_CYCLE_TIMEOUT_SEC}s, tick={WATCHDOG_TICK_SEC}s)",
        "INFO",
    )
    while not _WATCHDOG_STOP_EVENT.is_set():
        try:
            state = get_scheduler_state()
            cycle_started = state.get("cycle_started_at")
            scheduler_owner = state.get("owner_pid", 0) or 0

            if cycle_started and scheduler_owner == my_pid:
                try:
                    started_dt = datetime.strptime(
                        cycle_started, "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=timezone(timedelta(hours=8)))
                    age = (get_myt_now() - started_dt).total_seconds()
                except Exception:
                    age = None

                if age is not None and age > WATCHDOG_CYCLE_TIMEOUT_SEC:
                    log_scheduler_event(
                        "CYCLE_TIMEOUT",
                        f"Watchdog: cycle has been running {age:.0f}s "
                        f"(> {WATCHDOG_CYCLE_TIMEOUT_SEC}s limit). "
                        f"Forcing handoff.",
                        "ERROR",
                        payload={"stuck_duration_sec": age,
                                 "owner_pid": scheduler_owner},
                    )
                    update_scheduler_state(
                        cycle_started_at=None,
                        owner_pid=WATCHDOG_TIMEOUT_OWNER_SENTINEL,
                        running=0,
                        last_error=(
                            f"Cycle exceeded {WATCHDOG_CYCLE_TIMEOUT_SEC}s "
                            f"(ran {age:.0f}s). Watchdog forced handoff. "
                            "Likely a data-source / network hang."
                        ),
                    )
                    try:
                        with _LOCK:
                            if (_THREAD is not None
                                    and _THREAD.is_alive()
                                    and _THREAD.ident is not None):
                                _ORPHANED_THREAD_IDS.add(_THREAD.ident)
                    except Exception:
                        pass
        except Exception as e:
            try:
                log_scheduler_event(
                    "WATCHDOG_ERROR", f"{e}", "ERROR")
            except Exception:
                pass

        _WATCHDOG_STOP_EVENT.wait(timeout=WATCHDOG_TICK_SEC)


def _ensure_watchdog_running(my_pid: int) -> None:
    """Idempotent watchdog spawner. Safe to call from any code path."""
    global _WATCHDOG_THREAD
    if (_WATCHDOG_THREAD is not None
            and _WATCHDOG_THREAD.is_alive()):
        return

    is_respawn = _WATCHDOG_THREAD is not None
    _WATCHDOG_STOP_EVENT.clear()
    _WATCHDOG_THREAD = threading.Thread(
        target=_watchdog_loop, args=(my_pid,),
        name="bursa-watchdog", daemon=True,
    )
    _WATCHDOG_THREAD.start()
    if is_respawn:
        try:
            log_scheduler_event(
                "WATCHDOG_RESPAWN",
                f"PID {my_pid}: watchdog had died — respawning",
                "WARN",
            )
        except Exception:
            pass


# Backwards-compat alias
_start_watchdog = _ensure_watchdog_running


def _stop_watchdog() -> None:
    """Signal + join the watchdog thread."""
    global _WATCHDOG_THREAD
    _WATCHDOG_STOP_EVENT.set()
    if _WATCHDOG_THREAD is not None:
        try:
            _WATCHDOG_THREAD.join(timeout=3)
        except Exception:
            pass
    _WATCHDOG_THREAD = None


# -------------------------------------------------------------------------
# Loop
# -------------------------------------------------------------------------

def _loop(interval_sec: int, my_pid: int):
    """The actual background loop. Exits when _STOP_EVENT is set,
    kill_switch is engaged, or a newer process has claimed ownership.

    DEBOUNCE: On fresh startup, we sleep until the next scheduled
    boundary before the first cycle. The user can bypass this via
    "⚡ Run Cycle Now" (calls run_once() directly).
    """
    next_boundary = _next_run_at(interval_sec)
    update_scheduler_state(
        running=1,
        last_heartbeat=myt_iso(),
        next_run_at=myt_iso(next_boundary),
        last_error="",
        owner_pid=my_pid,
        cycle_started_at=None,
    )
    log_scheduler_event(
        "STARTED",
        f"Robo-Trader started (PID {my_pid}, interval {interval_sec}s). "
        f"First cycle deferred to next boundary: "
        f"{next_boundary.strftime('%Y-%m-%d %H:%M:%S')} MYT.",
    )

    # Debounce: sleep until the next scheduled boundary.
    delay = max(0, (next_boundary - get_myt_now()).total_seconds())
    if delay > 0:
        _STOP_EVENT.wait(timeout=delay)

    while not _STOP_EVENT.is_set():
        state = get_scheduler_state()
        if state.get("kill_switch", 0):
            log_scheduler_event("KILLED", "kill_switch=1 — exiting loop", "WARN")
            break

        # Ghost-thread eviction: check ownership and exit if another
        # live owner exists or watchdog sentinel was set.
        current_owner = state.get("owner_pid", 0) or 0
        if current_owner and current_owner != my_pid:
            other_hb = state.get("last_heartbeat")
            other_is_alive = False
            age_sec = None
            if other_hb:
                try:
                    other_dt = datetime.strptime(other_hb, "%Y-%m-%d %H:%M:%S")
                    other_dt = other_dt.replace(tzinfo=timezone(timedelta(hours=8)))
                    age_sec = (get_myt_now() - other_dt).total_seconds()
                    other_is_alive = (age_sec < 300)
                except Exception:
                    pass
            if (current_owner == WATCHDOG_TIMEOUT_OWNER_SENTINEL
                    or other_is_alive):
                if not getattr(_loop, "_silent_exit_logged", False):
                    log_scheduler_event(
                        "GHOST_EXIT",
                        f"PID {my_pid} exiting — owner PID {current_owner} "
                        f"(beat {age_sec:.0f}s ago)" if age_sec is not None
                        else f"PID {my_pid} exiting — owner_pid changed "
                             f"to {current_owner}",
                        "WARN")
                    setattr(_loop, "_silent_exit_logged", True)
                break
            # Previous owner appears dead — take over silently.
            log_scheduler_event(
                "OWNER_TAKEOVER",
                f"PID {my_pid} taking over from stale owner "
                f"PID {current_owner} (no beat for "
                f"{age_sec if age_sec is not None else '?'}s)", "WARN")

        # HEARTBEAT
        next_at = myt_iso(_next_run_at(interval_sec))
        update_scheduler_state(
            last_heartbeat=myt_iso(),
            next_run_at=next_at,
            owner_pid=my_pid,
        )
        log_scheduler_event("HEARTBEAT", f"alive (PID {my_pid})")

        # Hourly DB backup
        try:
            from persistence import backup as _pers_backup, is_configured
            if is_configured():
                res = _pers_backup(reason="hourly heartbeat")
                if not res.get("ok") and not res.get("skipped"):
                    log_scheduler_event(
                        "BACKUP_FAIL",
                        f"Hourly backup failed: {res.get('reason','?')}",
                        "WARN")
        except Exception:
            pass

        # ---- v3.7: dispatch on trading mode ----
        if _is_intraday_mode():
            # INTRADAY PATH (5-min cadence, US RTH only)
            try:
                t0 = time.time()
                update_scheduler_state(cycle_started_at=myt_iso())
                try:
                    summary = _run_intraday_cycle(
                        autotrade=bool(state.get("autotrade_enabled", 1)),
                        autoexit=bool(state.get("autoexit_enabled", 1)),
                        my_pid=my_pid,
                    )
                    duration = time.time() - t0
                    update_scheduler_state(
                        last_run_at=myt_iso(),
                        next_run_at=myt_iso(
                            _next_run_at(INTRADAY_CYCLE_SEC)),
                        consecutive_failures=0,
                        last_error="",
                        cycle_started_at=None,
                    )
                    if duration > CYCLE_DURATION_WARN_SEC:
                        log_scheduler_event(
                            "CYCLE_SLOW",
                            f"Intraday cycle {duration:.0f}s",
                            "WARN", duration_sec=duration,
                        )
                    log_scheduler_event(
                        "CYCLE_OK",
                        f"intraday: scan={summary.get('scan_count', 0)} "
                        f"settled={summary.get('settled', 0)} "
                        f"entries={summary.get('auto_entries', 0)} "
                        f"forced_flats={summary.get('forced_flats', 0)}",
                        duration_sec=duration, payload=summary,
                    )
                finally:
                    try:
                        update_scheduler_state(cycle_started_at=None)
                    except Exception:
                        pass
            except Exception as e:
                tb = traceback.format_exc()
                fails = (state.get("consecutive_failures") or 0) + 1
                update_scheduler_state(
                    consecutive_failures=fails,
                    last_error=f"{e}\n{tb}",
                    cycle_started_at=None,
                )
                log_scheduler_event(
                    "CYCLE_ERROR", str(e), "ERROR",
                    payload={"trace": tb})

            summary["_intraday_did_sleep"] = True
            _STOP_EVENT.wait(timeout=INTRADAY_CYCLE_SEC)

        else:
            # SWING PATH (hourly, byte-identical to v3.6)
            # Check market hours
            if not _is_market_hours():
                from risk_manager import check_trading_time_window
                tw = check_trading_time_window()
                log_scheduler_event(
                    "SKIP",
                    f"Outside market hours — {tw.get('reason','closed')}. "
                    f"Sleeping until {next_at}",
                )
            else:
                try:
                    autotrade = bool(state.get("autotrade_enabled", 1))
                    autoexit = bool(state.get("autoexit_enabled", 1))
                    t0 = time.time()
                    update_scheduler_state(cycle_started_at=myt_iso())
                    try:
                        summary = _run_one_cycle(autotrade=autotrade,
                                                  autoexit=autoexit,
                                                  my_pid=my_pid)
                        duration = time.time() - t0
                        update_scheduler_state(
                            last_run_at=myt_iso(),
                            next_run_at=myt_iso(_next_run_at(interval_sec)),
                            consecutive_failures=0,
                            last_error="",
                            cycle_started_at=None,
                        )
                        if duration > CYCLE_DURATION_WARN_SEC:
                            log_scheduler_event(
                                "CYCLE_SLOW",
                                f"Cycle completed in {duration:.0f}s "
                                f"(> {CYCLE_DURATION_WARN_SEC}s warn threshold).",
                                "WARN",
                                duration_sec=duration,
                            )
                        log_scheduler_event(
                            "CYCLE_OK",
                            f"scan={summary['scan_count']} settled={summary['settled']} "
                            f"partials={summary['partials']} entries={summary['auto_entries']}",
                            duration_sec=duration, payload=summary,
                        )
                    finally:
                        try:
                            update_scheduler_state(cycle_started_at=None)
                        except Exception:
                            pass
                except Exception as e:
                    tb = traceback.format_exc()
                    fails = (state.get("consecutive_failures") or 0) + 1
                    update_scheduler_state(consecutive_failures=fails,
                                           last_error=f"{e}\n{tb}",
                                           cycle_started_at=None)
                    log_scheduler_event("CYCLE_ERROR", str(e), "ERROR",
                                        payload={"trace": tb})

        # ---- Daily maintenance window (idempotent) ----
        try:
            now = get_myt_now()
            in_maintenance_window = (now.hour == 1 and now.minute < 5)

            current_owner = get_scheduler_state().get("owner_pid", 0) or 0
            if current_owner and current_owner != my_pid:
                pass
            elif in_maintenance_window:
                from repository import (
                    try_claim_daily_task, record_daily_task_result,
                )

                if try_claim_daily_task("prune_logs", my_pid):
                    prune_logs(5000)
                    record_daily_task_result("prune_logs", "ok")

                if try_claim_daily_task("ml_retrain", my_pid):
                    try:
                        from learner import train_setup_classifier
                        res = train_setup_classifier()
                        if res:
                            log_scheduler_event(
                                "NIGHTLY_RETRAIN",
                                f"ML classifier retrained, OOS acc={res[1]:.3f} "
                                f"(PID {my_pid})")
                            record_daily_task_result(
                                "ml_retrain", f"oos_acc={res[1]:.4f}")
                        else:
                            record_daily_task_result(
                                "ml_retrain", "no_result")
                    except Exception as e:
                        log_scheduler_event(
                            "NIGHTLY_RETRAIN", f"failed: {e}", "ERROR")
                        record_daily_task_result(
                            "ml_retrain", f"error: {e}")

            from repository import closed_trades, try_claim_daily_task, record_daily_task_result
            ss = get_scheduler_state()
            if ss.get("exploration_mode"):
                tgt = ss.get("exploration_trades_target", 50) or 50
                done = len(closed_trades())
                if done >= tgt:
                    if try_claim_daily_task("exploration_end", my_pid):
                        update_scheduler_state(exploration_mode=0)
                        log_scheduler_event(
                            "EXPLORATION_END",
                            f"Closed {done} trades — switching to "
                            "exploitation (LCB) mode", "INFO")
                        record_daily_task_result(
                            "exploration_end", f"flipped_at_{done}_trades")
        except Exception as e:
            log_scheduler_event(
                "MAINTENANCE_ERROR", f"{e}", "ERROR")

        # Sleep until next scheduled time
        # v3.7: intraday already slept inside its own dispatch branch above.
        # Skip the generic sleep here if intraday ran this iteration.
        try:
            _skip_sleep = summary.get("_intraday_did_sleep", False)
        except Exception:
            _skip_sleep = False
        if not _skip_sleep:
            nxt = _next_run_at(interval_sec)
            sleep_secs = max(60, (nxt - get_myt_now()).total_seconds())
            _STOP_EVENT.wait(timeout=sleep_secs)

    update_scheduler_state(running=0,
                           last_heartbeat=myt_iso(),
                           cycle_started_at=None)
    log_scheduler_event("STOPPED", "Robo-Trader loop exited")


# -------------------------------------------------------------------------
# Internal helpers
# -------------------------------------------------------------------------

def _orphan_all_stale_scheduler_threads() -> None:
    """
    Mark every alive "bursa-scheduler" thread as orphaned UNLESS it is
    our current _THREAD (and alive + not already orphaned).

    Called by start() before spawning a new thread. This is the nuclear
    option — any thread that isn't the one we're about to create is
    treated as a zombie. They will self-exit via owner_pid mismatch
    on their next wake-up.
    """
    my_thread = _THREAD
    for t in threading.enumerate():
        if getattr(t, "name", None) != "bursa-scheduler":
            continue
        if not t.is_alive():
            continue
        if t.ident is None:
            continue
        # Don't orphan our own currently-valid thread
        if (my_thread is not None
                and my_thread is t
                and t.ident not in _ORPHANED_THREAD_IDS):
            continue
        _ORPHANED_THREAD_IDS.add(t.ident)


# -------------------------------------------------------------------------
# Public control — simplified lifecycle (v3.2)
# -------------------------------------------------------------------------

def start(interval_sec: int = 3600) -> bool:
    """
    Start the background scheduler thread.

    v3.2 simplified design:
      1. If our own _THREAD is alive and not orphaned → already running, return False
      2. Orphan ALL other alive "bursa-scheduler" threads (they are zombies)
      3. Write DB state: running=1, kill_switch=0, owner_pid=my_pid
      4. Clear stop event, spawn fresh thread + watchdog
      5. Return True

    No ADOPT_THREAD path. No multi-guard tree. If in doubt, start fresh.
    """
    global _THREAD
    with _LOCK:
        my_pid = os.getpid()

        # GC dead entries from orphan registry
        _gc_orphaned_thread_ids()

        # Guard: if OUR _THREAD is alive and not orphaned, we're already running.
        if (_THREAD is not None
                and _THREAD.is_alive()
                and _THREAD.ident not in _ORPHANED_THREAD_IDS):
            log_scheduler_event(
                "START_REJECT",
                f"PID {my_pid}: scheduler already running "
                f"(thread {_THREAD.ident})",
                "INFO",
            )
            return False

        # Orphan any stale "bursa-scheduler" threads from previous
        # starts, module reloads, or Streamlit reruns.
        _orphan_all_stale_scheduler_threads()

        # Clear the stop event BEFORE writing DB state so the new
        # thread sees a clean event.
        _STOP_EVENT.clear()

        # Write DB: running=1, kill_switch=0, owner_pid=my_pid.
        # This evicts any ghost from a previous container AND clears
        # any lingering kill_switch from a previous stop.
        update_scheduler_state(
            interval_sec=interval_sec,
            kill_switch=0,
            running=1,
            owner_pid=my_pid,
            last_heartbeat=myt_iso(),
            cycle_started_at=None,
        )

        _THREAD = threading.Thread(
            target=_loop, args=(interval_sec, my_pid),
            name="bursa-scheduler", daemon=True,
        )
        _THREAD.start()

        # Start (or restart) the watchdog
        _ensure_watchdog_running(my_pid)

        log_scheduler_event(
            "START_OK",
            f"PID {my_pid}: scheduler thread spawned",
            "INFO",
        )
        return True


def stop() -> None:
    """
    Request the scheduler thread to exit.

    v3.2: does NOT set kill_switch. Only the dedicated Kill-Switch button
    should engage the kill_switch (via engage_kill_switch()). This was a
    source of bugs: force_restart() called stop() which set kill_switch=1,
    then start() via the ADOPT_THREAD path failed to clear it.

    Now stop() just:
      1. Signal the thread to exit (_STOP_EVENT)
      2. Write DB: running=0, owner_pid=0
      3. Orphan the thread handle
      4. Join with timeout, then clear handle
      5. Stop watchdog
    """
    global _THREAD
    with _LOCK:
        _STOP_EVENT.set()
        update_scheduler_state(running=0, owner_pid=0,
                               cycle_started_at=None)
        if _THREAD is not None:
            try:
                if _THREAD.ident is not None:
                    _ORPHANED_THREAD_IDS.add(_THREAD.ident)
            except Exception:
                pass
            _THREAD.join(timeout=5)
        _THREAD = None
        _stop_watchdog()


def engage_kill_switch() -> None:
    """
    Emergency stop. Sets kill_switch=1 so the loop won't restart until
    the user explicitly clears it in Settings.

    Separate from stop() because stop() is used internally by
    force_restart() and should NOT engage the kill_switch.
    """
    stop()
    update_scheduler_state(kill_switch=1)


def is_running() -> bool:
    """
    True iff we have a local non-orphaned alive thread AND the DB still
    shows running=1. Both conditions must hold — this prevents stale DB
    state from making the badge lie.
    """
    with _LOCK:
        alive = (_THREAD is not None
                 and _THREAD.is_alive()
                 and _THREAD.ident not in _ORPHANED_THREAD_IDS)
    state = get_scheduler_state()
    return alive and bool(state.get("running", 0))


def force_restart(interval_sec: int = 3600) -> None:
    """
    User-facing 'Force Reboot Robo-Trader'.
    stop() + start(). No ADOPT_THREAD complexity.
    """
    stop()
    start(interval_sec=interval_sec)


def ensure_started(interval_sec: int = 3600,
                   max_heartbeat_age_sec: int | None = None) -> None:
    """
    Idempotent + self-healing. Called on every Streamlit rerun.

    v3.2 simplified:
      1. If is_running() → ensure watchdog is alive → return
      2. Otherwise → start()

    No 5-case decision tree. No heartbeat age checks. No "another
    process owns it" deference. The start() function handles ghost
    eviction internally.
    """
    if is_running():
        # Scheduler is healthy. Just make sure the watchdog is alive too.
        _ensure_watchdog_running(os.getpid())
        return

    # Not running — start it.
    start(interval_sec=interval_sec)


def run_once() -> dict:
    """Trigger a single immediate cycle (used by 'Run now' button).

    v3.7: dispatches on active trading mode — intraday runs
    _run_intraday_cycle(), swing runs _run_one_cycle().
    """
    state = get_scheduler_state()
    if _is_intraday_mode():
        return _run_intraday_cycle(
            autotrade=bool(state.get("autotrade_enabled", 1)),
            autoexit=bool(state.get("autoexit_enabled", 1)),
        )
    return _run_one_cycle(
        autotrade=bool(state.get("autotrade_enabled", 1)),
        autoexit=bool(state.get("autoexit_enabled", 1)),
    )


def get_watchdog_status() -> dict:
    """
    Read-only health summary for the UI to render in the 🤖 Robo-Trader
    tab. Cheap — one SQLite read + a few in-memory comparisons.
    """
    status = {
        "watchdog_alive": False,
        "watchdog_thread_ident": None,
        "cycle_in_flight": False,
        "cycle_running_for_sec": None,
        "cycle_started_at": None,
        "recent_timeouts_24h": 0,
        "recent_slow_cycles_24h": 0,
        "recent_respawns_24h": 0,
        "orphan_thread_count": 0,
        "watchdog_timeout_sec": WATCHDOG_CYCLE_TIMEOUT_SEC,
        "cycle_warn_sec": CYCLE_DURATION_WARN_SEC,
    }

    try:
        wt = _WATCHDOG_THREAD
        if wt is not None and wt.is_alive():
            status["watchdog_alive"] = True
            status["watchdog_thread_ident"] = wt.ident
    except Exception:
        pass

    try:
        status["orphan_thread_count"] = len(_ORPHANED_THREAD_IDS)
    except Exception:
        pass

    try:
        ss = get_scheduler_state()
        stamp = ss.get("cycle_started_at")
        if stamp:
            status["cycle_started_at"] = stamp
            status["cycle_in_flight"] = True
            try:
                started_dt = datetime.strptime(
                    stamp, "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone(timedelta(hours=8)))
                age = (get_myt_now() - started_dt).total_seconds()
                status["cycle_running_for_sec"] = max(0.0, age)
            except Exception:
                pass
    except Exception:
        pass

    try:
        from logger import get_scheduler_log
        recent = get_scheduler_log(limit=500) or []
        cutoff = get_myt_now() - timedelta(hours=24)
        for row in recent:
            ts = row.get("timestamp") or ""
            event = row.get("event") or ""
            try:
                rt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone(timedelta(hours=8)))
                if rt < cutoff:
                    continue
            except Exception:
                pass
            if event == "CYCLE_TIMEOUT":
                status["recent_timeouts_24h"] += 1
            elif event == "CYCLE_SLOW":
                status["recent_slow_cycles_24h"] += 1
            elif event == "WATCHDOG_RESPAWN":
                status["recent_respawns_24h"] += 1
    except Exception:
        pass

    return status


# -------------------------------------------------------------------------
# CLI entry — `python -m scheduler`
# -------------------------------------------------------------------------

if __name__ == "__main__":
    interval = 3600
    if "--interval" in sys.argv:
        idx = sys.argv.index("--interval")
        try:
            interval = int(sys.argv[idx + 1])
        except Exception:
            pass

    start(interval_sec=interval)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        stop()


# -------------------------------------------------------------------------
# v3.7 — Intraday cycle helpers (Block 5)
# -------------------------------------------------------------------------

INTRADAY_CYCLE_SEC = 300  # 5-minute scan interval during US RTH


def _build_intraday_bar_data() -> dict:
    """Build {ticker: {price, high, low}} from the latest available 5m bar
    for each active intraday trade.

    Used by auto_settle_intraday() and force_flat_all_intraday().
    Falls back to last known price from the DB if a live fetch fails.
    """
    bar_data: dict = {}
    try:
        from intraday_engine import get_active_intraday_tickers
        tickers = get_active_intraday_tickers()
    except Exception:
        tickers = set()
    if not tickers:
        return bar_data

    try:
        from data_provider import get_history
        for tk in tickers:
            try:
                df = get_history(tk, interval="5m", period="5d", timeout=30)
                if df is None or df.empty:
                    continue
                last = df.iloc[-1]
                bar_data[tk] = {
                    "price": float(last["Close"]),
                    "high": float(last["High"]),
                    "low": float(last["Low"]),
                }
            except Exception:
                # Fallback: use the trade's own highest/lowest tracking
                try:
                    from repository import active_trades
                    for t in active_trades():
                        if t["ticker"] == tk and t.get(
                                "execution_type") == "AGENT_INTRADAY":
                            px = float(t.get("highest_price",
                                             t.get("entry_price", 0)))
                            bar_data[tk] = {
                                "price": px,
                                "high": px,
                                "low": float(t.get("lowest_price", px)),
                            }
                except Exception:
                    pass
    except Exception:
        pass
    return bar_data


def _run_intraday_cycle(autotrade: bool, autoexit: bool,
                        my_pid: int | None = None) -> dict:
    """One 5-minute intraday scan + settle + entry cycle.

    Runs only during US RTH. Uses the intraday screener and intraday
    engine — completely separate from the swing path.

    Session states:
        PREMARKET      → do nothing
        OR_WINDOW      → scan, build opening range, no entries yet
        ACTIVE_TRADING → scan + settle + enter
        FORCE_FLAT     → close everything
        POSTMARKET     → do nothing
    """
    summary = {
        "scan_count": 0, "settled": 0, "partials": 0,
        "auto_entries": 0, "rejected": 0, "forced_flats": 0,
        "errors": [], "aborted": False,
    }

    # ---- Ownership check ----
    if my_pid is not None:
        state = get_scheduler_state()
        current_owner = state.get("owner_pid", 0) or 0
        if current_owner and current_owner != my_pid:
            log_scheduler_event(
                "CYCLE_ABORT",
                f"PID {my_pid} aborting intraday cycle — owner now {current_owner}",
                "WARN",
            )
            summary["aborted"] = True
            return summary

    # ---- Session status ----
    try:
        from intraday_engine import intraday_session_status
        status = intraday_session_status()
    except Exception as e:
        log_scheduler_event(
            "ERROR", f"intraday_session_status failed: {e}", "ERROR")
        summary["errors"].append(f"session_status: {e}")
        return summary

    state_name = status["state"]

    # ---- FORCE_FLAT / POSTMARKET: close everything ----
    if status["should_force_flat"]:
        try:
            from intraday_engine import force_flat_all_intraday
            bar_data = _build_intraday_bar_data()
            closed = force_flat_all_intraday(bar_data, actor="AGENT")
            summary["forced_flats"] = closed
            summary["settled"] = closed
            if closed > 0:
                log_scheduler_event(
                    "INTRADAY_FORCE_FLAT",
                    f"Force-flatted {closed} intraday position(s) "
                    f"({state_name}).",
                    payload={"state": state_name, "closed": closed},
                )
                try:
                    from persistence import backup as _pers_backup, is_configured
                    if is_configured():
                        _pers_backup(reason=f"intraday force-flat: {closed} trades")
                except Exception:
                    pass
        except Exception as e:
            log_scheduler_event(
                "ERROR", f"force_flat_all_intraday failed: {e}", "ERROR")
            summary["errors"].append(f"force_flat: {e}")
        return summary

    # ---- PREMARKET / POSTMARKET: nothing to do ----
    if not status["can_scan"]:
        log_scheduler_event(
            "SKIP",
            f"Intraday {state_name} — sleeping {INTRADAY_CYCLE_SEC}s.",
        )
        return summary

    # ---- Local-only enforcement: intraday requires Moomoo OpenD ----
    try:
        import data_provider
        data_provider.ensure_probed()
        h = data_provider.health()
        moomoo_ok = bool(h.get("moomoo_available"))
    except Exception:
        moomoo_ok = False

    if not moomoo_ok:
        log_scheduler_event(
            "INTRADAY_UNAVAILABLE",
            "OpenD not connected — intraday trading disabled. "
            "Set TRADING_MODE=SWING or connect Moomoo OpenD.",
            "WARN",
        )
        return summary

    # ---- OR_WINDOW / ACTIVE_TRADING: scan + settle + enter ----

    # 1. Scan for new signals
    try:
        from intraday_screener import screen_intraday, DEFAULT_INTRADAY_WATCHLIST
        from intraday_engine import get_active_intraday_tickers as _get_active

        already_triggered = _get_active()
        signals = screen_intraday(
            DEFAULT_INTRADAY_WATCHLIST,
            already_triggered=already_triggered,
        )
        summary["scan_count"] = len(signals)
    except Exception as e:
        log_scheduler_event(
            "ERROR", f"Intraday scan failed: {e}", "ERROR")
        summary["errors"].append(f"scan: {e}")
        return summary

    if not signals:
        log_scheduler_event(
            "INTRADAY_SCAN",
            f"{state_name} — 0 signals for curated-6 watchlist.",
        )
        return summary

    # 2. Build live bar data for settlement
    bar_data = _build_intraday_bar_data()

    # 3. Auto-settle (only if autoexit is on)
    if autoexit and bar_data:
        try:
            from intraday_engine import auto_settle_intraday
            settle_res = auto_settle_intraday(bar_data, actor="AGENT")
            summary["settled"] = len(settle_res.get("settled", []))
            summary["partials"] = len(settle_res.get("partials", []))

            for ev in settle_res.get("settled", []):
                try:
                    from repository import get_trade
                    t = get_trade(ev["trade_id"])
                    if t and t.get("status") == "CLOSED":
                        from learner import learn_from_trade_outcome
                        learn_from_trade_outcome(t)
                except Exception as e:
                    log.error(
                        f"intraday learning failed for "
                        f"{ev.get('trade_id')}: {e}")

            if settle_res.get("settled"):
                try:
                    from persistence import backup as _pers_backup, is_configured
                    if is_configured():
                        _pers_backup(
                            reason=f"intraday: {len(settle_res['settled'])} closed")
                except Exception:
                    pass
        except Exception as e:
            log_scheduler_event(
                "ERROR", f"Intraday settle failed: {e}", "ERROR")
            summary["errors"].append(f"settle: {e}")

    # 4. Auto-entry (only during ACTIVE_TRADING, not OR_WINDOW)
    if autotrade and status["can_enter"] and moomoo_ok:
        try:
            from intraday_engine import execute_intraday_entry
            from repository import load_account, load_trades
            from risk_manager import run_full_risk_check

            active_set = _get_active()
            acc = load_account()
            trades = load_trades()
            cash = acc["cash_balance"]
            initial_capital = acc["initial_capital"]
            # v3.7 fix: use total_equity not cash_balance for drawdown check.
            # Same fix as SWING path — cash drops when position opens but
            # equity stays flat (cash + position value = same).
            try:
                from repository import active_trades as _at_intraday
                _intraday_mv = sum(
                    float(t.get("entry_price", 0)) * int(t.get("shares_remaining", 0))
                    for t in _at_intraday()
                    if t.get("execution_type") == "AGENT_INTRADAY"
                )
            except Exception:
                _intraday_mv = 0.0
            intraday_total_equity = cash + _intraday_mv

            for sig in signals:
                ticker = sig["ticker"]
                if ticker in active_set:
                    # Already have an open intraday position — skip
                    continue

                # Run risk checks
                risk_result = run_full_risk_check(
                    trades,
                    {
                        "ticker": ticker,
                        "sector": sig.get("sector", ""),
                        "entry": sig["entry"],
                        "stop_loss": sig["stop_loss"],
                        "cost": sig["entry"] * 10,
                        "risk_amount": (
                            sig["entry"] - sig["stop_loss"]) * 10,
                    },
                    intraday_total_equity,
                    initial_capital,
                )
                if not risk_result.get("pass"):
                    summary["rejected"] += 1
                    from logger import log_trade_event
                    log_trade_event(
                        "RISK_REJECTED", trade_id=None, ticker=ticker,
                        actor="AGENT",
                        payload={"verdict": risk_result["final_verdict"]})
                    continue

                ok, tid, msg = execute_intraday_entry(sig, actor="AGENT")
                if ok:
                    summary["auto_entries"] += 1
                    active_set.add(ticker)
                else:
                    summary["rejected"] += 1
                    log_scheduler_event(
                        "INTRADAY_ENTRY_REJECT",
                        f"{ticker}: {msg}", "WARN")

            if summary["auto_entries"] == 0:
                log_scheduler_event(
                    "INTRADAY_AUTO_ENTRY_END",
                    f"0 intraday entries — {summary['rejected']} rejected, "
                    f"{summary['scan_count']} signals scanned.",
                    payload={"state": state_name},
                )
            else:
                log_scheduler_event(
                    "INTRADAY_AUTO_ENTRY_END",
                    f"{summary['auto_entries']} intraday entries, "
                    f"{summary['rejected']} rejected",
                )
        except Exception as e:
            log_scheduler_event(
                "ERROR", f"Intraday entry failed: {e}", "ERROR")
            summary["errors"].append(f"entry: {e}")

    return summary


def _is_intraday_mode() -> bool:
    """Check if the active trading mode is INTRADAY.

    Returns False gracefully if the market_profiles module isn't available
    (e.g. early bootstrap, tests without the module).
    """
    try:
        from market_profiles import is_intraday as _is_intraday
        return _is_intraday()
    except Exception:
        return False
