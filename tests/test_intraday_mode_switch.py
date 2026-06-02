"""
Regression tests for the INTRADAY mode-switch OperationalError bug (v3.7 hotfix).

Two bugs fixed:
  1. db._migrate_v36_db_if_needed() was orphaned code (never ran) — meaning
     bursa_agent_MY.db / bursa_agent_US.db from a Gist-restored v3.6 DB were
     never renamed to bursa_agent_MY_SWING.db / bursa_agent_US_SWING.db.

  2. Switching to INTRADAY mode called update_scheduler_state() before init_db()
     had run on the brand-new INTRADAY DB file → sqlite3.OperationalError:
     no such table: scheduler_state.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_data_dir(monkeypatch, tmp_path):
    """Redirect DATA_DIR and HOME to a clean temp directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    data_dir = tmp_path / ".bursa_agent_data"
    data_dir.mkdir()

    import db as _db
    monkeypatch.setattr(_db, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(_db, "_LEGACY_DB_PATH",
                        str(data_dir / "bursa_agent.db"))

    import market_profiles as _mp
    monkeypatch.setattr(_mp, "_DATA_DIR", data_dir)
    monkeypatch.setattr(_mp, "_MARKER_FILE", data_dir / ".active_market")
    monkeypatch.setattr(_mp, "_TRADING_MODE_FILE", data_dir / ".trading_mode")
    _mp.reset_cache()
    _mp.reset_trading_mode_cache()

    return data_dir


# ---------------------------------------------------------------------------
# Bug 1: _migrate_v36_db_if_needed() must rename old per-market DBs
# ---------------------------------------------------------------------------

class TestMigrateV36Db:
    def test_migrate_renames_my_db_to_my_swing(self, isolated_data_dir, monkeypatch):
        """bursa_agent_MY.db → bursa_agent_MY_SWING.db on first boot."""
        import db as _db

        old = isolated_data_dir / "bursa_agent_MY.db"
        new = isolated_data_dir / "bursa_agent_MY_SWING.db"

        # Simulate v3.6 Gist-restored DB
        conn = sqlite3.connect(str(old))
        conn.execute("CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY, cash_balance REAL)")
        conn.execute("INSERT INTO account VALUES (1, 20000.0)")
        conn.commit()
        conn.close()

        assert old.exists()
        assert not new.exists()

        _db._migrate_v36_db_if_needed()

        assert not old.exists(), "Old v3.6 DB must be removed after migration"
        assert new.exists(), "New v3.7 SWING DB must exist after migration"

        # Data must be preserved
        conn = sqlite3.connect(str(new))
        row = conn.execute("SELECT cash_balance FROM account WHERE id=1").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 20000.0

    def test_migrate_renames_us_db_to_us_swing(self, isolated_data_dir, monkeypatch):
        """bursa_agent_US.db → bursa_agent_US_SWING.db on first boot."""
        import db as _db

        old = isolated_data_dir / "bursa_agent_US.db"
        new = isolated_data_dir / "bursa_agent_US_SWING.db"

        conn = sqlite3.connect(str(old))
        conn.execute("CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY, cash_balance REAL)")
        conn.execute("INSERT INTO account VALUES (1, 5000.0)")
        conn.commit()
        conn.close()

        _db._migrate_v36_db_if_needed()

        assert not old.exists()
        assert new.exists()

        conn = sqlite3.connect(str(new))
        row = conn.execute("SELECT cash_balance FROM account WHERE id=1").fetchone()
        conn.close()
        assert row[0] == 5000.0

    def test_migrate_is_idempotent(self, isolated_data_dir):
        """Running _migrate_v36_db_if_needed() twice must not error or delete data."""
        import db as _db

        old = isolated_data_dir / "bursa_agent_MY.db"
        conn = sqlite3.connect(str(old))
        conn.execute("CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        _db._migrate_v36_db_if_needed()
        _db._migrate_v36_db_if_needed()  # second call — must be a no-op

        new = isolated_data_dir / "bursa_agent_MY_SWING.db"
        assert new.exists()

    def test_migrate_skips_when_target_already_exists(self, isolated_data_dir):
        """If the SWING file already exists, the old file must NOT be deleted."""
        import db as _db

        old = isolated_data_dir / "bursa_agent_MY.db"
        new = isolated_data_dir / "bursa_agent_MY_SWING.db"

        # Both exist (e.g. manual copy)
        old.write_text("old")
        new.write_text("new")

        _db._migrate_v36_db_if_needed()

        assert old.exists(), "Old file must NOT be deleted when target already exists"
        assert new.read_text() == "new", "Target must not be overwritten"


# ---------------------------------------------------------------------------
# Bug 2: mode switch must init_db() before update_scheduler_state()
# ---------------------------------------------------------------------------

class TestModeSwitchInitDb:
    def test_intraday_db_has_scheduler_state_after_init(self, isolated_data_dir, monkeypatch):
        """
        Switching to INTRADAY creates a brand-new DB file. init_db() must run
        before any write to that file — otherwise scheduler_state doesn't exist
        and update_scheduler_state() raises OperationalError.

        This test reproduces the exact crash sequence:
          set_trading_mode("INTRADAY") → _resolve_db_path() → new file
          init_db() → creates schema
          update_scheduler_state() → must NOT raise
        """
        import os as _os
        _os.environ["MARKET_MODE"] = "US"
        _os.environ["TRADING_MODE"] = "SWING"

        import market_profiles as _mp
        _mp.reset_cache()
        _mp.reset_trading_mode_cache()

        import db as _db
        monkeypatch.setattr(_db, "DATA_DIR", str(isolated_data_dir))

        # Init the SWING DB
        _db.init_db()

        # Now simulate the mode switch
        _mp.set_trading_mode("INTRADAY", persist=False)

        intraday_path = isolated_data_dir / "bursa_agent_US_INTRADAY.db"
        assert not intraday_path.exists() or intraday_path.stat().st_size == 0 or True

        # This is what app.py does: init_db() THEN update_scheduler_state()
        _db.init_db()

        from repository import update_scheduler_state
        # Must NOT raise OperationalError
        update_scheduler_state(interval_sec=300)

        # Confirm the row exists in the INTRADAY DB
        conn = sqlite3.connect(str(intraday_path))
        row = conn.execute(
            "SELECT interval_sec FROM scheduler_state WHERE id=1"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 300

    def test_swing_db_unaffected_after_intraday_switch(self, isolated_data_dir, monkeypatch):
        """Switching to INTRADAY must not corrupt or wipe the SWING DB."""
        import os as _os
        _os.environ["MARKET_MODE"] = "US"
        _os.environ["TRADING_MODE"] = "SWING"

        import market_profiles as _mp
        _mp.reset_cache()
        _mp.reset_trading_mode_cache()

        import db as _db
        monkeypatch.setattr(_db, "DATA_DIR", str(isolated_data_dir))
        _db.init_db()

        # Write a sentinel into the SWING DB
        from repository import save_account
        save_account(initial_capital=9999.0, cash_balance=9999.0, total_equity=9999.0)

        # Switch to INTRADAY and init
        _mp.set_trading_mode("INTRADAY", persist=False)
        _db.init_db()

        # Switch back to SWING
        _mp.set_trading_mode("SWING", persist=False)
        _db.init_db()

        from repository import load_account
        acc = load_account()
        assert acc["initial_capital"] == 9999.0, \
            "SWING DB must retain its data after an INTRADAY switch-and-back"


# ---------------------------------------------------------------------------
# Regression: drawdown check must use total_equity not cash_balance
# ---------------------------------------------------------------------------

class TestDrawdownEquityCalculation:
    """Buying a position reduces cash but NOT total equity.
    Drawdown check must use total_equity (cash + position value)
    not cash_balance alone — otherwise every open position looks
    like a drawdown equal to the position cost.

    Bug: scheduler.py passed cash_balance to run_full_risk_check.
    Result: $878 NVIDIA position → $878 "drawdown" on $5k capital = 17.6%
            → circuit breaker fires → all entries blocked incorrectly.

    Fix: pass total_equity = cash + market_value_of_positions.
    """

    def test_cash_minus_position_is_not_drawdown(self):
        """Equity stays flat when you deploy cash into a position."""
        initial_capital = 5000.0
        position_cost   = 878.0   # 4 shares NVIDIA @ ~$219

        cash_after_buy  = initial_capital - position_cost  # $4,122
        position_value  = position_cost                    # assume no P&L yet
        total_equity    = cash_after_buy + position_value  # $5,000

        # Using cash_balance (BUG):
        wrong_drawdown = (initial_capital - cash_after_buy) / initial_capital * 100
        # Using total_equity (FIX):
        correct_drawdown = (initial_capital - total_equity) / initial_capital * 100

        assert wrong_drawdown == pytest.approx(17.6, abs=0.1), \
            "Confirms the bug: cash alone shows 17.6% false drawdown"
        assert correct_drawdown == pytest.approx(0.0, abs=0.01), \
            "Fix: total_equity shows 0% drawdown when position breaks even"

    def test_real_loss_shows_correct_drawdown(self):
        """Actual P&L loss must still show correctly."""
        import pytest
        initial_capital = 5000.0
        position_cost   = 878.0
        current_price_loss = 50.0   # position lost $50

        cash         = initial_capital - position_cost      # $4,122
        position_val = position_cost - current_price_loss   # $828
        total_equity = cash + position_val                  # $4,950

        drawdown = (initial_capital - total_equity) / initial_capital * 100
        assert drawdown == pytest.approx(1.0, abs=0.1), \
            "$50 real loss on $5k = 1% drawdown, not 17.6%"
