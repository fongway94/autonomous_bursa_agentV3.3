"""
Professional Swing Trader — Daily Brain Summary
Generates a concise Telegram-ready summary of:
- State priors (top states by sample)
- Strategy / sector performance
- Calibration
- Recent trades
- Current regime

Used by scheduler daily maintenance (01:00 MYT) and manual button.
"""

from __future__ import annotations
from datetime import datetime
from typing import Dict, List

from db import connect, myt_iso, get_myt_now
from repository import closed_trades, get_scheduler_state, load_account
from learner import get_strategy_performance_report
from market_profiles import active_profile, active_market_code

def _ccy():
    try:
        return active_profile().currency_symbol
    except Exception:
        return "$"

def get_brain_state_table(limit: int = 10) -> List[Dict]:
    """Top N states by n_trades with posterior mean."""
    try:
        with connect(readonly=True) as c:
            rows = c.execute(
                "SELECT state_id, action, alpha, beta, n_trades, total_r, last_updated "
                "FROM state_priors ORDER BY n_trades DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            alpha = float(d["alpha"])
            beta = float(d["beta"])
            mean = alpha / (alpha + beta) if (alpha+beta)>0 else 0.5
            avg_r = float(d["total_r"])/max(int(d["n_trades"]),1)
            out.append({
                "state_id": d["state_id"],
                "action": d["action"],
                "n": int(d["n_trades"]),
                "win_prob": round(mean*100,1),
                "avg_R": round(avg_r,2),
                "alpha": round(alpha,1),
                "beta": round(beta,1),
            })
        return out
    except Exception:
        return []

def get_recent_closed(n: int = 5) -> List[Dict]:
    try:
        trades = closed_trades()
        trades_sorted = sorted(trades, key=lambda x: x.get("closed_at",""), reverse=True)[:n]
        return trades_sorted
    except Exception:
        return []

def get_brain_summary_text() -> str:
    """Professional daily summary for Telegram."""
    ccy = _ccy()
    market = active_market_code()
    profile = active_profile()
    ss = get_scheduler_state()
    acc = load_account()
    perf = get_strategy_performance_report()

    # Exploration progress
    explore = bool(ss.get("exploration_mode",1))
    target = int(ss.get("exploration_trades_target",50))
    done = perf["summary"]["total_trades"]
    mode_str = f"🔬 EXPLORE {done}/{target}" if explore else f"🎯 EXPLOIT {done} trades learned"
    win_rate = perf["summary"].get("win_rate",0)
    total_pnl = perf["summary"].get("total_pnl_rm",0)
    avg_win = perf["summary"].get("avg_win_rm",0)
    avg_loss = perf["summary"].get("avg_loss_rm",0)

    # Equity
    equity = acc.get("total_equity", acc.get("cash_balance",0))
    cash = acc.get("cash_balance",0)
    ret_pct = (equity / acc.get("initial_capital",1) -1)*100 if acc.get("initial_capital") else 0

    # State priors
    states = get_brain_state_table(8)

    # Calibration (if enough trades)
    calib_line = ""
    try:
        from evaluation import calibration_buckets
        buckets = calibration_buckets(closed_trades(), n_buckets=3)
        if buckets:
            # check top bucket
            top = buckets[-1]
            calib_line = f"Top conf {top['predicted_pct']:.0f}% → realized {top['realized_win_rate_pct']:.0f}% (n={top['n_trades']})"
    except Exception:
        pass

    # Regime
    regime_txt = "—"
    try:
        from market_analyzer import get_full_market_analysis
        mr = get_full_market_analysis()
        rd = mr.get("regime_data",{})
        regime_txt = f"{rd.get('regime','?')} {rd.get('conviction',0):.0f}% | {mr.get('guidance','')[:120]}"
    except Exception:
        pass

    # Recent trades
    recent = get_recent_closed(5)
    recent_lines = []
    for t in recent:
        rps = t.get("risk_per_share") or 0
        shares = t.get("shares") or 1
        risk = rps*shares if rps else 1
        pnl = t.get("closed_pnl") or t.get("realized_pnl") or 0
        r_mult = pnl / risk if risk else 0
        recent_lines.append(f"{t['ticker']} {t.get('outcome','?')} {r_mult:+.2f}R {ccy}{pnl:+.0f} held {(t.get('closed_at','')[:10])}")

    txt = (
        f"🧠 DAILY BRAIN — {profile.flag_emoji} {profile.display_name} {market} SWING | {myt_iso()} MYT\n"
        f"Mode: {mode_str} | WR {win_rate}% | PnL {ccy}{total_pnl:+,.0f} (avg W {ccy}{avg_win:.0f} L {ccy}{avg_loss:.0f})\n"
        f"Equity {ccy}{equity:,.0f} (Cash {ccy}{cash:,.0f}) {ret_pct:+.1f}% | Regime: {regime_txt}\n"
        f"----------------------------------------\n"
        f"Top learned states (state_id, action, n, win%, avgR):\n"
    )
    if states:
        for s in states:
            txt += f"  #{s['state_id']} {s['action']} n={s['n']} win={s['win_prob']}% avgR={s['avg_R']} α={s['alpha']} β={s['beta']}\n"
    else:
        txt += "  No state priors yet — need closed trades.\n"

    txt += "----------------------------------------\n"
    if calib_line:
        txt += f"Calibration: {calib_line}\n"
    if perf.get("by_strategy"):
        txt += "By strategy:\n"
        for k,v in list(perf["by_strategy"].items())[:4]:
            txt += f"  {k}: {v['wins']}W/{v['losses']}L WR{v.get('win_rate',0)}% PnL{ccy}{v.get('total_pnl',0):+.0f}\n"
    if perf.get("by_sector"):
        txt += "By sector (top):\n"
        # sort by pnl
        sorted_sec = sorted(perf["by_sector"].items(), key=lambda x: x[1].get("total_pnl",0), reverse=True)[:4]
        for k,v in sorted_sec:
            txt += f"  {k}: WR{v.get('win_rate',0)}% PnL{ccy}{v.get('total_pnl',0):+.0f} ({v['total']} trades)\n"

    txt += "----------------------------------------\nRecent closes:\n"
    if recent_lines:
        for line in recent_lines:
            txt += f"  {line}\n"
    else:
        txt += "  No closed trades yet.\n"

    txt += (
        "----------------------------------------\n"
        "What to do: Follow GOLD BUY signals with conf >= threshold (BULL 60%, NEUTRAL 65%, BEAR 65%). "
        "Use 1% risk sizing already computed. Keep max positions per regime. Review calibration — if top bucket WR <50%, raise threshold.\n"
        f"DB: {active_market_code()} brain isolated, no cross-contamination."
    )
    return txt

def send_daily_brain_summary() -> dict:
    """Send summary via Telegram (if configured). Returns dispatch result."""
    try:
        text = get_brain_summary_text()
        from notifier import dispatch
        from live_trigger import load_config
        cfg = load_config()
        channels = {
            "telegram": bool(cfg.get("telegram_enabled",1)),
            "email": False,  # daily summary only telegram to avoid spam
            "dashboard": True,
        }
        # Use notifier directly
        result = dispatch(
            event_type="DAILY_BRAIN",
            message_text=text,
            message_html=f"<pre style='font-family:monospace;white-space:pre-wrap;'>{text}</pre>",
            subject=f"[BursaAI] Daily Brain {active_market_code()} {myt_iso()}",
            trade_id=None,
            ticker=None,
            channels=channels,
            recipients=[],
            payload={"type":"daily_brain"},
        )
        return result
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    print(get_brain_summary_text())
