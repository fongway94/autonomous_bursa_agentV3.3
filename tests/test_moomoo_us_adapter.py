"""
Tests for MoomooUSAdapter (Block 5).

These tests mock the entire `moomoo` SDK so they require no live OpenD,
no network, and no moomoo package installation. They exercise:

  - TCP pre-check (port closed → connect returns False, no SDK init)
  - SIMULATE mode: connect succeeds without unlock_pwd
  - REAL mode: connect fails when MOOMOO_TRADING_PWD missing
  - REAL mode: connect calls unlock_trade and succeeds when ret==RET_OK
  - REAL mode: connect fails when unlock_trade returns non-RET_OK
  - place_order: maps response to OrderResponse correctly
  - place_order: handles SDK timeout (thread join times out)
  - place_order: handles SDK exception (returns ERROR not raises)
  - cancel_order, get_order, get_account_snapshot, list_positions happy paths
  - Mirror hooks: NOOP path is a true no-op
  - Mirror hooks: SIMULATE path calls adapter.place_order
  - Factory: MY market always returns Noop regardless of mode
  - Factory: US + SIMULATE/REAL returns MoomooUSAdapter
  - reset_adapter_cache disconnects + clears
"""

from __future__ import annotations

import sys
import types
from unittest import mock

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Fake moomoo SDK
# ---------------------------------------------------------------------------

class _FakeRet:
    OK = 0


def _make_fake_moomoo(
    *,
    construct_raises: Exception | None = None,
    unlock_ret: int = 0,
    unlock_data: str = "ok",
    place_order_ret: int = 0,
    place_order_df: pd.DataFrame | None = None,
    place_order_raises: Exception | None = None,
    place_order_sleep_sec: float = 0.0,
    accinfo_df: pd.DataFrame | None = None,
    positions_df: pd.DataFrame | None = None,
    cancel_ret: int = 0,
    order_query_df: pd.DataFrame | None = None,
):
    """Build a `moomoo` module stub and install it into sys.modules."""
    fake = types.ModuleType("moomoo")

    class _TrdMarket:
        US = "US"
        HK = "HK"
        MY = "MY"

    class _SecurityFirm:
        FUTUINC = "FUTUINC"
        FUTUSECURITIES = "FUTUSECURITIES"

    class _TrdEnv:
        REAL = "REAL"
        SIMULATE = "SIMULATE"

    class _TrdSide:
        BUY = "BUY"
        SELL = "SELL"

    class _OT:
        MARKET = "MARKET"
        NORMAL = "NORMAL"

    class _ModifyOp:
        CANCEL = "CANCEL"

    fake.TrdMarket = _TrdMarket
    fake.SecurityFirm = _SecurityFirm
    fake.TrdEnv = _TrdEnv
    fake.TrdSide = _TrdSide
    fake.OrderType = _OT
    fake.ModifyOrderOp = _ModifyOp
    fake.RET_OK = 0

    default_place = pd.DataFrame([{
        "order_id": "fake-order-1",
        "order_status": "SUBMITTED",
        "dealt_qty": 0,
        "dealt_avg_price": 0.0,
    }])
    default_acc = pd.DataFrame([{
        "us_cash": 4500.0,
        "total_assets": 5200.0,
        "market_val": 700.0,
    }])
    default_positions = pd.DataFrame([
        {"code": "US.AAPL", "qty": 10, "cost_price": 150.0,
         "nominal_price": 160.0, "pl_val": 100.0},
        {"code": "US.NVDA", "qty": 5, "cost_price": 800.0,
         "nominal_price": 820.0, "pl_val": 100.0},
    ])
    default_order_query = pd.DataFrame([{
        "order_status": "FILLED_ALL",
        "dealt_qty": 10,
        "dealt_avg_price": 160.5,
    }])

    class _Ctx:
        def __init__(self, filter_trdmarket=None, host=None, port=None,
                     security_firm=None):
            if construct_raises:
                raise construct_raises
            self.host = host
            self.port = port

        def unlock_trade(self, pwd):
            return unlock_ret, unlock_data

        def place_order(self, price=0, qty=0, code="", trd_side=None,
                        order_type=None, trd_env=None, **kw):
            if place_order_sleep_sec > 0:
                import time
                time.sleep(place_order_sleep_sec)
            if place_order_raises:
                raise place_order_raises
            return place_order_ret, (
                place_order_df if place_order_df is not None else default_place)

        def modify_order(self, op, order_id, qty, price, trd_env=None):
            return cancel_ret, pd.DataFrame()

        def order_list_query(self, order_id=None, trd_env=None):
            return 0, (order_query_df if order_query_df is not None
                       else default_order_query)

        def accinfo_query(self, trd_env=None):
            return 0, (accinfo_df if accinfo_df is not None else default_acc)

        def position_list_query(self, trd_env=None):
            return 0, (positions_df if positions_df is not None
                       else default_positions)

        def close(self):
            pass

    fake.OpenSecTradeContext = _Ctx
    return fake


@pytest.fixture
def install_fake_moomoo(monkeypatch):
    """Install fake moomoo + force the TCP probe to report 'open'."""
    def _install(**kw):
        fake = _make_fake_moomoo(**kw)
        monkeypatch.setitem(sys.modules, "moomoo", fake)
        # Bypass the TCP pre-check (treat port as listening)
        import broker_adapter
        monkeypatch.setattr(broker_adapter, "_is_port_open",
                            lambda h, p, timeout=1.0: True)
        # Drop any cached adapter from prior tests
        broker_adapter.reset_adapter_cache()
        return fake
    return _install


@pytest.fixture
def adapter_us_simulate(install_fake_moomoo):
    install_fake_moomoo()
    from broker_adapter import MoomooUSAdapter
    a = MoomooUSAdapter(trd_env="SIMULATE")
    yield a
    a.disconnect()


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------

class TestConnect:
    def test_port_closed_returns_false(self, monkeypatch, install_fake_moomoo):
        install_fake_moomoo()
        import broker_adapter
        # Override the port check to closed AFTER install
        monkeypatch.setattr(broker_adapter, "_is_port_open",
                            lambda h, p, timeout=1.0: False)
        a = broker_adapter.MoomooUSAdapter(trd_env="SIMULATE")
        assert a.connect() is False
        assert a.is_connected() is False
        assert "not listening" in (a.last_error() or "")

    def test_simulate_connects_without_password(self, install_fake_moomoo):
        install_fake_moomoo()
        from broker_adapter import MoomooUSAdapter
        a = MoomooUSAdapter(trd_env="SIMULATE", unlock_pwd=None)
        assert a.connect() is True
        assert a.is_connected() is True
        assert a.last_error() is None

    def test_real_without_password_fails_loudly(self, install_fake_moomoo):
        install_fake_moomoo()
        from broker_adapter import MoomooUSAdapter
        a = MoomooUSAdapter(trd_env="REAL", unlock_pwd=None)
        assert a.connect() is False
        assert "MOOMOO_TRADING_PWD" in (a.last_error() or "")
        assert a.is_connected() is False

    def test_real_with_password_unlocks_and_connects(self, install_fake_moomoo):
        install_fake_moomoo(unlock_ret=0)  # 0 == RET_OK
        from broker_adapter import MoomooUSAdapter
        a = MoomooUSAdapter(trd_env="REAL", unlock_pwd="secret")
        assert a.connect() is True
        assert a.is_connected() is True

    def test_real_unlock_rejected_disconnects(self, install_fake_moomoo):
        install_fake_moomoo(unlock_ret=-1, unlock_data="bad pwd")
        from broker_adapter import MoomooUSAdapter
        a = MoomooUSAdapter(trd_env="REAL", unlock_pwd="wrong")
        assert a.connect() is False
        assert "rejected" in (a.last_error() or "")
        assert a.is_connected() is False

    def test_connect_is_idempotent(self, install_fake_moomoo):
        install_fake_moomoo()
        from broker_adapter import MoomooUSAdapter
        a = MoomooUSAdapter(trd_env="SIMULATE")
        assert a.connect() is True
        # second call should be a no-op and still return True
        assert a.connect() is True

    def test_construct_failure_returns_false(self, install_fake_moomoo):
        install_fake_moomoo(construct_raises=RuntimeError("SDK boom"))
        from broker_adapter import MoomooUSAdapter
        a = MoomooUSAdapter(trd_env="SIMULATE")
        assert a.connect() is False
        assert "init failed" in (a.last_error() or "")


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

class TestPlaceOrder:
    def test_market_buy_success(self, adapter_us_simulate):
        from broker_adapter import OrderRequest
        adapter_us_simulate.connect()
        req = OrderRequest(ticker="AAPL", side="BUY", quantity=10,
                            order_type="MARKET")
        resp = adapter_us_simulate.place_order(req)
        assert resp.broker_order_id == "fake-order-1"
        assert resp.status == "SUBMITTED"

    def test_market_sell_success(self, adapter_us_simulate):
        from broker_adapter import OrderRequest
        adapter_us_simulate.connect()
        req = OrderRequest(ticker="AAPL", side="SELL", quantity=5,
                            order_type="MARKET")
        resp = adapter_us_simulate.place_order(req)
        assert resp.status == "SUBMITTED"
        assert resp.broker_order_id != ""

    def test_limit_order_passes_price(self, install_fake_moomoo):
        # Capture the place_order args to verify limit_price is forwarded
        captured = {}
        original = _make_fake_moomoo

        fake = _make_fake_moomoo()
        sys.modules["moomoo"] = fake

        orig_init = fake.OpenSecTradeContext.__init__
        orig_place = fake.OpenSecTradeContext.place_order

        def spy_place(self, price=0, qty=0, code="", trd_side=None,
                      order_type=None, trd_env=None, **kw):
            captured["price"] = price
            captured["qty"] = qty
            captured["code"] = code
            captured["order_type"] = order_type
            return orig_place(self, price=price, qty=qty, code=code,
                              trd_side=trd_side, order_type=order_type,
                              trd_env=trd_env, **kw)

        fake.OpenSecTradeContext.place_order = spy_place

        import broker_adapter
        broker_adapter._is_port_open = lambda h, p, timeout=1.0: True
        broker_adapter.reset_adapter_cache()
        from broker_adapter import MoomooUSAdapter, OrderRequest
        a = MoomooUSAdapter(trd_env="SIMULATE")
        a.connect()
        req = OrderRequest(ticker="AAPL", side="BUY", quantity=5,
                            order_type="LIMIT", limit_price=150.25)
        a.place_order(req)
        assert captured["price"] == 150.25
        assert captured["qty"] == 5
        assert captured["code"] == "US.AAPL"
        assert captured["order_type"] == "NORMAL"  # moomoo's name for limit

    def test_place_order_filled_response_parsed(self, install_fake_moomoo):
        df = pd.DataFrame([{
            "order_id": "filled-99",
            "order_status": "FILLED_ALL",
            "dealt_qty": 10,
            "dealt_avg_price": 150.45,
        }])
        install_fake_moomoo(place_order_df=df)
        from broker_adapter import MoomooUSAdapter, OrderRequest
        a = MoomooUSAdapter(trd_env="SIMULATE")
        a.connect()
        resp = a.place_order(OrderRequest(ticker="AAPL", side="BUY",
                                            quantity=10))
        assert resp.status == "FILLED"
        assert resp.filled_quantity == 10
        assert resp.avg_fill_price == 150.45

    def test_place_order_rejected_response(self, install_fake_moomoo):
        install_fake_moomoo(place_order_ret=-1,
                             place_order_df=pd.DataFrame())
        from broker_adapter import MoomooUSAdapter, OrderRequest
        a = MoomooUSAdapter(trd_env="SIMULATE")
        a.connect()
        resp = a.place_order(OrderRequest(ticker="AAPL", side="BUY",
                                            quantity=10))
        assert resp.status == "REJECTED"

    def test_place_order_sdk_exception_returns_error_does_not_raise(
            self, install_fake_moomoo):
        install_fake_moomoo(place_order_raises=RuntimeError("kaboom"))
        from broker_adapter import MoomooUSAdapter, OrderRequest
        a = MoomooUSAdapter(trd_env="SIMULATE")
        a.connect()
        resp = a.place_order(OrderRequest(ticker="AAPL", side="BUY",
                                            quantity=10))
        assert resp.status == "ERROR"
        assert "kaboom" in (resp.error or "")

    def test_place_order_timeout_returns_error(self, install_fake_moomoo,
                                                 monkeypatch):
        # Tiny timeout so the test is fast
        install_fake_moomoo(place_order_sleep_sec=2.0)
        import broker_adapter
        monkeypatch.setattr(broker_adapter, "MOOMOO_CALL_TIMEOUT_SEC", 0.5)
        from broker_adapter import MoomooUSAdapter, OrderRequest
        a = MoomooUSAdapter(trd_env="SIMULATE")
        a.connect()
        resp = a.place_order(OrderRequest(ticker="AAPL", side="BUY",
                                            quantity=10))
        assert resp.status == "ERROR"
        assert "timeout" in (resp.error or "").lower()


# ---------------------------------------------------------------------------
# Account snapshot + positions
# ---------------------------------------------------------------------------

class TestAccountAndPositions:
    def test_account_snapshot_parses_us_cash(self, adapter_us_simulate):
        adapter_us_simulate.connect()
        snap = adapter_us_simulate.get_account_snapshot()
        assert snap.cash == 4500.0
        assert snap.total_assets == 5200.0
        assert snap.market_value == 700.0
        assert snap.currency == "USD"

    def test_get_cash_balance_shortcut(self, adapter_us_simulate):
        adapter_us_simulate.connect()
        assert adapter_us_simulate.get_cash_balance() == 4500.0

    def test_list_positions_parses_and_strips_prefix(self, adapter_us_simulate):
        adapter_us_simulate.connect()
        positions = adapter_us_simulate.list_positions()
        assert len(positions) == 2
        tickers = {p.ticker for p in positions}
        assert tickers == {"AAPL", "NVDA"}
        # Verify cost / pnl parsed
        aapl = next(p for p in positions if p.ticker == "AAPL")
        assert aapl.quantity == 10
        assert aapl.avg_cost == 150.0
        assert aapl.unrealized_pnl == 100.0

    def test_list_positions_skips_zero_quantity_rows(self, install_fake_moomoo):
        df = pd.DataFrame([
            {"code": "US.AAPL", "qty": 10, "cost_price": 100,
             "nominal_price": 110, "pl_val": 100},
            {"code": "US.OLD", "qty": 0, "cost_price": 50,
             "nominal_price": 55, "pl_val": 50},  # closed
        ])
        install_fake_moomoo(positions_df=df)
        from broker_adapter import MoomooUSAdapter
        a = MoomooUSAdapter(trd_env="SIMULATE")
        a.connect()
        positions = a.list_positions()
        assert len(positions) == 1
        assert positions[0].ticker == "AAPL"

    def test_account_snapshot_when_disconnected_returns_empty_no_raise(
            self, install_fake_moomoo, monkeypatch):
        install_fake_moomoo()
        import broker_adapter
        # Simulate port-closed AFTER fake install
        monkeypatch.setattr(broker_adapter, "_is_port_open",
                            lambda h, p, timeout=1.0: False)
        from broker_adapter import MoomooUSAdapter
        a = MoomooUSAdapter(trd_env="SIMULATE")
        # No connect() call — adapter is offline. Method must NOT raise.
        snap = a.get_account_snapshot()
        assert snap.cash == 0.0
        assert snap.currency == "USD"


# ---------------------------------------------------------------------------
# Cancel + query
# ---------------------------------------------------------------------------

class TestCancelAndQuery:
    def test_cancel_order_success(self, adapter_us_simulate):
        adapter_us_simulate.connect()
        assert adapter_us_simulate.cancel_order("fake-1") is True

    def test_get_order_parses_filled_status(self, adapter_us_simulate):
        adapter_us_simulate.connect()
        resp = adapter_us_simulate.get_order("fake-1")
        assert resp.status == "FILLED"
        assert resp.filled_quantity == 10
        assert resp.avg_fill_price == 160.5


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------

class TestStatusMapping:
    @pytest.mark.parametrize("raw,expected", [
        ("FILLED_ALL", "FILLED"),
        ("FILLED_PART", "PARTIAL"),
        ("SUBMITTED", "SUBMITTED"),
        ("SUBMITTING", "PENDING"),
        ("CANCELLED_ALL", "CANCELLED"),
        ("FAILED", "REJECTED"),
        ("DELETED", "CANCELLED"),
        ("UNKNOWN_NEW_STATUS", "ERROR"),
        ("", "ERROR"),
    ])
    def test_status_map(self, raw, expected):
        from broker_adapter import _map_moomoo_status
        assert _map_moomoo_status(raw) == expected


# ---------------------------------------------------------------------------
# Ticker normalisation
# ---------------------------------------------------------------------------

class TestTickerHelpers:
    def test_to_moomoo_code_bare(self):
        from broker_adapter import MoomooUSAdapter
        assert MoomooUSAdapter._to_moomoo_code("AAPL") == "US.AAPL"

    def test_to_moomoo_code_already_prefixed(self):
        from broker_adapter import MoomooUSAdapter
        assert MoomooUSAdapter._to_moomoo_code("US.AAPL") == "US.AAPL"
        assert MoomooUSAdapter._to_moomoo_code("HK.0700") == "HK.0700"

    def test_to_moomoo_code_lowercase_normalised(self):
        from broker_adapter import MoomooUSAdapter
        assert MoomooUSAdapter._to_moomoo_code("aapl") == "US.AAPL"

    def test_strip_prefix(self):
        from broker_adapter import MoomooUSAdapter
        assert MoomooUSAdapter._strip_moomoo_prefix("US.AAPL") == "AAPL"
        assert MoomooUSAdapter._strip_moomoo_prefix("AAPL") == "AAPL"
        assert MoomooUSAdapter._strip_moomoo_prefix("") == ""


# ---------------------------------------------------------------------------
# Factory + cache + mode persistence
# ---------------------------------------------------------------------------

class TestFactory:
    def test_my_market_always_returns_noop(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "MY")
        import market_profiles
        market_profiles.reset_cache()
        import broker_adapter
        broker_adapter.reset_adapter_cache()
        for mode in ("NOOP", "SIMULATE", "REAL"):
            broker_adapter.reset_adapter_cache()
            a = broker_adapter.get_broker_adapter(mode=mode)
            assert a.name == "noop", f"MY+{mode} must be Noop, got {a.name}"

    def test_us_noop_returns_noop(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        import broker_adapter
        broker_adapter.reset_adapter_cache()
        a = broker_adapter.get_broker_adapter(mode="NOOP")
        assert a.name == "noop"

    def test_us_simulate_returns_moomoo_us(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        import broker_adapter
        broker_adapter.reset_adapter_cache()
        a = broker_adapter.get_broker_adapter(mode="SIMULATE")
        assert a.name == "moomoo_us"

    def test_us_real_returns_moomoo_us_with_real_env(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "US")
        monkeypatch.setenv("MOOMOO_TRADING_PWD", "x")
        import market_profiles
        market_profiles.reset_cache()
        import broker_adapter
        broker_adapter.reset_adapter_cache()
        a = broker_adapter.get_broker_adapter(mode="REAL")
        assert a.name == "moomoo_us"
        assert a._env == "REAL"

    def test_reset_adapter_cache_calls_disconnect(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        import broker_adapter
        broker_adapter.reset_adapter_cache()
        a = broker_adapter.get_broker_adapter(mode="SIMULATE")
        disconnected = {"called": False}
        original = a.disconnect
        def spy():
            disconnected["called"] = True
            original()
        a.disconnect = spy
        broker_adapter.reset_adapter_cache()
        assert disconnected["called"] is True


class TestBrokerModePersistence:
    def test_set_and_get_broker_mode_roundtrip(self):
        from broker_adapter import set_broker_mode, get_broker_mode
        for mode in ("NOOP", "SIMULATE", "REAL"):
            set_broker_mode(mode)
            assert get_broker_mode() == mode

    def test_set_broker_mode_rejects_garbage(self):
        from broker_adapter import set_broker_mode
        with pytest.raises(ValueError):
            set_broker_mode("YOLO")


# ---------------------------------------------------------------------------
# Mirror hooks
# ---------------------------------------------------------------------------

class TestMirrorHooks:
    def test_mirror_entry_noop_mode_does_nothing(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        from broker_adapter import (
            mirror_entry_to_broker, set_broker_mode, get_broker_adapter,
            reset_adapter_cache,
        )
        set_broker_mode("NOOP")
        reset_adapter_cache()
        # Should not raise even with no SDK available
        mirror_entry_to_broker(ticker="AAPL", shares=10, fill_price=150.0)

    def test_mirror_exit_noop_mode_does_nothing(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        from broker_adapter import (
            mirror_exit_to_broker, set_broker_mode, reset_adapter_cache,
        )
        set_broker_mode("NOOP")
        reset_adapter_cache()
        mirror_exit_to_broker(ticker="AAPL", shares=10, fill_price=160.0)

    def test_mirror_entry_my_market_is_noop_even_in_simulate_mode(
            self, monkeypatch):
        """Critical safety: MY profile says moomoo_available=False, so
        mirror must be a no-op REGARDLESS of broker_mode setting."""
        monkeypatch.setenv("MARKET_MODE", "MY")
        import market_profiles
        market_profiles.reset_cache()
        from broker_adapter import (mirror_entry_to_broker, set_broker_mode,
                                     reset_adapter_cache)
        set_broker_mode("SIMULATE")  # try to enable
        reset_adapter_cache()
        # Even with no mocked SDK, this MUST be a true no-op
        mirror_entry_to_broker(ticker="1155.KL", shares=100, fill_price=10.0)
        # Reset to NOOP for other tests
        set_broker_mode("NOOP")

    def test_mirror_entry_simulate_calls_place_order(self, install_fake_moomoo,
                                                       monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        install_fake_moomoo()
        import broker_adapter
        broker_adapter.reset_adapter_cache()
        broker_adapter.set_broker_mode("SIMULATE")
        broker_adapter.reset_adapter_cache()

        # Spy on the adapter that get_broker_adapter() returns
        adapter = broker_adapter.get_broker_adapter()
        captured = {}
        original = adapter.place_order
        def spy(req):
            captured["req"] = req
            return original(req)
        adapter.place_order = spy

        broker_adapter.mirror_entry_to_broker(
            ticker="AAPL", shares=10, fill_price=150.0, trade_id=42)
        assert "req" in captured
        assert captured["req"].side == "BUY"
        assert captured["req"].quantity == 10
        assert captured["req"].client_order_id == "trade-42"

        # Reset for other tests
        broker_adapter.set_broker_mode("NOOP")
        broker_adapter.reset_adapter_cache()


# ---------------------------------------------------------------------------
# adapter_health diagnostics (for Settings tab)
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_dict_shape(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "US")
        import market_profiles
        market_profiles.reset_cache()
        from broker_adapter import adapter_health
        h = adapter_health()
        for k in ("market", "mode", "moomoo_available_for_market",
                  "adapter_name", "connected", "openD_host", "openD_port",
                  "real_pwd_configured"):
            assert k in h, f"missing {k}"

    def test_health_my_market_says_moomoo_unavailable(self, monkeypatch):
        monkeypatch.setenv("MARKET_MODE", "MY")
        import market_profiles
        market_profiles.reset_cache()
        from broker_adapter import adapter_health
        h = adapter_health()
        assert h["market"] == "MY"
        assert h["moomoo_available_for_market"] is False
