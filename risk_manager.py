# risk_manager.py
"""
Risk Manager — Central risk control hub.

v3.6 multi-market change
------------------------
* `min_risk_per_trade_rm` is reseeded from the ACTIVE market profile on
  first init so US deployments get USD 20 (not RM 50) by default.
* All user-facing reason strings still say "RM" for MY, "$" for US,
  resolved from `active_profile().currency_symbol`.
* Behaviour & math unchanged — same checks, same verdict shape. Cash
  conservation, drawdown breaker, position limits all proceed exactly
  as in v3.3/v3.5.

Fixed bugs from v1 (still guarded)
----------------------------------
* `size_multiplier` is now ACTUALLY enforced by callers (engines respect it).
* `check_risk_amount.adjusted_shares` removed (was nonsense formula).
* `position_limit_check` returns `allowed=False` when *no* size reduction
  would bring it under cap (was always True).
* Risk params persisted in DB.
"""

from __future__ import annotations
import json


from db import connect, myt_iso, get_myt_now


def _ccy() -> str:
    """Active market currency symbol for user-facing strings."""
    try:
        from market_profiles import active_profile
        return active_profile().currency_symbol
    except Exception:
        return "RM"


def _profile_min_risk() -> float:
    """Per-market minimum risk floor; falls back to RM 50 (legacy MY default)."""
    try:
        from market_profiles import active_profile
        return float(active_profile().min_risk_per_trade)
    except Exception:
        return 50.0


def _profile_max_positions() -> int:
    """BULL-regime concurrent-position ceiling from the active profile."""
    try:
        from market_profiles import active_profile
        return int(active_profile().bull_max_positions)
    except Exception:
        return 8


DEFAULT_RISK_PARAMS = {
    "max_drawdown_pct": 8.0,
    "max_drawdown_strict_pct": 15.0,
    # NB: `min_risk_per_trade_rm` retains its legacy key name to avoid breaking
    # existing JSON payloads in the DB. The value is currency-agnostic
    # (RM for MY, USD for US — resolved at seed time below).
    "min_risk_per_trade_rm": 50.0,
    "max_risk_per_trade_pct": 1.0,  # v3: safer default for auto-trade
    "max_position_cost_pct": 20.0,
    "max_sector_exposure_pct": 40.0,
    "max_concurrent_positions": 8,
    "max_correlation_threshold": 0.7,
    "max_trades_per_day": 5,
    "min_trades_per_week": 0,
    "no_entry_before_time": "09:00",  # MY morning open — US overrides on seed
    "no_entry_after_time": "17:00",   # MY TaL close   — US overrides on seed
    "max_stop_loss_pct": 10.0,
    "min_stop_loss_pct": 1.5,
    "trailing_stop_activation": "TP1",
    "trailing_stop_buffer_pct": 0.5,
    "enforce_lot_size": True,
}


def _profile_seed_overrides() -> dict:
    """Per-market overrides applied on first init only."""
    try:
        from market_profiles import active_profile, active_market_code
        prof = active_profile()
        out = {
            "min_risk_per_trade_rm": prof.min_risk_per_trade,
            "max_concurrent_positions": prof.bull_max_positions,
        }
        if active_market_code() == "US":
            # US RTH: 09:30–16:00 ET. Times stored as HH:MM in market-local TZ.
            out["no_entry_before_time"] = "09:30"
            out["no_entry_after_time"] = "16:00"
        return out
    except Exception:
        return {}


def _ensure_risk_row():
    with connect() as c:
        row = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='risk_params'"
        ).fetchone()
        if not row:
            c.execute("CREATE TABLE IF NOT EXISTS risk_params "
                      "(id INTEGER PRIMARY KEY CHECK (id=1), payload TEXT, "
                      "updated_at TEXT)")
        # Seed (idempotent — only inserts if id=1 missing)
        seed = DEFAULT_RISK_PARAMS.copy()
        seed.update(_profile_seed_overrides())
        c.execute("INSERT OR IGNORE INTO risk_params (id, payload, updated_at) "
                  "VALUES (1, ?, ?)",
                  (json.dumps(seed), myt_iso()))


def load_risk_params() -> dict:
    _ensure_risk_row()
    with connect(readonly=True) as c:
        row = c.execute("SELECT payload FROM risk_params WHERE id=1").fetchone()
    params = DEFAULT_RISK_PARAMS.copy()
    params.update(_profile_seed_overrides())
    if row:
        try:
            params.update(json.loads(row["payload"]))
        except Exception:
            pass
    return params


def save_risk_params(params: dict) -> None:
    _ensure_risk_row()
    merged = DEFAULT_RISK_PARAMS.copy()
    merged.update(_profile_seed_overrides())
    merged.update(params or {})
    with connect() as c:
        c.execute("UPDATE risk_params SET payload=?, updated_at=? WHERE id=1",
                  (json.dumps(merged), myt_iso()))


# -------------------------------------------------------------------------
# Individual checks
# -------------------------------------------------------------------------

def check_drawdown_circuit_breaker(initial_capital: float,
                                   current_equity: float) -> dict:
    if initial_capital <= 0:
        return {"allowed": True, "level": "NONE", "reason": "No capital data",
                "pct_drop": 0}
    pct = (initial_capital - current_equity) / initial_capital * 100
    p = load_risk_params()
    if pct >= p["max_drawdown_strict_pct"]:
        return {"allowed": False, "level": "STRICT_CIRCUIT_BREAKER",
                "reason": f"Equity dropped {pct:.1f}% (limit "
                          f"{p['max_drawdown_strict_pct']}%). ALL trading paused.",
                "pct_drop": round(pct, 2)}
    if pct >= p["max_drawdown_pct"]:
        return {"allowed": True, "level": "WARN_DRAWDOWN",
                "reason": f"Equity dropped {pct:.1f}% (warn at "
                          f"{p['max_drawdown_pct']}%). New positions at 50% size.",
                "pct_drop": round(pct, 2)}
    return {"allowed": True, "level": "OK",
            "reason": f"Drawdown {pct:.1f}% (within limits).",
            "pct_drop": round(pct, 2)}


def check_position_limits(trades: list, new_trade_cost: float,
                          sector: str, capital: float) -> dict:
    p = load_risk_params()
    ccy = _ccy()
    max_pos = p["max_concurrent_positions"]
    max_cost_pct = p["max_position_cost_pct"]
    max_sec_pct = p["max_sector_exposure_pct"]
    active = [t for t in trades if t.get("status") == "ACTIVE"]

    if len(active) >= max_pos:
        return {"allowed": False,
                "reason": f"Max {max_pos} concurrent positions reached.",
                "size_reduction_pct": 0}

    # Correlation Shield: professional swing — limit sector concentration
    # For MY and general: max 2 per sector
    # For US Leveraged ETF / Leveraged Sector: max 1 (they are 90%+ correlated, e.g. SPXL+UPRO+TQQQ)
    # For Crypto: max 1 (BTC beta)
    sector_active_count = sum(1 for t in active if t.get("sector") == sector)
    sector_cap = 2
    if sector in ("Leveraged ETF", "Leveraged Sector"):
        sector_cap = 1
    elif sector in ("Crypto",):
        sector_cap = 1
    elif sector in ("Volatility",):
        sector_cap = 1  # UVXY/VXX decay — max 1 hedge at a time
    if sector_active_count >= sector_cap:
        return {"allowed": False,
                "reason": f"Correlation Shield: Sector '{sector}' already has "
                          f"{sector_active_count} active (max {sector_cap} for {sector}). "
                          f"Highly correlated leveraged ETFs capped at 1 to avoid double exposure.",
                "size_reduction_pct": 100}

    max_cost = capital * (max_cost_pct / 100)
    if new_trade_cost > max_cost:
        reduce_pct = min((new_trade_cost - max_cost) / new_trade_cost * 100, 80)
        if reduce_pct >= 80:
            return {"allowed": False,
                    "reason": f"Position cost {ccy} {new_trade_cost:,.0f} "
                              f"hugely over {max_cost_pct}% cap.",
                    "size_reduction_pct": 100}
        return {"allowed": True,
                "reason": f"Position size > {max_cost_pct}% cap. "
                          f"Reduced {reduce_pct:.0f}%.",
                "size_reduction_pct": reduce_pct}

    sector_cost = sum(t.get("cost", 0) for t in active
                      if t.get("sector") == sector)
    sec_cap = capital * (max_sec_pct / 100)
    if sector_cost + new_trade_cost > sec_cap:
        avail = sec_cap - sector_cost
        if avail <= 0:
            return {"allowed": False,
                    "reason": f"Sector '{sector}' exposure already at cap "
                              f"({sector_cost / capital * 100:.1f}%/{max_sec_pct}%).",
                    "size_reduction_pct": 100}
        reduce_pct = (new_trade_cost - avail) / new_trade_cost * 100
        return {"allowed": True,
                "reason": f"Sector cap reached — reduce {reduce_pct:.0f}%.",
                "size_reduction_pct": reduce_pct}

    return {"allowed": True, "reason": "All position limits OK.",
            "size_reduction_pct": 0}


def check_risk_amount(trade_risk_amount: float, capital: float) -> dict:
    p = load_risk_params()
    ccy = _ccy()
    min_r = p["min_risk_per_trade_rm"]
    max_r = capital * (p["max_risk_per_trade_pct"] / 100)
    if trade_risk_amount < min_r:
        return {"allowed": False,
                "reason": f"Risk {ccy} {trade_risk_amount:.2f} below min {ccy} {min_r:.2f}."}
    if trade_risk_amount > max_r:
        return {"allowed": True,
                "reason": f"Risk capped at {ccy} {max_r:.2f} "
                          f"({p['max_risk_per_trade_pct']}% of capital)."}
    return {"allowed": True, "reason": "Risk amount OK."}


def check_daily_trade_limit(trades: list) -> dict:
    p = load_risk_params()
    today = get_myt_now().strftime("%Y-%m-%d")
    n = sum(1 for t in trades if (t.get("logged_at") or "").startswith(today)
            and t.get("status") != "REJECTED")
    if n >= p["max_trades_per_day"]:
        return {"allowed": False,
                "reason": f"Daily trade limit ({p['max_trades_per_day']}) reached.",
                "count": n}
    return {"allowed": True,
            "reason": f"{p['max_trades_per_day'] - n} trades remaining today.",
            "count": n}


def check_trading_time_window() -> dict:
    """
    Delegate to market_calendar for accurate session handling.

    market_calendar dispatches on the active market profile, so this works
    for both MY (with lunch break + Bursa holidays) and US (RTH only).
    """
    from market_calendar import (
        is_market_open, is_safe_entry_window, market_status_text,
        current_session,
    )
    p = load_risk_params()
    # Use the calendar's local-time helpers so we evaluate against the
    # active market's timezone (MYT for MY, ET for US, etc.)
    from datetime import datetime
    try:
        from market_profiles import active_profile
        tz = active_profile().timezone
    except Exception:
        from datetime import timezone, timedelta
        tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    t = now.strftime("%H:%M")

    status = market_status_text(now)

    if not is_market_open(now):
        return {"allowed": False,
                "reason": status["reason"] + f" (next: {status['next_event']})",
                "window": status["session"]}

    # Market is open. Now apply the user's optional tighter window
    user_min = p.get("no_entry_before_time", "09:00")
    user_max = p.get("no_entry_after_time", "17:00")
    if t < user_min:
        return {"allowed": False,
                "reason": f"User-configured pre-market: opens {user_min} local.",
                "window": f"Before {user_min}"}
    if t > user_max:
        return {"allowed": False,
                "reason": f"User-configured cutoff: after {user_max} local.",
                "window": f"After {user_max}"}

    # Optionally also block new entries in the no-safe-entry tail
    if not is_safe_entry_window(now):
        sess = current_session(now)
        sess_name = sess.name if sess else "?"
        try:
            from market_profiles import active_profile
            cutoff = active_profile().safe_entry_cutoff.strftime("%H:%M")
        except Exception:
            cutoff = "16:00"
        return {"allowed": False,
                "reason": (f"In {sess_name} — too late for new entries "
                           f"(safe-entry window ended {cutoff})."),
                "window": sess_name}

    sess = current_session(now)
    return {"allowed": True,
            "reason": f"{sess.name} session — fills active.",
            "window": f"{sess.name} ({t})"}


def validate_stop_loss(entry_price: float, proposed_sl: float) -> dict:
    p = load_risk_params()
    risk_pct = (entry_price - proposed_sl) / entry_price * 100
    if risk_pct < p["min_stop_loss_pct"]:
        adj = entry_price * (1 - p["min_stop_loss_pct"] / 100)
        return {"valid": True, "adjusted_sl": round(adj, 3),
                "reason": f"Stop too tight ({risk_pct:.2f}%) — set to "
                          f"min {p['min_stop_loss_pct']}%."}
    if risk_pct > p["max_stop_loss_pct"]:
        adj = entry_price * (1 - p["max_stop_loss_pct"] / 100)
        return {"valid": True, "adjusted_sl": round(adj, 3),
                "reason": f"Stop too wide ({risk_pct:.2f}%) — capped "
                          f"at {p['max_stop_loss_pct']}%."}
    return {"valid": True, "adjusted_sl": round(proposed_sl, 3),
            "reason": f"Stop {risk_pct:.2f}% within range."}


# -------------------------------------------------------------------------
# Aggregated check
# -------------------------------------------------------------------------

def run_full_risk_check(trades: list, new_trade_info: dict,
                        capital: float, initial_capital: float) -> dict:
    checks = {
        "drawdown_check": check_drawdown_circuit_breaker(initial_capital, capital),
        "position_limit_check": check_position_limits(
            trades, new_trade_info.get("cost", 0),
            new_trade_info.get("sector", "Unknown"), capital),
        "risk_amount_check": check_risk_amount(
            new_trade_info.get("risk_amount", 0), capital),
        "daily_limit_check": check_daily_trade_limit(trades),
        "time_window_check": check_trading_time_window(),
    }

    # Compose verdict.
    rejecting = []
    size_mult = 1.0

    dd = checks["drawdown_check"]
    if not dd["allowed"]:
        rejecting.append(f"drawdown: {dd['reason']}")
    elif dd["level"] == "WARN_DRAWDOWN":
        size_mult = min(size_mult, 0.5)

    pl = checks["position_limit_check"]
    if not pl["allowed"]:
        rejecting.append(f"position_limit: {pl['reason']}")
    elif pl["size_reduction_pct"] > 0:
        size_mult = min(size_mult, 1 - pl["size_reduction_pct"] / 100)

    if not checks["risk_amount_check"]["allowed"]:
        rejecting.append(f"risk: {checks['risk_amount_check']['reason']}")

    if not checks["daily_limit_check"]["allowed"]:
        rejecting.append(f"daily: {checks['daily_limit_check']['reason']}")

    if not checks["time_window_check"]["allowed"]:
        rejecting.append(f"time: {checks['time_window_check']['reason']}")

    # Progressive Exposure (The Minervini Rule)
    # FIX #3-1: Simplified — only checks consecutive recent losses.
    # The secondary win_rate check was removed — it used a buggy denominator
    # (always 5 = slice size) and could fire incorrectly after a broken streak.
    closed_trades_pe_list = sorted([t for t in trades if t.get("status") == "CLOSED"],
                              key=lambda x: x.get("id", 0), reverse=True)[:5]
    pe_multiplier = 1.0
    pe_reason = ""

    if len(closed_trades_pe_list) >= 1:
        losses = 0
        for ct in closed_trades_pe_list:
            pnl = ct.get("realized_pnl", 0)
            if pnl < 0:
                losses += 1
            else:
                break  # streak broken by a win or breakeven
        if losses >= 3:
            pe_multiplier = 0.5
            pe_reason = (f"Progressive Exposure: {losses} consecutive recent "
                         f"losses. Scaling down size to 50%.")
        elif losses >= 1:
            pe_reason = (f"Progressive Exposure: {losses} recent loss(es). "
                         f"Monitoring — full size if streak < 3.")

    if pe_multiplier < 1.0:
        size_mult = min(size_mult, pe_multiplier)

    passed = not rejecting
    verdict = "✅ APPROVED" if passed else "❌ REJECTED — " + "; ".join(rejecting)
    if passed and pe_reason and pe_multiplier < 1.0:
        verdict += f" | ⚠️ {pe_reason}"

    return {
        "pass": passed,
        "checks": checks,
        "final_verdict": verdict,
        "size_multiplier": round(max(size_mult, 0.0), 3),
        "risk_level": dd["level"],
        "progressive_exposure_reason": pe_reason,
        "progressive_exposure_multiplier": pe_multiplier,
    }


def get_risk_dashboard_stats(trades: list, capital: float,
                             initial_capital: float) -> dict:
    p = load_risk_params()
    active = [t for t in trades if t.get("status") == "ACTIVE"]
    dd = check_drawdown_circuit_breaker(initial_capital, capital)
    total_exp = sum(t.get("cost", 0) for t in active)
    exp_pct = (total_exp / capital * 100) if capital > 0 else 0
    sec_exp: dict[str, float] = {}
    for t in active:
        sec = t.get("sector") or "Unknown"
        sec_exp[sec] = sec_exp.get(sec, 0) + (t.get("cost") or 0)
    tw = check_trading_time_window()
    dl = check_daily_trade_limit(trades)

    # Progressive Exposure stats
    closed_trades = sorted([t for t in trades if t.get("status") == "CLOSED"], 
                           key=lambda x: x.get("id", 0), reverse=True)[:5]
    pe_multiplier = 1.0
    pe_reason = "OK (No consecutive losing streaks detected)"
    if len(closed_trades) >= 3:
        losses = 0
        for ct in closed_trades:
            pnl = ct.get("realized_pnl", 0)
            if pnl < 0:
                losses += 1
            else:
                break
        if losses >= 3:
            pe_multiplier = 0.5
            pe_reason = (f"ALERT: {losses} consecutive recent losses. "
                         f"Scaling down next trade sizes to 50%.")
        elif losses >= 1:
            pe_reason = (f"CAUTION: {losses} recent loss(es). "
                         f"Monitoring — PE triggers at 3 consecutive.")

    return {
        "drawdown_pct": dd["pct_drop"],
        "drawdown_level": dd["level"],
        "drawdown_allowed": dd["allowed"],
        "drawdown_reason": dd["reason"],
        "total_exposure_rm": round(total_exp, 2),
        "exposure_pct": round(exp_pct, 1),
        "active_positions": len(active),
        "max_positions_allowed": p["max_concurrent_positions"],
        "sector_exposure": {k: round(v, 2) for k, v in sec_exp.items()},
        "trades_today": dl["count"],
        "trades_daily_limit": p["max_trades_per_day"],
        "trading_window": tw["window"],
        "can_trade_now": tw["allowed"] and dd["allowed"],
        "risk_params": p,
        "progressive_exposure_reason": pe_reason,
        "progressive_exposure_multiplier": pe_multiplier,
    }
