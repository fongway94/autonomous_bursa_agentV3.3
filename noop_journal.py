"""
noop_journal.py — Repository access for the NOOP decision_journal table.

Follows the existing repository.py conventions: all SQL lives here, business
logic elsewhere. Per-(market,mode) DB via db.connect(). Never raises on the
write path in a way that could crash a scheduler cycle — callers wrap in
try/except, but we also degrade gracefully here.
"""

from __future__ import annotations

from typing import Any, Optional

from db import connect, myt_iso


# Columns we accept on insert (mirrors decision_tiers.build_decision_record()).
_INSERT_COLUMNS = (
    "cycle_id", "decided_at", "review_at", "market", "mode", "ticker", "name",
    "sector", "tier", "would_execute", "signal", "confidence", "regime",
    "regime_threshold", "state_id", "entry", "stop_loss", "tp1", "tp2", "tp3",
    "rsi", "vol_ratio", "atr", "reasoning", "expected_scenario",
    "invalidation_condition", "what_proves_wrong", "status", "outcome",
    "outcome_r", "max_favorable_pct", "max_adverse_pct", "resolved_at",
    "resolver_notes",
)


def insert_decision(record: dict) -> int:
    """
    Insert one decision record. Returns the new row id (or -1 on failure).
    Missing keys default to None / sensible defaults.
    """
    rec = dict(record)
    rec.setdefault("status", "OPEN")
    rec.setdefault("would_execute", 0)
    rec.setdefault("decided_at", myt_iso())

    cols = list(_INSERT_COLUMNS)
    placeholders = ",".join("?" for _ in cols)
    values = [rec.get(c) for c in cols]
    sql = (
        f"INSERT INTO decision_journal ({','.join(cols)}) "
        f"VALUES ({placeholders})"
    )
    with connect() as c:
        cur = c.execute(sql, values)
        return int(cur.lastrowid)


def insert_decisions(records: list[dict]) -> int:
    """Bulk insert. Returns count inserted. Skips malformed rows defensively."""
    n = 0
    for r in records:
        try:
            insert_decision(r)
            n += 1
        except Exception:
            # Never let one bad record abort the batch — journaling must not
            # crash the cycle.
            continue
    return n


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def get_open_decisions(due_only_before: Optional[str] = None,
                       limit: Optional[int] = None) -> list[dict]:
    """
    Return OPEN journal rows. If due_only_before is given (ISO ts), only rows
    whose review_at <= that timestamp are returned (i.e. due for resolution).
    """
    sql = "SELECT * FROM decision_journal WHERE status = 'OPEN'"
    params: list[Any] = []
    if due_only_before is not None:
        sql += " AND review_at IS NOT NULL AND review_at <= ?"
        params.append(due_only_before)
    sql += " ORDER BY review_at ASC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    with connect(readonly=True) as c:
        rows = c.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def resolve_decision(decision_id: int, *, outcome: str, outcome_r: Optional[float],
                     max_favorable_pct: Optional[float] = None,
                     max_adverse_pct: Optional[float] = None,
                     resolver_notes: str = "") -> None:
    """Mark a decision RESOLVED with its shadow outcome."""
    with connect() as c:
        c.execute(
            "UPDATE decision_journal SET "
            "status='RESOLVED', outcome=?, outcome_r=?, "
            "max_favorable_pct=?, max_adverse_pct=?, resolved_at=?, "
            "resolver_notes=? WHERE id=?",
            (outcome, outcome_r, max_favorable_pct, max_adverse_pct,
             myt_iso(), resolver_notes, int(decision_id)),
        )


def mark_skipped(decision_id: int, reason: str = "") -> None:
    """Mark a decision SKIPPED (e.g. could not resolve — no data)."""
    with connect() as c:
        c.execute(
            "UPDATE decision_journal SET status='SKIPPED', resolver_notes=?, "
            "resolved_at=? WHERE id=?",
            (reason, myt_iso(), int(decision_id)),
        )


def get_recent_decisions(limit: int = 200) -> list[dict]:
    with connect(readonly=True) as c:
        rows = c.execute(
            "SELECT * FROM decision_journal ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def tier_counts(since: Optional[str] = None) -> dict:
    """Counts per tier (optionally since an ISO timestamp)."""
    sql = "SELECT tier, COUNT(*) AS n FROM decision_journal"
    params: list[Any] = []
    if since:
        sql += " WHERE decided_at >= ?"
        params.append(since)
    sql += " GROUP BY tier"
    out = {"A": 0, "B": 0, "C": 0, "D": 0}
    with connect(readonly=True) as c:
        for r in c.execute(sql, params).fetchall():
            out[r["tier"]] = int(r["n"])
    return out


def count_all() -> int:
    with connect(readonly=True) as c:
        return int(c.execute("SELECT COUNT(*) AS n FROM decision_journal")
                   .fetchone()["n"])
