# tests/conftest.py
"""
Pytest config:

* Redirect DATA_DIR to a temp directory for every test session.
* Re-import all DB-touching modules so they see the new path.
* Reset module-level state between tests (scheduler thread handle,
  orphan registry, stop event) so each test starts from a clean slate
  regardless of run order. v3.1.10.
"""

import os
import sys
import tempfile
import importlib

import pytest

# Resolve project root (one level up from tests/)
_PROJ = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _PROJ)


@pytest.fixture(scope="session", autouse=True)
def _isolate_data_dir():
    tmp = tempfile.mkdtemp(prefix="bursa_test_")
    os.environ["HOME"] = tmp  # makes DATA_DIR resolve to <tmp>/.bursa_agent_data

    # v3.6: also redirect the market_profiles marker file into the temp dir
    # and ensure the active-market cache starts fresh.
    try:
        import market_profiles
        from pathlib import Path
        marker_dir = Path(tmp) / ".bursa_agent_data"
        marker_dir.mkdir(parents=True, exist_ok=True)
        market_profiles._DATA_DIR = marker_dir
        market_profiles._MARKER_FILE = marker_dir / ".active_market"
        market_profiles.reset_cache()
    except Exception:
        pass

    # (Re)import in order — persistence + app need fresh DATA_DIR too
    for mod_name in [
        "market_profiles", "db", "logger", "data_quality", "repository",
        "risk_manager", "trading_engine", "learner",
        "market_analyzer", "scheduler", "watchlist", "evaluation",
        "persistence", "notifier", "live_trigger",
        "broker_adapter", "maintenance_reminders", "app",
    ]:
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
        else:
            try:
                importlib.import_module(mod_name)
            except ImportError:
                # market_profiles is required; others are optional in test contexts.
                if mod_name == "market_profiles":
                    raise
    yield tmp


@pytest.fixture(autouse=True)
def _reset_market_cache_between_tests():
    """v3.6: reset the cached MarketProfile + clear any cross-test leakage.

    Tests that set MARKET_MODE env var or write the marker file MUST not
    leak state into the next test. We:
        1. Snapshot the env var on entry, restore on exit
        2. Reset the profile cache before and after
        3. Delete the marker file so it doesn't outlive the test
    """
    import os
    saved_env = os.environ.get("MARKET_MODE")
    try:
        import market_profiles
        market_profiles.reset_cache()
        # Wipe any leftover marker file from previous tests
        try:
            if market_profiles._MARKER_FILE.exists():
                market_profiles._MARKER_FILE.unlink()
        except Exception:
            pass
    except Exception:
        pass
    yield
    # Restore MARKET_MODE
    if saved_env is None:
        os.environ.pop("MARKET_MODE", None)
    else:
        os.environ["MARKET_MODE"] = saved_env
    try:
        import market_profiles
        market_profiles.reset_cache()
        try:
            if market_profiles._MARKER_FILE.exists():
                market_profiles._MARKER_FILE.unlink()
        except Exception:
            pass
    except Exception:
        pass


def _reset_one_db():
    """Helper: truncate volatile tables + reset singletons on the ACTIVE DB."""
    from db import connect
    with connect() as c:
        for tbl in ("trades", "partial_exits", "trade_log",
                    "scheduler_log", "learning_events", "parameter_history",
                    "bias_history", "state_priors", "data_quality_log",
                    "scan_cache", "alert_log", "maintenance_state",
                    "regime_history", "meta", "custom_watchlist",
                    # v3.5: corporate-action audit trail
                    "corporate_actions_processed"):
            c.execute(f"DELETE FROM {tbl}")
        # Reset scheduler_state singleton to v3 defaults.
        # v3.1.10: also clears cycle_started_at (added for the watchdog).
        c.execute(
            "UPDATE scheduler_state SET "
            "running=0, interval_sec=3600, last_run_at=NULL, "
            "next_run_at=NULL, last_heartbeat=NULL, "
            "consecutive_failures=0, last_error=NULL, "
            "autotrade_enabled=1, autoexit_enabled=1, kill_switch=0, "
            "exploration_mode=1, exploration_trades_target=50, "
            "owner_pid=0, cycle_started_at=NULL, "
            # v3.5: reset corp-action toggle to default ON and clear scan ts
            "corp_action_autoadjust=1, last_corp_action_scan_at=NULL "
            "WHERE id=1"
        )
        # Reset live_trigger_config singleton
        c.execute(
            "UPDATE live_trigger_config SET "
            "enabled=0, min_confidence=70.0, exploit_mode_only=0, "
            "alert_on_entry=1, alert_on_full_exit=1, alert_on_stop_loss=1, "
            "alert_on_trailing_stop=1, alert_on_partial_exit=0, "
            "alert_on_risk_rejected=0, telegram_enabled=1, "
            "email_enabled=0, email_recipients='', actor_filter='AGENT' "
            "WHERE id=1"
        )
    # Reset singletons
    from repository import save_account, save_bias_state, save_parameters
    save_account(initial_capital=20000.0, cash_balance=20000.0,
                 total_equity=20000.0)
    save_bias_state({"breakout_bias": 1.0, "pullback_bias": 1.0,
                     "sector_biases": {}, "strategy_stats": {},
                     "sector_stats": {}, "total_closed_trades": 0,
                     "system_win_rate": 0.5})
    save_parameters({
        "ema_trend": 200, "ema_fast": 10, "ema_slow": 20,
        "rsi_oversold_pullback": 40.0, "rsi_overbought": 70.0,
        "volume_surge_ratio": 1.5, "breakout_period": 20,
        "atr_period": 14, "atr_multiplier_stop": 1.5,
        "min_price": 0.30, "max_price": 4.00,
    }, source="TEST", reason="reset")

    # v3.1.10: reset scheduler module-level state so per-test runs don't
    # leak threads/orphans/stop signals across each other. Without this,
    # tests like test_start_and_stop_idempotent failed when run alone
    # because a previous test's _THREAD handle stayed set.
    try:
        import scheduler
        # Best-effort: signal any live thread to exit, then drop the handle.
        try:
            scheduler._STOP_EVENT.set()
        except Exception:
            pass
        try:
            if scheduler._THREAD is not None and scheduler._THREAD.is_alive():
                scheduler._THREAD.join(timeout=2)
        except Exception:
            pass
        scheduler._THREAD = None
        try:
            scheduler._STOP_EVENT.clear()
        except Exception:
            pass
        try:
            scheduler._ORPHANED_THREAD_IDS.clear()
        except Exception:
            pass
        # Reset the once-per-process silent-exit log latch
        if hasattr(scheduler._loop, "_silent_exit_logged"):
            try:
                delattr(scheduler._loop, "_silent_exit_logged")
            except Exception:
                pass
        # v3.1.10: tear down any watchdog from prior test
        try:
            scheduler._stop_watchdog()
        except Exception:
            pass
    except Exception:
        # scheduler may not be importable in some collection paths; skip
        pass


@pytest.fixture(autouse=True)
def _reset_db_between_tests():
    """v3.6: reset ALL per-market DBs (MY + US) before each test.

    Tests that flip MARKET_MODE=US would otherwise leave stale data in
    bursa_agent_US.db that leaks into the next test using US mode.

    We iterate every market, temporarily activate it, init_db() and
    _reset_one_db(), then restore the original MARKET_MODE so the test
    starts on whichever market it expects.
    """
    import os as _os
    from db import init_db
    from market_profiles import available_markets, reset_cache as _reset_mp

    saved_env = _os.environ.get("MARKET_MODE")
    for code in available_markets():
        _os.environ["MARKET_MODE"] = code
        _reset_mp()
        try:
            init_db()
        except Exception:
            pass
        try:
            _reset_one_db()
        except Exception:
            pass

    # Restore env so the test's own MARKET_MODE setup wins
    if saved_env is None:
        _os.environ.pop("MARKET_MODE", None)
    else:
        _os.environ["MARKET_MODE"] = saved_env
    _reset_mp()
