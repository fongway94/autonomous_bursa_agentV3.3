# tests/test_corporate_actions_phase2.py
"""
Phase-2 tests for corporate_actions.py: detection layer.

Covers:
  - _to_moomoo_code() ticker conversion
  - Moomoo path success / failure / timeout / unavailable
  - Sticky demote after N consecutive failures
  - yfinance path success / empty / exception
  - detect_for_ticker auto-fallback (Moomoo None → yfinance)
  - detect_for_ticker prefers Moomoo when both work
  - detect_for_tickers per-ticker isolation (one fail doesn't break batch)
  - detection_health() / reset_detection_state()
  - Real-world row shapes from both providers parsed correctly
"""

from __future__ import annotations

import sys
import types
from datetime import date, datetime, timedelta
from unittest import mock

import pandas as pd
import pytest

import corporate_actions as ca


# ---------------------------------------------------------------------------
# Auto-reset detection state before each test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_corp_actions_state():
    ca.reset_detection_state()
    yield
    ca.reset_detection_state()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _fake_moomoo_rehab_df(rows: list[dict]) -> pd.DataFrame:
    """Build a request_rehab response DataFrame from a list of row dicts."""
    default = {
        "ex_div_date": None,
        "split_ratio": 1.0,
        "bonus_div_ratio": 0.0,
        "per_cash_div": 0.0,
        "forward_adj_factorA": 1.0,
        "forward_adj_factorB": 1.0,
    }
    return pd.DataFrame([{**default, **r} for r in rows])


class _FakeQuoteCtx:
    """Stand-in for data_provider._quote_ctx during tests."""
    def __init__(self, *, rehab_ret=0, rehab_df=None, rehab_raises=None,
                 rehab_hangs=False):
        self._ret = rehab_ret
        self._df = rehab_df if rehab_df is not None else pd.DataFrame()
        self._raises = rehab_raises
        self._hangs = rehab_hangs

    def request_rehab(self, code):
        if self._hangs:
            import time
            time.sleep(60)  # longer than any test timeout
        if self._raises is not None:
            raise self._raises
        return self._ret, self._df


def _install_fake_data_provider(monkeypatch, *,
                                moomoo_available=True,
                                ctx=None,
                                init_error=None):
    """Patch data_provider state so corporate_actions thinks Moomoo is up."""
    import data_provider as dp
    dp.reset()

    monkeypatch.setattr(dp, "_moomoo_available", moomoo_available, raising=False)
    monkeypatch.setattr(dp, "_quote_ctx", ctx, raising=False)
    monkeypatch.setattr(dp, "_init_error", init_error, raising=False)
    # Make ensure_probed a no-op so we don't re-probe and undo our patches.
    monkeypatch.setattr(dp, "ensure_probed", lambda: None, raising=False)


# ---------------------------------------------------------------------------
# Ticker conversion
# ---------------------------------------------------------------------------

class TestTickerConversion:
    def test_bursa_ticker(self):
        assert ca._to_moomoo_code("0166.KL") == "MY.0166"

    def test_lowercase(self):
        assert ca._to_moomoo_code("0166.kl") == "MY.0166"

    def test_non_bursa_returns_none(self):
        assert ca._to_moomoo_code("AAPL") is None
        assert ca._to_moomoo_code("^KLSE") is None  # index — skip Moomoo

    def test_empty_returns_none(self):
        assert ca._to_moomoo_code("") is None
        assert ca._to_moomoo_code(None) is None


# ---------------------------------------------------------------------------
# Moomoo path: success cases
# ---------------------------------------------------------------------------

class TestMoomooDetectionSuccess:

    def test_forward_split_detected(self, monkeypatch):
        df = _fake_moomoo_rehab_df([{
            "ex_div_date": "2026-05-20", "split_ratio": 5.0,
        }])
        _install_fake_data_provider(monkeypatch, ctx=_FakeQuoteCtx(rehab_df=df))

        events = ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29))
        assert len(events) == 1
        e = events[0]
        assert e.event_type == "SPLIT"
        assert e.ratio == 5.0
        assert e.source == "moomoo"
        assert e.ex_date == "2026-05-20"

    def test_reverse_split_detected(self, monkeypatch):
        df = _fake_moomoo_rehab_df([{
            "ex_div_date": "2026-05-15", "split_ratio": 0.2,
        }])
        _install_fake_data_provider(monkeypatch, ctx=_FakeQuoteCtx(rehab_df=df))

        events = ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29))
        assert len(events) == 1
        assert events[0].event_type == "SPLIT"
        assert events[0].ratio == 0.2

    def test_bonus_issue_converted_correctly(self, monkeypatch):
        # bonus_div_ratio=0.5 → 1 free for every 2 → effective ratio 1.5
        df = _fake_moomoo_rehab_df([{
            "ex_div_date": "2026-05-10", "bonus_div_ratio": 0.5,
        }])
        _install_fake_data_provider(monkeypatch, ctx=_FakeQuoteCtx(rehab_df=df))

        events = ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29))
        assert len(events) == 1
        assert events[0].event_type == "BONUS"
        assert events[0].ratio == 1.5

    def test_cash_dividend_detected(self, monkeypatch):
        df = _fake_moomoo_rehab_df([{
            "ex_div_date": "2026-05-05", "per_cash_div": 0.20,
        }])
        _install_fake_data_provider(monkeypatch, ctx=_FakeQuoteCtx(rehab_df=df))

        events = ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29))
        assert len(events) == 1
        assert events[0].event_type == "DIVIDEND"
        assert events[0].amount_per_share == 0.20

    def test_combined_split_and_dividend_same_date(self, monkeypatch):
        """One row with both a split AND a cash dividend → two events."""
        df = _fake_moomoo_rehab_df([{
            "ex_div_date": "2026-05-15",
            "split_ratio": 2.0,
            "per_cash_div": 0.10,
        }])
        _install_fake_data_provider(monkeypatch, ctx=_FakeQuoteCtx(rehab_df=df))

        events = ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29))
        assert len(events) == 2
        kinds = {e.event_type for e in events}
        assert kinds == {"SPLIT", "DIVIDEND"}

    def test_events_outside_window_filtered_out(self, monkeypatch):
        df = _fake_moomoo_rehab_df([
            {"ex_div_date": "2025-12-01", "split_ratio": 5.0},  # before window
            {"ex_div_date": "2026-05-15", "split_ratio": 2.0},  # in window
            {"ex_div_date": "2026-12-01", "split_ratio": 3.0},  # after window
        ])
        _install_fake_data_provider(monkeypatch, ctx=_FakeQuoteCtx(rehab_df=df))

        events = ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29))
        assert len(events) == 1
        assert events[0].ratio == 2.0

    def test_empty_response_returns_empty_list_not_none(self, monkeypatch):
        """Moomoo success with no events → [], not None (don't fall back)."""
        df = _fake_moomoo_rehab_df([])
        _install_fake_data_provider(monkeypatch, ctx=_FakeQuoteCtx(rehab_df=df))

        events = ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29))
        assert events == []  # NOT None

    def test_no_split_just_factor_skipped(self, monkeypatch):
        """A row where everything is 1.0 / 0 (no events) is skipped silently."""
        df = _fake_moomoo_rehab_df([{
            "ex_div_date": "2026-05-15",
            "split_ratio": 1.0,
            "bonus_div_ratio": 0.0,
            "per_cash_div": 0.0,
        }])
        _install_fake_data_provider(monkeypatch, ctx=_FakeQuoteCtx(rehab_df=df))
        events = ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29))
        assert events == []


# ---------------------------------------------------------------------------
# Moomoo path: failure cases
# ---------------------------------------------------------------------------

class TestMoomooDetectionFailure:

    def test_non_bursa_ticker_returns_none(self, monkeypatch):
        _install_fake_data_provider(monkeypatch, ctx=_FakeQuoteCtx())
        assert ca._detect_moomoo("AAPL", date(2026, 5, 1), date(2026, 5, 29)) is None

    def test_moomoo_unavailable_returns_none(self, monkeypatch):
        _install_fake_data_provider(monkeypatch, moomoo_available=False, ctx=None,
                                    init_error="port not open")
        assert ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29)) is None

    def test_no_quote_ctx_returns_none(self, monkeypatch):
        _install_fake_data_provider(monkeypatch, moomoo_available=True, ctx=None)
        assert ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29)) is None

    def test_request_rehab_exception_returns_none(self, monkeypatch):
        ctx = _FakeQuoteCtx(rehab_raises=RuntimeError("boom"))
        _install_fake_data_provider(monkeypatch, ctx=ctx)
        assert ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29)) is None

    def test_request_rehab_nonzero_ret_returns_none(self, monkeypatch):
        ctx = _FakeQuoteCtx(rehab_ret=-1, rehab_df="error message")
        _install_fake_data_provider(monkeypatch, ctx=ctx)
        assert ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29)) is None

    def test_request_rehab_timeout_returns_none(self, monkeypatch):
        """The thread-join-with-timeout path: SDK hangs → we abandon it."""
        ctx = _FakeQuoteCtx(rehab_hangs=True)
        _install_fake_data_provider(monkeypatch, ctx=ctx)
        result = ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29),
                                   timeout=1.0)
        assert result is None

    def test_malformed_row_skipped_not_crashed(self, monkeypatch):
        """One garbage row shouldn't prevent processing the others."""
        df = pd.DataFrame([
            {"ex_div_date": "bad-date", "split_ratio": "not-a-number"},
            {"ex_div_date": "2026-05-15", "split_ratio": 2.0,
             "bonus_div_ratio": 0.0, "per_cash_div": 0.0},
        ])
        _install_fake_data_provider(monkeypatch, ctx=_FakeQuoteCtx(rehab_df=df))
        events = ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29))
        # Bad row silently skipped, good row returned
        assert len(events) == 1
        assert events[0].ratio == 2.0


# ---------------------------------------------------------------------------
# Sticky demote
# ---------------------------------------------------------------------------

class TestStickyDemote:

    def test_demote_after_max_failures(self, monkeypatch):
        ctx = _FakeQuoteCtx(rehab_raises=RuntimeError("persistent"))
        _install_fake_data_provider(monkeypatch, ctx=ctx)

        # Trigger N failures
        for _ in range(ca.MAX_CONSECUTIVE_MOOMOO_FAILURES):
            ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29))

        # Moomoo is now demoted — even with a fresh successful ctx, it won't be used
        ctx_good = _FakeQuoteCtx(rehab_df=_fake_moomoo_rehab_df([
            {"ex_div_date": "2026-05-15", "split_ratio": 5.0}
        ]))
        # Don't reset _moomoo_state — but _install_fake_data_provider patches dp
        # directly. We need to verify _moomoo_state was demoted.
        health = ca.detection_health()
        assert health["moomoo_available"] is False
        assert "demoted" in (health["moomoo_init_error"] or "")

    def test_success_resets_failure_counter(self, monkeypatch):
        # Start failing
        ctx_bad = _FakeQuoteCtx(rehab_raises=RuntimeError("boom"))
        _install_fake_data_provider(monkeypatch, ctx=ctx_bad)
        ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29))
        ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29))
        assert ca.detection_health()["moomoo_consecutive_failures"] == 2

        # Swap to a healthy ctx
        ctx_good = _FakeQuoteCtx(rehab_df=_fake_moomoo_rehab_df([
            {"ex_div_date": "2026-05-15", "split_ratio": 5.0}
        ]))
        _install_fake_data_provider(monkeypatch, ctx=ctx_good)

        events = ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29))
        assert len(events) == 1
        assert ca.detection_health()["moomoo_consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# yfinance fallback path
# ---------------------------------------------------------------------------

def _fake_yf_history_with_events(splits: list[tuple[str, float]] = None,
                                 dividends: list[tuple[str, float]] = None,
                                 rows: int = 60) -> pd.DataFrame:
    splits = splits or []
    dividends = dividends or []
    idx = pd.date_range(end=datetime.now().date(), periods=rows, freq="B")
    df = pd.DataFrame({
        "Open":   [1.0] * rows,
        "High":   [1.1] * rows,
        "Low":    [0.9] * rows,
        "Close":  [1.05] * rows,
        "Volume": [100000] * rows,
        "Stock Splits": [0.0] * rows,
        "Dividends": [0.0] * rows,
    }, index=idx)
    for ds, ratio in splits:
        try:
            df.loc[ds, "Stock Splits"] = ratio
        except KeyError:
            pass
    for dd, amt in dividends:
        try:
            df.loc[dd, "Dividends"] = amt
        except KeyError:
            pass
    return df


class TestYfinanceDetection:

    def test_split_detected(self):
        today = datetime.now().date()
        split_date = (today - timedelta(days=5)).strftime("%Y-%m-%d")
        df = _fake_yf_history_with_events(splits=[(split_date, 5.0)])
        with mock.patch("yfinance.Ticker") as MockT:
            MockT.return_value.history.return_value = df
            events = ca._detect_yfinance(
                "0166.KL", today - timedelta(days=30), today)
        assert any(e.event_type == "SPLIT" and e.ratio == 5.0 for e in events)
        for e in events:
            assert e.source == "yfinance"

    def test_dividend_detected(self):
        today = datetime.now().date()
        div_date = (today - timedelta(days=3)).strftime("%Y-%m-%d")
        df = _fake_yf_history_with_events(dividends=[(div_date, 0.15)])
        with mock.patch("yfinance.Ticker") as MockT:
            MockT.return_value.history.return_value = df
            events = ca._detect_yfinance(
                "5347.KL", today - timedelta(days=30), today)
        divs = [e for e in events if e.event_type == "DIVIDEND"]
        assert len(divs) == 1
        assert divs[0].amount_per_share == pytest.approx(0.15)

    def test_window_filtering(self):
        today = datetime.now().date()
        old = (today - timedelta(days=100)).strftime("%Y-%m-%d")
        recent = (today - timedelta(days=5)).strftime("%Y-%m-%d")
        df = _fake_yf_history_with_events(
            splits=[(old, 2.0), (recent, 5.0)], rows=200)
        with mock.patch("yfinance.Ticker") as MockT:
            MockT.return_value.history.return_value = df
            events = ca._detect_yfinance(
                "0166.KL", today - timedelta(days=30), today)
        # Old event filtered out, recent event kept
        ratios = [e.ratio for e in events if e.event_type == "SPLIT"]
        assert 5.0 in ratios
        assert 2.0 not in ratios

    def test_empty_history_returns_empty_list(self):
        with mock.patch("yfinance.Ticker") as MockT:
            MockT.return_value.history.return_value = pd.DataFrame()
            events = ca._detect_yfinance(
                "0166.KL", date(2026, 5, 1), date(2026, 5, 29))
        assert events == []

    def test_yfinance_exception_returns_empty_list(self):
        with mock.patch("yfinance.Ticker") as MockT:
            MockT.return_value.history.side_effect = RuntimeError("yf down")
            events = ca._detect_yfinance(
                "0166.KL", date(2026, 5, 1), date(2026, 5, 29))
        assert events == []


# ---------------------------------------------------------------------------
# Public API: detect_for_ticker auto-fallback
# ---------------------------------------------------------------------------

class TestDetectForTicker:

    def test_moomoo_success_takes_precedence(self, monkeypatch):
        df = _fake_moomoo_rehab_df([
            {"ex_div_date": "2026-05-15", "split_ratio": 5.0}
        ])
        _install_fake_data_provider(monkeypatch, ctx=_FakeQuoteCtx(rehab_df=df))

        # yfinance should NOT be called when Moomoo succeeds
        with mock.patch("yfinance.Ticker") as MockT:
            events = ca.detect_for_ticker(
                "0166.KL", date(2026, 5, 1), date(2026, 5, 29))
            MockT.assert_not_called()

        assert len(events) == 1
        assert events[0].source == "moomoo"

    def test_moomoo_failure_falls_back_to_yfinance(self, monkeypatch):
        ctx = _FakeQuoteCtx(rehab_raises=RuntimeError("boom"))
        _install_fake_data_provider(monkeypatch, ctx=ctx)

        today = datetime.now().date()
        split_date = (today - timedelta(days=5)).strftime("%Y-%m-%d")
        yf_df = _fake_yf_history_with_events(splits=[(split_date, 3.0)])

        with mock.patch("yfinance.Ticker") as MockT:
            MockT.return_value.history.return_value = yf_df
            events = ca.detect_for_ticker(
                "0166.KL", today - timedelta(days=30), today)
            MockT.assert_called_once()

        assert len(events) == 1
        assert events[0].source == "yfinance"
        assert events[0].ratio == 3.0

    def test_moomoo_empty_success_does_not_fallback(self, monkeypatch):
        """Moomoo confirming 'no events' must be trusted — don't double-check."""
        _install_fake_data_provider(monkeypatch,
                                    ctx=_FakeQuoteCtx(rehab_df=_fake_moomoo_rehab_df([])))
        with mock.patch("yfinance.Ticker") as MockT:
            events = ca.detect_for_ticker(
                "0166.KL", date(2026, 5, 1), date(2026, 5, 29))
            MockT.assert_not_called()
        assert events == []

    def test_non_bursa_ticker_uses_yfinance(self, monkeypatch):
        _install_fake_data_provider(monkeypatch, ctx=_FakeQuoteCtx(rehab_df=_fake_moomoo_rehab_df([])))
        with mock.patch("yfinance.Ticker") as MockT:
            MockT.return_value.history.return_value = pd.DataFrame()
            events = ca.detect_for_ticker(
                "AAPL", date(2026, 5, 1), date(2026, 5, 29))
            MockT.assert_called_once()
        assert events == []


# ---------------------------------------------------------------------------
# detect_for_tickers batch behaviour
# ---------------------------------------------------------------------------

class TestDetectForTickersBatch:

    def test_per_ticker_isolation(self, monkeypatch):
        """One ticker's exception must not break the rest of the batch."""
        call_count = {"n": 0}

        def fake_detect(ticker, start, end, timeout=15.0):
            call_count["n"] += 1
            if ticker == "BAD.KL":
                raise RuntimeError("simulated detector failure")
            return [CorporateAction(
                ticker=ticker, ex_date="2026-05-15",
                event_type="SPLIT", ratio=2.0, source="yfinance",
            )]

        monkeypatch.setattr(ca, "detect_for_ticker", fake_detect)

        events = ca.detect_for_tickers(
            ["0166.KL", "BAD.KL", "1155.KL"],
            date(2026, 5, 1), date(2026, 5, 29))

        # Got events from the 2 good tickers, BAD.KL silently dropped
        assert len(events) == 2
        assert {e.ticker for e in events} == {"0166.KL", "1155.KL"}
        # All 3 were attempted
        assert call_count["n"] == 3


# ---------------------------------------------------------------------------
# Need to import CorporateAction for the batch test above
# ---------------------------------------------------------------------------

from corporate_actions import CorporateAction


# ---------------------------------------------------------------------------
# Diagnostics helpers
# ---------------------------------------------------------------------------

class TestDiagnostics:

    def test_detection_health_has_expected_keys(self):
        h = ca.detection_health()
        assert set(h.keys()) >= {
            "moomoo_available",
            "moomoo_consecutive_failures",
            "moomoo_init_error",
            "max_consecutive_failures_before_demote",
        }

    def test_reset_clears_state(self, monkeypatch):
        ctx = _FakeQuoteCtx(rehab_raises=RuntimeError("boom"))
        _install_fake_data_provider(monkeypatch, ctx=ctx)
        ca._detect_moomoo("0166.KL", date(2026, 5, 1), date(2026, 5, 29))
        assert ca.detection_health()["moomoo_consecutive_failures"] == 1

        ca.reset_detection_state()
        h = ca.detection_health()
        assert h["moomoo_available"] is None
        assert h["moomoo_consecutive_failures"] == 0
