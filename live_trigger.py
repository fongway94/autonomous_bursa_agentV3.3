# live_trigger.py
"""
Live trigger bridge — turns paper-trade events into user notifications.

Called by trading_engine and scheduler immediately after each trade
action. Applies user filters (master switch, confidence threshold,
EXPLOIT-mode-only, actor filter, per-event toggles), formats messages,
and dispatches via notifier.py.

Dedup
-----
Per (trade_id, event_type). The same event for the same trade will only
fire once, even if the scheduler retries.
"""

from __future__ import annotations
import json
from datetime import datetime

from db import connect, myt_iso
from repository import get_trade, get_scheduler_state
from logger import get_logger
from notifier import dispatch


def _ccy() -> str:
    """Active market's currency symbol ('RM' / '$'). Falls back to 'RM'.

    Alerts mirror real paper-trades, so the symbol must match the market the
    trade was taken in (US trades show '$', MY trades show 'RM').
    """
    try:
        from market_profiles import active_profile
        return active_profile().currency_symbol
    except Exception:
        return "RM"

log = get_logger("live_trigger")


# --------------------------------------------------------------------- #
# Config IO
# --------------------------------------------------------------------- #

DEFAULT_CONFIG = {
    "enabled": 0,
    "min_confidence": 70.0,
    "exploit_mode_only": 0,
    "alert_on_entry": 1,
    "alert_on_full_exit": 1,
    "alert_on_stop_loss": 1,
    "alert_on_trailing_stop": 1,
    "alert_on_partial_exit": 0,
    "alert_on_risk_rejected": 0,
    "telegram_enabled": 1,
    "email_enabled": 0,
    "email_recipients": "",
    "actor_filter": "AGENT",  # AGENT | BOTH
}


def load_config() -> dict:
    with connect(readonly=True) as c:
        row = c.execute(
            "SELECT * FROM live_trigger_config WHERE id=1"
        ).fetchone()
    if row is None:
        return DEFAULT_CONFIG.copy()
    out = dict(row)
    out.pop("id", None); out.pop("updated_at", None)
    return out


def save_config(cfg: dict) -> None:
    merged = DEFAULT_CONFIG.copy()
    merged.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
    # Coerce booleans -> 0/1
    bool_keys = ("enabled", "exploit_mode_only", "alert_on_entry",
                 "alert_on_full_exit", "alert_on_stop_loss",
                 "alert_on_trailing_stop", "alert_on_partial_exit",
                 "alert_on_risk_rejected", "telegram_enabled", "email_enabled")
    for k in bool_keys:
        merged[k] = int(bool(merged[k]))
    merged["min_confidence"] = float(merged["min_confidence"])
    with connect() as c:
        c.execute(
            "UPDATE live_trigger_config SET "
            "enabled=?, min_confidence=?, exploit_mode_only=?, "
            "alert_on_entry=?, alert_on_full_exit=?, alert_on_stop_loss=?, "
            "alert_on_trailing_stop=?, alert_on_partial_exit=?, "
            "alert_on_risk_rejected=?, telegram_enabled=?, email_enabled=?, "
            "email_recipients=?, actor_filter=?, updated_at=? WHERE id=1",
            (merged["enabled"], merged["min_confidence"],
             merged["exploit_mode_only"],
             merged["alert_on_entry"], merged["alert_on_full_exit"],
             merged["alert_on_stop_loss"], merged["alert_on_trailing_stop"],
             merged["alert_on_partial_exit"], merged["alert_on_risk_rejected"],
             merged["telegram_enabled"], merged["email_enabled"],
             merged["email_recipients"], merged["actor_filter"],
             myt_iso()),
        )


# --------------------------------------------------------------------- #
# Filter logic
# --------------------------------------------------------------------- #

EVENT_TO_FLAG = {
    "ENTRY":         "alert_on_entry",
    "FULL_EXIT":     "alert_on_full_exit",
    "STOP_LOSS":     "alert_on_stop_loss",
    "TRAILING_STOP": "alert_on_trailing_stop",
    "PARTIAL_EXIT":  "alert_on_partial_exit",
    "RISK_REJECTED": "alert_on_risk_rejected",
}


def _should_fire(event_type: str, trade: dict | None, actor: str,
                 cfg: dict) -> tuple[bool, str]:
    """
    Returns (should_fire, reason). Reason explains skip if False.
    """
    if not cfg.get("enabled"):
        return False, "live_trigger_disabled"

    flag = EVENT_TO_FLAG.get(event_type)
    if flag and not cfg.get(flag):
        return False, f"event_{event_type}_disabled"

    actor_filter = (cfg.get("actor_filter") or "AGENT").upper()
    if actor_filter == "AGENT" and actor != "AGENT":
        return False, "actor_not_agent"

    # Confidence + EXPLOIT-mode checks only apply to ENTRY signals
    if event_type == "ENTRY" and trade:
        conf = trade.get("confidence_score") or 0
        if conf < cfg.get("min_confidence", 70.0):
            return False, f"confidence_{conf:.0f}_below_threshold"

        if cfg.get("exploit_mode_only"):
            state = get_scheduler_state()
            if state.get("exploration_mode"):
                return False, "still_in_explore_mode"

    return True, "ok"


def _was_already_sent(trade_id: int, event_type: str) -> bool:
    """Dedup: per (trade_id, event_type)."""
    if not trade_id:
        return False
    with connect(readonly=True) as c:
        row = c.execute(
            "SELECT id FROM alert_log WHERE trade_id=? AND event_type=? "
            "AND status IN ('SENT','DASHBOARD') LIMIT 1",
            (trade_id, event_type),
        ).fetchone()
    return row is not None


# --------------------------------------------------------------------- #
# Formatters
# --------------------------------------------------------------------- #

def _icon(event: str) -> str:
    return {
        "ENTRY": "🟢", "FULL_EXIT": "✅",
        "STOP_LOSS": "🔴", "TRAILING_STOP": "🟡",
        "PARTIAL_EXIT": "🔵", "RISK_REJECTED": "⛔",
    }.get(event, "🔔")


def _format_entry(t: dict, ss: dict) -> tuple[str, str, str]:
    """Returns (text, html, subject) — Professional Swing Edition."""
    regime = t.get("market_regime") or "—"
    brain = "🎯 EXPLOIT" if not ss.get("exploration_mode") else "🔬 EXPLORE"
    conf = t.get("confidence_score") or 0
    risk_pct = t.get("actual_risk_pct") or 0
    sector = t.get("sector") or "—"
    # Professional additions
    exp_target = ss.get("exploration_trades_target", 50)
    from repository import closed_trades as _ct
    try:
        closed_n = len(_ct())
    except Exception:
        closed_n = 0
    progress = f"{closed_n}/{exp_target}" if ss.get("exploration_mode") else f"{closed_n} trades learned"

    # Entry indicators for pro context
    ind = {}
    try:
        import json
        ind = json.loads(t.get("entry_indicators_json") or "{}") if isinstance(t.get("entry_indicators_json"), str) else (t.get("entry_indicators") or {})
    except Exception:
        ind = t.get("entry_indicators") or {}
    rsi = ind.get("rsi", "—")
    vol_ratio = ind.get("vol_ratio", "—")
    atr = ind.get("atr", "—")

    # Risk R-multiple calc
    entry = float(t.get("entry_price", 0))
    sl = float(t.get("stop_loss", 0))
    tp1 = float(t.get("tp1", 0))
    tp2 = float(t.get("tp2", 0))
    tp3 = float(t.get("tp3", 0))
    risk_per_share = max(entry - sl, 0.001)
    r_tp1 = (tp1 - entry) / risk_per_share if risk_per_share else 0
    r_tp2 = (tp2 - entry) / risk_per_share if risk_per_share else 0
    r_tp3 = (tp3 - entry) / risk_per_share if risk_per_share else 0

    cur = _ccy()
    # Professional swing template
    text = (
        f"{_icon('ENTRY')} {t.get('signal_type','ENTRY')} — {t['ticker']} {t.get('name','')}\n"
        f"⏰ {t.get('logged_at','')} MYT | 🧠 {brain} {progress} | 📊 Regime {regime} | Sector {sector}\n"
        f"🎯 Confidence {conf:.0f}/100 | RSI {rsi} | Vol {vol_ratio}x | ATR {atr}\n"
        f"----------------------------------------\n"
        f"BUY {t.get('shares',0):,} @ {cur}{entry:.3f} | SL {cur}{sl:.3f} (-{risk_pct:.1f}%) | Risk {cur}{risk_per_share * t.get('shares',0):,.0f}\n"
        f"TP1 {cur}{tp1:.3f} ({r_tp1:.1f}R) | TP2 {cur}{tp2:.3f} ({r_tp2:.1f}R) | TP3 {cur}{tp3:.3f} ({r_tp3:.1f}R)\n"
        f"----------------------------------------\n"
        f"🧠 Brain: {(t.get('entry_reasoning') or '')[:500]}\n"
        f"----------------------------------------\n"
        f"📝 How to trade (manual): Place LIMIT/MARKET in your broker at {cur}{entry:.3f}, "
        f"stop {cur}{sl:.3f}, targets as above. Position size already risk-adjusted (1% of capital). "
        f"If you close early in broker, also close in Dashboard → Portfolio → Manual Close so brain learns."
    )
    html = ("<pre style=\"font-family:monospace;font-size:13px;white-space:pre-wrap;\">"
            + text.replace("<", "&lt;").replace(">", "&gt;")
            + "</pre>")
    subject = f"[BursaAI US-SWING] {t['ticker']} {t.get('signal_type','')} conf {conf:.0f} | {progress}"
    return text, html, subject


def _format_exit(t: dict, event_type: str, exit_price: float,
                 pnl: float) -> tuple[str, str, str]:
    cost = t.get("cost") or 1
    pnl_pct = (pnl / cost * 100) if cost > 0 else 0
    # R-multiple
    risk_per_share = float(t.get("risk_per_share") or 0)
    shares = int(t.get("shares") or 0)
    risk_amount = risk_per_share * shares if risk_per_share and shares else 1
    r_mult = pnl / risk_amount if risk_amount else 0
    try:
        logged = datetime.strptime(t["logged_at"], "%Y-%m-%d %H:%M:%S")
        closed = datetime.strptime(t.get("closed_at") or myt_iso(),
                                    "%Y-%m-%d %H:%M:%S")
        held = (closed - logged).days
    except Exception:
        held = "?"

    label_map = {
        "STOP_LOSS": "STOP LOSS HIT", "TRAILING_STOP": "TRAILING STOP HIT",
        "FULL_EXIT": "FULL EXIT", "PARTIAL_EXIT": "PARTIAL EXIT",
    }
    label = label_map.get(event_type, event_type)
    outcome = t.get("outcome") or ("WIN" if pnl > 0 else "LOSS")
    # Professional exit quality
    exit_type = t.get("exit_type") or event_type

    cur = _ccy()
    text = (
        f"{_icon(event_type)} {label} — {t['ticker']} {t.get('name','')} | Exit: {exit_type} | {outcome}\n"
        f"⏰ {t.get('closed_at','')} MYT | Held {held}d | Conf {t.get('confidence_score',0):.0f} | Regime {t.get('market_regime','—')}\n"
        f"----------------------------------------\n"
        f"Entry {cur}{t.get('entry_price',0):.3f} → Exit {cur}{exit_price:.3f} | {cur}{pnl:+,.2f} ({pnl_pct:+.2f}%) | {r_mult:+.2f}R\n"
        f"Shares {shares:,} | Risk was {cur}{risk_amount:,.0f} | Cost {cur}{cost:,.0f}\n"
        f"SL {cur}{t.get('stop_loss',0):.3f} | TP1 {cur}{t.get('tp1',0):.3f} TP2 {cur}{t.get('tp2',0):.3f} TP3 {cur}{t.get('tp3',0):.3f}\n"
        f"----------------------------------------\n"
        f"🧠 Brain learns from this R={r_mult:.2f} outcome — state priors updated."
    )
    html = ("<pre style=\"font-family:monospace;font-size:13px;white-space:pre-wrap;\">"
            + text.replace("<", "&lt;").replace(">", "&gt;")
            + "</pre>")
    subject = f"[BursaAI US-SWING] {label} {t['ticker']} {outcome} {r_mult:+.2f}R | {cur}{pnl:+,.0f}"
    return text, html, subject


# --------------------------------------------------------------------- #
# Public hooks (called from trading_engine + scheduler)
# --------------------------------------------------------------------- #

def fire(event_type: str, trade_id: int | None, ticker: str | None,
         actor: str, payload: dict | None = None) -> dict | None:
    """
    Decide + fire (or skip) an alert. Safe to call from anywhere.
    Always swallows exceptions so the trading engine is never blocked.

    Returns the dispatch result dict, or None if skipped.
    """
    try:
        cfg = load_config()
        ss = get_scheduler_state()
        trade = get_trade(trade_id) if trade_id else None

        fire_it, reason = _should_fire(event_type, trade, actor, cfg)
        if not fire_it:
            # Record skip so the user can see why
            try:
                with connect() as c:
                    c.execute(
                        "INSERT INTO alert_log "
                        "(timestamp, event_type, trade_id, ticker, channel, "
                        " status, message, payload_json) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (myt_iso(), event_type, trade_id, ticker,
                         "FILTER", "SKIPPED_FILTER",
                         f"skipped: {reason}",
                         json.dumps(payload or {}, default=str)),
                    )
            except Exception:
                pass
            return None

        if _was_already_sent(trade_id, event_type):
            try:
                with connect() as c:
                    c.execute(
                        "INSERT INTO alert_log "
                        "(timestamp, event_type, trade_id, ticker, channel, "
                        " status, message) VALUES (?,?,?,?,?,?,?)",
                        (myt_iso(), event_type, trade_id, ticker,
                         "DEDUP", "DEDUPED", "already_sent"),
                    )
            except Exception:
                pass
            return None

        # ---- Format the message ----
        if event_type == "ENTRY":
            text, html, subject = _format_entry(trade or {}, ss)
        elif event_type in ("FULL_EXIT", "STOP_LOSS",
                            "TRAILING_STOP", "PARTIAL_EXIT"):
            exit_price = (payload or {}).get("fill_price",
                                              trade.get("exit_price", 0) if trade else 0)
            pnl = (payload or {}).get("net_pnl",
                                       trade.get("closed_pnl", 0) if trade else 0)
            text, html, subject = _format_exit(
                trade or {}, event_type, exit_price, pnl)
        else:
            text = f"{_icon(event_type)} {event_type} — {ticker}"
            html = text; subject = f"[BursaAI] {event_type} — {ticker}"

        # ---- Channels ----
        channels = {
            "telegram": bool(cfg.get("telegram_enabled")),
            "email": bool(cfg.get("email_enabled")),
            "dashboard": True,
        }
        recipients = [r.strip() for r in
                       (cfg.get("email_recipients") or "").split(",")
                       if r.strip()]
        return dispatch(event_type, text, html, subject,
                        trade_id, ticker, channels, recipients, payload)
    except Exception as e:
        log.error(f"live_trigger.fire failed for {event_type} {ticker}: {e}")
        return None


# --------------------------------------------------------------------- #
# Test alert (for the UI "Send test alert" button)
# --------------------------------------------------------------------- #

def send_test_alert() -> dict:
    cfg = load_config()
    text = (
        "🧪 TEST ALERT — BursaAI v3.1\n"
        f"Time: {myt_iso()} MYT\n\n"
        "If you can see this, your alert channel is working."
    )
    html = ("<pre style=\"font-family:monospace;font-size:13px;\">"
            + text.replace("<", "&lt;").replace(">", "&gt;")
            + "</pre>")
    channels = {
        "telegram": bool(cfg.get("telegram_enabled")),
        "email": bool(cfg.get("email_enabled")),
        "dashboard": True,
    }
    recipients = [r.strip() for r in
                   (cfg.get("email_recipients") or "").split(",")
                   if r.strip()]
    return dispatch("TEST", text, html, "[BursaAI] Test alert",
                    None, None, channels, recipients)


# --------------------------------------------------------------------- #
# Read helpers (dashboard)
# --------------------------------------------------------------------- #

def recent_alerts(limit: int = 100,
                  channel: str | None = None,
                  event_type: str | None = None) -> list[dict]:
    sql = "SELECT * FROM alert_log WHERE 1=1"
    args: list = []
    if channel:
        sql += " AND channel=?"; args.append(channel)
    if event_type:
        sql += " AND event_type=?"; args.append(event_type)
    sql += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    with connect(readonly=True) as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
