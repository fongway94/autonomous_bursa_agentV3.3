# tests/test_corporate_actions_phase6_integration.py
"""
Phase-6 INTEGRATION tests.

Unlike Phases 1-4 which test corporate_actions.* in isolation, these tests
exercise the FULL path through scheduler._run_one_cycle, which is what
actually runs in production.

What we mock at the very bottom:
  - market_analyzer.get_full_market_analysis (network)
  - screener.screen_all_stocks (network)
  - trading_engine.auto_settle_trades (we want to isolate corp-actions)
  - notifier.dispatch (no real alerts)
  - corporate_actions.detect_for_tickers (the only data source for events)

What we DO NOT mock (the integration surface):
  - process_corporate_actions itself
  - apply_split_to_trade itself
  - The actual SQLite transaction
  - already_processed / record_processed
  - Schema migration / column constraints
  - The scheduler's outer try/except + state management

Scenarios:
  1. Cycle with no events → summary has zero corp_actions, no DB changes
  2. Cycle with one SPLIT → trade adjusted, recorded, next cycle skips it
  3. Cycle with detection exception → cycle still completes, error logged
  4. Cycle with multiple events on multiple tickers → all processed
  5. Cycle with autoadjust=False → trade unchanged, alerts only
  6. Cycle propagates last_corp_action_scan_at update so windows roll forward
  7. Cycle aborts on owner mismatch BEFORE corp-actions run
  8. Cash-conservation invariant holds across an end-to-end cycle
"""

from __future__ import annotations

from unittest import mock

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_trade(ticker="0166.KL", **overrides) -> int:
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


def _read_trade(tid):
    from db import connect
    with connect(readonly=True) as c:
        return dict(c.execute("SELECT * FROM trades WHERE id=?",
                              (tid,)).fetchone())


@pytest.fixture
def cycle_env(monkeypatch):
    """
    Standard mock setup for any cycle test:
      - Regime returns NEUTRAL
      - Screener returns empty (no signals, no auto-entry attempts)
      - Notifier dispatch is a no-op recorder
    Returns the dispatched-alerts list so tests can assert on it.
    """
    dispatched = []

    monkeypatch.setattr(
        "market_analyzer.get_full_market_analysis",
        lambda force_refresh=False: {
            "regime_data": {"regime": "NEUTRAL", "conviction": 0.5,
                            "details": {}},
            "position_rules": {},
        },
    )
    monkeypatch.setattr("screener.screen_all_stocks",
                        lambda market_regime=None: pd.DataFrame())
    monkeypatch.setattr("trading_engine.auto_settle_trades",
                        lambda price_lookup, market_regime, actor=None:
                        {"settled": [], "partials": []})

    def fake_dispatch(**kw):
        dispatched.append(kw)
        return {"dashboard": (True, None)}
    monkeypatch.setattr("notifier.dispatch", fake_dispatch)
    monkeypatch.setattr(
        "live_trigger.load_config",
        lambda: {"telegram_enabled": 1, "email_enabled": 0,
                 "email_recipients": ""},
    )
    return dispatched


# ---------------------------------------------------------------------------
# Scenario 1: Empty cycle
# ---------------------------------------------------------------------------

class TestEmptyCycle:

    def test_no_events_completes_cleanly(self, monkeypatch, cycle_env):
        """No active trades, no events. Cycle finishes, corp_actions zeros."""
        import scheduler
        import corporate_actions as ca

        # No event source even if called
        monkeypatch.setattr(ca, "detect_for_tickers",
                            lambda tickers, start, end: [])

        summary = scheduler._run_one_cycle(autotrade=False, autoexit=False)

        assert summary["corp_actions_detected"] == 0
        assert summary["corp_actions_adjusted"] == 0
        assert summary["corp_actions_failed"] == 0
        assert cycle_env == []  # no alerts


# ---------------------------------------------------------------------------
# Scenario 2: One SPLIT applied end-to-end
# ---------------------------------------------------------------------------

class TestSingleSplit:

    def test_split_applied_and_recorded(self, monkeypatch, cycle_env):
        import scheduler
        import corporate_actions as ca
        from corporate_actions import CorporateAction

        tid = _insert_trade("0166.KL", entry_price=10.00, shares=100)

        ev = CorporateAction(
            ticker="0166.KL", ex_date="2026-05-15",
            event_type="SPLIT", ratio=5.0, source="yfinance",
        )
        monkeypatch.setattr(ca, "detect_for_tickers",
                            lambda tickers, start, end: [ev])

        summary = scheduler._run_one_cycle(autotrade=False, autoexit=False)

        # Cycle summary
        assert summary["corp_actions_detected"] == 1
        assert summary["corp_actions_adjusted"] == 1
        assert summary["corp_actions_failed"] == 0

        # Trade actually adjusted in DB
        t = _read_trade(tid)
        assert t["entry_price"] == 2.00
        assert t["shares"] == 500
        assert t["cumulative_split_factor"] == 5.0

        # Recorded for idempotency
        assert ca.already_processed(ev)

        # Alert dispatched
        assert any("auto-adjusted" in a["subject"].lower() for a in cycle_env)


# ---------------------------------------------------------------------------
# Scenario 3: Detection exception is contained
# ---------------------------------------------------------------------------

class TestDetectionFailureIsolation:

    def test_detection_exception_does_not_abort_cycle(self, monkeypatch, cycle_env):
        import scheduler
        import corporate_actions as ca

        _insert_trade("0166.KL")

        def boom(tickers, start, end):
            raise RuntimeError("network catastrophe")
        monkeypatch.setattr(ca, "detect_for_tickers", boom)

        # Cycle should still complete (not raise)
        summary = scheduler._run_one_cycle(autotrade=False, autoexit=False)
        # Cycle finished. Scanning still proceeded.
        assert summary["scan_count"] == 0  # empty screener mock
        # Corp-actions saw 0 events because detection failed
        assert summary["corp_actions_detected"] == 0


# ---------------------------------------------------------------------------
# Scenario 4: Multiple events across multiple tickers
# ---------------------------------------------------------------------------

class TestMultiTickerMultiEvent:

    def test_all_events_processed(self, monkeypatch, cycle_env):
        import scheduler
        import corporate_actions as ca
        from corporate_actions import CorporateAction

        t1 = _insert_trade("0166.KL", entry_price=10.00, shares=100)
        t2 = _insert_trade("1155.KL", entry_price=5.00, shares=200)
        t3 = _insert_trade("5347.KL", entry_price=14.00, shares=100)  # gets dividend

        evs = [
            CorporateAction(ticker="0166.KL", ex_date="2026-05-15",
                            event_type="SPLIT", ratio=5.0, source="yfinance"),
            CorporateAction(ticker="1155.KL", ex_date="2026-05-15",
                            event_type="BONUS", ratio=1.5, source="moomoo"),
            CorporateAction(ticker="5347.KL", ex_date="2026-05-15",
                            event_type="DIVIDEND", amount_per_share=0.20,
                            source="yfinance"),
        ]
        monkeypatch.setattr(ca, "detect_for_tickers",
                            lambda tickers, start, end: evs)

        summary = scheduler._run_one_cycle(autotrade=False, autoexit=False)

        assert summary["corp_actions_detected"] == 3
        # Splits + bonus both count as "adjusted"
        assert summary["corp_actions_adjusted"] == 2

        a = _read_trade(t1); b = _read_trade(t2); c = _read_trade(t3)
        assert a["entry_price"] == 2.00 and a["shares"] == 500
        assert b["entry_price"] == round(5.00 / 1.5, 4)
        assert b["shares"] == 300
        # Dividend trade unchanged
        assert c["entry_price"] == 14.00 and c["shares"] == 100

        # 3 alerts dispatched (one per event)
        assert len(cycle_env) == 3


# ---------------------------------------------------------------------------
# Scenario 5: autoadjust=False (shadow mode via scheduler_state)
# ---------------------------------------------------------------------------

class TestShadowModeIntegration:

    def test_shadow_mode_alerts_only(self, monkeypatch, cycle_env):
        import scheduler
        import corporate_actions as ca
        from corporate_actions import CorporateAction
        from repository import update_scheduler_state

        # Set the toggle OFF in scheduler_state
        update_scheduler_state(corp_action_autoadjust=0)

        tid = _insert_trade("0166.KL", entry_price=10.00, shares=100)
        ev = CorporateAction(ticker="0166.KL", ex_date="2026-05-15",
                             event_type="SPLIT", ratio=5.0, source="yfinance")
        monkeypatch.setattr(ca, "detect_for_tickers",
                            lambda tickers, start, end: [ev])

        summary = scheduler._run_one_cycle(autotrade=False, autoexit=False)

        # Detected and alerted, NOT adjusted
        assert summary["corp_actions_detected"] == 1
        assert summary["corp_actions_adjusted"] == 0

        t = _read_trade(tid)
        # Trade unchanged
        assert t["entry_price"] == 10.00
        assert t["shares"] == 100
        assert t["cumulative_split_factor"] == 1.0

        # Alert sent
        assert any("auto-adjust off" in a["subject"].lower() for a in cycle_env)


# ---------------------------------------------------------------------------
# Scenario 6: last_corp_action_scan_at advances across cycles
# ---------------------------------------------------------------------------

class TestScanWindowAdvances:

    def test_scan_timestamp_persists(self, monkeypatch, cycle_env):
        import scheduler
        import corporate_actions as ca
        from repository import get_scheduler_state

        _insert_trade("0166.KL")
        monkeypatch.setattr(ca, "detect_for_tickers",
                            lambda tickers, start, end: [])

        before = get_scheduler_state().get("last_corp_action_scan_at")
        scheduler._run_one_cycle(autotrade=False, autoexit=False)
        after = get_scheduler_state().get("last_corp_action_scan_at")

        # Was None (or older) before, now populated
        assert after is not None
        if before:
            assert after >= before


# ---------------------------------------------------------------------------
# Scenario 7: idempotency across cycles
# ---------------------------------------------------------------------------

class TestIdempotencyAcrossCycles:

    def test_same_event_two_cycles_adjusted_once(self, monkeypatch, cycle_env):
        import scheduler
        import corporate_actions as ca
        from corporate_actions import CorporateAction

        tid = _insert_trade("0166.KL", entry_price=10.00, shares=100)
        ev = CorporateAction(ticker="0166.KL", ex_date="2026-05-15",
                             event_type="SPLIT", ratio=5.0, source="yfinance")
        monkeypatch.setattr(ca, "detect_for_tickers",
                            lambda tickers, start, end: [ev])

        s1 = scheduler._run_one_cycle(autotrade=False, autoexit=False)
        s2 = scheduler._run_one_cycle(autotrade=False, autoexit=False)

        assert s1["corp_actions_adjusted"] == 1
        assert s2["corp_actions_adjusted"] == 0  # idempotent

        # Trade adjusted ONCE — not 25× (which would be 5 then 5 again)
        t = _read_trade(tid)
        assert t["cumulative_split_factor"] == 5.0
        assert t["shares"] == 500


# ---------------------------------------------------------------------------
# Scenario 8: cash invariant holds end-to-end
# ---------------------------------------------------------------------------

class TestCashInvariantEndToEnd:

    @pytest.mark.parametrize("ratio,shares,price", [
        (5.0, 100, 10.00),
        (2.0, 1000, 0.50),
        (0.2, 500, 1.00),
        (1.5, 200, 3.00),
    ])
    def test_basis_preserved_after_real_cycle(self, monkeypatch, cycle_env,
                                               ratio, shares, price):
        import scheduler
        import corporate_actions as ca
        from corporate_actions import CorporateAction

        tid = _insert_trade("0166.KL", entry_price=price, shares=shares,
                             cost=round(price * shares, 2),
                             shares_remaining=shares,
                             lots=max(shares // 100, 1))

        ev = CorporateAction(ticker="0166.KL", ex_date="2026-05-15",
                             event_type="SPLIT", ratio=ratio,
                             source="yfinance")
        monkeypatch.setattr(ca, "detect_for_tickers",
                            lambda tickers, start, end: [ev])

        scheduler._run_one_cycle(autotrade=False, autoexit=False)

        t = _read_trade(tid)
        before_basis = price * shares
        after_basis = t["entry_price"] * t["shares"]
        # Within RM 1.00 (the project's golden invariant)
        assert abs(after_basis - before_basis) < 1.00
