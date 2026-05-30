# broker_adapter.py
"""
Broker adapter — abstract interface + Moomoo stub + mirror hooks.

v3.6 multi-market change
------------------------
The historic agent runs in NOTIFICATION-ONLY mode (paper trades + Telegram
alerts; user mirrors orders into Moomoo manually). This file now ALSO
provides two thin mirror hooks used by trading_engine.py:

    mirror_entry_to_broker(...)
    mirror_exit_to_broker(...)

In NOOP mode (default, and ALWAYS for MY since OpenAPI doesn't yet
support Bursa), these are no-ops. In SIMULATE / REAL mode (US only as of
v3.6), they delegate to MoomooUSAdapter.

The full MoomooUSAdapter execution wiring lands in Block 5. This file
provides the contract + safe NOOP defaults so the rest of the codebase
keeps working today.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, Optional


# --------------------------------------------------------------------- #
# Domain types
# --------------------------------------------------------------------- #

OrderSide = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT"]
OrderStatus = Literal["PENDING", "SUBMITTED", "FILLED", "PARTIAL",
                       "CANCELLED", "REJECTED", "ERROR"]


@dataclass
class OrderRequest:
    """A broker-agnostic order intent."""
    ticker: str               # e.g. "0166.KL" (MY) or "AAPL" (US)
    side: OrderSide
    quantity: int
    order_type: OrderType = "MARKET"
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    client_order_id: str | None = None


@dataclass
class OrderResponse:
    broker_order_id: str
    status: OrderStatus
    filled_quantity: int = 0
    avg_fill_price: float = 0.0
    error: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class Position:
    ticker: str
    quantity: int
    avg_cost: float
    current_price: float
    unrealized_pnl: float


@dataclass
class AccountSnapshot:
    """Lightweight broker-side account view for reconciliation."""
    cash: float
    total_assets: float
    market_value: float
    currency: str
    raw: dict = field(default_factory=dict)


# --------------------------------------------------------------------- #
# Abstract base
# --------------------------------------------------------------------- #

class BrokerAdapter(ABC):
    """Every concrete broker integration MUST implement these."""

    name: str = "abstract"

    @abstractmethod
    def connect(self) -> bool:
        """Establish session. Returns True on success."""

    @abstractmethod
    def disconnect(self) -> None:
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        ...

    @abstractmethod
    def place_order(self, req: OrderRequest) -> OrderResponse:
        """Submit a single order."""

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool:
        ...

    @abstractmethod
    def get_order(self, broker_order_id: str) -> OrderResponse:
        ...

    @abstractmethod
    def list_positions(self) -> list[Position]:
        ...

    @abstractmethod
    def get_account_snapshot(self) -> AccountSnapshot:
        ...

    @abstractmethod
    def get_cash_balance(self) -> float:
        ...


# --------------------------------------------------------------------- #
# No-op adapter — used in notification-only mode
# --------------------------------------------------------------------- #

class NoopAdapter(BrokerAdapter):
    """Safe default: pretends to be connected, does nothing."""
    name = "noop"

    def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        pass

    def is_connected(self) -> bool:
        return True

    def place_order(self, req: OrderRequest) -> OrderResponse:
        return OrderResponse(
            broker_order_id="noop-0", status="REJECTED",
            error="NoopAdapter — notification-only mode")

    def cancel_order(self, broker_order_id: str) -> bool:
        return False

    def get_order(self, broker_order_id: str) -> OrderResponse:
        return OrderResponse(broker_order_id=broker_order_id,
                              status="ERROR", error="noop")

    def list_positions(self) -> list[Position]:
        return []

    def get_account_snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(cash=0.0, total_assets=0.0, market_value=0.0,
                                currency="N/A")

    def get_cash_balance(self) -> float:
        return 0.0


# --------------------------------------------------------------------- #
# Moomoo MY stub (kept for forward-compat when OpenAPI adds MY market)
# --------------------------------------------------------------------- #

class MoomooMYAdapter(BrokerAdapter):
    """Stub: Moomoo OpenAPI does not yet support the MY market.

    Kept as a forward-looking placeholder; flipping
    `MY_PROFILE.moomoo_available = True` + implementing this class will
    enable execution without touching the rest of the codebase.
    """
    name = "moomoo_my"

    def connect(self) -> bool:
        return False  # Not yet supported

    def disconnect(self) -> None:
        pass

    def is_connected(self) -> bool:
        return False

    def place_order(self, req: OrderRequest) -> OrderResponse:
        return OrderResponse(
            broker_order_id="my-na",
            status="REJECTED",
            error="Moomoo OpenAPI does not yet support Bursa Malaysia (MY) market")

    def cancel_order(self, broker_order_id: str) -> bool:
        return False

    def get_order(self, broker_order_id: str) -> OrderResponse:
        return OrderResponse(broker_order_id=broker_order_id,
                              status="ERROR",
                              error="MY OpenAPI not yet supported")

    def list_positions(self) -> list[Position]:
        return []

    def get_account_snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(cash=0.0, total_assets=0.0, market_value=0.0,
                                currency="MYR")

    def get_cash_balance(self) -> float:
        return 0.0


# --------------------------------------------------------------------- #
# Moomoo US adapter — INTERFACE ONLY in this block.
# Full implementation arrives in Block 5.
# --------------------------------------------------------------------- #

class MoomooUSAdapter(BrokerAdapter):
    """
    Moomoo OpenAPI adapter for US market (TrdMarket.US).

    v3.6 status: SKELETON. All methods raise NotImplementedError so the
    UI's "SIMULATE / REAL" toggle can't accidentally activate before
    Block 5 lands. NOOP mode users see no behaviour change.

    Block-5 implementation will mirror the pattern from
    lookatwallstreet/WallTrading-Bot-MooMoo-Futu:
        OpenSecTradeContext(filter_trdmarket=TrdMarket.US,
                            host=127.0.0.1, port=11112,
                            security_firm=SecurityFirm.FUTUINC)
        ctx.unlock_trade(TRADING_PWD)
        ctx.place_order(price, qty, code='US.AAPL', trd_side=TrdSide.BUY,
                        order_type=OrderType.MARKET, trd_env=TrdEnv.REAL)
        ctx.accinfo_query()       → us_cash, total_assets, market_val
        ctx.position_list_query()
    """
    name = "moomoo_us"

    def __init__(self,
                 host: str = "127.0.0.1",
                 port: int = 11111,
                 trd_env: str = "SIMULATE",          # SIMULATE | REAL
                 unlock_pwd: Optional[str] = None,
                 security_firm: str = "FUTUINC"):
        self._host = host
        self._port = port
        self._env = trd_env
        self._unlock_pwd = unlock_pwd
        self._security_firm = security_firm
        self._connected = False

    def connect(self) -> bool:
        raise NotImplementedError(
            "MoomooUSAdapter.connect() — Block 5 will wire the "
            "OpenSecTradeContext + unlock_trade flow.")

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def place_order(self, req: OrderRequest) -> OrderResponse:
        raise NotImplementedError(
            "MoomooUSAdapter.place_order() — Block 5 will wire moomoo place_order.")

    def cancel_order(self, broker_order_id: str) -> bool:
        raise NotImplementedError(
            "MoomooUSAdapter.cancel_order() — Block 5.")

    def get_order(self, broker_order_id: str) -> OrderResponse:
        raise NotImplementedError(
            "MoomooUSAdapter.get_order() — Block 5.")

    def list_positions(self) -> list[Position]:
        raise NotImplementedError(
            "MoomooUSAdapter.list_positions() — Block 5.")

    def get_account_snapshot(self) -> AccountSnapshot:
        raise NotImplementedError(
            "MoomooUSAdapter.get_account_snapshot() — Block 5.")

    def get_cash_balance(self) -> float:
        raise NotImplementedError(
            "MoomooUSAdapter.get_cash_balance() — Block 5.")


# --------------------------------------------------------------------- #
# Factory + cached singleton
# --------------------------------------------------------------------- #

# Aliases for backward compatibility — `MoomooAdapter` was the historic name.
MoomooAdapter = MoomooMYAdapter


_CACHED_ADAPTER: Optional[BrokerAdapter] = None
_CACHED_KEY: tuple = ()


def get_broker_adapter(mode: Optional[str] = None) -> BrokerAdapter:
    """Resolve the correct adapter for the active market + execution mode.

    Resolution:
      mode == "NOOP" (default)   → NoopAdapter
      mode == "SIMULATE"         → MoomooUSAdapter(trd_env="SIMULATE") (US only)
      mode == "REAL"             → MoomooUSAdapter(trd_env="REAL")     (US only)
      MY market always           → NoopAdapter (OpenAPI not supported yet)

    `mode` defaults to the value of `scheduler_state.broker_mode` (DB column
    introduced in v3.6 — see db.py SCHEMA migration). Falls back to "NOOP".
    """
    global _CACHED_ADAPTER, _CACHED_KEY

    # Resolve market
    try:
        from market_profiles import active_market_code
        market = active_market_code()
    except Exception:
        market = "MY"

    # Resolve mode
    if mode is None:
        mode = _read_broker_mode_from_db()
    mode = (mode or "NOOP").upper()

    # MY always NOOP today
    if market == "MY":
        mode = "NOOP"

    key = (market, mode)
    if key == _CACHED_KEY and _CACHED_ADAPTER is not None:
        return _CACHED_ADAPTER

    if mode == "NOOP" or market != "US":
        adapter: BrokerAdapter = NoopAdapter()
    else:
        # US SIMULATE / REAL
        import os
        adapter = MoomooUSAdapter(
            host=os.getenv("MOOMOO_HOST", "127.0.0.1"),
            port=int(os.getenv("MOOMOO_PORT", "11111")),
            trd_env=mode,
            unlock_pwd=os.getenv("MOOMOO_TRADING_PWD"),
            security_firm=os.getenv("MOOMOO_SECURITY_FIRM", "FUTUINC"),
        )

    _CACHED_ADAPTER = adapter
    _CACHED_KEY = key
    return adapter


def reset_adapter_cache() -> None:
    """Force a fresh adapter on the next get_broker_adapter() call."""
    global _CACHED_ADAPTER, _CACHED_KEY
    _CACHED_ADAPTER = None
    _CACHED_KEY = ()


def _read_broker_mode_from_db() -> str:
    try:
        from db import connect
        with connect(readonly=True) as c:
            row = c.execute(
                "SELECT broker_mode FROM scheduler_state WHERE id=1"
            ).fetchone()
        if row and row["broker_mode"]:
            return row["broker_mode"]
    except Exception:
        pass
    return "NOOP"


def set_broker_mode(mode: str) -> str:
    """Persist execution mode to scheduler_state. Validates input."""
    mode = (mode or "NOOP").upper().strip()
    if mode not in {"NOOP", "SIMULATE", "REAL"}:
        raise ValueError(f"Invalid broker_mode {mode!r}; expected NOOP/SIMULATE/REAL")
    try:
        from db import connect, myt_iso
        with connect() as c:
            c.execute(
                "UPDATE scheduler_state SET broker_mode=? WHERE id=1",
                (mode,),
            )
    except Exception:
        pass
    reset_adapter_cache()
    return mode


def get_broker_mode() -> str:
    return _read_broker_mode_from_db()


# --------------------------------------------------------------------- #
# Mirror hooks (called from trading_engine.py)
# --------------------------------------------------------------------- #

def _mirror_enabled() -> bool:
    """Cheap guard: should we even try to mirror to a real broker?"""
    if get_broker_mode() == "NOOP":
        return False
    try:
        from market_profiles import active_profile
        if not active_profile().moomoo_available:
            return False
    except Exception:
        return False
    return True


def mirror_entry_to_broker(*, ticker: str, shares: int,
                           fill_price: float,
                           stop_loss: Optional[float] = None,
                           tp1: Optional[float] = None,
                           trade_id: Optional[int] = None) -> None:
    """Called by trading_engine.execute_entry after a successful paper fill.

    In NOOP mode (or MY market): does nothing.
    In SIMULATE/REAL: places a matching order via the active broker adapter.

    Failures are logged but NEVER raise — paper trading is the source of truth
    and the periodic reconciliation cycle will surface any drift.
    """
    if not _mirror_enabled():
        return
    try:
        from logger import get_logger
        log = get_logger("broker_mirror")
    except Exception:
        import logging
        log = logging.getLogger("broker_mirror")
    try:
        adapter = get_broker_adapter()
        req = OrderRequest(
            ticker=ticker, side="BUY", quantity=int(shares),
            order_type="MARKET",
            stop_loss=stop_loss, take_profit=tp1,
            client_order_id=(f"trade-{trade_id}" if trade_id else None),
        )
        resp = adapter.place_order(req)
        log.info(f"mirror_entry: {ticker} qty={shares} → "
                 f"broker_order_id={resp.broker_order_id} status={resp.status}")
    except NotImplementedError as e:
        log.warning(f"mirror_entry skipped (Block 5 pending): {e}")
    except Exception as e:
        log.warning(f"mirror_entry failed (non-fatal): {e}")


def mirror_exit_to_broker(*, ticker: str, shares: int,
                          fill_price: float,
                          trade_id: Optional[int] = None,
                          kind: str = "FULL") -> None:
    """Called by trading_engine.execute_full_exit / execute_partial_exit.

    `kind` is "FULL" or "PARTIAL"; both produce a SELL market order for
    `shares` units. Adapter-side, the broker may use position_list_query()
    to confirm we still hold the position; we don't enforce that here.
    """
    if not _mirror_enabled():
        return
    try:
        from logger import get_logger
        log = get_logger("broker_mirror")
    except Exception:
        import logging
        log = logging.getLogger("broker_mirror")
    try:
        adapter = get_broker_adapter()
        req = OrderRequest(
            ticker=ticker, side="SELL", quantity=int(shares),
            order_type="MARKET",
            client_order_id=(f"trade-{trade_id}-{kind.lower()}" if trade_id else None),
        )
        resp = adapter.place_order(req)
        log.info(f"mirror_exit({kind}): {ticker} qty={shares} → "
                 f"broker_order_id={resp.broker_order_id} status={resp.status}")
    except NotImplementedError as e:
        log.warning(f"mirror_exit skipped (Block 5 pending): {e}")
    except Exception as e:
        log.warning(f"mirror_exit failed (non-fatal): {e}")
