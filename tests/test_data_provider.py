# tests/test_data_provider.py
"""
Tests for data_provider.py.

These tests do NOT require a live Moomoo OpenD or network access — both
yfinance and moomoo are fully mocked. They exercise:

  - Ticker conversion (0166.KL ↔ MY.0166, ^KLSE → MY.800000, unknown → None)
  - Provider auto-detect (moomoo missing → yfinance)
  - Provider env override (BURSA_DATA_PROVIDER=yfinance)
  - Moomoo success path returns yfinance-shaped DataFrame
  - Per-call fallback when Moomoo raises
  - Sticky demotion after N consecutive Moomoo failures
  - Window resolution for period= and start/end=
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta
from unittest import mock

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Fixtures: stub out yfinance and moomoo so the module can be imported and
# exercised hermetically.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Default: 'auto' provider, no host overrides."""
    monkeypatch.delenv("BURSA_DATA_PROVIDER", raising=False)
    monkeypatch.delenv("MOOMOO_HOST", raising=False)
    monkeypatch.delenv("MOOMOO_PORT", raising=False)


def _fake_yf_df(rows=60):
    idx = pd.date_range(end=datetime.utcnow().date(), periods=rows, freq="B",
                        tz="Asia/Kuala_Lumpur", name="Date")
    return pd.DataFrame({
        "Open":   [1.0 + i * 0.01 for i in range(rows)],
        "High":   [1.1 + i * 0.01 for i in range(rows)],
        "Low":    [0.9 + i * 0.01 for i in range(rows)],
        "Close":  [1.05 + i * 0.01 for i in range(rows)],
        "Volume": [100000 + i * 100 for i in range(rows)],
    }, index=idx)


def _fake_moomoo_kline_df(rows=60):
    """Mimics what request_history_kline returns: lowercase columns + time_key."""
    dates = pd.date_range(end=datetime.utcnow().date(), periods=rows, freq="B")
    return pd.DataFrame({
        "code":     ["MY.0166"] * rows,
        "time_key": [d.strftime("%Y-%m-%d %H:%M:%S") for d in dates],
        "open":     [1.0 + i * 0.01 for i in range(rows)],
        "close":    [1.05 + i * 0.01 for i in range(rows)],
        "high":     [1.1 + i * 0.01 for i in range(rows)],
        "low":      [0.9 + i * 0.01 for i in range(rows)],
        "volume":   [100000 + i * 100 for i in range(rows)],
        "turnover": [120000 + i * 100 for i in range(rows)],
    })


def _install_fake_moomoo(monkeypatch, *,
                         connect_ok=True,
                         global_state_ret=0,
                         kline_ret=0,
                         kline_df=None,
                         kline_raises=None):
    """Install a fake `moomoo` module into sys.modules."""

    fake = types.ModuleType("moomoo")

    class _KLType:
        K_DAY = "K_DAY"

    class _AuType:
        QFQ = "QFQ"

    fake.KLType = _KLType
    fake.AuType = _AuType
    fake.RET_OK = 0

    class _FakeCtx:
        def __init__(self, host=None, port=None):
            if not connect_ok:
                raise ConnectionError("OpenD not reachable")
            self.host = host
            self.port = port
            self.closed = False

        def get_global_state(self):
            return global_state_ret, {"market_my": "MARKET_OPEN"}

        def request_history_kline(self, code, start=None, end=None,
                                  ktype=None, autype=None, max_count=1000,
                                  page_req_key=None, **kwargs):
            if kline_raises is not None:
                raise kline_raises
            df = kline_df if kline_df is not None else _fake_moomoo_kline_df()
            return kline_ret, df, None  # ret, df, next page_req_key

        def close(self):
            self.closed = True

    fake.OpenQuoteContext = _FakeCtx
    monkeypatch.setitem(sys.modules, "moomoo", fake)
    return fake


def _uninstall_moomoo(monkeypatch):
    """Make `import moomoo` fail (simulates a yfinance-only deployment)."""
    # Remove if present
    monkeypatch.delitem(sys.modules, "moomoo", raising=False)

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name == "moomoo" or name.startswith("moomoo."):
            raise ImportError("No module named 'moomoo'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)


@pytest.fixture
def dp(monkeypatch):
    """Fresh data_provider import per test (clears module state)."""
    # Force a fresh import so module-level state is clean.
    sys.modules.pop("data_provider", None)
    import data_provider as _dp
    _dp.reset()
    yield _dp
    _dp.reset()


# ---------------------------------------------------------------------------
# Ticker conversion
# ---------------------------------------------------------------------------

class TestTickerConversion:
    def test_bursa_equity(self, dp):
        assert dp._to_moomoo_code("0166.KL") == "MY.0166"
        assert dp._to_moomoo_code("1155.KL") == "MY.1155"

    def test_lowercase_input(self, dp):
        assert dp._to_moomoo_code("0166.kl") == "MY.0166"

    def test_klci_index(self, dp):
        assert dp._to_moomoo_code("^KLSE") == "MY.800000"

    def test_unknown_returns_none(self, dp):
        assert dp._to_moomoo_code("AAPL") is None
        assert dp._to_moomoo_code("") is None
        assert dp._to_moomoo_code(None) is None


# ---------------------------------------------------------------------------
# Window resolution
# ---------------------------------------------------------------------------

class TestWindowResolution:
    def test_period_1y(self, dp):
        s, e = dp._resolve_window("1y", None, None)
        s_d, e_d = pd.to_datetime(s).date(), pd.to_datetime(e).date()
        assert (e_d - s_d).days >= 360

    def test_explicit_start_end(self, dp):
        s, e = dp._resolve_window(None, "2024-01-01", "2024-06-30")
        assert s == "2024-01-01"
        assert e == "2024-06-30"

    def test_ytd(self, dp):
        s, e = dp._resolve_window("ytd", None, None)
        assert pd.to_datetime(s).month == 1
        assert pd.to_datetime(s).day == 1


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

class TestProviderDetection:
    def test_env_force_yfinance(self, monkeypatch):
        monkeypatch.setenv("BURSA_DATA_PROVIDER", "yfinance")
        sys.modules.pop("data_provider", None)
        import data_provider as dp
        dp.reset()
        # Even if moomoo is installed, env override wins.
        _install_fake_moomoo(monkeypatch, connect_ok=True)
        dp._ensure_provider_decided()
        assert dp._moomoo_available is False
        assert "forced" in (dp._init_error or "")

    def test_moomoo_missing_falls_back(self, monkeypatch, dp):
        _uninstall_moomoo(monkeypatch)
        dp._ensure_provider_decided()
        assert dp._moomoo_available is False
        assert "not installed" in (dp._init_error or "")

    def test_moomoo_opend_unreachable_falls_back(self, monkeypatch, dp):
        _install_fake_moomoo(monkeypatch, connect_ok=False)
        dp._ensure_provider_decided()
        assert dp._moomoo_available is False
        assert "connect failed" in (dp._init_error or "")

    def test_moomoo_connect_ok(self, monkeypatch, dp):
        _install_fake_moomoo(monkeypatch, connect_ok=True)
        dp._ensure_provider_decided()
        assert dp._moomoo_available is True
        assert dp._init_error is None


# ---------------------------------------------------------------------------
# End-to-end get_history()
# ---------------------------------------------------------------------------

class TestGetHistoryMoomooHappyPath:
    def test_returns_yfinance_shaped_df(self, monkeypatch, dp):
        _install_fake_moomoo(monkeypatch, connect_ok=True)

        df = dp.get_history("0166.KL", period="1y")
        assert not df.empty
        assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert df.index.name == "Date"
        assert dp.provider_name() == "moomoo"

    def test_unknown_ticker_uses_yfinance(self, monkeypatch, dp):
        _install_fake_moomoo(monkeypatch, connect_ok=True)

        with mock.patch.object(dp.yf, "Ticker") as MockTicker:
            MockTicker.return_value.history.return_value = _fake_yf_df(30)
            df = dp.get_history("AAPL", period="1y")

        assert not df.empty
        assert dp.provider_name() == "yfinance"
        MockTicker.assert_called_once_with("AAPL")


class TestGetHistoryFallback:
    def test_per_call_fallback_on_exception(self, monkeypatch, dp):
        _install_fake_moomoo(
            monkeypatch,
            connect_ok=True,
            kline_raises=RuntimeError("kline boom"),
        )
        with mock.patch.object(dp.yf, "Ticker") as MockTicker:
            MockTicker.return_value.history.return_value = _fake_yf_df(40)
            df = dp.get_history("0166.KL", period="1y")

        assert not df.empty
        assert dp.provider_name() == "yfinance"
        # Still considered "available" — one failure shouldn't demote.
        assert dp._moomoo_available is True
        assert dp._moomoo_failures == 1

    def test_sticky_demote_after_max_failures(self, monkeypatch, dp):
        _install_fake_moomoo(
            monkeypatch,
            connect_ok=True,
            kline_raises=RuntimeError("persistent boom"),
        )
        with mock.patch.object(dp.yf, "Ticker") as MockTicker:
            MockTicker.return_value.history.return_value = _fake_yf_df(10)
            for _ in range(dp.MOOMOO_MAX_CONSECUTIVE_FAILURES):
                dp.get_history("0166.KL", period="1y")

        assert dp._moomoo_available is False
        assert "demoted" in (dp._init_error or "")

    def test_success_resets_failure_counter(self, monkeypatch, dp):
        # First install: failing.
        _install_fake_moomoo(
            monkeypatch,
            connect_ok=True,
            kline_raises=RuntimeError("boom"),
        )
        with mock.patch.object(dp.yf, "Ticker") as MockTicker:
            MockTicker.return_value.history.return_value = _fake_yf_df(5)
            dp.get_history("0166.KL", period="1y")
            dp.get_history("0166.KL", period="1y")
        assert dp._moomoo_failures == 2

        # Swap in a healthy context (simulate OpenD recovering).
        _install_fake_moomoo(monkeypatch, connect_ok=True)
        # Rebuild the cached ctx with the new (healthy) class.
        dp._quote_ctx = sys.modules["moomoo"].OpenQuoteContext(
            host="127.0.0.1", port=11111
        )

        df = dp.get_history("0166.KL", period="1y")
        assert not df.empty
        assert dp._moomoo_failures == 0
        assert dp.provider_name() == "moomoo"

    def test_kline_returns_empty_falls_back(self, monkeypatch, dp):
        _install_fake_moomoo(
            monkeypatch,
            connect_ok=True,
            kline_df=pd.DataFrame(),  # empty
        )
        with mock.patch.object(dp.yf, "Ticker") as MockTicker:
            MockTicker.return_value.history.return_value = _fake_yf_df(20)
            df = dp.get_history("0166.KL", period="1y")
        assert not df.empty
        assert dp.provider_name() == "yfinance"


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

class TestDiagnostics:
    def test_health_keys(self, monkeypatch, dp):
        _uninstall_moomoo(monkeypatch)
        # Trigger one call so provider is decided.
        with mock.patch.object(dp.yf, "Ticker") as MockTicker:
            MockTicker.return_value.history.return_value = _fake_yf_df(5)
            dp.get_history("0166.KL", period="1mo")

        h = dp.health()
        assert set(h.keys()) >= {
            "provider_env", "moomoo_available", "moomoo_host", "moomoo_port",
            "moomoo_consecutive_failures", "last_served_by", "init_error",
        }
        assert h["last_served_by"] == "yfinance"
        assert h["moomoo_available"] is False

    def test_reset_clears_state(self, monkeypatch, dp):
        _install_fake_moomoo(monkeypatch, connect_ok=True)
        dp._ensure_provider_decided()
        assert dp._moomoo_available is True
        dp.reset()
        assert dp._moomoo_available is None
        assert dp._quote_ctx is None
