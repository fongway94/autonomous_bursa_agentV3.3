"""
noop_resolver.py — Shadow-outcome resolver for the NOOP decision journal.

For every OPEN journal row whose review_at has passed, this resolver replays the
*frozen* exit rules (stop-loss / take-profit / timeout) on REAL price history,
on paper, and labels the outcome:

    WIN     — TP1 touched before stop, within the horizon
    LOSS    — stop touched before TP1
    FLAT    — neither touched by review (timed out)
    UNKNOWN — could not determine (data gap) -> row marked SKIPPED instead

Crucially this resolves ALL tiers (A/B/C/D BUY setups), so setups we did NOT
take still become labeled training examples. That is what dissolves the
"strict -> starvation / loose -> noise" dilemma.

This module places NO orders. It only reads price history and writes journal
labels. It is import-safe and degrades gracefully if the data provider is down.

First-touch convention (deterministic, conservative): within a single bar we
cannot know whether the high or low came first. We resolve LOSS-before-WIN when
both stop and TP appear in the same bar (pessimistic / honest). This avoids
optimistic shadow results that would flatter the strategy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import noop_journal
from noop_safety import noop_mode_active


# How many calendar days of history to pull when resolving. Generous so the
# review window is always covered even across weekends/holidays.
_HISTORY_LOOKBACK_DAYS = 90


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    s = str(ts)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt) + 6], fmt)
        except Exception:
            continue
    # Last resort: try fromisoformat on the date portion
    try:
        return datetime.fromisoformat(s.replace("Z", "").split(".")[0])
    except Exception:
        return None


def _fetch_bars(ticker: str, interval: str):
    """Fetch recent OHLC bars via the existing data pipeline. Returns df or None."""
    try:
        import data_provider
        df = data_provider.get_history(
            ticker, period=None, interval=interval,
            start=None, end=None,
        )
        if df is None or len(df) == 0:
            return None
        return df
    except Exception:
        return None


def _resolve_single(row: dict, *, interval: str = "1d") -> dict:
    """
    Compute the shadow outcome for one decision row.

    Returns a dict:
      {"resolvable": bool, "outcome": str|None, "outcome_r": float|None,
       "mfe_pct": float|None, "mae_pct": float|None, "notes": str}
    """
    entry = row.get("entry")
    stop = row.get("stop_loss")
    tp1 = row.get("tp1")
    decided_at = _parse_ts(row.get("decided_at"))
    review_at = _parse_ts(row.get("review_at"))
    ticker = row.get("ticker")

    # Tier D / non-BUY rows have no entry/stop -> nothing to simulate.
    if row.get("tier") == "D" or entry is None or stop is None or tp1 is None:
        return {"resolvable": False, "notes": "no entry/stop/tp (tier D or incomplete)"}

    if decided_at is None:
        return {"resolvable": False, "notes": "unparseable decided_at"}

    df = _fetch_bars(ticker, interval)
    if df is None:
        return {"resolvable": False, "notes": "no price data available"}

    # Normalise column access (data_provider returns Open/High/Low/Close).
    cols = {c.lower(): c for c in df.columns}
    hi_c = cols.get("high")
    lo_c = cols.get("low")
    cl_c = cols.get("close")
    if not (hi_c and lo_c and cl_c):
        return {"resolvable": False, "notes": "price df missing OHLC columns"}

    entry = float(entry)
    stop = float(stop)
    tp1 = float(tp1)
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return {"resolvable": False, "notes": "non-positive risk (stop >= entry)"}

    # Only consider bars strictly AFTER the decision timestamp and up to review.
    try:
        idx = df.index
        # Index may be tz-aware; compare by date string to be robust.
        def _bar_dt(ix):
            try:
                return ix.to_pydatetime().replace(tzinfo=None)
            except Exception:
                return None

        outcome = None
        mfe = 0.0  # max favorable % (high above entry)
        mae = 0.0  # max adverse % (low below entry)
        bars_seen = 0
        for i in range(len(df)):
            bdt = _bar_dt(idx[i])
            if bdt is not None and bdt <= decided_at:
                continue  # skip bars at/before decision (entry bar excluded)
            if review_at is not None and bdt is not None and bdt > review_at:
                break
            bars_seen += 1
            hi = float(df[hi_c].iloc[i])
            lo = float(df[lo_c].iloc[i])

            mfe = max(mfe, (hi - entry) / entry * 100.0)
            mae = min(mae, (lo - entry) / entry * 100.0)

            hit_stop = lo <= stop
            hit_tp = hi >= tp1
            if hit_stop and hit_tp:
                # Ambiguous single bar -> pessimistic: count the loss first.
                outcome = "LOSS"
                break
            if hit_stop:
                outcome = "LOSS"
                break
            if hit_tp:
                outcome = "WIN"
                break

        if bars_seen == 0:
            return {"resolvable": False, "notes": "no bars after decision yet"}

        if outcome is None:
            outcome = "FLAT"  # timed out, neither level touched

        if outcome == "WIN":
            outcome_r = (tp1 - entry) / risk_per_share
        elif outcome == "LOSS":
            outcome_r = -1.0  # stop = -1R by construction
        else:  # FLAT — mark-to-last-close R
            last_close = float(df[cl_c].iloc[-1])
            outcome_r = (last_close - entry) / risk_per_share

        return {
            "resolvable": True,
            "outcome": outcome,
            "outcome_r": round(float(outcome_r), 3),
            "mfe_pct": round(float(mfe), 3),
            "mae_pct": round(float(mae), 3),
            "notes": f"resolved over {bars_seen} bars ({interval})",
        }
    except Exception as e:
        return {"resolvable": False, "notes": f"resolver error: {e}"}


def resolve_due(now_iso: Optional[str] = None, *, interval: str = "1d",
                limit: Optional[int] = None) -> dict:
    """
    Resolve all OPEN journal rows whose review_at <= now.

    Returns a summary dict. Safe to call from a scheduled task; never raises.
    """
    from db import myt_iso
    now_iso = now_iso or myt_iso()

    summary = {"checked": 0, "resolved": 0, "skipped": 0,
               "wins": 0, "losses": 0, "flats": 0, "errors": []}
    try:
        due = noop_journal.get_open_decisions(due_only_before=now_iso, limit=limit)
    except Exception as e:
        summary["errors"].append(f"fetch: {e}")
        return summary

    for row in due:
        summary["checked"] += 1
        try:
            res = _resolve_single(row, interval=interval)
            if not res.get("resolvable"):
                noop_journal.mark_skipped(row["id"], reason=res.get("notes", ""))
                summary["skipped"] += 1
                continue
            noop_journal.resolve_decision(
                row["id"],
                outcome=res["outcome"],
                outcome_r=res["outcome_r"],
                max_favorable_pct=res.get("mfe_pct"),
                max_adverse_pct=res.get("mae_pct"),
                resolver_notes=res.get("notes", ""),
            )
            summary["resolved"] += 1
            if res["outcome"] == "WIN":
                summary["wins"] += 1
            elif res["outcome"] == "LOSS":
                summary["losses"] += 1
            else:
                summary["flats"] += 1
        except Exception as e:
            summary["errors"].append(f"{row.get('ticker')}: {e}")
            continue

    return summary
