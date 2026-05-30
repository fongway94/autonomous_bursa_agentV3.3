"""
Tests for reconciliation.py (Block 6).

Covers:
  - NOOP mode → skipped with reason
  - MY market → skipped with reason (no broker support)
  - Clean state: zero drift → RECONCILE_OK, no alert
  - Equity drift above threshold → RECONCILE_DRIFT logged
  - Position mismatch above tolerance → RECONCILE_DRIFT logged
  - Position-only-in-broker (manual trade) → flagged
  - Position-only-in-internal (mirror failed) → flagged
  - Errors from broker → result.error set, ran=True, never raises
  - last_reconcile_at + last_reconcile_drift persisted to scheduler_state
"""

from __future__ import annotations

import sys
import types
from unittest import mock

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helpers — fake broker adapter
# ---------------------------------------------------------------------------

def _patch_broker_adapter(monkeypatch, *,
                          cash=5000.0, total_assets=5500.0,
                          market_value=500.0,
                          positions: list[tuple[str, int]] | None = None,
                          connected=True,
                          connect_returns=True,
                          fail_account_snapshot=False):
    """Replace `broker_adapter.get_broker_adapter` with a fake."""
    import broker_adapter as ba
    from broker_adapter import Position, AccountSnapshot

    class _FakeAdapter:
        name = "moomoo_us"

        def is_connected(self):
            return connected

        def connect(self):
            return connect_returns

        def last_error(self):
            return None if connect_returns else "fake disconnected"

        def disconnect(self):
            pass

        def get_account_snapshot(self):
            if fail_account_snapshot:
                raise RuntimeError("simulated broker outage")
            return AccountSnapshot(
                cash=cash, total_assets=total_assets,
                market_value=market_value, currency="USD",
            )

        def list_positions(self):
            return [
                Position(ticker=t, quantity=q, avg_cost=100.0,
                         current_price=110.0, unrealized_pnl=10 * q)
                for t, q in (positions or [])
            ]

        def place_order(self, req):
            return None

    monkeypatch.setattr(ba, "get_broker_adapter", lambda: _FakeAdapter())


def _seed_internal_state(*, cash=5000.0, equity=5500.0,
                         trades: list[tuple[str, int]] | None = None):
    """Set internal account + active trades to known values."""
    from repository import save_account
    save_account(cash_balance=cash, total_equity=equity)
    # Insert trades directly via DB to avoid full execute_entry path
    if trades:
        from db import connect, myt_iso
        with connect() as c:
            for ticker, qty in trades:
                c.execute(
                    "INSERT INTO trades "
                    "(ticker, name, sector, signal_type, entry_price, stop_loss, "
                    " tp1, tp2, tp3, shares, lots, cost, fee, total_outlay, "
                    " risk_per_share, status, phase, logged_at, "
                    " shares_remaining, cumulative_split_factor) "
                    "VALUES (?, ?, 'TestSector', 'GOLD BUY', 100.0, 95.0, "
                    " 105, 110, 115, ?, ?, ?, 0, ?, 5.0, 'ACTIVE', 'FULL', ?, ?, 1.0)",
                    (ticker, f"Test {ticker}", qty, qty // 100 or 1,
                     qty * 100.0, qty * 100.0, myt_iso(), qty),
                )


# ---------------------------------------------------------------------------
# Skip paths
# ---------------------------------------------------------------------------

class TestSkipPaths:
    def test_skipped_when_broker_mode_noop(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        from broker_adapter import set_broker_mode
        set_broker_mode("NOOP")

        from reconciliation import run_reconciliation
        result = run_reconciliation()
        assert result.ran is False
        assert "NOOP" in (result.reason_skipped or "")

    def test_skipped_when_my_market(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "MY")
        import market_profiles
        market_profiles.reset_cache()
        from broker_adapter import set_broker_mode
        # Try to enable — MY should ignore (factory returns NoopAdapter)
        set_broker_mode("SIMULATE")
        # The set_broker_mode persists SIMULATE but the should_run gate
        # checks moomoo_available which is False for MY.

        from reconciliation import run_reconciliation
        result = run_reconciliation()
        assert result.ran is False
        assert ("moomoo_available=False" in (result.reason_skipped or "")
                or "NOOP" in (result.reason_skipped or ""))


# ---------------------------------------------------------------------------
# Clean state
# ---------------------------------------------------------------------------

class TestCleanReconcile:
    def test_zero_drift_zero_positions_is_clean(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        from broker_adapter import set_broker_mode
        set_broker_mode("SIMULATE")

        # Internal: cash=5000, equity=5500
        _seed_internal_state(cash=5000.0, equity=5500.0)
        # Broker matches exactly
        _patch_broker_adapter(monkeypatch, cash=5000.0, total_assets=5500.0)

        from reconciliation import run_reconciliation
        result = run_reconciliation()
        assert result.ran is True
        assert result.drift_flagged is False
        assert result.equity_drift == 0.0
        assert result.position_diffs == []
        assert result.is_clean()

    def test_drift_below_threshold_is_clean(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        from broker_adapter import set_broker_mode
        set_broker_mode("SIMULATE")

        # 0.4% drift on USD 5000 = USD 20 — under 0.5% threshold
        _seed_internal_state(cash=5000.0, equity=5000.0)
        _patch_broker_adapter(monkeypatch, cash=5020.0, total_assets=5020.0)

        from reconciliation import run_reconciliation
        result = run_reconciliation()
        assert result.ran is True
        assert result.drift_flagged is False


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------

class TestDriftDetection:
    def test_equity_drift_above_threshold_flagged(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        from broker_adapter import set_broker_mode
        set_broker_mode("SIMULATE")

        # 2% drift on USD 5000 = USD 100 → flagged
        _seed_internal_state(cash=5000.0, equity=5000.0)
        _patch_broker_adapter(monkeypatch, cash=5100.0, total_assets=5100.0)

        from reconciliation import run_reconciliation
        result = run_reconciliation()
        assert result.ran is True
        assert result.drift_flagged is True
        assert result.equity_drift == 100.0
        assert result.equity_drift_pct == 2.0

    def test_position_only_in_broker_flagged(self, monkeypatch):
        """User opened an AAPL position manually in Moomoo → drift alert."""
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        from broker_adapter import set_broker_mode
        set_broker_mode("SIMULATE")

        _seed_internal_state(cash=5000.0, equity=5000.0, trades=[])
        _patch_broker_adapter(
            monkeypatch, cash=5000.0, total_assets=5000.0,
            positions=[("AAPL", 10)],
        )

        from reconciliation import run_reconciliation
        result = run_reconciliation()
        assert result.drift_flagged is True
        assert "AAPL" in result.positions_only_in_broker
        assert len(result.position_diffs) == 1
        assert result.position_diffs[0].ticker == "AAPL"
        assert result.position_diffs[0].internal_qty == 0
        assert result.position_diffs[0].broker_qty == 10

    def test_position_only_in_internal_flagged(self, monkeypatch):
        """Mirror order failed silently → broker doesn't have the position."""
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        from broker_adapter import set_broker_mode
        set_broker_mode("SIMULATE")

        _seed_internal_state(cash=5000.0, equity=5000.0,
                              trades=[("TSLA", 5)])
        _patch_broker_adapter(monkeypatch, cash=5000.0, total_assets=5000.0,
                                positions=[])

        from reconciliation import run_reconciliation
        result = run_reconciliation()
        assert result.drift_flagged is True
        assert "TSLA" in result.positions_only_in_internal

    def test_position_quantity_mismatch_flagged(self, monkeypatch):
        """Partial fill — internal thinks 100, broker holds 95."""
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        from broker_adapter import set_broker_mode
        set_broker_mode("SIMULATE")

        _seed_internal_state(cash=5000.0, equity=5000.0,
                              trades=[("NVDA", 100)])
        _patch_broker_adapter(monkeypatch, cash=5000.0, total_assets=5000.0,
                                positions=[("NVDA", 95)])  # 5% off

        from reconciliation import run_reconciliation
        result = run_reconciliation()
        assert result.drift_flagged is True
        nvda = next(d for d in result.position_diffs if d.ticker == "NVDA")
        assert nvda.internal_qty == 100
        assert nvda.broker_qty == 95

    def test_position_within_tolerance_not_flagged(self, monkeypatch):
        """0.5% mismatch (within tolerance) shouldn't flag a 1000-share position."""
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        from broker_adapter import set_broker_mode
        set_broker_mode("SIMULATE")

        # Tolerance: max(1 share, 1% of qty). For qty=1000, that's 10 shares.
        # 1000 vs 1005 = 5-share delta = 0.5% → within tolerance.
        _seed_internal_state(cash=5000.0, equity=5000.0,
                              trades=[("AAPL", 1000)])
        _patch_broker_adapter(monkeypatch, cash=5000.0, total_assets=5000.0,
                                positions=[("AAPL", 1005)])

        from reconciliation import run_reconciliation
        result = run_reconciliation()
        # equity drift is zero, position delta within tolerance → clean
        assert result.drift_flagged is False


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_broker_outage_does_not_raise(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        from broker_adapter import set_broker_mode
        set_broker_mode("SIMULATE")

        _seed_internal_state()
        _patch_broker_adapter(monkeypatch, fail_account_snapshot=True)

        from reconciliation import run_reconciliation
        result = run_reconciliation()
        # Must NOT raise. Must record the error.
        assert result.error is not None
        assert "simulated broker outage" in result.error
        assert result.is_clean() is False

    def test_broker_not_connectable_records_error(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        from broker_adapter import set_broker_mode
        set_broker_mode("SIMULATE")

        _seed_internal_state()
        _patch_broker_adapter(monkeypatch, connected=False,
                                connect_returns=False)

        from reconciliation import run_reconciliation
        result = run_reconciliation()
        assert result.error is not None
        assert "not connectable" in result.error


# ---------------------------------------------------------------------------
# Persistence to scheduler_state
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_last_reconcile_at_stamped(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        from broker_adapter import set_broker_mode
        set_broker_mode("SIMULATE")

        _seed_internal_state(cash=5000.0, equity=5000.0)
        _patch_broker_adapter(monkeypatch, cash=5000.0, total_assets=5000.0)

        from reconciliation import run_reconciliation
        run_reconciliation()

        from db import connect
        with connect(readonly=True) as c:
            row = c.execute(
                "SELECT last_reconcile_at, last_reconcile_drift "
                "FROM scheduler_state WHERE id=1"
            ).fetchone()
        assert row["last_reconcile_at"] is not None
        # Drift was zero, but the column should be a real number not NULL
        assert row["last_reconcile_drift"] is not None

    def test_get_reconciliation_status_returns_dict(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        from reconciliation import get_reconciliation_status
        status = get_reconciliation_status()
        assert "broker_mode" in status
        assert "drift_threshold_pct" in status


# ---------------------------------------------------------------------------
# Threshold knob
# ---------------------------------------------------------------------------

class TestThreshold:
    def test_custom_threshold_can_make_drift_pass(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        from broker_adapter import set_broker_mode
        set_broker_mode("SIMULATE")

        # 1% drift normally fails
        _seed_internal_state(cash=5000.0, equity=5000.0)
        _patch_broker_adapter(monkeypatch, cash=5050.0, total_assets=5050.0)

        from reconciliation import run_reconciliation
        # Raise threshold to 2% — 1% drift now passes
        result = run_reconciliation(drift_threshold_pct=0.02)
        assert result.drift_flagged is False
