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
                         kline_raises=None,
                         port_open=True):
    """
    Install a fake `moomoo` module into sys.modules.

    Also patches data_provider._is_port_open to return `port_open` so the
    new TCP pre-check doesn't block tests that want to exercise the
    OpenQuoteContext path.
    """
    # Patch the port pre-check so OpenQuoteContext construction proceeds
    # when the test wants to simulate "OpenD is up".
    import data_provider as _dp
    monkeypatch.setattr(_dp, "_is_port_open",
                        lambda host, port, timeout=1.0: port_open)

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

    def test_us_equity(self, dp):
        # v3.6: bare symbols map to the US market for Moomoo OpenD.
        assert dp._to_moomoo_code("AAPL") == "US.AAPL"
        assert dp._to_moomoo_code("SPY") == "US.SPY"
        assert dp._to_moomoo_code("aapl") == "US.AAPL"

    def test_unknown_returns_none(self, dp):
        # Non-KLSE indices (^SPX, ^IXIC, ^VIX…) are not mappable to a
        # Moomoo MARKET.CODE — caller must fall back to yfinance.
        assert dp._to_moomoo_code("^SPX") is None
        assert dp._to_moomoo_code("^IXIC") is None
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

    def test_moomoo_opend_port_closed_falls_back(self, monkeypatch, dp):
        """OpenD port not listening — most common case (Streamlit Cloud, no Desktop)."""
        _install_fake_moomoo(monkeypatch, port_open=False)
        dp._ensure_provider_decided()
        assert dp._moomoo_available is False
        assert "not open" in (dp._init_error or "")

    def test_moomoo_opend_port_open_but_constructor_fails_falls_back(self, monkeypatch, dp):
        """Port is listening but OpenD construction throws (mismatched version, etc.)."""
        _install_fake_moomoo(monkeypatch, connect_ok=False, port_open=True)
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
    # NOTE (v3.6): the live Moomoo path is the *US* market. MY is gated off
    # via MY_PROFILE.moomoo_available=False (Moomoo OpenAPI has no MY coverage
    # yet), so MY always routes to yfinance. These tests therefore use a US
    # ticker to exercise the real Moomoo OpenD path. MY-gating behaviour is
    # covered separately in TestMarketGating below.
    def test_returns_yfinance_shaped_df(self, monkeypatch, dp):
        _install_fake_moomoo(monkeypatch, connect_ok=True)

        df = dp.get_history("AAPL", period="1y")
        assert not df.empty
        # Moomoo path returns exactly OHLCV (we normalise to this shape).
        # yfinance path may add Dividends/Stock Splits — both are fine for
        # downstream consumers, but the moomoo path is strict.
        assert {"Open", "High", "Low", "Close", "Volume"}.issubset(set(df.columns))
        assert df.index.name == "Date"
        assert dp.provider_name() == "moomoo"

    def test_unknown_ticker_uses_yfinance(self, monkeypatch, dp):
        # A non-mappable index symbol (^SPX) can't be served by Moomoo, so
        # even with OpenD up the provider must fall back to yfinance.
        _install_fake_moomoo(monkeypatch, connect_ok=True)

        with mock.patch.object(dp.yf, "Ticker") as MockTicker:
            MockTicker.return_value.history.return_value = _fake_yf_df(30)
            df = dp.get_history("^SPX", period="1y")

        assert not df.empty
        assert dp.provider_name() == "yfinance"
        MockTicker.assert_called_once_with("^SPX")


class TestGetHistoryFallback:
    # v3.6: exercised against a US ticker (the live Moomoo path).
    def test_per_call_fallback_on_exception(self, monkeypatch, dp):
        _install_fake_moomoo(
            monkeypatch,
            connect_ok=True,
            kline_raises=RuntimeError("kline boom"),
        )
        with mock.patch.object(dp.yf, "Ticker") as MockTicker:
            MockTicker.return_value.history.return_value = _fake_yf_df(40)
            df = dp.get_history("AAPL", period="1y")

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
                dp.get_history("AAPL", period="1y")

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
            dp.get_history("AAPL", period="1y")
            dp.get_history("AAPL", period="1y")
        assert dp._moomoo_failures == 2

        # Swap in a healthy context (simulate OpenD recovering).
        _install_fake_moomoo(monkeypatch, connect_ok=True)
        # Rebuild the cached ctx with the new (healthy) class.
        dp._quote_ctx = sys.modules["moomoo"].OpenQuoteContext(
            host="127.0.0.1", port=11111
        )

        df = dp.get_history("AAPL", period="1y")
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
            df = dp.get_history("AAPL", period="1y")
        assert not df.empty
        assert dp.provider_name() == "yfinance"


# ---------------------------------------------------------------------------
# Market gating (v3.6) — the per-market moomoo_available switch
# ---------------------------------------------------------------------------

class TestMarketGating:
    """The design contract for the dual-market data source:

      * BOTH markets fall back to yfinance when Moomoo OpenD is absent.
      * US uses Moomoo when OpenD is connected.
      * MY *always* uses yfinance today, because Moomoo OpenAPI has no MY
        coverage — gated off via MY_PROFILE.moomoo_available = False.
      * The day Moomoo enables MY: flip that one flag to True and MY auto-
        goes-live on Moomoo with zero other code changes. These tests guard
        that promise so it can never silently regress.
    """

    def test_my_ticker_always_uses_yfinance_even_with_opend_up(
            self, monkeypatch, dp):
        """MY is gated off: even with a healthy OpenD, 0166.KL → yfinance and
        Moomoo is never called (no failure recorded)."""
        _install_fake_moomoo(monkeypatch, connect_ok=True)
        with mock.patch.object(dp.yf, "Ticker") as MockTicker:
            MockTicker.return_value.history.return_value = _fake_yf_df(30)
            df = dp.get_history("0166.KL", period="1y")

        assert not df.empty
        assert dp.provider_name() == "yfinance"
        # Gated off BEFORE the Moomoo call, so no failure is recorded.
        assert dp._moomoo_failures == 0
        assert dp.health()["last_moomoo_error"] is None

    def test_my_gate_honoured_by_market_supports_moomoo(self, dp):
        """The gate is read live from the profile flag, not hardcoded."""
        assert dp._market_supports_moomoo("0166.KL") is False
        assert dp._market_supports_moomoo("AAPL") is True

    def test_my_goes_live_when_coverage_flag_flipped(self, monkeypatch, dp):
        """Future-proofing: the day Moomoo adds MY coverage, flipping
        MY_PROFILE.moomoo_available = True must route MY tickers to Moomoo
        with no other code change."""
        import dataclasses
        from market_profiles import my_profile

        # Simulate Moomoo announcing MY support: produce a copy of the MY
        # profile with moomoo_available flipped on. MarketProfile is a frozen
        # dataclass, so we use dataclasses.replace and swap the singleton.
        live_my = dataclasses.replace(my_profile.MY_PROFILE,
                                      moomoo_available=True)
        monkeypatch.setattr(my_profile, "MY_PROFILE", live_my)

        # Gate now opens for MY.
        assert dp._market_supports_moomoo("0166.KL") is True

        _install_fake_moomoo(monkeypatch, connect_ok=True)
        df = dp.get_history("0166.KL", period="1y")
        assert not df.empty
        assert dp.provider_name() == "moomoo"


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

    def test_ensure_probed_triggers_detection(self, monkeypatch, dp):
        """ensure_probed() must populate _moomoo_available so the UI panel
        can show the actually-active provider instead of 'auto'."""
        _uninstall_moomoo(monkeypatch)
        assert dp._moomoo_available is None  # not probed yet
        dp.ensure_probed()
        assert dp._moomoo_available is False  # probed and decided

    def test_ensure_probed_is_idempotent(self, monkeypatch, dp):
        """Calling ensure_probed() multiple times must not re-probe or change state."""
        _install_fake_moomoo(monkeypatch, connect_ok=True)
        dp.ensure_probed()
        assert dp._moomoo_available is True
        first_ctx = dp._quote_ctx
        dp.ensure_probed()
        dp.ensure_probed()
        assert dp._moomoo_available is True
        assert dp._quote_ctx is first_ctx  # same ctx, no re-probe

    def test_last_moomoo_error_surfaced_in_health(self, monkeypatch, dp):
        """
        v3.6: per-call Moomoo failure reasons (e.g. 'Unsupported quote market')
        must be visible in health() so users can diagnose without grepping logs.
        """
        _install_fake_moomoo(
            monkeypatch,
            connect_ok=True,
            kline_raises=RuntimeError("Unsupported quote market"),
        )
        with mock.patch.object(dp.yf, "Ticker") as MockTicker:
            MockTicker.return_value.history.return_value = _fake_yf_df(5)
            dp.get_history("AAPL", period="1y")

        h = dp.health()
        assert "last_moomoo_error" in h
        assert h["last_moomoo_error"] is not None
        assert "Unsupported quote market" in h["last_moomoo_error"]
        assert h["moomoo_consecutive_failures"] == 1

    def test_last_moomoo_error_cleared_on_success(self, monkeypatch, dp):
        """A successful Moomoo call must clear the prior error string."""
        # First a failing ctx
        _install_fake_moomoo(monkeypatch, connect_ok=True,
                             kline_raises=RuntimeError("transient hiccup"))
        with mock.patch.object(dp.yf, "Ticker") as MockTicker:
            MockTicker.return_value.history.return_value = _fake_yf_df(5)
            dp.get_history("AAPL", period="1y")
        assert dp.health()["last_moomoo_error"] is not None

        # Now swap in a healthy ctx and re-fetch
        _install_fake_moomoo(monkeypatch, connect_ok=True)
        dp._quote_ctx = sys.modules["moomoo"].OpenQuoteContext(host="127.0.0.1", port=11111)
        dp.get_history("AAPL", period="1y")

        assert dp.health()["last_moomoo_error"] is None

    def test_last_moomoo_error_cleared_on_reset(self, monkeypatch, dp):
        """reset() must also clear last_moomoo_error."""
        _install_fake_moomoo(monkeypatch, connect_ok=True,
                             kline_raises=RuntimeError("Unsupported quote market"))
        with mock.patch.object(dp.yf, "Ticker") as MockTicker:
            MockTicker.return_value.history.return_value = _fake_yf_df(5)
            dp.get_history("AAPL", period="1y")
        assert dp.health()["last_moomoo_error"] is not None

        dp.reset()
        assert dp._last_moomoo_error is None


# ---------------------------------------------------------------------------
# TCP pre-check (prevents moomoo SDK retry-thread spawn on Streamlit Cloud)
# ---------------------------------------------------------------------------

class TestPortPreCheck:
    def test_port_closed_skips_opend_construction(self, monkeypatch, dp):
        """
        Critical regression test: if the OpenD port is closed, we must NOT
        instantiate OpenQuoteContext (its constructor spawns a background
        reconnect thread that spams ECONNREFUSED forever).
        """
        # Install a moomoo fake whose constructor would explode if called —
        # this proves we never reached it.
        ctx_construct_count = {"n": 0}

        import sys, types
        fake = types.ModuleType("moomoo")
        fake.RET_OK = 0
        class _KLType: K_DAY = "K_DAY"
        class _AuType: QFQ = "QFQ"
        fake.KLType = _KLType
        fake.AuType = _AuType

        class _CtxThatShouldNeverBeCalled:
            def __init__(self, *a, **kw):
                ctx_construct_count["n"] += 1
                raise AssertionError(
                    "OpenQuoteContext was constructed even though port was closed!"
                )
        fake.OpenQuoteContext = _CtxThatShouldNeverBeCalled
        monkeypatch.setitem(sys.modules, "moomoo", fake)

        # Force the port check to report closed.
        monkeypatch.setattr(dp, "_is_port_open",
                            lambda host, port, timeout=1.0: False)

        dp._ensure_provider_decided()

        assert dp._moomoo_available is False
        assert ctx_construct_count["n"] == 0, "OpenQuoteContext was constructed!"
        assert "not open" in (dp._init_error or "")

    def test_port_open_proceeds_to_opend_check(self, monkeypatch, dp):
        """When the port IS open, we DO instantiate OpenQuoteContext."""
        _install_fake_moomoo(monkeypatch, connect_ok=True)
        monkeypatch.setattr(dp, "_is_port_open",
                            lambda host, port, timeout=1.0: True)
        dp._ensure_provider_decided()
        assert dp._moomoo_available is True

    def test_is_port_open_with_unreachable_host(self, dp):
        """The TCP probe itself: a closed port should return False quickly."""
        # 127.0.0.1:1 is reserved and never listening in test envs.
        result = dp._is_port_open("127.0.0.1", 1, timeout=0.5)
        assert result is False


# ---------------------------------------------------------------------------
# v3.7 — Intraday interval support
# ---------------------------------------------------------------------------

class TestIntradayInterval:
    """The `interval` arg added to get_history() in v3.7. Daily callers
    (interval=\"1d\", the default) must be byte-identical to pre-v3.7; new
    intraday callers (5m, 15m, …) get sub-daily candles."""

    # ---- pure helpers ----

    def test_is_intraday_helper(self, dp):
        assert dp._is_intraday("5m") is True
        assert dp._is_intraday("15m") is True
        assert dp._is_intraday("1m") is True
        # Defaults / daily / empties → NOT intraday.
        assert dp._is_intraday("1d") is False
        assert dp._is_intraday(None) is False
        assert dp._is_intraday("") is False

    def test_default_lookback_capped_to_provider_limit(self, dp):
        # yfinance hard caps: 1m=7d, 5m/15m/30m=60d, 60m/1h=730d.
        assert dp._default_intraday_lookback_days("1m") == 7
        assert dp._default_intraday_lookback_days("5m") == 60
        assert dp._default_intraday_lookback_days("15m") == 60
        assert dp._default_intraday_lookback_days("60m") == 730
        assert dp._default_intraday_lookback_days("1h") == 730
        # Unknown interval falls back to 60 (safe default).
        assert dp._default_intraday_lookback_days("99x") == 60

    def test_moomoo_ktype_for_interval(self, monkeypatch, dp):
        """When moomoo is installed, intervals map to the right KLType enum."""
        _install_fake_moomoo(monkeypatch, connect_ok=True)
        # Our fake module only has K_DAY; we add the others so the mapper
        # has something to return (proving the mapper LOOKS at KLType.K_5M
        # rather than always returning K_DAY).
        import sys as _sys
        mod = _sys.modules["moomoo"]
        mod.KLType.K_5M = "K_5M_ENUM"
        mod.KLType.K_15M = "K_15M_ENUM"
        mod.KLType.K_1M = "K_1M_ENUM"
        mod.KLType.K_60M = "K_60M_ENUM"

        assert dp._moomoo_ktype_for_interval("1d") == "K_DAY"
        assert dp._moomoo_ktype_for_interval("5m") == "K_5M_ENUM"
        assert dp._moomoo_ktype_for_interval("15m") == "K_15M_ENUM"
        assert dp._moomoo_ktype_for_interval("1m") == "K_1M_ENUM"
        assert dp._moomoo_ktype_for_interval("1h") == "K_60M_ENUM"
        # Unknown interval → None (caller must fall back to yfinance).
        assert dp._moomoo_ktype_for_interval("99x") is None

    def test_moomoo_ktype_returns_none_when_moomoo_absent(self, monkeypatch, dp):
        _uninstall_moomoo(monkeypatch)
        assert dp._moomoo_ktype_for_interval("5m") is None
        assert dp._moomoo_ktype_for_interval("1d") is None

    # ---- window resolution ----

    def test_resolve_window_intraday_default_uses_intraday_lookback(self, dp):
        """No period/start/end + 5m interval → defaults to a recent window
        (≤ yfinance cap), NOT 1 year of (nonexistent) intraday data."""
        s, e = dp._resolve_window(None, None, None, interval="5m")
        s_d, e_d = pd.to_datetime(s).date(), pd.to_datetime(e).date()
        span = (e_d - s_d).days
        assert span <= 60, f"5m default lookback should be <=60d, got {span}d"
        assert span >= 30, f"5m default lookback should be reasonably long"

    def test_resolve_window_intraday_with_explicit_start(self, dp):
        s, e = dp._resolve_window(None, "2025-01-10", "2025-01-15",
                                  interval="5m")
        assert s == "2025-01-10"
        assert e == "2025-01-15"

    def test_resolve_window_intraday_with_only_end_uses_intraday_default(self, dp):
        """If only `end` is given (no start) with an intraday interval, the
        default look-back must use the intraday cap, not 365 days."""
        s, e = dp._resolve_window(None, None, "2025-03-01", interval="5m")
        span = (pd.to_datetime(e).date() - pd.to_datetime(s).date()).days
        assert span <= 60

    def test_resolve_window_daily_unchanged(self, dp):
        """Sanity: the daily-path window is byte-identical (1y default)."""
        s, e = dp._resolve_window("1y", None, None)  # no interval arg
        span = (pd.to_datetime(e).date() - pd.to_datetime(s).date()).days
        assert span >= 360
        # Same call with explicit interval="1d" must produce the same window.
        s2, e2 = dp._resolve_window("1y", None, None, interval="1d")
        assert (s, e) == (s2, e2)

    # ---- end-to-end through get_history ----

    def test_get_history_intraday_routes_through_yfinance_with_interval(
            self, monkeypatch, dp):
        """interval=\"5m\" on a US ticker, no Moomoo: yfinance must be called
        with interval=\"5m\" AND a period inside the 60d cap."""
        _uninstall_moomoo(monkeypatch)
        with mock.patch.object(dp.yf, "Ticker") as MockTicker:
            MockTicker.return_value.history.return_value = _fake_yf_df(30)
            df = dp.get_history("AAPL", interval="5m")

        assert not df.empty
        MockTicker.assert_called_once_with("AAPL")
        kwargs = MockTicker.return_value.history.call_args.kwargs
        assert kwargs.get("interval") == "5m"
        # When no explicit start/end was passed we use period="60d".
        assert kwargs.get("period") == "60d"

    def test_get_history_intraday_explicit_range_passed_through(
            self, monkeypatch, dp):
        _uninstall_moomoo(monkeypatch)
        with mock.patch.object(dp.yf, "Ticker") as MockTicker:
            MockTicker.return_value.history.return_value = _fake_yf_df(10)
            dp.get_history("AAPL", start="2025-01-10", end="2025-01-15",
                           interval="5m")

        kwargs = MockTicker.return_value.history.call_args.kwargs
        assert kwargs.get("interval") == "5m"
        assert kwargs.get("start") == "2025-01-10"
        assert kwargs.get("end") == "2025-01-15"
        # When start/end is explicit, no period= is set.
        assert kwargs.get("period") is None or "period" not in kwargs

    def test_get_history_daily_default_unchanged(self, monkeypatch, dp):
        """interval defaults to \"1d\". The yfinance call must look IDENTICAL
        to what pre-v3.7 callers got — no interval=, no period=60d.

        This is the byte-identical-daily-path guard.
        """
        _uninstall_moomoo(monkeypatch)
        with mock.patch.object(dp.yf, "Ticker") as MockTicker:
            MockTicker.return_value.history.return_value = _fake_yf_df(30)
            dp.get_history("AAPL", period="1y")  # no interval arg

        kwargs = MockTicker.return_value.history.call_args.kwargs
        assert kwargs.get("period") == "1y"
        # Must NOT have passed interval to yfinance on the daily path
        # (would change v3.6 behaviour for downstream indicator code).
        assert "interval" not in kwargs

    def test_get_history_intraday_via_moomoo_uses_5m_ktype(
            self, monkeypatch, dp):
        """When OpenD is up AND interval=5m, the Moomoo call must use the
        K_5M ktype (proved by inspecting what request_history_kline saw)."""
        captured = {"ktype": None}

        # Build a custom fake that captures the ktype it was asked for.
        import sys as _sys, types as _types
        fake = _types.ModuleType("moomoo")
        fake.RET_OK = 0

        class _KLType:
            K_DAY = "K_DAY"
            K_5M = "K_5M"
            K_15M = "K_15M"

        class _AuType:
            QFQ = "QFQ"

        fake.KLType = _KLType
        fake.AuType = _AuType

        class _Ctx:
            def __init__(self, host=None, port=None):
                pass

            def get_global_state(self):
                return 0, {}

            def request_history_kline(self, code, start=None, end=None,
                                      ktype=None, autype=None,
                                      max_count=1000, page_req_key=None,
                                      **kw):
                captured["ktype"] = ktype
                return 0, _fake_moomoo_kline_df(20), None

            def close(self):
                pass

        fake.OpenQuoteContext = _Ctx
        monkeypatch.setitem(_sys.modules, "moomoo", fake)
        monkeypatch.setattr(dp, "_is_port_open",
                            lambda h, p, timeout=1.0: True)

        df = dp.get_history("AAPL", interval="5m")
        assert not df.empty
        assert captured["ktype"] == "K_5M"
