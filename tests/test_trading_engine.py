"""
Cash-conservation property tests + lot-size enforcement.
"""

import pytest


def _market_regime():
    return {"regime_data": {"regime": "NEUTRAL"},
            "position_rules": {"conviction_pct": 50}}


def _analysis():
    return {"reasoning": "test", "rsi": 55, "vol_ratio": 1.5,
            "atr": 0.05, "support": 2.85, "resistance": 3.10,
            "macd_hist": 0.01, "ema_trend": 2.95}


def test_entry_below_lot_size_rejected():
    from trading_engine import execute_entry
    ok, tid, msg = execute_entry(
        "0166.KL", "Inari", "Technology",
        entry_price=3.0, stop_loss=2.85,
        tp1=3.15, tp2=3.30, tp3=3.45,
        signal_type="GOLD BUY (BREAKOUT)",
        shares=50,  # below 100-share lot
        analysis_data=_analysis(),
        market_regime=_market_regime(), confidence_score=70,
    )
    assert not ok
    assert "lot" in msg.lower()


def test_entry_rounds_down_to_lot():
    """137 shares should become 100."""
    from trading_engine import execute_entry
    from repository import get_trade

    ok, tid, _ = execute_entry(
        "0166.KL", "Inari", "Technology", 3.0, 2.85,
        3.15, 3.30, 3.45, "GOLD BUY (BREAKOUT)", 137,
        _analysis(), _market_regime(), 70,
    )
    assert ok
    t = get_trade(tid)
    assert t["shares"] == 100
    assert t["lots"] == 1


def test_insufficient_cash_rejected():
    from trading_engine import execute_entry
    from repository import save_account

    save_account(cash_balance=10.0)
    ok, _, msg = execute_entry(
        "0166.KL", "Inari", "Technology", 3.0, 2.85,
        3.15, 3.30, 3.45, "GOLD BUY (BREAKOUT)", 1000,
        _analysis(), _market_regime(), 70,
    )
    assert not ok
    assert "insufficient" in msg.lower()


def test_cash_conservation_full_cycle_tp3():
    """
    Buy 100 sh @ 3.00, settle at TP3 = 3.45. Verify:
        final_cash == initial_cash + net_pnl
    where net_pnl includes both legs' fees + buy/sell slippage.
    """
    from trading_engine import execute_entry, execute_full_exit
    from repository import load_account, get_trade, save_account

    save_account(initial_capital=20000.0, cash_balance=20000.0,
                 total_equity=20000.0)
    pre = load_account()["cash_balance"]

    ok, tid, _ = execute_entry(
        "0166.KL", "Inari", "Technology", 3.0, 2.85,
        3.15, 3.30, 3.45, "GOLD BUY (BREAKOUT)", 100,
        _analysis(), _market_regime(), 70,
    )
    assert ok

    t = get_trade(tid)
    cash_after_entry = load_account()["cash_balance"]
    # entry cost ≈ filled_price * 100 * 1.0015
    assert cash_after_entry < pre

    ok, _ = execute_full_exit(tid, 3.45, reason="TP3 test",
                              outcome="WIN", actor="USER")
    assert ok
    post = load_account()["cash_balance"]
    t = get_trade(tid)

    # net_pnl from the trade
    realized = t["realized_pnl"]
    # ledger invariant
    assert abs((post - pre) - realized) < 0.05  # 2dp rounding tolerance


def test_partial_then_full_exit_preserves_cash():
    """
    Buy 200 sh, partial 100 at TP2, then full exit 100 at TP3.
    Verify final cash == initial - entry_outlay + partial_proceeds + full_proceeds.
    """
    from trading_engine import (execute_entry, execute_partial_exit,
                                execute_full_exit)
    from repository import load_account, get_trade

    pre = load_account()["cash_balance"]

    ok, tid, _ = execute_entry(
        "0166.KL", "Inari", "Technology", 3.0, 2.85,
        3.15, 3.30, 3.45, "GOLD BUY (BREAKOUT)", 200,
        _analysis(), _market_regime(), 70,
    )
    assert ok
    after_entry = load_account()["cash_balance"]

    ok, _ = execute_partial_exit(tid, "TP2", 3.30, 100,
                                  reason="test partial")
    assert ok
    after_partial = load_account()["cash_balance"]
    assert after_partial > after_entry  # cash returned

    ok, _ = execute_full_exit(tid, 3.45, reason="rest",
                              outcome="WIN", actor="USER")
    assert ok
    final = load_account()["cash_balance"]
    t = get_trade(tid)
    # Ledger invariant — final cash equals starting cash + total realized P&L
    realized = t["realized_pnl"]
    assert abs((final - pre) - realized) < 0.15  # 2-leg 2dp rounding


def test_full_exit_loss_marked_correctly():
    from trading_engine import execute_entry, execute_full_exit
    from repository import get_trade

    ok, tid, _ = execute_entry(
        "0166.KL", "Inari", "Technology", 3.0, 2.85,
        3.15, 3.30, 3.45, "GOLD BUY (BREAKOUT)", 100,
        _analysis(), _market_regime(), 70,
    )
    assert ok
    execute_full_exit(tid, 2.85, reason="SL hit",
                      outcome="LOSS", actor="USER")
    t = get_trade(tid)
    assert t["status"] == "CLOSED"
    assert t["outcome"] == "LOSS"
    assert (t["realized_pnl"] or 0) < 0


# ---------------------------------------------------------------------------
# v3.8 — Ratcheting (chandelier) trailing stop
# ---------------------------------------------------------------------------

def _settle_lookup(price, high=None, low=None):
    high = high if high is not None else price
    low = low if low is not None else price
    return {"0166.KL": {"price": price, "high": high, "low": low}}


def _ratchet_trade(atr=0.05, highest=3.16, trailing=3.134):
    """An ACTIVE trade as if entered at 3.00 with trail already armed at TP1."""
    from db import myt_iso
    from repository import insert_trade

    return insert_trade({
        "ticker": "0166.KL", "name": "Inari", "sector": "Technology",
        "signal_type": "TEST", "entry_price": 3.0, "stop_loss": 2.85,
        "tp1": 3.15, "tp2": 3.60, "tp3": 3.90,  # targets far away — no interference
        "shares": 100, "lots": 1, "cost": 300.0, "fee": 0.45,
        "total_outlay": 300.45, "risk_per_share": 0.15, "actual_risk_pct": 5.0,
        "status": "ACTIVE", "phase": "FULL",
        "shares_remaining": 100, "logged_at": myt_iso(),
        "highest_price": highest, "lowest_price": 2.95,
        "trailing_stop": trailing,
        "entry_indicators": {"atr": atr} if atr else {},
    })


def test_trail_ratchets_up_only_off_yesterdays_peak():
    """Chandelier lags one cycle: today's high tightens tomorrow's trail."""
    from trading_engine import auto_settle_trades, TRAIL_ATR_MULT
    from repository import get_trade

    tid = _ratchet_trade(atr=0.05, highest=3.16, trailing=3.134)

    # Day A: new high 3.30 — ratchet still uses yesterday's peak (3.16):
    # candidate 3.16 - 2.5x0.05 = 3.035 < trail 3.134 → no move.
    auto_settle_trades(_settle_lookup(3.28, high=3.30, low=3.20),
                       _market_regime(), actor="TEST")
    t = get_trade(tid)
    assert t["status"] == "ACTIVE"
    assert t["trailing_stop"] == 3.134
    assert t["highest_price"] == 3.30

    # Day B: now the 3.30 peak is "yesterday" → trail tightens to 3.175.
    auto_settle_trades(_settle_lookup(3.28, high=3.29, low=3.19),
                       _market_regime(), actor="TEST")
    t = get_trade(tid)
    assert t["status"] == "ACTIVE"
    assert t["trailing_stop"] == round(3.30 - TRAIL_ATR_MULT * 0.05, 3)


def test_trail_never_moves_down():
    from trading_engine import auto_settle_trades
    from repository import get_trade

    # Candidate from a faded peak (3.20 - 2.5x0.05 = 3.075) is BELOW the
    # current trail 3.129 — a naive implementation would lower the stop.
    tid = _ratchet_trade(atr=0.05, highest=3.20, trailing=3.129)
    auto_settle_trades(_settle_lookup(3.30, high=3.40, low=3.15),
                       _market_regime(), actor="TEST")
    t = get_trade(tid)
    assert t["status"] == "ACTIVE"
    assert t["trailing_stop"] == 3.129


def test_trail_fallback_pct_when_atr_missing():
    """Trades without entry ATR (e.g. legacy/manual) use fixed % distance."""
    from trading_engine import auto_settle_trades, TRAIL_FALLBACK_PCT
    from repository import get_trade

    tid = _ratchet_trade(atr=None, highest=3.60, trailing=3.40)
    auto_settle_trades(_settle_lookup(3.58, high=3.59, low=3.50),
                       _market_regime(), actor="TEST")
    t = get_trade(tid)
    assert t["status"] == "ACTIVE"
    assert t["trailing_stop"] == round(3.60 * (1 - TRAIL_FALLBACK_PCT / 100), 3)


def test_trail_hit_same_day_uses_tightened_level():
    """Give-back after a peak fills at the tightened chandelier, a WIN."""
    from trading_engine import auto_settle_trades
    from repository import get_trade

    tid = _ratchet_trade(atr=0.05, highest=3.30, trailing=3.134)
    res = auto_settle_trades(_settle_lookup(3.12, high=3.29, low=3.10),
                             _market_regime(), actor="TEST")
    t = get_trade(tid)
    assert t["status"] == "CLOSED"
    assert t["outcome"] == "WIN"          # trail 3.175 > entry 3.00
    assert t["shares_remaining"] == 0
    # Filled at tightened trail 3.175 minus a touch of sell slippage.
    assert 3.165 < t["exit_price"] <= 3.175
    assert any(s["type"] == "TRAIL" for s in res["settled"])


def test_tp1_arm_still_sets_trail_once():
    """Regression: arm-at-TP1 is unchanged; ratchet skips un-armed trades."""
    from trading_engine import auto_settle_trades
    from repository import get_trade

    tid = _ratchet_trade(atr=0.05, highest=None, trailing=None)
    auto_settle_trades(_settle_lookup(3.14, high=3.16, low=3.10),
                       _market_regime(), actor="TEST")
    t = get_trade(tid)
    assert t["status"] == "ACTIVE"
    assert t["trailing_stop"] == round(max(3.0 * 1.005, 3.14 * 0.995), 3)
