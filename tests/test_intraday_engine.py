#!/usr/bin/env python3
# tests/test_intraday_engine.py
"""
Unit tests for intraday_engine.py (v3.7 Block 4).

Covers:
  * Position sizing math
  * execute_intraday_entry() — guards, execution_type tag
  * auto_settle_intraday() — SL/TP exit priorities
  * force_flat_all_intraday() — THE invariant (zero trades left)
  * get_active_intraday_tickers() — screener integration
  * intraday_session_status() — all 5 session states
  * No trailing stop for intraday trades
"""

from __future__ import annotations

from datetime import datetime, date, time as dtime, timedelta
from unittest import mock

import pytest

from intraday_engine import (
    intraday_position_size,
    execute_intraday_entry,
    auto_settle_intraday,
    force_flat_all_intraday,
    get_active_intraday_tickers,
    intraday_session_status,
    INTRADAY_EXECUTION_TYPE,
    INTRADAY_DEFAULT_RISK_PCT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(ticker="TNA", entry=102.0, stop=99.0,
                 tp1=105.0, tp2=106.0, tp3=107.0,
                 confidence=70.0, signal_type="GOLD BUY (ORB)"):
    return {
        "ticker": ticker, "name": ticker, "sector": "Leveraged ETF",
        "source": "INTRADAY",
        "entry": entry, "stop_loss": stop,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "signal": signal_type, "confidence": confidence,
        "reasoning": "Test signal",
        "price": entry, "prev_price": entry,
        "change_pct": 0.0, "volume": 5000, "vol_ratio": 2.0,
        "rsi": 50.0, "risk_pct": 2.9,
        "atr": 3.0, "support": stop, "resistance": 101.0,
        "ema_trend": 0.0, "ema_fast": 0.0, "ema_slow": 0.0,
        "macd_hist": 0.0, "bb_upper": 0.0, "bb_lower": 0.0,
        "market_regime": "", "rs_rank": None,
        "rs_signal": None, "rs_ratio": None,
        "q_action": None, "q_confidence": 0.0, "q_reasoning": None,
        "indicators": {
            "vwap": 100.5, "rel_vol": 2.0,
            "or_high": 101.0, "or_low": 99.0, "or_range": 2.0,
            "ema_trend_direction": "UP",
            "entry_timestamp": "2026-06-03T09:50:00",
        },
    }


def _make_mock_trade(trade_id=1, ticker="TNA", entry=102.0, stop=99.0,
                     tp1=105.0, tp2=106.0, tp3=107.0,
                     shares=100, shares_rem=100, status="ACTIVE",
                     exec_type=INTRADAY_EXECUTION_TYPE,
                     phase="FULL", high_px=102.0, low_px=102.0):
    """Return a dict shaped like the trade rows from repository.active_trades()."""
    return {
        "id": trade_id,
        "ticker": ticker,
        "entry_price": entry,
        "stop_loss": stop,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "shares": shares,
        "shares_remaining": shares_rem,
        "status": status,
        "execution_type": exec_type,
        "phase": phase,
        "highest_price": high_px,
        "lowest_price": low_px,
        "mae_pct": 0.0, "mfe_pct": 0.0,
        "unrealized_pnl": 0.0, "realized_pnl": 0.0,
        "slippage_pct": 0.0,
        "fee": 0.0,
        "cost": entry * shares,
        "logged_at": "2026-06-03 14:00:00",
        "trailing_stop": None,
        "notes": "",
        "tags_json": "[]",
        "signal_type": "GOLD BUY (ORB)",
        "confidence_score": 70.0,
    }


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

class TestPositionSizing:
    def test_basic(self, monkeypatch):
        # Mock load_account to return known capital
        monkeypatch.setattr(
            "intraday_engine.load_account",
            lambda: {"total_equity": 20000.0, "cash_balance": 20000.0},
        )
        # US lot size = 1 (no rounding)
        monkeypatch.setattr(
            "intraday_engine.round_to_lot", lambda s: int(s),
        )
        # entry=102, stop=99, risk_per_share=3
        # capital=20000, risk=1% → risk_amount=200
        # shares = 200/3 = 66.66 → round to lot (US lot=1) → 66
        shares = intraday_position_size(102.0, 99.0)
        assert shares == 66

    def test_negative_risk_per_share(self, monkeypatch):
        monkeypatch.setattr(
            "intraday_engine.load_account",
            lambda: {"total_equity": 5000.0},
        )
        # stop above entry → negative risk → 0 shares
        shares = intraday_position_size(100.0, 101.0)
        assert shares == 0

    def test_custom_risk_pct(self, monkeypatch):
        monkeypatch.setattr(
            "intraday_engine.load_account",
            lambda: {"total_equity": 10000.0},
        )
        monkeypatch.setattr(
            "intraday_engine.round_to_lot", lambda s: int(s),
        )
        # risk_per_share=2, 0.5% risk → risk_amount=50, shares=25
        shares = intraday_position_size(100.0, 98.0, risk_pct=0.5)
        assert shares == 25

    def test_explicit_capital(self, monkeypatch):
        # Don't mock load_account — pass capital explicitly
        monkeypatch.setattr(
            "intraday_engine.round_to_lot", lambda s: int(s),
        )
        shares = intraday_position_size(100.0, 98.0, capital=10000.0)
        assert shares == 50  # risk=100, rps=2 → 50

    def test_default_risk_pct_is_1(self):
        assert INTRADAY_DEFAULT_RISK_PCT == 1.0


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

class TestExecuteIntradayEntry:
    def test_entry_delegates_to_trading_engine(self, monkeypatch):
        """execute_intraday_entry should call trading_engine.execute_entry
        with the correct execution_type."""
        calls = []

        def fake_execute_entry(**kwargs):
            calls.append(kwargs)
            return True, 42, "OK"

        monkeypatch.setattr(
            "intraday_engine.execute_entry", fake_execute_entry,
        )
        monkeypatch.setattr(
            "intraday_engine.load_account",
            lambda: {"total_equity": 10000.0, "cash_balance": 10000.0},
        )
        # Must be during session — mock _is_during_session to True
        monkeypatch.setattr(
            "intraday_engine._is_during_session", lambda now_et=None: True,
        )
        monkeypatch.setattr(
            "intraday_engine._is_past_flat_by", lambda now_et=None: False,
        )
        monkeypatch.setattr(
            "intraday_engine.round_to_lot", lambda s: int(s),
        )

        sig = _make_signal()
        # Mock save_account to prevent DB errors during equity update at end of settle
        monkeypatch.setattr("intraday_engine.save_account", lambda **kw: None)
        ok, tid, msg = execute_intraday_entry(sig)

        assert ok
        assert tid == 42
        assert len(calls) == 1
        kw = calls[0]
        assert kw["execution_type"] == INTRADAY_EXECUTION_TYPE
        assert kw["ticker"] == "TNA"
        assert kw["entry_price"] == 102.0
        assert kw["stop_loss"] == 99.0

    def test_refuses_past_flat_by(self, monkeypatch):
        monkeypatch.setattr(
            "intraday_engine._is_past_flat_by", lambda now_et=None: True,
        )
        monkeypatch.setattr(
            "intraday_engine._is_during_session", lambda now_et=None: True,
        )
        sig = _make_signal()
        # Mock save_account to prevent DB errors during equity update at end of settle
        monkeypatch.setattr("intraday_engine.save_account", lambda **kw: None)
        ok, tid, msg = execute_intraday_entry(sig)
        assert not ok
        assert "Past force-flat time" in msg

    def test_refuses_outside_session(self, monkeypatch):
        monkeypatch.setattr(
            "intraday_engine._is_past_flat_by", lambda now_et=None: False,
        )
        monkeypatch.setattr(
            "intraday_engine._is_during_session", lambda now_et=None: False,
        )
        sig = _make_signal()
        # Mock save_account to prevent DB errors during equity update at end of settle
        monkeypatch.setattr("intraday_engine.save_account", lambda **kw: None)
        ok, tid, msg = execute_intraday_entry(sig)
        assert not ok
        assert "Outside US session" in msg

    def test_refuses_invalid_prices(self, monkeypatch):
        monkeypatch.setattr(
            "intraday_engine._is_during_session", lambda now_et=None: True,
        )
        monkeypatch.setattr(
            "intraday_engine._is_past_flat_by", lambda now_et=None: False,
        )
        sig = _make_signal(entry=100.0, stop=100.0)  # equal
        # Mock save_account to prevent DB errors during equity update at end of settle
        monkeypatch.setattr("intraday_engine.save_account", lambda **kw: None)
        ok, tid, msg = execute_intraday_entry(sig)
        assert not ok
        assert "Invalid prices" in msg

    def test_refuses_zero_shares(self, monkeypatch):
        monkeypatch.setattr(
            "intraday_engine._is_during_session", lambda now_et=None: True,
        )
        monkeypatch.setattr(
            "intraday_engine._is_past_flat_by", lambda now_et=None: False,
        )
        monkeypatch.setattr(
            "intraday_engine.load_account",
            lambda: {"total_equity": 100.0},  # tiny capital → 0 shares
        )
        sig = _make_signal()
        # Mock save_account to prevent DB errors during equity update at end of settle
        monkeypatch.setattr("intraday_engine.save_account", lambda **kw: None)
        ok, tid, msg = execute_intraday_entry(sig)
        assert not ok
        assert "Position size is zero" in msg


# ---------------------------------------------------------------------------
# auto_settle_intraday
# ---------------------------------------------------------------------------

class TestAutoSettleIntraday:
    def test_skips_non_intraday_trades(self, monkeypatch):
        """Only trades with execution_type=AGENT_INTRADAY are settled."""
        monkeypatch.setattr(
            "intraday_engine.active_trades",
            lambda: [_make_mock_trade(exec_type="AGENT")],  # swing trade
        )
        result = auto_settle_intraday({})
        assert result["settled"] == []
        assert result["partials"] == []

    def test_sl_exit(self, monkeypatch):
        """A bar with low below stop_loss triggers full exit."""
        exit_calls = []

        def fake_full_exit(trade_id, price, **kw):
            exit_calls.append((trade_id, price, kw))
            return True, "SL hit"

        monkeypatch.setattr(
            "intraday_engine.active_trades",
            lambda: [_make_mock_trade(entry=102, stop=99)],
        )
        monkeypatch.setattr(
            "intraday_engine.execute_full_exit", fake_full_exit,
        )
        monkeypatch.setattr(
            "intraday_engine.update_trade", lambda tid, data: None,
        )

        bar = {"price": 98.5, "high": 99.5, "low": 98.0}
        monkeypatch.setattr("intraday_engine.save_account", lambda **kw: None)
        result = auto_settle_intraday({"TNA": bar})

        assert len(exit_calls) == 1
        assert exit_calls[0][0] == 1
        assert exit_calls[0][1] == 99.0  # exit at stop price
        assert exit_calls[0][2]["reason"] == "Hard SL hit (OR_low)"
        assert len(result["settled"]) == 1
        assert result["settled"][0]["type"] == "SL"

    def test_tp3_exit(self, monkeypatch):
        exit_calls = []

        def fake_full_exit(trade_id, price, **kw):
            exit_calls.append((trade_id, price, kw))
            return True, "TP3 hit"

        monkeypatch.setattr(
            "intraday_engine.active_trades",
            lambda: [_make_mock_trade(entry=102, stop=99, tp3=107)],
        )
        monkeypatch.setattr(
            "intraday_engine.execute_full_exit", fake_full_exit,
        )
        monkeypatch.setattr(
            "intraday_engine.update_trade", lambda tid, data: None,
        )

        bar = {"price": 107.5, "high": 108.0, "low": 106.0}
        monkeypatch.setattr("intraday_engine.save_account", lambda **kw: None)
        result = auto_settle_intraday({"TNA": bar})

        assert len(exit_calls) == 1
        assert exit_calls[0][1] == 107.0  # exit at TP3 price
        assert result["settled"][0]["type"] == "TP3"

    def test_tp3_priority_over_sl(self, monkeypatch):
        """If a bar's high hits TP3 AND low hits SL, TP3 wins."""
        exit_calls = []

        def fake_full_exit(trade_id, price, **kw):
            exit_calls.append((trade_id, price, kw["reason"]))
            return True, "ok"

        monkeypatch.setattr(
            "intraday_engine.active_trades",
            lambda: [_make_mock_trade(entry=102, stop=99, tp3=107)],
        )
        monkeypatch.setattr(
            "intraday_engine.execute_full_exit", fake_full_exit,
        )
        monkeypatch.setattr(
            "intraday_engine.update_trade", lambda tid, data: None,
        )

        # Bar that hits BOTH TP3 and SL
        bar = {"price": 103.0, "high": 108.0, "low": 98.0}
        monkeypatch.setattr("intraday_engine.save_account", lambda **kw: None)
        result = auto_settle_intraday({"TNA": bar})

        assert len(exit_calls) == 1
        assert "TP3" in exit_calls[0][2]  # TP3 wins (checked first)

    def test_tp2_partial_exit(self, monkeypatch):
        partial_calls = []

        def fake_partial_exit(trade_id, level, price, shares, **kw):
            partial_calls.append((trade_id, shares))
            return True, "ok"

        monkeypatch.setattr(
            "intraday_engine.active_trades",
            lambda: [_make_mock_trade(entry=102, tp2=106, tp3=110, shares=100,
                                      shares_rem=100, phase="FULL")],
        )
        monkeypatch.setattr(
            "intraday_engine.update_trade", lambda tid, data: None,
        )
        monkeypatch.setattr(
            "intraday_engine.execute_partial_exit", fake_partial_exit,
        )
        monkeypatch.setattr(
            "intraday_engine.round_to_lot", lambda s: int(s),
        )

        bar = {"price": 106.5, "high": 107.0, "low": 105.0}
        monkeypatch.setattr("intraday_engine.save_account", lambda **kw: None)
        result = auto_settle_intraday({"TNA": bar})

        assert len(partial_calls) == 1
        assert partial_calls[0][0] == 1
        assert partial_calls[0][1] == 50  # half of 100 shares
        assert len(result["partials"]) == 1

    def test_tp1_does_not_set_trailing(self, monkeypatch):
        """Unlike swing, intraday TP1 does NOT set a trailing stop."""
        monkeypatch.setattr(
            "intraday_engine.active_trades",
            lambda: [_make_mock_trade(entry=102, tp1=105, phase="FULL")],
        )
        update_calls = []

        def fake_update(tid, data):
            update_calls.append(data)

        monkeypatch.setattr(
            "intraday_engine.update_trade", fake_update,
        )

        bar = {"price": 105.5, "high": 106.0, "low": 104.0}
        monkeypatch.setattr("intraday_engine.save_account", lambda **kw: None)
        result = auto_settle_intraday({"TNA": bar})

        # No exit, no partial, and NO trailing_stop set
        assert result["settled"] == []
        assert result["partials"] == []
        # The update should only be MAE/MFE tracking, not trailing_stop
        for call in update_calls:
            assert "trailing_stop" not in call, (
                "intraday should never set trailing stops"
            )


# ---------------------------------------------------------------------------
# FORCE-FLAT INVARIANT
# ---------------------------------------------------------------------------

class TestForceFlat:
    def test_closes_all_intraday_trades(self, monkeypatch):
        exit_calls = []

        def fake_full_exit(trade_id, price, **kw):
            exit_calls.append(trade_id)
            return True, "force-flat ok"

        monkeypatch.setattr(
            "intraday_engine.active_trades",
            lambda: [
                _make_mock_trade(1, "TNA"),
                _make_mock_trade(2, "GOOGL"),
                _make_mock_trade(3, "SPY", exec_type="AGENT"),  # swing — skip
            ],
        )
        monkeypatch.setattr(
            "intraday_engine.execute_full_exit", fake_full_exit,
        )

        bar = {"price": 103.0, "high": 104.0, "low": 102.0}
        closed = force_flat_all_intraday({"TNA": bar, "GOOGL": bar})

        assert closed == 2
        assert 1 in exit_calls
        assert 2 in exit_calls
        assert 3 not in exit_calls  # swing trade untouched

    def test_returns_zero_when_no_intraday_trades(self, monkeypatch):
        monkeypatch.setattr(
            "intraday_engine.active_trades",
            lambda: [_make_mock_trade(exec_type="AGENT")],  # only swing
        )
        closed = force_flat_all_intraday({})
        assert closed == 0

    def test_force_flat_reason_is_clear(self, monkeypatch):
        exit_calls = []

        def fake_full_exit(trade_id, price, **kw):
            exit_calls.append(kw)
            return True, "ok"

        monkeypatch.setattr(
            "intraday_engine.active_trades",
            lambda: [_make_mock_trade(1, "TNA")],
        )
        monkeypatch.setattr(
            "intraday_engine.execute_full_exit", fake_full_exit,
        )

        bar = {"price": 100.0}
        force_flat_all_intraday({"TNA": bar})

        assert len(exit_calls) == 1
        assert "FORCE FLAT" in exit_calls[0]["reason"]
        assert "15:55" in exit_calls[0]["reason"]

    def test_handles_missing_price_data(self, monkeypatch):
        """If a ticker has no bar data, fall back to highest_price."""
        exit_calls = []

        def fake_full_exit(trade_id, price, **kw):
            exit_calls.append((trade_id, price))
            return True, "ok"

        monkeypatch.setattr(
            "intraday_engine.active_trades",
            lambda: [_make_mock_trade(1, "TNA", entry=102, high_px=105)],
        )
        monkeypatch.setattr(
            "intraday_engine.execute_full_exit", fake_full_exit,
        )

        closed = force_flat_all_intraday({})  # no data for TNA
        assert closed == 1
        assert exit_calls[0][1] == 105.0  # fallback to highest_price


# ---------------------------------------------------------------------------
# Active ticker tracking
# ---------------------------------------------------------------------------

class TestActiveTickers:
    def test_only_intraday(self, monkeypatch):
        monkeypatch.setattr(
            "intraday_engine.active_trades",
            lambda: [
                _make_mock_trade(1, "TNA"),
                _make_mock_trade(2, "GOOGL"),
                _make_mock_trade(3, "AAPL", exec_type="AGENT"),
                _make_mock_trade(4, "MSTR", status="CLOSED"),
            ],
        )
        tickers = get_active_intraday_tickers()
        assert tickers == {"TNA", "GOOGL"}

    def test_empty(self, monkeypatch):
        monkeypatch.setattr(
            "intraday_engine.active_trades", lambda: [],
        )
        assert get_active_intraday_tickers() == set()


# ---------------------------------------------------------------------------
# Session status
# ---------------------------------------------------------------------------

class TestSessionStatus:
    PREMARKET = datetime(2026, 6, 3, 8, 0)     # 08:00 ET
    OR_WINDOW = datetime(2026, 6, 3, 9, 35)     # 09:35 ET (inside OR)
    ACTIVE = datetime(2026, 6, 3, 10, 30)        # 10:30 ET
    FORCE_FLAT = datetime(2026, 6, 3, 15, 57)    # 15:57 ET
    POSTMARKET = datetime(2026, 6, 3, 16, 30)    # 16:30 ET

    def test_premarket(self):
        s = intraday_session_status(self.PREMARKET)
        assert s["state"] == "PREMARKET"
        assert not s["can_enter"]

    def test_or_window(self):
        s = intraday_session_status(self.OR_WINDOW)
        assert s["state"] == "OR_WINDOW"
        assert s["can_scan"]
        assert not s["can_enter"]

    def test_active_trading(self):
        s = intraday_session_status(self.ACTIVE)
        assert s["state"] == "ACTIVE_TRADING"
        assert s["can_scan"]
        assert s["can_enter"]
        assert not s["should_force_flat"]

    def test_force_flat_window(self):
        s = intraday_session_status(self.FORCE_FLAT)
        assert s["state"] == "FORCE_FLAT_WINDOW"
        assert not s["can_enter"]
        assert s["should_force_flat"]

    def test_postmarket(self):
        s = intraday_session_status(self.POSTMARKET)
        assert s["state"] == "POSTMARKET"
        assert not s["can_scan"]
        assert s["should_force_flat"]


# ---------------------------------------------------------------------------
# Execution type tag
# ---------------------------------------------------------------------------

class TestExecutionType:
    def test_tag_is_consistent(self):
        assert INTRADAY_EXECUTION_TYPE == "AGENT_INTRADAY"

    def test_tag_differs_from_swing(self):
        assert INTRADAY_EXECUTION_TYPE != "AGENT"
        assert INTRADAY_EXECUTION_TYPE != "MANUAL"
