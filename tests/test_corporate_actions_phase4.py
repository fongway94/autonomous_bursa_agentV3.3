# tests/test_corporate_actions_phase4.py
"""
Phase-4 tests for corporate_actions.process_corporate_actions
(scheduler orchestrator).

Covers:
  - No active trades → empty cheap exit
  - Detection runs only for tickers with active positions
  - SPLIT auto-adjusts when autoadjust=True
  - SPLIT alerts only when autoadjust=False (shadow mode)
  - DIVIDEND always alerts only (no P&L touch)
  - Idempotency: same event seen twice → skipped second time
  - Multiple splits on same trade compose in date order
  - No active position for ticker → recorded as SKIPPED_NO_POSITION
  - One bad event doesn't poison the rest of the batch
  - Per-event apply_split failure → recorded as FAILED, alert sent
  - Alerts dispatched via notifier.dispatch (mocked)
  - scheduler_state.last_corp_action_scan_at updated by caller (scheduler.py),
    not by process_corporate_actions itself
"""

from __future__ import annotations

from datetime import date
from unittest import mock

import pytest

import corporate_actions as ca
from corporate_actions import (
    CorporateAction,
    process_corporate_actions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_active_trade(ticker: str = "0166.KL", **overrides) -> int:
    from db import connect, myt_iso
    defaults = {
        "ticker": ticker, "name": ticker, "sector": "Technology",
        "signal_type": "BREAKOUT",
        "entry_price": 10.00, "stop_loss": 9.00,
        "tp1": 11.0, "tp2": 12.0, "tp3": 13.0,
        "shares": 100, "lots": 1, "shares_remaining": 100,
        "cost": 1000.0, "fee": 10.0, "total_outlay": 1010.0,
        "risk_per_share": 1.0, "actual_risk_pct": 1.0,
        "status": "ACTIVE", "phase": "FULL",
        "logged_at": myt_iso(), "execution_type": "AUTO",
    }
    defaults.update(overrides)
    cols = ", ".join(defaults.keys())
    phs = ", ".join(["?"] * len(defaults))
    with connect() as c:
        cur = c.execute(f"INSERT INTO trades ({cols}) VALUES ({phs})",
                        tuple(defaults.values()))
        return cur.lastrowid


def _read_trade(tid: int) -> dict:
    from db import connect
    with connect(readonly=True) as c:
        return dict(c.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone())


@pytest.fixture(autouse=True)
def _silence_notifier(monkeypatch):
    """Replace dispatch with a no-op recorder to avoid network calls."""
    sent = []
    def _fake_dispatch(**kw):
        sent.append(kw)
        return {"dashboard": (True, None)}
    monkeypatch.setattr("notifier.dispatch", _fake_dispatch)
    # Also patch live_trigger.load_config so we don't depend on DB state
    monkeypatch.setattr(
        "live_trigger.load_config",
        lambda: {"telegram_enabled": 1, "email_enabled": 0,
                 "email_recipients": ""},
    )
    return sent


# ---------------------------------------------------------------------------
# Empty / cheap paths
# ---------------------------------------------------------------------------

class TestEmptyPaths:

    def test_no_active_trades_returns_zero(self, _silence_notifier):
        summary = process_corporate_actions(autoadjust=True)
        assert summary["tickers_scanned"] == 0
        assert summary["events_detected"] == 0
        assert summary["splits_adjusted"] == 0

    def test_active_trade_but_no_events(self, monkeypatch, _silence_notifier):
        _insert_active_trade("0166.KL")
        monkeypatch.setattr(ca, "detect_for_tickers",
                            lambda tickers, start, end: [])
        summary = process_corporate_actions(autoadjust=True)
        assert summary["tickers_scanned"] == 1
        assert summary["events_detected"] == 0


# ---------------------------------------------------------------------------
# Auto-adjust path
# ---------------------------------------------------------------------------

class TestAutoAdjustOn:

    def test_split_adjusts_trade(self, monkeypatch, _silence_notifier):
        tid = _insert_active_trade("0166.KL", entry_price=10.00, shares=100)

        ev = CorporateAction(
            ticker="0166.KL", ex_date="2026-05-15",
            event_type="SPLIT", ratio=5.0, source="yfinance",
        )
        monkeypatch.setattr(ca, "detect_for_tickers",
                            lambda tickers, start, end: [ev])

        summary = process_corporate_actions(autoadjust=True)

        assert summary["splits_adjusted"] == 1
        assert summary["failures"] == 0

        t = _read_trade(tid)
        assert t["entry_price"] == 2.00
        assert t["shares"] == 500
        assert t["cumulative_split_factor"] == 5.0

        # Alert was sent
        assert len(_silence_notifier) == 1
        assert "auto-adjusted" in _silence_notifier[0]["subject"].lower()

    def test_bonus_issue_adjusts_trade(self, monkeypatch, _silence_notifier):
        tid = _insert_active_trade("1155.KL", entry_price=3.00, shares=200)

        ev = CorporateAction(
            ticker="1155.KL", ex_date="2026-05-15",
            event_type="BONUS", ratio=1.5, source="moomoo",
        )
        monkeypatch.setattr(ca, "detect_for_tickers",
                            lambda tickers, start, end: [ev])

        summary = process_corporate_actions(autoadjust=True)
        assert summary["splits_adjusted"] == 1

        t = _read_trade(tid)
        assert t["entry_price"] == 2.00     # 3.00 / 1.5
        assert t["shares"] == 300

    def test_multiple_splits_compose_in_date_order(self, monkeypatch, _silence_notifier):
        tid = _insert_active_trade("0166.KL", entry_price=10.00, shares=100)

        evs = [
            CorporateAction(ticker="0166.KL", ex_date="2026-06-15",
                            event_type="SPLIT", ratio=2.0, source="yfinance"),
            CorporateAction(ticker="0166.KL", ex_date="2026-05-15",
                            event_type="SPLIT", ratio=5.0, source="yfinance"),
        ]
        monkeypatch.setattr(ca, "detect_for_tickers",
                            lambda tickers, start, end: evs)

        summary = process_corporate_actions(autoadjust=True)
        assert summary["splits_adjusted"] == 2

        t = _read_trade(tid)
        # Compose order: 5x first (older), then 2x = 10x cumulative
        assert t["cumulative_split_factor"] == 10.0
        assert t["entry_price"] == 1.00
        assert t["shares"] == 1000


# ---------------------------------------------------------------------------
# Shadow mode (autoadjust=False)
# ---------------------------------------------------------------------------

class TestShadowMode:

    def test_split_only_alerts_when_autoadjust_off(self, monkeypatch, _silence_notifier):
        tid = _insert_active_trade("0166.KL", entry_price=10.00, shares=100)
        ev = CorporateAction(ticker="0166.KL", ex_date="2026-05-15",
                             event_type="SPLIT", ratio=5.0, source="yfinance")
        monkeypatch.setattr(ca, "detect_for_tickers",
                            lambda tickers, start, end: [ev])

        summary = process_corporate_actions(autoadjust=False)

        assert summary["splits_adjusted"] == 0
        assert summary["splits_alerted_only"] == 1

        # Trade unchanged
        t = _read_trade(tid)
        assert t["entry_price"] == 10.00
        assert t["shares"] == 100

        # Alert was sent
        assert len(_silence_notifier) == 1
        assert "auto-adjust off" in _silence_notifier[0]["subject"].lower()


# ---------------------------------------------------------------------------
# Dividends
# ---------------------------------------------------------------------------

class TestDividends:

    def test_dividend_always_alert_only(self, monkeypatch, _silence_notifier):
        tid = _insert_active_trade("5347.KL", entry_price=14.00, shares=100)
        ev = CorporateAction(ticker="5347.KL", ex_date="2026-05-15",
                             event_type="DIVIDEND", amount_per_share=0.20,
                             source="yfinance")
        monkeypatch.setattr(ca, "detect_for_tickers",
                            lambda tickers, start, end: [ev])

        summary = process_corporate_actions(autoadjust=True)
        assert summary["dividends_alerted"] == 1
        assert summary["splits_adjusted"] == 0

        # Trade unchanged
        t = _read_trade(tid)
        assert t["entry_price"] == 14.00
        assert t["shares"] == 100

        # Alert sent
        assert len(_silence_notifier) == 1
        assert "dividend" in _silence_notifier[0]["subject"].lower()


# ---------------------------------------------------------------------------
# Idempotency: same event seen twice
# ---------------------------------------------------------------------------

class TestIdempotency:

    def test_event_processed_twice_skipped_second_time(self, monkeypatch, _silence_notifier):
        _insert_active_trade("0166.KL", entry_price=10.00, shares=100)
        ev = CorporateAction(ticker="0166.KL", ex_date="2026-05-15",
                             event_type="SPLIT", ratio=5.0, source="yfinance")
        monkeypatch.setattr(ca, "detect_for_tickers",
                            lambda tickers, start, end: [ev])

        # First cycle: applies the split
        s1 = process_corporate_actions(autoadjust=True)
        assert s1["splits_adjusted"] == 1

        # Second cycle: same event detected → skipped
        s2 = process_corporate_actions(autoadjust=True)
        assert s2["splits_adjusted"] == 0
        assert s2["skipped_dupes"] == 1

        # Trade was only adjusted ONCE (not 5 → 25)
        from db import connect
        with connect(readonly=True) as c:
            t = dict(c.execute(
                "SELECT * FROM trades WHERE ticker='0166.KL'").fetchone())
        assert t["cumulative_split_factor"] == 5.0
        assert t["shares"] == 500


# ---------------------------------------------------------------------------
# No active position for an event's ticker
# ---------------------------------------------------------------------------

class TestNoActivePosition:

    def test_event_for_unheld_ticker_recorded_skip(self, monkeypatch, _silence_notifier):
        _insert_active_trade("0166.KL")  # we hold 0166
        # ...but the event is for a different ticker
        ev = CorporateAction(ticker="5347.KL", ex_date="2026-05-15",
                             event_type="SPLIT", ratio=5.0, source="yfinance")
        monkeypatch.setattr(ca, "detect_for_tickers",
                            lambda tickers, start, end: [ev])

        # Note: this scenario shouldn't actually happen because detect_for_tickers
        # only scans active-trade tickers. But we test defensively.
        summary = process_corporate_actions(autoadjust=True)
        assert summary["events_detected"] == 1
        assert summary["splits_adjusted"] == 0
        # Recorded as processed so we don't keep retrying
        assert ca.already_processed(ev) is True


# ---------------------------------------------------------------------------
# Failure isolation: one bad event doesn't break the batch
# ---------------------------------------------------------------------------

class TestFailureIsolation:

    def test_one_failed_adjustment_doesnt_block_others(self, monkeypatch, _silence_notifier):
        good_tid = _insert_active_trade("0166.KL", entry_price=10.00, shares=100)
        bad_tid = _insert_active_trade("1155.KL", entry_price=5.00, shares=200)

        # Both events arrive
        evs = [
            CorporateAction(ticker="0166.KL", ex_date="2026-05-15",
                            event_type="SPLIT", ratio=5.0, source="yfinance"),
            CorporateAction(ticker="1155.KL", ex_date="2026-05-15",
                            event_type="SPLIT", ratio=3.0, source="yfinance"),
        ]
        monkeypatch.setattr(ca, "detect_for_tickers",
                            lambda tickers, start, end: evs)

        # Force the second adjustment to fail
        from trading_engine import apply_split_to_trade as real_apply
        def fake_apply(trade_id, ratio, ex_date, note=None):
            if trade_id == bad_tid:
                raise RuntimeError("simulated DB failure")
            return real_apply(trade_id, ratio, ex_date, note=note)
        monkeypatch.setattr("trading_engine.apply_split_to_trade", fake_apply)

        summary = process_corporate_actions(autoadjust=True)

        # Good trade got adjusted
        good = _read_trade(good_tid)
        assert good["entry_price"] == 2.00
        # Bad trade untouched
        bad = _read_trade(bad_tid)
        assert bad["entry_price"] == 5.00

        assert summary["splits_adjusted"] == 1
        assert summary["failures"] == 1

    def test_detection_exception_returns_empty_summary(self, monkeypatch, _silence_notifier):
        _insert_active_trade("0166.KL")
        def boom(tickers, start, end):
            raise RuntimeError("yfinance + moomoo both down")
        monkeypatch.setattr(ca, "detect_for_tickers", boom)

        summary = process_corporate_actions(autoadjust=True)
        # Returned cleanly, no exception
        assert summary["events_detected"] == 0
        assert summary["splits_adjusted"] == 0


# ---------------------------------------------------------------------------
# Sanity check: tickers_scanned matches DISTINCT active tickers
# ---------------------------------------------------------------------------

class TestTickerCollection:

    def test_dedupes_multiple_trades_same_ticker(self, monkeypatch, _silence_notifier):
        # Two open trades on same ticker
        _insert_active_trade("0166.KL", entry_price=10.00, shares=100)
        _insert_active_trade("0166.KL", entry_price=10.50, shares=100)

        captured = {"tickers": None}
        def capture(tickers, start, end):
            captured["tickers"] = list(tickers)
            return []
        monkeypatch.setattr(ca, "detect_for_tickers", capture)

        summary = process_corporate_actions(autoadjust=True)
        assert summary["tickers_scanned"] == 1
        assert captured["tickers"] == ["0166.KL"]

    def test_split_applies_to_all_active_trades_on_ticker(self, monkeypatch, _silence_notifier):
        """If you have two open trades on the same ticker, both must be adjusted."""
        t1 = _insert_active_trade("0166.KL", entry_price=10.00, shares=100)
        t2 = _insert_active_trade("0166.KL", entry_price=10.50, shares=200)

        ev = CorporateAction(ticker="0166.KL", ex_date="2026-05-15",
                             event_type="SPLIT", ratio=5.0, source="yfinance")
        monkeypatch.setattr(ca, "detect_for_tickers",
                            lambda tickers, start, end: [ev])

        process_corporate_actions(autoadjust=True)

        a = _read_trade(t1); b = _read_trade(t2)
        assert a["entry_price"] == 2.00 and a["shares"] == 500
        assert b["entry_price"] == 2.10 and b["shares"] == 1000
