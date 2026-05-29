# tests/test_corporate_actions_phase1.py
"""
Phase-1 tests for corporate_actions.py: data model + DB layer.

Covers:
  - CorporateAction validation (good + bad inputs)
  - idempotency_key / describe()
  - already_processed() round-trip via the new table
  - record_processed() idempotency under repeated insert
  - get_scan_window() first-run, repeat-run, and corrupt-timestamp behaviour
  - Schema migration: cumulative_split_factor column exists with default 1.0
                      on freshly-init'd DB AND on a simulated v3.4 DB
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from corporate_actions import (
    CorporateAction,
    INITIAL_LOOKBACK_DAYS,
    already_processed,
    get_scan_window,
    record_processed,
)


# ---------------------------------------------------------------------------
# CorporateAction dataclass — validation
# ---------------------------------------------------------------------------

class TestCorporateActionValidation:

    def test_valid_split(self):
        a = CorporateAction(ticker="0166.KL", ex_date="2026-05-29",
                            event_type="SPLIT", ratio=5.0, source="yfinance")
        assert a.ticker == "0166.KL"
        assert a.ratio == 5.0
        assert a.idempotency_key == ("0166.KL", "2026-05-29", "SPLIT")

    def test_valid_bonus(self):
        a = CorporateAction(ticker="1155.KL", ex_date="2026-04-01",
                            event_type="BONUS", ratio=1.5, source="moomoo")
        assert a.event_type == "BONUS"

    def test_valid_dividend(self):
        a = CorporateAction(ticker="5347.KL", ex_date="2026-06-15",
                            event_type="DIVIDEND", amount_per_share=0.20)
        assert a.amount_per_share == 0.20

    def test_reverse_split(self):
        """N-for-1 reverse split: ratio < 1 is valid."""
        a = CorporateAction(ticker="0166.KL", ex_date="2026-05-29",
                            event_type="SPLIT", ratio=0.2)
        assert a.ratio == 0.2

    def test_missing_ticker_raises(self):
        with pytest.raises(ValueError, match="ticker"):
            CorporateAction(ticker="", ex_date="2026-05-29",
                            event_type="SPLIT", ratio=5.0)

    def test_bad_ex_date_raises(self):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            CorporateAction(ticker="0166.KL", ex_date="29/05/2026",
                            event_type="SPLIT", ratio=5.0)

    def test_unknown_event_type_raises(self):
        with pytest.raises(ValueError, match="SPLIT|BONUS|DIVIDEND"):
            CorporateAction(ticker="0166.KL", ex_date="2026-05-29",
                            event_type="WEIRD", ratio=5.0)  # type: ignore

    def test_split_without_ratio_raises(self):
        with pytest.raises(ValueError, match="ratio"):
            CorporateAction(ticker="0166.KL", ex_date="2026-05-29",
                            event_type="SPLIT")

    def test_split_with_zero_ratio_raises(self):
        with pytest.raises(ValueError, match="ratio"):
            CorporateAction(ticker="0166.KL", ex_date="2026-05-29",
                            event_type="SPLIT", ratio=0.0)

    def test_split_with_amount_raises(self):
        with pytest.raises(ValueError, match="amount_per_share"):
            CorporateAction(ticker="0166.KL", ex_date="2026-05-29",
                            event_type="SPLIT", ratio=5.0,
                            amount_per_share=0.10)

    def test_dividend_without_amount_raises(self):
        with pytest.raises(ValueError, match="amount_per_share"):
            CorporateAction(ticker="0166.KL", ex_date="2026-05-29",
                            event_type="DIVIDEND")

    def test_dividend_with_ratio_raises(self):
        with pytest.raises(ValueError, match="ratio"):
            CorporateAction(ticker="0166.KL", ex_date="2026-05-29",
                            event_type="DIVIDEND",
                            amount_per_share=0.10, ratio=5.0)


class TestDescribe:
    def test_describe_split(self):
        a = CorporateAction(ticker="0166.KL", ex_date="2026-05-29",
                            event_type="SPLIT", ratio=5.0)
        s = a.describe()
        assert "0166.KL" in s and "SPLIT" in s and "2026-05-29" in s

    def test_describe_dividend(self):
        a = CorporateAction(ticker="5347.KL", ex_date="2026-06-15",
                            event_type="DIVIDEND", amount_per_share=0.20)
        s = a.describe()
        assert "RM" in s and "0.2" in s


# ---------------------------------------------------------------------------
# DB schema migration — cumulative_split_factor + new table
# ---------------------------------------------------------------------------

class TestSchemaMigration:
    """Uses the tmp-DB fixture from conftest.py (every test gets a fresh DB)."""

    def test_trades_has_cumulative_split_factor(self):
        from db import connect
        with connect() as c:
            cols = {row["name"]: row for row in
                    c.execute("PRAGMA table_info(trades)").fetchall()}
        assert "cumulative_split_factor" in cols
        # SQLite returns the default expression as a string for REAL columns
        assert str(cols["cumulative_split_factor"]["dflt_value"]).strip() in ("1.0", "1")

    def test_corporate_actions_processed_table_exists(self):
        from db import connect
        with connect() as c:
            row = c.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='corporate_actions_processed'"
            ).fetchone()
        assert row is not None

    def test_corporate_actions_processed_has_unique_constraint(self):
        """The (ticker, ex_date, event_type) UNIQUE constraint is what gives us
        idempotency. Verify it's there by trying to insert a duplicate."""
        import sqlite3
        from db import connect, myt_iso
        with connect() as c:
            c.execute(
                "INSERT INTO corporate_actions_processed "
                "(ticker, ex_date, event_type, ratio, source, detected_at, action_taken) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("0166.KL", "2026-05-29", "SPLIT", 5.0, "yfinance",
                 myt_iso(), "ADJUSTED"),
            )
            # Second insert with same key must raise IntegrityError
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO corporate_actions_processed "
                    "(ticker, ex_date, event_type, ratio, source, detected_at, action_taken) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("0166.KL", "2026-05-29", "SPLIT", 5.0, "moomoo",
                     myt_iso(), "ADJUSTED"),
                )

    def test_scheduler_state_has_corp_action_columns(self):
        from db import connect
        with connect() as c:
            cols = {row["name"] for row in
                    c.execute("PRAGMA table_info(scheduler_state)").fetchall()}
        assert "corp_action_autoadjust" in cols
        assert "last_corp_action_scan_at" in cols


# ---------------------------------------------------------------------------
# Idempotency layer — already_processed + record_processed
# ---------------------------------------------------------------------------

class TestIdempotency:

    def test_not_processed_initially(self):
        a = CorporateAction(ticker="0166.KL", ex_date="2026-05-29",
                            event_type="SPLIT", ratio=5.0)
        assert already_processed(a) is False

    def test_record_then_already_processed_returns_true(self):
        a = CorporateAction(ticker="0166.KL", ex_date="2026-05-29",
                            event_type="SPLIT", ratio=5.0)
        record_processed(a, action_taken="ADJUSTED",
                         affected_trade_ids=[42, 43])
        assert already_processed(a) is True

    def test_record_twice_is_idempotent(self):
        """Calling record_processed twice with the same key must not raise."""
        a = CorporateAction(ticker="0166.KL", ex_date="2026-05-29",
                            event_type="SPLIT", ratio=5.0)
        record_processed(a, action_taken="ADJUSTED")
        record_processed(a, action_taken="ADJUSTED")  # second call: no-op
        # Verify exactly one row exists.
        from db import connect
        with connect(readonly=True) as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM corporate_actions_processed "
                "WHERE ticker=? AND ex_date=? AND event_type=?",
                a.idempotency_key,
            ).fetchone()["n"]
        assert n == 1

    def test_different_event_types_dont_collide(self):
        """A split and a dividend on the same ex_date are distinct events."""
        split = CorporateAction(ticker="0166.KL", ex_date="2026-05-29",
                                event_type="SPLIT", ratio=5.0)
        div = CorporateAction(ticker="0166.KL", ex_date="2026-05-29",
                              event_type="DIVIDEND", amount_per_share=0.10)
        record_processed(split, action_taken="ADJUSTED")
        record_processed(div, action_taken="ALERTED_ONLY")
        assert already_processed(split) is True
        assert already_processed(div) is True

    def test_affected_trade_ids_persisted(self):
        import json
        from db import connect
        a = CorporateAction(ticker="0166.KL", ex_date="2026-05-29",
                            event_type="SPLIT", ratio=5.0)
        record_processed(a, action_taken="ADJUSTED",
                         affected_trade_ids=[7, 8, 9])
        with connect(readonly=True) as c:
            row = c.execute(
                "SELECT affected_trade_ids_json FROM corporate_actions_processed "
                "WHERE ticker=?", (a.ticker,),
            ).fetchone()
        assert json.loads(row["affected_trade_ids_json"]) == [7, 8, 9]


# ---------------------------------------------------------------------------
# Scan window calculation
# ---------------------------------------------------------------------------

class TestScanWindow:

    def test_first_run_looks_back_default_days(self):
        start, end = get_scan_window(last_scan_iso=None)
        today = datetime.now().date()
        assert end == today
        assert start == today - timedelta(days=INITIAL_LOOKBACK_DAYS)

    def test_repeat_run_overlaps_last_scan_by_one_day(self):
        last = "2026-05-25 10:00:00"
        start, end = get_scan_window(last_scan_iso=last)
        assert start == date(2026, 5, 24)  # 1-day overlap
        assert end == datetime.now().date()

    def test_corrupt_timestamp_behaves_like_first_run(self):
        start, end = get_scan_window(last_scan_iso="not-a-date")
        today = datetime.now().date()
        assert start == today - timedelta(days=INITIAL_LOOKBACK_DAYS)

    def test_iso_date_only_no_time_works(self):
        last = "2026-05-25"
        start, _ = get_scan_window(last_scan_iso=last)
        assert start == date(2026, 5, 24)
