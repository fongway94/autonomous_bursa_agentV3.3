# broker_adapter.py
"""
Broker adapter — abstract interface + concrete Noop / Moomoo implementations.

v3.6 status (Blocks 4 + 5 complete)
-----------------------------------
* NoopAdapter — notification-only mode (default; always used for MY today)
* MoomooMYAdapter — stub kept for forward-compat (when Moomoo adds Bursa)
* MoomooUSAdapter — FULL IMPLEMENTATION (Block 5):
    - OpenSecTradeContext lifecycle with TCP pre-check
    - SIMULATE and REAL trd_env
    - Market + limit, BUY + SELL
    - Account snapshot + positions query
    - Order status mapping (moomoo → our `OrderStatus` literal)
    - Thread-based timeout on every SDK call (handbook rule #15)
    - Every external call wrapped in try/except — never raises into trading_engine
* Factory + mirror hooks (`mirror_entry_to_broker`, `mirror_exit_to_broker`)
  used by trading_engine.execute_entry / execute_full_exit / execute_partial_exit.

Design choices vs the reference repo (lookatwallstreet/WallTrading-Bot-MooMoo-Futu)
----------------------------------------------------------------------------------
* Reference repo opens + closes a fresh OpenSecTradeContext per call.
  We keep a long-lived context for efficiency, with explicit reconnect on
  failure. Background reconnect threads are suppressed by our TCP pre-check.
* Reference repo uses fully blocking calls. We wrap each SDK call in a
  helper thread with a hard deadline — a hung OpenD won't freeze the
  scheduler cycle.
* Reference repo silently swallows errors. We translate every failure
  into an OrderResponse(status="ERROR", error=...) so trading_engine
  can log and continue without exceptions surfacing.
* SIMULATE mode does NOT require `unlock_trade()` (moomoo's simulate
  account is permissionless). REAL mode demands it; we fail loudly if
  MOOMOO_TRADING_PWD is missing in env.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, Optional
import os
import socket
import threading
import time

try:
    from logger import get_logger
    log = get_logger("broker_adapter")
except Exception:  # pragma: no cover — keep importable in tests
    import logging
    log = logging.getLogger("broker_adapter")


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
    ticker: str               # e.g. "0166.KL" (MY) or "AAPL" (US — bare symbol)
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
    ticker: str               # bare symbol (no MY./US. prefix)
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
# Moomoo MY stub (forward-compat for when OpenAPI adds Bursa)
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
# Moomoo US — FULL IMPLEMENTATION (Block 5)
# --------------------------------------------------------------------- #

# Hardcoded per-SDK-call deadline. Watchdog (handbook rule #15) is the
# safety net; this is the first line of defence.
MOOMOO_CALL_TIMEOUT_SEC = 15

# How many seconds we wait for the OpenSecTradeContext constructor to
# finish before declaring the moomoo SDK hung.
MOOMOO_CONNECT_TIMEOUT_SEC = 8


def _moomoo_call_with_timeout(fn, timeout: float | None = None):
    """
    Run `fn()` in a daemon thread; return its result or None on timeout.

    Moomoo's SDK does not accept a `timeout=` kwarg on any of its trade
    methods. This wrapper enforces the deadline ourselves so a hung OpenD
    cannot block the scheduler cycle.

    `timeout` defaults to the module-level MOOMOO_CALL_TIMEOUT_SEC at
    call time (so monkey-patching that constant in tests works).

    Returns (result, error_str). `error_str` is None on success.
    """
    # Resolve timeout DYNAMICALLY so tests can monkey-patch MOOMOO_CALL_TIMEOUT_SEC
    effective_timeout = (timeout if timeout is not None
                          else MOOMOO_CALL_TIMEOUT_SEC)

    box: dict = {"result": None, "error": None}

    def runner():
        try:
            box["result"] = fn()
        except Exception as e:
            box["error"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout=max(0.1, float(effective_timeout)))
    if t.is_alive():
        return None, f"timeout after {effective_timeout}s"
    if box["error"]:
        return None, box["error"]
    return box["result"], None


def _is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """TCP probe — identical pattern to data_provider._is_port_open.

    Prevents OpenSecTradeContext's background reconnect thread from
    spawning when OpenD isn't running. (Streamlit Cloud, headless servers
    without Moomoo Desktop, etc.)
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


# Map moomoo's order status strings → our OrderStatus literal.
# Source: https://openapi.moomoo.com/moomoo-api-doc/en/trade/place-order.html
_MOOMOO_STATUS_MAP = {
    "WAITING_SUBMIT":     "PENDING",
    "SUBMITTING":         "PENDING",
    "SUBMITTED":          "SUBMITTED",
    "FILLED_PART":        "PARTIAL",
    "FILLED_ALL":         "FILLED",
    "CANCELLING_PART":    "PARTIAL",
    "CANCELLING_ALL":     "SUBMITTED",
    "CANCELLED_PART":     "PARTIAL",
    "CANCELLED_ALL":      "CANCELLED",
    "FAILED":             "REJECTED",
    "DISABLED":           "REJECTED",
    "DELETED":            "CANCELLED",
    "SUBMIT_FAILED":      "REJECTED",
    "TIMEOUT":            "ERROR",
}


def _map_moomoo_status(raw_status: str) -> OrderStatus:
    if not raw_status:
        return "ERROR"
    return _MOOMOO_STATUS_MAP.get(raw_status.upper(), "ERROR")  # type: ignore[return-value]


class MoomooUSAdapter(BrokerAdapter):
    """
    Moomoo OpenAPI adapter for US market (TrdMarket.US).

    Modes:
      - SIMULATE: paper-trading via moomoo's simulate account (no unlock needed)
      - REAL:     live money; requires MOOMOO_TRADING_PWD env var

    Lifecycle:
      adapter = MoomooUSAdapter(trd_env="SIMULATE", unlock_pwd=...)
      adapter.connect()        # idempotent; True if OpenD reachable
      adapter.place_order(req) # uses long-lived context
      adapter.list_positions()
      adapter.disconnect()     # called on shutdown

    Thread safety:
      All methods acquire `_lock`; the long-lived context is not safe to
      use concurrently across threads.

    Failure semantics:
      Every public method NEVER raises into trading_engine. On any error
      it returns an OrderResponse(status="ERROR", error=...) or an empty
      AccountSnapshot/list. Errors are logged at WARN level.
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
        self._env = (trd_env or "SIMULATE").upper()
        self._unlock_pwd = unlock_pwd
        self._security_firm = security_firm
        self._connected = False
        self._unlocked = False
        self._ctx = None
        self._lock = threading.RLock()
        self._last_error: Optional[str] = None

    # -----------------------------------------------------------------
    # Connection lifecycle
    # -----------------------------------------------------------------

    def connect(self) -> bool:
        """Establish session. Idempotent — safe to call repeatedly.

        Sequence:
          1. TCP pre-check on host:port (instant fail if no listener)
          2. Construct OpenSecTradeContext (wrapped in timeout)
          3. If REAL mode: unlock_trade(pwd) — REQUIRED, fails if no pwd
          4. SIMULATE mode: skip unlock (not needed)

        Returns True on success. Sets self._last_error on failure.
        """
        with self._lock:
            if self._connected:
                return True

            # 1. TCP pre-check
            if not _is_port_open(self._host, self._port, timeout=1.0):
                self._last_error = (
                    f"OpenD port {self._host}:{self._port} not listening — "
                    "is Moomoo OpenD running on the host?"
                )
                log.warning(f"MoomooUSAdapter.connect: {self._last_error}")
                return False

            # 2. Resolve SDK and construct context
            try:
                from moomoo import (OpenSecTradeContext, TrdMarket,
                                     SecurityFirm, RET_OK)
            except ImportError as e:
                self._last_error = f"moomoo-api not installed: {e}"
                log.warning(f"MoomooUSAdapter.connect: {self._last_error}")
                return False

            sec_firm_enum = getattr(SecurityFirm, self._security_firm,
                                     SecurityFirm.FUTUINC)

            def _construct():
                return OpenSecTradeContext(
                    filter_trdmarket=TrdMarket.US,
                    host=self._host,
                    port=self._port,
                    security_firm=sec_firm_enum,
                )

            ctx, err = _moomoo_call_with_timeout(
                _construct, timeout=MOOMOO_CONNECT_TIMEOUT_SEC)
            if err is not None or ctx is None:
                self._last_error = f"OpenSecTradeContext init failed: {err}"
                log.warning(f"MoomooUSAdapter.connect: {self._last_error}")
                return False

            self._ctx = ctx

            # 3. Unlock if REAL mode
            if self._env == "REAL":
                if not self._unlock_pwd:
                    self._last_error = (
                        "REAL mode requires MOOMOO_TRADING_PWD env var — "
                        "set it in Streamlit Secrets or your shell."
                    )
                    log.error(f"MoomooUSAdapter.connect: {self._last_error}")
                    self._close_ctx()
                    return False

                def _unlock():
                    return self._ctx.unlock_trade(self._unlock_pwd)

                result, err = _moomoo_call_with_timeout(_unlock)
                if err is not None:
                    self._last_error = f"unlock_trade timed out / errored: {err}"
                    log.error(f"MoomooUSAdapter.connect: {self._last_error}")
                    self._close_ctx()
                    return False
                ret, data = result
                if ret != RET_OK:
                    self._last_error = f"unlock_trade rejected: {data}"
                    log.error(f"MoomooUSAdapter.connect: {self._last_error}")
                    self._close_ctx()
                    return False
                self._unlocked = True
                log.info("MoomooUSAdapter: REAL trading unlocked")
            else:
                # SIMULATE: no unlock needed
                self._unlocked = True
                log.info("MoomooUSAdapter: SIMULATE mode (no unlock required)")

            self._connected = True
            self._last_error = None
            return True

    def _close_ctx(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:
                pass
        self._ctx = None
        self._connected = False
        self._unlocked = False

    def disconnect(self) -> None:
        with self._lock:
            self._close_ctx()

    def is_connected(self) -> bool:
        return self._connected and self._ctx is not None

    def last_error(self) -> Optional[str]:
        """Diagnostic — what went wrong on the most recent failure?"""
        return self._last_error

    # -----------------------------------------------------------------
    # Ticker normalisation
    # -----------------------------------------------------------------

    @staticmethod
    def _to_moomoo_code(ticker: str) -> str:
        """Bare US symbol → 'US.AAPL'. Already-prefixed strings pass through."""
        if not ticker:
            return ""
        t = ticker.strip().upper()
        if t.startswith(("US.", "MY.", "HK.", "SG.")):
            return t
        return f"US.{t}"

    @staticmethod
    def _strip_moomoo_prefix(code: str) -> str:
        """'US.AAPL' → 'AAPL'. Bare symbols pass through."""
        if not code:
            return ""
        if "." in code:
            return code.split(".", 1)[1]
        return code

    # -----------------------------------------------------------------
    # Order placement
    # -----------------------------------------------------------------

    def place_order(self, req: OrderRequest) -> OrderResponse:
        """Place a market or limit order.

        Notes:
          - For MARKET orders, moomoo still requires a `price`. We pass 0
            (the SDK accepts this for MARKET) — fill is at NBBO.
          - We do NOT enable `fill_outside_rth` by default. To trade in
            extended hours, build the OrderRequest with `order_type='LIMIT'`
            and supply a limit_price; even then RTH-only is safer.
        """
        with self._lock:
            if not self._ensure_connected():
                return OrderResponse(
                    broker_order_id="",
                    status="ERROR",
                    error=self._last_error or "not connected")

            try:
                from moomoo import (TrdSide, OrderType as _MOT,
                                     TrdEnv, RET_OK)
            except ImportError as e:
                return OrderResponse(broker_order_id="", status="ERROR",
                                      error=f"moomoo SDK gone missing: {e}")

            code = self._to_moomoo_code(req.ticker)
            side = TrdSide.BUY if req.side == "BUY" else TrdSide.SELL
            order_type = _MOT.MARKET if req.order_type == "MARKET" else _MOT.NORMAL
            trd_env = TrdEnv.REAL if self._env == "REAL" else TrdEnv.SIMULATE
            price = float(req.limit_price) if req.limit_price is not None else 0.0

            def _place():
                return self._ctx.place_order(
                    price=price,
                    qty=int(req.quantity),
                    code=code,
                    trd_side=side,
                    order_type=order_type,
                    trd_env=trd_env,
                )

            result, err = _moomoo_call_with_timeout(_place)
            if err is not None:
                self._last_error = f"place_order timed out / errored: {err}"
                log.warning(
                    f"MoomooUSAdapter.place_order({req.ticker}): {self._last_error}")
                return OrderResponse(broker_order_id="", status="ERROR",
                                      error=self._last_error)
            ret, data = result
            if ret != RET_OK:
                err_str = str(data)[:300]
                log.warning(
                    f"MoomooUSAdapter.place_order({req.ticker}) rejected: {err_str}")
                return OrderResponse(broker_order_id="", status="REJECTED",
                                      error=err_str)

            # `data` is a DataFrame with at least 'order_id', 'order_status'
            try:
                row = data.iloc[0]
                broker_id = str(row.get("order_id") or "")
                raw_status = str(row.get("order_status") or "SUBMITTED")
                qty_filled = int(float(row.get("dealt_qty") or 0))
                avg_price = float(row.get("dealt_avg_price") or 0.0)
            except Exception as e:
                log.warning(f"MoomooUSAdapter.place_order parse error: {e}")
                return OrderResponse(broker_order_id="", status="ERROR",
                                      error=f"response parse failed: {e}")

            mapped = _map_moomoo_status(raw_status)
            log.info(
                f"MoomooUSAdapter.place_order: {req.ticker} qty={req.quantity} "
                f"side={req.side} → order_id={broker_id} status={mapped}")
            return OrderResponse(
                broker_order_id=broker_id, status=mapped,
                filled_quantity=qty_filled, avg_fill_price=avg_price,
                raw={"moomoo_raw_status": raw_status})

    # -----------------------------------------------------------------
    # Order management
    # -----------------------------------------------------------------

    def cancel_order(self, broker_order_id: str) -> bool:
        with self._lock:
            if not self._ensure_connected():
                return False
            try:
                from moomoo import (ModifyOrderOp, TrdEnv, RET_OK)
            except ImportError:
                return False
            trd_env = TrdEnv.REAL if self._env == "REAL" else TrdEnv.SIMULATE

            def _cancel():
                return self._ctx.modify_order(
                    ModifyOrderOp.CANCEL, broker_order_id, 0, 0,
                    trd_env=trd_env)

            result, err = _moomoo_call_with_timeout(_cancel)
            if err is not None:
                log.warning(
                    f"MoomooUSAdapter.cancel_order({broker_order_id}): {err}")
                return False
            ret, _ = result
            return ret == RET_OK

    def get_order(self, broker_order_id: str) -> OrderResponse:
        with self._lock:
            if not self._ensure_connected():
                return OrderResponse(broker_order_id=broker_order_id,
                                      status="ERROR",
                                      error=self._last_error or "not connected")
            try:
                from moomoo import (TrdEnv, RET_OK)
            except ImportError as e:
                return OrderResponse(broker_order_id=broker_order_id,
                                      status="ERROR", error=str(e))
            trd_env = TrdEnv.REAL if self._env == "REAL" else TrdEnv.SIMULATE

            def _query():
                return self._ctx.order_list_query(order_id=broker_order_id,
                                                   trd_env=trd_env)

            result, err = _moomoo_call_with_timeout(_query)
            if err is not None:
                return OrderResponse(broker_order_id=broker_order_id,
                                      status="ERROR", error=err)
            ret, data = result
            if ret != RET_OK or data is None or data.empty:
                return OrderResponse(broker_order_id=broker_order_id,
                                      status="ERROR",
                                      error=f"query returned ret={ret}")
            try:
                row = data.iloc[0]
                raw_status = str(row.get("order_status") or "")
                qty_filled = int(float(row.get("dealt_qty") or 0))
                avg_price = float(row.get("dealt_avg_price") or 0.0)
            except Exception as e:
                return OrderResponse(broker_order_id=broker_order_id,
                                      status="ERROR", error=str(e))
            return OrderResponse(
                broker_order_id=broker_order_id,
                status=_map_moomoo_status(raw_status),
                filled_quantity=qty_filled, avg_fill_price=avg_price,
                raw={"moomoo_raw_status": raw_status})

    # -----------------------------------------------------------------
    # Account + positions
    # -----------------------------------------------------------------

    def get_account_snapshot(self) -> AccountSnapshot:
        """Pulls accinfo_query. Returns empty snapshot on any failure."""
        with self._lock:
            empty = AccountSnapshot(cash=0.0, total_assets=0.0,
                                     market_value=0.0, currency="USD")
            if not self._ensure_connected():
                return empty
            try:
                from moomoo import (TrdEnv, RET_OK)
            except ImportError:
                return empty
            trd_env = TrdEnv.REAL if self._env == "REAL" else TrdEnv.SIMULATE

            def _query():
                return self._ctx.accinfo_query(trd_env=trd_env)

            result, err = _moomoo_call_with_timeout(_query)
            if err is not None:
                self._last_error = f"accinfo_query: {err}"
                log.warning(f"MoomooUSAdapter.get_account_snapshot: {err}")
                return empty
            ret, data = result
            if ret != RET_OK or data is None or data.empty:
                log.warning(
                    f"MoomooUSAdapter.get_account_snapshot: ret={ret} data={data}")
                return empty
            try:
                row = data.iloc[0]
                # Per moomoo docs: us_cash for USD margin/cash; total_assets,
                # market_val are top-line. Some SDK versions use 'cash' instead.
                cash = float(row.get("us_cash", row.get("cash", 0.0)) or 0.0)
                total = float(row.get("total_assets", 0.0) or 0.0)
                mkt_val = float(row.get("market_val", 0.0) or 0.0)
                return AccountSnapshot(
                    cash=round(cash, 2),
                    total_assets=round(total, 2),
                    market_value=round(mkt_val, 2),
                    currency="USD",
                    raw=row.to_dict(),
                )
            except Exception as e:
                log.warning(
                    f"MoomooUSAdapter.get_account_snapshot parse error: {e}")
                return empty

    def get_cash_balance(self) -> float:
        return self.get_account_snapshot().cash

    def list_positions(self) -> list[Position]:
        with self._lock:
            if not self._ensure_connected():
                return []
            try:
                from moomoo import (TrdEnv, RET_OK)
            except ImportError:
                return []
            trd_env = TrdEnv.REAL if self._env == "REAL" else TrdEnv.SIMULATE

            def _query():
                return self._ctx.position_list_query(trd_env=trd_env)

            result, err = _moomoo_call_with_timeout(_query)
            if err is not None:
                log.warning(f"MoomooUSAdapter.list_positions: {err}")
                return []
            ret, data = result
            if ret != RET_OK or data is None:
                return []
            positions: list[Position] = []
            try:
                for _, row in data.iterrows():
                    code = str(row.get("code") or "")
                    qty = int(float(row.get("qty") or 0))
                    if qty <= 0:
                        continue
                    positions.append(Position(
                        ticker=self._strip_moomoo_prefix(code),
                        quantity=qty,
                        avg_cost=float(row.get("cost_price") or 0.0),
                        current_price=float(row.get("nominal_price") or 0.0),
                        unrealized_pnl=float(row.get("pl_val") or 0.0),
                    ))
            except Exception as e:
                log.warning(f"MoomooUSAdapter.list_positions parse: {e}")
            return positions

    # -----------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------

    def _ensure_connected(self) -> bool:
        """Re-establish session if dropped; called at the top of every method."""
        if self.is_connected():
            return True
        return self.connect()


# --------------------------------------------------------------------- #
# Factory + cached singleton
# --------------------------------------------------------------------- #

# Backward-compat alias — old code imported `MoomooAdapter` (the MY stub).
MoomooAdapter = MoomooMYAdapter


_CACHED_ADAPTER: Optional[BrokerAdapter] = None
_CACHED_KEY: tuple = ()
_FACTORY_LOCK = threading.RLock()


def get_broker_adapter(mode: Optional[str] = None) -> BrokerAdapter:
    """Resolve the correct adapter for the active market + execution mode.

    Resolution:
      mode == "NOOP" (default)   → NoopAdapter
      mode == "SIMULATE"         → MoomooUSAdapter(trd_env="SIMULATE") (US only)
      mode == "REAL"             → MoomooUSAdapter(trd_env="REAL")     (US only)
      MY market always           → NoopAdapter (OpenAPI not yet supported)

    `mode` defaults to the value of `scheduler_state.broker_mode` (DB column
    introduced in v3.6). Falls back to "NOOP".

    Cached across calls — `reset_adapter_cache()` to drop.
    """
    global _CACHED_ADAPTER, _CACHED_KEY

    try:
        from market_profiles import active_market_code
        market = active_market_code()
    except Exception:
        market = "MY"

    if mode is None:
        mode = _read_broker_mode_from_db()
    mode = (mode or "NOOP").upper()

    # MY always NOOP today
    if market == "MY":
        mode = "NOOP"

    key = (market, mode)
    with _FACTORY_LOCK:
        if key == _CACHED_KEY and _CACHED_ADAPTER is not None:
            return _CACHED_ADAPTER

        if mode == "NOOP" or market != "US":
            adapter: BrokerAdapter = NoopAdapter()
        else:
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
    """Force a fresh adapter on the next get_broker_adapter() call.

    Also disconnects the previous adapter cleanly. Called when broker_mode
    changes via the Settings UI.
    """
    global _CACHED_ADAPTER, _CACHED_KEY
    with _FACTORY_LOCK:
        if _CACHED_ADAPTER is not None:
            try:
                _CACHED_ADAPTER.disconnect()
            except Exception:
                pass
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
    """Persist execution mode to scheduler_state. Validates input.

    Triggers `reset_adapter_cache()` so the next get_broker_adapter()
    returns a fresh instance with the new mode.

    Calls init_db() first to ensure the active market's DB has the
    scheduler_state row (handles the "first time using this market" case
    where db.py was imported before MARKET_MODE was set).
    """
    mode = (mode or "NOOP").upper().strip()
    if mode not in {"NOOP", "SIMULATE", "REAL"}:
        raise ValueError(f"Invalid broker_mode {mode!r}; expected NOOP/SIMULATE/REAL")
    try:
        # Ensure scheduler_state row exists for the CURRENTLY-active market's DB.
        from db import init_db, connect
        init_db()
        with connect() as c:
            c.execute(
                "UPDATE scheduler_state SET broker_mode=? WHERE id=1",
                (mode,),
            )
    except Exception as e:
        log.warning(f"set_broker_mode persistence failed: {e}")
    reset_adapter_cache()
    return mode


def get_broker_mode() -> str:
    return _read_broker_mode_from_db()


# --------------------------------------------------------------------- #
# Diagnostics — surfaced in Settings tab (Block 7)
# --------------------------------------------------------------------- #

def adapter_health() -> dict:
    """Lightweight status dict for the UI. Does NOT trigger a connect."""
    try:
        from market_profiles import active_market_code, active_profile
        market = active_market_code()
        moomoo_supported = bool(active_profile().moomoo_available)
    except Exception:
        market = "MY"
        moomoo_supported = False

    mode = get_broker_mode()
    a = _CACHED_ADAPTER
    return {
        "market": market,
        "mode": mode,
        "moomoo_available_for_market": moomoo_supported,
        "adapter_name": a.name if a else "uncached",
        "connected": bool(a and a.is_connected()),
        "last_error": getattr(a, "_last_error", None) if a else None,
        "openD_host": os.getenv("MOOMOO_HOST", "127.0.0.1"),
        "openD_port": int(os.getenv("MOOMOO_PORT", "11111")),
        "real_pwd_configured": bool(os.getenv("MOOMOO_TRADING_PWD")),
    }


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
    In SIMULATE/REAL: places a matching MARKET BUY via the active adapter.

    Failures are logged but NEVER raise — paper trading is the source of truth
    and the periodic reconciliation cycle will surface any drift.
    """
    if not _mirror_enabled():
        return
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
        if resp.status in ("REJECTED", "ERROR"):
            # Best-effort alert via existing notifier (silent failure if no creds)
            try:
                from notifier import send_telegram
                send_telegram(
                    f"⚠️ Broker mirror_entry failed for {ticker}\n"
                    f"shares={shares}, mode={get_broker_mode()}\n"
                    f"reason: {resp.error}\n"
                    f"Paper trade #{trade_id} stands; reconciliation will catch drift."
                )
            except Exception:
                pass
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
        adapter = get_broker_adapter()
        req = OrderRequest(
            ticker=ticker, side="SELL", quantity=int(shares),
            order_type="MARKET",
            client_order_id=(f"trade-{trade_id}-{kind.lower()}" if trade_id else None),
        )
        resp = adapter.place_order(req)
        log.info(f"mirror_exit({kind}): {ticker} qty={shares} → "
                 f"broker_order_id={resp.broker_order_id} status={resp.status}")
        if resp.status in ("REJECTED", "ERROR"):
            try:
                from notifier import send_telegram
                send_telegram(
                    f"⚠️ Broker mirror_exit({kind}) failed for {ticker}\n"
                    f"shares={shares}, mode={get_broker_mode()}\n"
                    f"reason: {resp.error}\n"
                    f"You may need to close position {ticker} manually in Moomoo."
                )
            except Exception:
                pass
    except Exception as e:
        log.warning(f"mirror_exit failed (non-fatal): {e}")
