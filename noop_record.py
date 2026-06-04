"""
noop_record.py — Orchestration: turn a screened cycle into journal records.

This is the single integration seam between the existing screeners/scheduler and
the new NOOP measurement layer. Given the screened setups for a cycle plus the
regime context, it:

    1. classifies every setup into a tier (A/B/C/D)        [decision_tiers]
    2. builds a structured, falsifiable decision record     [decision_tiers]
    3. writes them all to the per-(market,mode) journal      [noop_journal]

It records ALL setups, including those we would NOT execute, so we can learn
from skipped trades (the whole point). It places NO orders. It is fully
try/except wrapped by callers and also degrades gracefully internally so it can
never crash a scheduler cycle.

Tier D handling: by default we do NOT journal a row per non-setup ticker (that
would be thousands of empty rows). Tier D is captured as an aggregate count in
the returned summary. This keeps the journal focused and the DB small.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

import decision_tiers
import noop_journal


# Review horizon per mode (calendar days for swing; intraday resolves same-day).
_SWING_REVIEW_DAYS = 10
_INTRADAY_REVIEW_DAYS = 1


def _now_iso() -> str:
    try:
        from db import myt_iso
        return myt_iso()
    except Exception:
        return datetime.utcnow().isoformat()


def _review_iso(decided: str, days: int) -> str:
    try:
        dt = datetime.fromisoformat(str(decided).replace("Z", "").split(".")[0])
    except Exception:
        dt = datetime.utcnow()
    return (dt + timedelta(days=days)).isoformat()


def _state_id_for(setup: dict) -> Optional[int]:
    """Compute the SAME state_id the brain uses, so journal rows are joinable."""
    try:
        from learner import discretize_state
        rsi = setup.get("rsi")
        vol = setup.get("vol_ratio")
        macd = setup.get("macd_hist", 0.0) or 0.0
        ind = setup.get("indicators", {}) or {}
        ema_dist = ind.get("ema_trend_distance")
        if ema_dist is None:
            # Derive from entry vs ema_trend if available.
            entry = setup.get("entry") or setup.get("price")
            ema_t = setup.get("ema_trend")
            if entry and ema_t:
                ema_dist = (float(entry) - float(ema_t)) / float(ema_t) * 100.0
            else:
                ema_dist = 0.0
        if rsi is None or vol is None:
            return None
        return int(discretize_state(float(rsi), float(vol),
                                    float(ema_dist), float(macd)))
    except Exception:
        return None


def _iter_setups(setups: Any) -> Iterable[dict]:
    """Accept a list[dict] or a pandas DataFrame; yield dict rows."""
    if setups is None:
        return []
    # DataFrame?
    if hasattr(setups, "iterrows"):
        return (row.to_dict() for _, row in setups.iterrows())
    return iter(setups)


def record_cycle_decisions(
    setups: Any,
    *,
    market: str,
    mode: str,
    regime: str,
    regime_threshold: float,
    cycle_id: Optional[str] = None,
    journal_tier_d: bool = False,
) -> dict:
    """
    Classify and journal all setups for one cycle.

    Args:
        setups: list[dict] or DataFrame of screener outputs (each must have at
                least 'ticker', 'signal', 'confidence').
        market: active market code, e.g. "MY" / "US".
        mode:   "SWING" / "INTRADAY".
        regime: regime label, e.g. "BULL" / "NEUTRAL" / "BEAR".
        regime_threshold: 0-100 confidence threshold for tier A qualification.
        cycle_id: optional id grouping records from one cycle.
        journal_tier_d: if True, also write a row for non-setup tickers
                        (default False — Tier D is aggregated, not stored).

    Returns a summary dict with per-tier counts and number written.
    """
    decided_at = _now_iso()
    review_days = _INTRADAY_REVIEW_DAYS if str(mode).upper() == "INTRADAY" else _SWING_REVIEW_DAYS
    review_at = _review_iso(decided_at, review_days)

    summary = {"written": 0, "A": 0, "B": 0, "C": 0, "D": 0, "errors": []}
    records = []

    for setup in _iter_setups(setups):
        try:
            sig = setup.get("signal")
            conf = setup.get("confidence")
            tier = decision_tiers.classify_tier(sig, conf, regime_threshold)
            summary[tier] = summary.get(tier, 0) + 1

            if tier == "D" and not journal_tier_d:
                continue

            rec = decision_tiers.build_decision_record(
                setup,
                market=market,
                mode=mode,
                regime=regime,
                regime_threshold=regime_threshold,
                state_id=_state_id_for(setup),
                decided_at=decided_at,
                review_at=review_at,
                cycle_id=cycle_id,
            )
            records.append(rec)
        except Exception as e:
            summary["errors"].append(str(e))
            continue

    if records:
        try:
            summary["written"] = noop_journal.insert_decisions(records)
        except Exception as e:
            summary["errors"].append(f"insert: {e}")

    return summary
