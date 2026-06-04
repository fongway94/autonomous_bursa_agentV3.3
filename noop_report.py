"""
noop_report.py — Weekly NOOP learning summary (descriptive only).

Reads the decision_journal and produces decision-QUALITY metrics for the current
(market, mode). It NEVER changes any rule, threshold, or risk parameter — it
only surfaces evidence for a human reviewer. Lessons are *generated*, not
*applied* (enforced conceptually here; rule mutation is blocked in noop_safety).

Metrics (the right ones for weeks 1-12):
  * tier counts (A/B/C/D)
  * resolved counts + win/loss/flat per tier
  * false-positive rate  (A/B that resolved LOSS)
  * missed-opportunity rate (B/C that resolved WIN — cost of being strict)
  * confidence calibration buckets (predicted vs actual win rate on resolved BUYs)
  * expectancy in R per tier
"""

from __future__ import annotations

from typing import Optional

from db import connect


def _resolved_buys(since: Optional[str]):
    sql = ("SELECT tier, confidence, outcome, outcome_r FROM decision_journal "
           "WHERE status='RESOLVED' AND tier IN ('A','B','C') "
           "AND outcome IS NOT NULL")
    params = []
    if since:
        sql += " AND decided_at >= ?"
        params.append(since)
    with connect(readonly=True) as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def _calibration_buckets(rows) -> list[dict]:
    """Group resolved BUYs into confidence buckets and report actual win rate."""
    buckets = [(0, 50), (50, 60), (60, 70), (70, 80), (80, 90), (90, 101)]
    out = []
    for lo, hi in buckets:
        sub = [r for r in rows if lo <= (r["confidence"] or 0) < hi]
        n = len(sub)
        if n == 0:
            out.append({"bucket": f"{lo}-{hi}", "n": 0,
                        "actual_win_rate": None})
            continue
        wins = sum(1 for r in sub if r["outcome"] == "WIN")
        out.append({
            "bucket": f"{lo}-{hi}",
            "n": n,
            "actual_win_rate": round(wins / n * 100, 1),
        })
    return out


def weekly_summary(since: Optional[str] = None) -> dict:
    """
    Build the weekly NOOP learning summary for the active (market, mode) DB.

    `since`: ISO timestamp lower bound (e.g. 7 days ago). None = all-time.
    """
    # Tier counts (all statuses)
    tier_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
    with connect(readonly=True) as c:
        q = "SELECT tier, COUNT(*) n FROM decision_journal"
        p = []
        if since:
            q += " WHERE decided_at >= ?"
            p.append(since)
        q += " GROUP BY tier"
        for r in c.execute(q, p).fetchall():
            tier_counts[r["tier"]] = int(r["n"])

    rows = _resolved_buys(since)
    by_tier = {"A": [], "B": [], "C": []}
    for r in rows:
        by_tier.setdefault(r["tier"], []).append(r)

    def _stats(sub):
        n = len(sub)
        if n == 0:
            return {"n": 0, "wins": 0, "losses": 0, "flats": 0,
                    "win_rate": None, "expectancy_r": None}
        wins = sum(1 for r in sub if r["outcome"] == "WIN")
        losses = sum(1 for r in sub if r["outcome"] == "LOSS")
        flats = sum(1 for r in sub if r["outcome"] == "FLAT")
        rs = [r["outcome_r"] for r in sub if r["outcome_r"] is not None]
        exp = round(sum(rs) / len(rs), 3) if rs else None
        return {"n": n, "wins": wins, "losses": losses, "flats": flats,
                "win_rate": round(wins / n * 100, 1),
                "expectancy_r": exp}

    a_stats = _stats(by_tier.get("A", []))
    b_stats = _stats(by_tier.get("B", []))
    c_stats = _stats(by_tier.get("C", []))

    # False-positive rate: Tier A/B that LOST (we'd have taken / nearly taken).
    ab = by_tier.get("A", []) + by_tier.get("B", [])
    ab_n = len(ab)
    ab_losses = sum(1 for r in ab if r["outcome"] == "LOSS")
    false_positive_rate = round(ab_losses / ab_n * 100, 1) if ab_n else None

    # Missed-opportunity rate: Tier B/C that WON (we rejected/under-rated them).
    bc = by_tier.get("B", []) + by_tier.get("C", [])
    bc_n = len(bc)
    bc_wins = sum(1 for r in bc if r["outcome"] == "WIN")
    missed_opportunity_rate = round(bc_wins / bc_n * 100, 1) if bc_n else None

    # ----- Human-readable lessons (generated, NOT applied) -----
    lessons = []
    if false_positive_rate is not None and ab_n >= 10 and false_positive_rate > 60:
        lessons.append(
            f"High false-positive rate ({false_positive_rate}%) on Tier A/B over "
            f"{ab_n} resolved setups — the high-confidence signal may be weak. "
            f"REVIEW (do not auto-change)."
        )
    if missed_opportunity_rate is not None and bc_n >= 10 and missed_opportunity_rate > 50:
        lessons.append(
            f"High missed-opportunity rate ({missed_opportunity_rate}%) on Tier "
            f"B/C — the threshold may be too strict. REVIEW (do not auto-change)."
        )
    if a_stats["n"] >= 10 and a_stats["expectancy_r"] is not None and a_stats["expectancy_r"] <= 0:
        lessons.append(
            f"Tier A expectancy is non-positive ({a_stats['expectancy_r']}R over "
            f"{a_stats['n']} trades) — no demonstrated edge yet. Keep observing."
        )
    if not lessons:
        lessons.append("No strong signal yet — continue observing. Need more "
                       "resolved decisions before any conclusion.")

    return {
        "since": since,
        "tier_counts": tier_counts,
        "tier_A": a_stats,
        "tier_B": b_stats,
        "tier_C": c_stats,
        "false_positive_rate_pct": false_positive_rate,
        "missed_opportunity_rate_pct": missed_opportunity_rate,
        "calibration": _calibration_buckets(rows),
        "lessons": lessons,
        "note": "DESCRIPTIVE ONLY — no rules were changed. Human review required.",
    }
