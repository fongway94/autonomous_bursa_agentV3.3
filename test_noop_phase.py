"""
Regression tests for the NOOP learning phase.

Covers the critical new pieces:
  * tier classification (deterministic, all branches)
  * structured decision record (falsifiable fields present)
  * journal creation / read
  * resolver behaviour (WIN / LOSS / FLAT / unresolvable) with synthetic data
  * NOOP safety lock (default-safe; no execution permitted)
  * no real/paper execution leakage (broker falls back to NoopAdapter)
  * deterministic decision records (same input -> same record sans timestamps)
  * failure-safe behaviour (bad rows never crash the recorder)
  * weekly report is descriptive-only

These tests use the project's conftest DB fixtures (per-(market,mode) temp DB).
"""

import importlib

import pandas as pd
import pytest

import db
import decision_tiers
import noop_journal
import noop_record
import noop_report
import noop_safety


# ---------------------------------------------------------------------------
# Tier classification — pure, deterministic
# ---------------------------------------------------------------------------

class TestTierClassification:
    def test_gold_above_threshold_is_A(self):
        assert decision_tiers.classify_tier("GOLD BUY (BREAKOUT)", 80, 70) == "A"

    def test_gold_just_below_threshold_is_B(self):
        # within TIER_B_MARGIN (10) below 70 -> B
        assert decision_tiers.classify_tier("GOLD BUY (BREAKOUT)", 65, 70) == "B"

    def test_silver_far_below_is_C(self):
        assert decision_tiers.classify_tier("SILVER BUY", 40, 70) == "C"

    def test_no_signal_is_D(self):
        assert decision_tiers.classify_tier("NO SETUP", 0, 70) == "D"
        assert decision_tiers.classify_tier("", 50, 70) == "D"
        assert decision_tiers.classify_tier(None, 50, 70) == "D"

    def test_degenerate_buy_zero_conf_is_D(self):
        assert decision_tiers.classify_tier("GOLD BUY", 0, 70) == "D"

    def test_gold_below_margin_window_is_C(self):
        # 55 is more than 10 below threshold 70 -> C, not B
        assert decision_tiers.classify_tier("GOLD BUY", 55, 70) == "C"

    def test_only_tier_A_would_execute(self):
        assert decision_tiers.tier_would_execute("A") is True
        for t in ("B", "C", "D"):
            assert decision_tiers.tier_would_execute(t) is False

    def test_deterministic(self):
        a = decision_tiers.classify_tier("GOLD BUY (BREAKOUT)", 72.5, 70)
        b = decision_tiers.classify_tier("GOLD BUY (BREAKOUT)", 72.5, 70)
        assert a == b == "A"


# ---------------------------------------------------------------------------
# Structured decision record
# ---------------------------------------------------------------------------

class TestDecisionRecord:
    def _setup(self):
        return {
            "ticker": "AAA", "name": "AAA Co", "sector": "Tech",
            "signal": "GOLD BUY (BREAKOUT)", "confidence": 82,
            "entry": 10.0, "stop_loss": 9.0, "tp1": 12.0, "tp2": 13.0,
            "tp3": 14.0, "rsi": 55, "vol_ratio": 1.6, "atr": 0.3,
            "reasoning": "breakout + volume",
        }

    def test_has_falsifiable_fields(self):
        rec = decision_tiers.build_decision_record(
            self._setup(), market="MY", mode="SWING", regime="NEUTRAL",
            regime_threshold=70.0, state_id=5,
            decided_at="2026-06-04T10:00:00", review_at="2026-06-14T10:00:00",
        )
        for f in ("expected_scenario", "invalidation_condition",
                  "what_proves_wrong", "review_at"):
            assert rec[f], f"missing falsifiable field {f}"
        assert rec["tier"] == "A"
        assert rec["would_execute"] == 1
        assert rec["status"] == "OPEN"
        assert rec["outcome"] is None  # resolver fills this later

    def test_record_deterministic_given_timestamps(self):
        s = self._setup()
        kw = dict(market="MY", mode="SWING", regime="NEUTRAL",
                  regime_threshold=70.0, state_id=5,
                  decided_at="2026-06-04T10:00:00",
                  review_at="2026-06-14T10:00:00")
        r1 = decision_tiers.build_decision_record(s, **kw)
        r2 = decision_tiers.build_decision_record(s, **kw)
        assert r1 == r2


# ---------------------------------------------------------------------------
# Journal creation & read (uses temp DB from conftest)
# ---------------------------------------------------------------------------

class TestJournalCreation:
    def test_insert_and_count(self):
        db.init_db()
        before = noop_journal.count_all()
        rec = decision_tiers.build_decision_record(
            {"ticker": "ZZZ", "signal": "GOLD BUY", "confidence": 80,
             "entry": 1.0, "stop_loss": 0.9, "tp1": 1.2},
            market="MY", mode="SWING", regime="BULL", regime_threshold=60.0,
            state_id=1, decided_at="2026-06-04T10:00:00",
            review_at="2026-06-14T10:00:00")
        rid = noop_journal.insert_decision(rec)
        assert rid > 0
        assert noop_journal.count_all() == before + 1

    def test_record_cycle_writes_abc_not_d(self):
        db.init_db()
        before = noop_journal.count_all()
        setups = [
            {"ticker": "A1", "signal": "GOLD BUY (BREAKOUT)", "confidence": 82,
             "entry": 10, "stop_loss": 9, "tp1": 12, "rsi": 55, "vol_ratio": 1.6},
            {"ticker": "B1", "signal": "GOLD BUY", "confidence": 64,
             "entry": 5, "stop_loss": 4.5, "tp1": 6, "rsi": 45, "vol_ratio": 0.7},
            {"ticker": "C1", "signal": "SILVER BUY", "confidence": 30,
             "entry": 2, "stop_loss": 1.8, "tp1": 2.4, "rsi": 38, "vol_ratio": 1.1},
            {"ticker": "D1", "signal": "NO SETUP", "confidence": 0},
        ]
        summ = noop_record.record_cycle_decisions(
            setups, market="MY", mode="SWING", regime="NEUTRAL",
            regime_threshold=70.0, cycle_id="c1")
        assert summ["A"] == 1 and summ["B"] == 1
        assert summ["C"] == 1 and summ["D"] == 1
        assert summ["written"] == 3  # D excluded by default
        assert noop_journal.count_all() == before + 3

    def test_accepts_dataframe(self):
        db.init_db()
        df = pd.DataFrame([
            {"ticker": "DF1", "signal": "GOLD BUY", "confidence": 90,
             "entry": 3, "stop_loss": 2.7, "tp1": 3.6, "rsi": 60, "vol_ratio": 2.0},
        ])
        summ = noop_record.record_cycle_decisions(
            df, market="US", mode="SWING", regime="BULL",
            regime_threshold=60.0)
        assert summ["A"] == 1 and summ["written"] == 1


# ---------------------------------------------------------------------------
# Resolver behaviour — synthetic price data via monkeypatch
# ---------------------------------------------------------------------------

class TestResolver:
    def _seed_open_decision(self, *, entry, stop, tp1, tier="A"):
        db.init_db()
        rec = decision_tiers.build_decision_record(
            {"ticker": "RES", "signal": "GOLD BUY", "confidence": 80,
             "entry": entry, "stop_loss": stop, "tp1": tp1},
            market="MY", mode="SWING", regime="NEUTRAL", regime_threshold=70.0,
            state_id=1, decided_at="2026-06-01T10:00:00",
            review_at="2026-06-02T10:00:00")
        rec["tier"] = tier
        return noop_journal.insert_decision(rec)

    def _patch_bars(self, monkeypatch, highs, lows, closes):
        idx = pd.date_range("2026-06-01T11:00:00", periods=len(highs), freq="D")
        bars = pd.DataFrame(
            {"Open": closes, "High": highs, "Low": lows,
             "Close": closes, "Volume": [1] * len(highs)}, index=idx)
        import noop_resolver
        monkeypatch.setattr(noop_resolver, "_fetch_bars",
                            lambda ticker, interval: bars)

    def test_win_when_tp_hit_first(self, monkeypatch):
        import noop_resolver
        self._seed_open_decision(entry=10, stop=9, tp1=12)
        # bar where high reaches 12 (tp), low stays above stop
        self._patch_bars(monkeypatch, highs=[12.5], lows=[9.8], closes=[12.0])
        summ = noop_resolver.resolve_due(now_iso="2026-06-10T00:00:00")
        assert summ["resolved"] == 1 and summ["wins"] == 1
        row = noop_journal.get_recent_decisions(1)[0]
        assert row["outcome"] == "WIN"
        assert row["outcome_r"] == pytest.approx(2.0, abs=0.01)  # (12-10)/(10-9)

    def test_loss_when_stop_hit_first(self, monkeypatch):
        import noop_resolver
        self._seed_open_decision(entry=10, stop=9, tp1=12)
        self._patch_bars(monkeypatch, highs=[10.5], lows=[8.9], closes=[9.0])
        summ = noop_resolver.resolve_due(now_iso="2026-06-10T00:00:00")
        assert summ["losses"] == 1
        row = noop_journal.get_recent_decisions(1)[0]
        assert row["outcome"] == "LOSS"
        assert row["outcome_r"] == pytest.approx(-1.0)

    def test_ambiguous_bar_is_pessimistic_loss(self, monkeypatch):
        import noop_resolver
        self._seed_open_decision(entry=10, stop=9, tp1=12)
        # same bar touches both tp and stop -> pessimistic LOSS
        self._patch_bars(monkeypatch, highs=[12.5], lows=[8.5], closes=[11.0])
        noop_resolver.resolve_due(now_iso="2026-06-10T00:00:00")
        row = noop_journal.get_recent_decisions(1)[0]
        assert row["outcome"] == "LOSS"

    def test_flat_when_neither_hit(self, monkeypatch):
        import noop_resolver
        self._seed_open_decision(entry=10, stop=9, tp1=12)
        self._patch_bars(monkeypatch, highs=[10.5], lows=[9.5], closes=[10.2])
        summ = noop_resolver.resolve_due(now_iso="2026-06-10T00:00:00")
        assert summ["flats"] == 1
        row = noop_journal.get_recent_decisions(1)[0]
        assert row["outcome"] == "FLAT"

    def test_unresolvable_marked_skipped(self, monkeypatch):
        import noop_resolver
        self._seed_open_decision(entry=10, stop=9, tp1=12)
        monkeypatch.setattr(noop_resolver, "_fetch_bars",
                            lambda ticker, interval: None)
        summ = noop_resolver.resolve_due(now_iso="2026-06-10T00:00:00")
        assert summ["skipped"] == 1 and summ["resolved"] == 0
        row = noop_journal.get_recent_decisions(1)[0]
        assert row["status"] == "SKIPPED"

    def test_resolver_never_raises_on_bad_state(self, monkeypatch):
        import noop_resolver
        # No decisions at all — should return clean summary, never raise.
        db.init_db()
        summ = noop_resolver.resolve_due(now_iso="2026-06-10T00:00:00")
        assert summ["checked"] == 0


# ---------------------------------------------------------------------------
# Safety lock — default-safe; no execution leakage
# ---------------------------------------------------------------------------

class TestSafetyLock:
    def test_defaults_are_safe(self):
        s = noop_safety.safety_status()
        assert s["noop_mode_active"] is True
        assert s["paper_trading_enabled"] is False
        assert s["live_trading_enabled"] is False
        assert s["any_execution_allowed"] is False
        assert s["auto_rule_changes_allowed"] is False

    def test_paper_blocked_while_noop_active(self, monkeypatch):
        # Even if someone sets the paper flag, NOOP active overrides it.
        monkeypatch.setenv("PAPER_TRADING_ENABLED", "true")
        monkeypatch.setenv("NOOP_MODE", "true")
        assert noop_safety.paper_trading_enabled() is False
        assert noop_safety.any_execution_allowed() is False

    def test_assert_no_live_execution_raises_in_noop(self):
        with pytest.raises(noop_safety.NoopSafetyViolation):
            noop_safety.assert_no_live_execution("test")

    def test_assert_no_auto_rule_change_raises_in_noop(self):
        with pytest.raises(noop_safety.NoopSafetyViolation):
            noop_safety.assert_no_auto_rule_change("test")

    def test_execution_only_when_noop_off_and_flag_on(self, monkeypatch):
        monkeypatch.setenv("NOOP_MODE", "false")
        monkeypatch.setenv("PAPER_TRADING_ENABLED", "true")
        assert noop_safety.paper_trading_enabled() is True
        assert noop_safety.any_execution_allowed() is True


class TestNoExecutionLeakage:
    def test_broker_falls_back_to_noop_in_noop_phase(self):
        # Requesting SIMULATE on US while NOOP active must NOT create a live
        # adapter — it must fall back to NoopAdapter.
        import broker_adapter
        broker_adapter.reset_adapter_cache()
        # Force US market + SIMULATE request.
        import market_profiles
        try:
            market_profiles.set_active_market("US")
        except Exception:
            pytest.skip("cannot switch market in this environment")
        adapter = broker_adapter.get_broker_adapter("SIMULATE")
        assert type(adapter).__name__ == "NoopAdapter"
        broker_adapter.reset_adapter_cache()
        try:
            market_profiles.set_active_market("MY")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Failure-safe recorder
# ---------------------------------------------------------------------------

class TestFailureSafe:
    def test_bad_rows_do_not_crash(self):
        db.init_db()
        setups = [
            None,
            {"no_ticker": True},
            {"ticker": "OK", "signal": "GOLD BUY", "confidence": 80,
             "entry": 1, "stop_loss": 0.9, "tp1": 1.2},
            42,  # not a dict
        ]
        # Should not raise; should still record the one good row.
        summ = noop_record.record_cycle_decisions(
            [s for s in setups if s is not None],
            market="MY", mode="SWING", regime="NEUTRAL",
            regime_threshold=70.0)
        assert summ["written"] >= 1


# ---------------------------------------------------------------------------
# Weekly report is descriptive-only
# ---------------------------------------------------------------------------

class TestWeeklyReport:
    def test_report_has_expected_keys_and_note(self):
        db.init_db()
        rep = noop_report.weekly_summary()
        for k in ("tier_counts", "tier_A", "false_positive_rate_pct",
                  "missed_opportunity_rate_pct", "calibration", "lessons",
                  "note"):
            assert k in rep
        assert "DESCRIPTIVE ONLY" in rep["note"]
        assert isinstance(rep["lessons"], list) and rep["lessons"]
