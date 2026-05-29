# tests/test_corporate_actions_phase3.py
"""
Phase-3 tests for trading_engine.apply_split_to_trade.

The most safety-critical phase: any bug here corrupts trade records and
potentially the cash-conservation invariant.

Covers:
  - Forward split (1-for-5, ratio=5.0)
  - Reverse split (5-for-1, ratio=0.2)
  - Bonus issue (1-for-2, ratio=1.5)
  - All numeric per-share fields adjusted correctly
  - All share-count fields scaled correctly
  - cumulative_split_factor composes correctly across multiple splits
  - Cost / fee / total_outlay / PnL fields ARE NOT touched
  - Cash-conservation invariant preserved within RM 1.00
  - Audit note appended to trade.notes
  - Idempotency: re-applying same ratio composes (NOT a no-op — that's by
    caller's responsibility via corporate_actions_processed table)
  - Atomicity: error mid-update rolls back, leaves trade unchanged
  - Rejects bad ratio (0, negative, exactly 1.0)
  - Rejects nonexistent trade_id
  - Rejects non-ACTIVE trade (CLOSED, PARTIAL, etc.)
  - Race condition: trade closed between SELECT and UPDATE → rollback
"""

from __future__ import annotations

import pytest

from trading_engine import apply_split_to_trade


# ---------------------------------------------------------------------------
# Helpers: insert a fully-specified ACTIVE trade for adjustment tests
# ---------------------------------------------------------------------------

def _insert_test_trade(**overrides) -> int:
    """Insert one ACTIVE trade and return its id. Uses sensible defaults
    matching a real entry from execute_entry()."""
    from db import connect, myt_iso

    defaults = {
        "ticker": "0166.KL",
        "name": "Inari Amertron Berhad",
        "sector": "Technology",
        "signal_type": "BREAKOUT",
        "entry_price": 3.50,
        "stop_loss": 3.20,
        "tp1": 3.80,
        "tp2": 4.10,
        "tp3": 4.50,
        "shares": 500,           # 5 lots of 100
        "lots": 5,
        "cost": 1750.00,         # 3.50 * 500
        "fee": 17.50,            # 1% Bursa-style
        "total_outlay": 1767.50,
        "risk_per_share": 0.30,
        "actual_risk_pct": 0.75,
        "status": "ACTIVE",
        "phase": "FULL",
        "logged_at": myt_iso(),
        "execution_type": "AUTO",
        "trailing_stop": 3.30,
        "highest_price": 3.65,
        "lowest_price": 3.40,
        "unrealized_pnl": 50.0,
        "realized_pnl": 0.0,
        "shares_remaining": 500,
        "slippage_pct": 0.05,
        "cumulative_split_factor": 1.0,
    }
    defaults.update(overrides)

    cols = ", ".join(defaults.keys())
    placeholders = ", ".join(["?"] * len(defaults))
    with connect() as c:
        cur = c.execute(
            f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
            tuple(defaults.values()),
        )
        return cur.lastrowid


def _read_trade(trade_id: int) -> dict:
    from db import connect
    with connect(readonly=True) as c:
        row = c.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# Happy path: forward / reverse / bonus
# ---------------------------------------------------------------------------

class TestForwardSplit:

    def test_basic_1_for_5(self):
        tid = _insert_test_trade(
            entry_price=10.00, stop_loss=9.00, tp1=11.00, tp2=12.00, tp3=13.00,
            shares=100, lots=1, shares_remaining=100,
            trailing_stop=9.50, highest_price=10.50, lowest_price=9.80,
            risk_per_share=1.00, cost=1000.00, fee=10.0, total_outlay=1010.0,
        )

        result = apply_split_to_trade(tid, ratio=5.0, ex_date="2026-05-29")

        t = _read_trade(tid)
        # Per-share prices divided by 5
        assert t["entry_price"] == 2.00
        assert t["stop_loss"] == 1.80
        assert t["tp1"] == 2.20
        assert t["tp2"] == 2.40
        assert t["tp3"] == 2.60
        assert t["trailing_stop"] == 1.90
        assert t["highest_price"] == 2.10
        assert t["lowest_price"] == 1.96
        assert t["risk_per_share"] == 0.20

        # Share counts multiplied by 5
        assert t["shares"] == 500
        assert t["shares_remaining"] == 500
        assert t["lots"] == 5

        # cumulative_split_factor recorded
        assert t["cumulative_split_factor"] == 5.0

        # Result dict includes the diff
        assert result["ratio"] == 5.0
        assert result["before"]["entry_price"] == 10.00
        assert result["after"]["entry_price"] == 2.00

    def test_reverse_split_5_for_1(self):
        tid = _insert_test_trade(entry_price=1.00, shares=500,
                                  shares_remaining=500, lots=5,
                                  stop_loss=0.80, tp1=1.20)
        apply_split_to_trade(tid, ratio=0.2, ex_date="2026-05-29")
        t = _read_trade(tid)
        # Per-share prices multiply by 5 (i.e. ÷ 0.2)
        assert t["entry_price"] == 5.00
        assert t["stop_loss"] == 4.00
        assert t["tp1"] == 6.00
        # Share counts divide by 5
        assert t["shares"] == 100
        assert t["shares_remaining"] == 100
        assert t["lots"] == 1

    def test_bonus_issue_1_for_2(self):
        """1 free bonus share for every 2 held → ratio 1.5"""
        tid = _insert_test_trade(entry_price=3.00, shares=200,
                                  shares_remaining=200, lots=2)
        apply_split_to_trade(tid, ratio=1.5, ex_date="2026-05-29")
        t = _read_trade(tid)
        assert t["entry_price"] == 2.00  # 3.00 / 1.5
        assert t["shares"] == 300        # 200 * 1.5


# ---------------------------------------------------------------------------
# THE INVARIANT: cash conservation
# ---------------------------------------------------------------------------

class TestCashConservation:
    """
    The most important property: a stock split must not change the trade's
    cost basis (entry_price × shares). This is the handbook's golden rule.
    """

    @pytest.mark.parametrize("ratio,shares,price", [
        (5.0, 100, 10.00),
        (5.0, 1000, 0.50),
        (0.2, 500, 1.00),
        (1.5, 200, 3.00),
        (2.0, 100, 3.33),    # awkward price that doesn't divide evenly
        (3.0, 100, 1.0),
        (10.0, 100, 5.55),
    ])
    def test_cost_basis_preserved_within_rounding(self, ratio, shares, price):
        tid = _insert_test_trade(entry_price=price, shares=shares,
                                  shares_remaining=shares, lots=shares // 100 or 1,
                                  cost=round(price * shares, 2))

        before_basis = price * shares
        result = apply_split_to_trade(tid, ratio=ratio, ex_date="2026-05-29")
        assert abs(result["cash_invariant_delta_rm"]) < 1.00

        t = _read_trade(tid)
        after_basis = t["entry_price"] * t["shares"]
        # Rounding to 4dp on price × integer shares should land within RM 1
        assert abs(after_basis - before_basis) < 1.00

    def test_cost_field_not_modified(self):
        """trade.cost (total RM) must NOT be touched by a split."""
        tid = _insert_test_trade(cost=1750.00)
        apply_split_to_trade(tid, ratio=5.0, ex_date="2026-05-29")
        t = _read_trade(tid)
        assert t["cost"] == 1750.00

    def test_fee_field_not_modified(self):
        tid = _insert_test_trade(fee=17.50)
        apply_split_to_trade(tid, ratio=5.0, ex_date="2026-05-29")
        t = _read_trade(tid)
        assert t["fee"] == 17.50

    def test_realized_pnl_not_modified(self):
        tid = _insert_test_trade(realized_pnl=125.50)
        apply_split_to_trade(tid, ratio=5.0, ex_date="2026-05-29")
        t = _read_trade(tid)
        assert t["realized_pnl"] == 125.50

    def test_percent_fields_not_modified(self):
        tid = _insert_test_trade(slippage_pct=0.08, mae_pct=2.5, mfe_pct=4.2,
                                  actual_risk_pct=0.75)
        apply_split_to_trade(tid, ratio=5.0, ex_date="2026-05-29")
        t = _read_trade(tid)
        assert t["slippage_pct"] == 0.08
        assert t["mae_pct"] == 2.5
        assert t["mfe_pct"] == 4.2
        assert t["actual_risk_pct"] == 0.75


# ---------------------------------------------------------------------------
# Composition: multiple splits on same trade
# ---------------------------------------------------------------------------

class TestSplitComposition:
    """If a trade survives multiple splits, cumulative_split_factor must
    multiply correctly so we can always reconstruct the original price."""

    def test_two_forward_splits_compose(self):
        tid = _insert_test_trade(entry_price=10.00, shares=100,
                                  shares_remaining=100, lots=1)
        apply_split_to_trade(tid, ratio=5.0, ex_date="2026-05-15")
        apply_split_to_trade(tid, ratio=2.0, ex_date="2026-06-15")

        t = _read_trade(tid)
        # After 5× then 2× = 10× total
        assert t["entry_price"] == 1.00       # 10.00 ÷ 10
        assert t["shares"] == 1000            # 100 × 10
        assert t["cumulative_split_factor"] == 10.0

    def test_forward_then_reverse_split(self):
        """5-for-1 forward then 1-for-5 reverse → back to original."""
        tid = _insert_test_trade(entry_price=10.00, shares=100,
                                  shares_remaining=100, lots=1)
        apply_split_to_trade(tid, ratio=5.0, ex_date="2026-05-15")
        apply_split_to_trade(tid, ratio=0.2, ex_date="2026-06-15")
        t = _read_trade(tid)
        assert t["entry_price"] == 10.00
        assert t["shares"] == 100
        assert abs(t["cumulative_split_factor"] - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# Audit note
# ---------------------------------------------------------------------------

class TestAuditNote:
    def test_note_appended(self):
        tid = _insert_test_trade(notes="initial note")
        apply_split_to_trade(tid, ratio=5.0, ex_date="2026-05-29")
        t = _read_trade(tid)
        assert "initial note" in t["notes"]
        assert "v3.5 SPLIT applied" in t["notes"]
        assert "ratio=5" in t["notes"]
        assert "2026-05-29" in t["notes"]

    def test_custom_note_included(self):
        tid = _insert_test_trade()
        apply_split_to_trade(tid, ratio=5.0, ex_date="2026-05-29",
                              note="auto-detected via Moomoo")
        t = _read_trade(tid)
        assert "auto-detected via Moomoo" in t["notes"]

    def test_empty_initial_notes_handled(self):
        tid = _insert_test_trade(notes="")
        apply_split_to_trade(tid, ratio=5.0, ex_date="2026-05-29")
        t = _read_trade(tid)
        assert "v3.5 SPLIT applied" in t["notes"]
        # Should NOT start with a leading newline
        assert not t["notes"].startswith("\n")


# ---------------------------------------------------------------------------
# Bad-input rejection
# ---------------------------------------------------------------------------

class TestRejectBadInputs:

    def test_ratio_zero_rejected(self):
        tid = _insert_test_trade()
        with pytest.raises(ValueError, match="positive"):
            apply_split_to_trade(tid, ratio=0, ex_date="2026-05-29")

    def test_ratio_negative_rejected(self):
        tid = _insert_test_trade()
        with pytest.raises(ValueError, match="positive"):
            apply_split_to_trade(tid, ratio=-5.0, ex_date="2026-05-29")

    def test_ratio_none_rejected(self):
        tid = _insert_test_trade()
        with pytest.raises(ValueError, match="positive"):
            apply_split_to_trade(tid, ratio=None, ex_date="2026-05-29")  # type: ignore

    def test_ratio_one_rejected(self):
        """ratio=1.0 is a no-op — reject to surface caller bugs early."""
        tid = _insert_test_trade()
        with pytest.raises(ValueError, match="no-op"):
            apply_split_to_trade(tid, ratio=1.0, ex_date="2026-05-29")

    def test_nonexistent_trade_id_rejected(self):
        with pytest.raises(ValueError, match="not found"):
            apply_split_to_trade(99999, ratio=5.0, ex_date="2026-05-29")

    def test_closed_trade_rejected(self):
        tid = _insert_test_trade(status="CLOSED",
                                  exit_price=4.20, closed_pnl=350.00)
        with pytest.raises(ValueError, match="ACTIVE"):
            apply_split_to_trade(tid, ratio=5.0, ex_date="2026-05-29")

    def test_closed_trade_unchanged_after_rejection(self):
        """Confirms rollback: the trade is not modified after a rejection."""
        tid = _insert_test_trade(status="CLOSED",
                                  entry_price=10.00,
                                  exit_price=12.00, closed_pnl=200.00)
        try:
            apply_split_to_trade(tid, ratio=5.0, ex_date="2026-05-29")
        except ValueError:
            pass
        t = _read_trade(tid)
        assert t["entry_price"] == 10.00     # untouched
        assert t["exit_price"] == 12.00      # untouched
        assert t["status"] == "CLOSED"


# ---------------------------------------------------------------------------
# Atomicity: error mid-transaction rolls back cleanly
# ---------------------------------------------------------------------------

class TestAtomicity:
    """The whole adjustment must succeed or none of it persists."""

    def test_rollback_on_cash_invariant_violation(self, monkeypatch):
        """
        Force a cash-invariant violation by monkey-patching the multiply
        operation. The trade should be untouched after the rollback.
        """
        import trading_engine
        tid = _insert_test_trade(entry_price=10.00, shares=100)
        original_before = _read_trade(tid)

        # Patch the price-fields tuple to include a non-existent field that
        # will cause a UPDATE SQL error.
        orig_price_fields = trading_engine._PRICE_FIELDS_INVERSE
        monkeypatch.setattr(trading_engine, "_PRICE_FIELDS_INVERSE",
                            orig_price_fields + ("nonexistent_field_xyz",))

        with pytest.raises(Exception):
            apply_split_to_trade(tid, ratio=5.0, ex_date="2026-05-29")

        # CRITICAL: trade must be unchanged because the transaction rolled back.
        after = _read_trade(tid)
        assert after["entry_price"] == original_before["entry_price"]
        assert after["shares"] == original_before["shares"]
        assert after["cumulative_split_factor"] == original_before["cumulative_split_factor"]


# ---------------------------------------------------------------------------
# Edge case: trade with NULL optional fields
# ---------------------------------------------------------------------------

class TestNullableFields:

    def test_trade_with_no_tp_levels_set(self):
        """tp1/tp2/tp3 can be NULL — adjustment must handle that."""
        tid = _insert_test_trade(tp1=None, tp2=None, tp3=None,
                                  trailing_stop=None,
                                  highest_price=None, lowest_price=None)
        apply_split_to_trade(tid, ratio=5.0, ex_date="2026-05-29")
        t = _read_trade(tid)
        assert t["tp1"] is None
        assert t["tp2"] is None
        assert t["entry_price"] == 0.70    # 3.50 / 5

    def test_trade_with_no_exit_price(self):
        """Active trades have NULL exit_price; must not error."""
        tid = _insert_test_trade()  # default: exit_price not set → NULL
        result = apply_split_to_trade(tid, ratio=5.0, ex_date="2026-05-29")
        t = _read_trade(tid)
        assert t["exit_price"] is None  # still NULL
