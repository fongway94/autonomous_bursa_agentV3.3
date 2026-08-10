# db.py
"""
SQLite persistence layer — multi-market aware (v3.6).

Single connection-per-call pattern with WAL mode for concurrent read safety.
Replaces the scattered JSON files that previously caused race conditions
in the multi-threaded scheduler.

Multi-market (v3.6)
-------------------
Each market has its OWN database file:
    ~/.bursa_agent_data/bursa_agent_MY.db
    ~/.bursa_agent_data/bursa_agent_US.db

The active market is resolved by `market_profiles.active_profile()` which
reads (in order): env var MARKET_MODE, then a small text-file marker
under DATA_DIR. NO cross-DB joins anywhere — each market is fully isolated
so switching markets cannot leak Bursa cash math into US trades or vice
versa.

Backward compatibility:
    If `~/.bursa_agent_data/bursa_agent.db` exists (the legacy v3.3 path)
    and the active market is MY, we rename it to `bursa_agent_MY.db` on
    first init so existing deployments seamlessly upgrade. Existing US
    deployments (there are none in the wild yet) start fresh.

Tables (unchanged from v3.3)
----------------------------
trades                — full trade journal (entry + exits + reasoning)
partial_exits         — per-TP partial exit records
account               — virtual paper-trade account state (single row)
parameters            — current scanner / risk params (json blob)
parameter_history     — every parameter change with timestamp + reason
bias_state            — strategy / sector bias multipliers (single row blob)
bias_history          — every bias update with before/after
state_priors          — Bayesian per-(state,action) Beta(alpha,beta) priors
learning_events       — high-level learning journal
scheduler_log         — robo-trader heartbeat + scheduled-run records
scheduler_state       — single-row scheduler status (last/next run, running flag,
                        cycle_started_at for watchdog — v3.1.10)
trade_log             — append-only trade execution audit log
data_quality_log      — issues detected during data fetch
scan_cache            — most recent screener output (json)
meta                  — key/value store for cross-container state (v3.1.9)
custom_watchlist      — user-added tickers (v3.1.9, previously in watchlist.py only)
"""

import sqlite3
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

HOME_DIR = os.path.expanduser("~")
DATA_DIR = os.path.join(HOME_DIR, ".bursa_agent_data")
os.makedirs(DATA_DIR, exist_ok=True)

# Legacy v3.3 single-DB path (pre multi-market). Kept for migration only.
_LEGACY_DB_PATH = os.path.join(DATA_DIR, "bursa_agent.db")


def _resolve_db_path() -> str:
    """Active DB path, dispatched on (market_code, trading_mode).

    v3.7: The DB file splits on BOTH the active market AND the active
    trading mode. SWING and INTRADAY have separate brains and must never
    cross-contaminate their Bayesian priors.

    Paths:
        ~/.bursa_agent_data/bursa_agent_MY_SWING.db
        ~/.bursa_agent_data/bursa_agent_MY_INTRADAY.db
        ~/.bursa_agent_data/bursa_agent_US_SWING.db      (live today)
        ~/.bursa_agent_data/bursa_agent_US_INTRADAY.db   (live today)

    v3.6 back-compat: if a caller (typically a v3.3 test) has monkey-patched
    `db.DB_PATH` to a custom path that DOESN'T match any auto-derived path,
    honour it. Override detection is by BASENAME — foreign filenames
    (e.g. `fake.db`, `test.db`, `restored.db`) are always real overrides;
    auto-computed basenames (always `bursa_agent_<CODE>_<MODE>.db` or the
    legacy `bursa_agent.db`) are never overrides.
    """
    overridden = globals().get("DB_PATH")
    try:
        from market_profiles import active_market_code, active_trading_mode, available_markets
        code = active_market_code()
        mode = active_trading_mode()
        real = os.path.join(DATA_DIR, f"bursa_agent_{code}_{mode}.db")
        # Basenames that are auto-computed (never a deliberate override),
        # regardless of which directory they live in.
        auto_basenames = {
            f"bursa_agent_{c}_{m}.db"
            for c in available_markets()
            for m in ("SWING", "INTRADAY")
        }
        auto_basenames.add(os.path.basename(_LEGACY_DB_PATH))  # bursa_agent.db
    except Exception:
        return _LEGACY_DB_PATH
    if overridden and os.path.basename(overridden) not in auto_basenames:
        # Foreign filename → a real test fixture override; respect it.
        return overridden
    return real


def _migrate_legacy_db_if_needed() -> None:
    """One-shot rename of legacy `bursa_agent.db` → `bursa_agent_MY_SWING.db`.

    The legacy file was the single-DB era (v3.3–v3.5). Today every market
    has two DB files (SWING + INTRADAY). The legacy MY data becomes MY_SWING
    so existing deployments migrate cleanly.

    Only runs if:
      * legacy file exists
      * active market is MY
      * no `bursa_agent_MY_SWING.db` yet
    Otherwise a no-op.
    """
    try:
        from market_profiles import active_market_code
        if active_market_code() != "MY":
            return
    except Exception:
        return
    target = os.path.join(DATA_DIR, "bursa_agent_MY_SWING.db")
    if os.path.exists(_LEGACY_DB_PATH) and not os.path.exists(target):
        try:
            os.rename(_LEGACY_DB_PATH, target)
            # Also move any -wal / -shm sidecars
            for suffix in ("-wal", "-shm"):
                src = _LEGACY_DB_PATH + suffix
                dst = target + suffix
                if os.path.exists(src) and not os.path.exists(dst):
                    try:
                        os.rename(src, dst)
                    except Exception:
                        pass
        except Exception as e:
            print(f"[db] legacy migration skipped: {e}")


# DB_PATH is now COMPUTED, not a constant. Old code that imported DB_PATH at
# module load time will still see the right path (we resolve below after
# migration). New code should prefer `current_db_path()` which always reflects
# the latest active market.
_migrate_legacy_db_if_needed()
DB_PATH = _resolve_db_path()


def current_db_path() -> str:
    """Always-fresh DB path. Use this in long-lived modules (persistence)."""
    return _resolve_db_path()


# Per-process locks keyed by DB path so two markets don't serialise on each other.
_WRITE_LOCKS_BY_PATH: dict[str, threading.RLock] = {}
_LOCKS_REGISTRY_LOCK = threading.Lock()


def _lock_for_path(path: str) -> threading.RLock:
    with _LOCKS_REGISTRY_LOCK:
        lock = _WRITE_LOCKS_BY_PATH.get(path)
        if lock is None:
            lock = threading.RLock()
            _WRITE_LOCKS_BY_PATH[path] = lock
        return lock


def _migrate_v36_db_if_needed() -> None:
    """One-shot rename of v3.6-era per-market DBs to the v3.7 (market, mode) scheme.

    v3.6 produced one file per market:
        bursa_agent_MY.db  →  bursa_agent_MY_SWING.db
        bursa_agent_US.db  →  bursa_agent_US_SWING.db

    We iterate ALL known markets so that both MY and US are migrated on the
    first boot, regardless of which market is currently active.  This is safe
    to call multiple times (checks existence before renaming).
    """
    try:
        from market_profiles import available_markets
        markets = available_markets()
    except Exception:
        markets = ["MY", "US"]

    for code in markets:
        legacy = os.path.join(DATA_DIR, f"bursa_agent_{code}.db")
        target = os.path.join(DATA_DIR, f"bursa_agent_{code}_SWING.db")
        if os.path.exists(legacy) and not os.path.exists(target):
            try:
                os.rename(legacy, target)
                for suffix in ("-wal", "-shm"):
                    src = legacy + suffix
                    dst = target + suffix
                    if os.path.exists(src) and not os.path.exists(dst):
                        try:
                            os.rename(src, dst)
                        except Exception:
                            pass
            except Exception as e:
                print(f"[db] v3.6→v3.7 migration skipped for {code}: {e}")


_migrate_v36_db_if_needed()

# Backward-compat: expose a `_WRITE_LOCK` that resolves to the active DB's
# lock. Some test fixtures import it directly.
class _ActiveLockProxy:
    def __enter__(self):
        self._lock = _lock_for_path(current_db_path())
        self._lock.acquire()
        return self
    def __exit__(self, *exc):
        self._lock.release()
    def acquire(self, *a, **kw):
        _lock_for_path(current_db_path()).acquire(*a, **kw)
    def release(self):
        _lock_for_path(current_db_path()).release()


_WRITE_LOCK = _ActiveLockProxy()


def get_myt_now():
    return datetime.now(timezone(timedelta(hours=8)))


def myt_iso(dt=None):
    if dt is None:
        dt = get_myt_now()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# CONNECTION
# ---------------------------------------------------------------------------

@contextmanager
def connect(readonly: bool = False):
    """
    Yields a sqlite3.Connection to the ACTIVE market's DB.

    Always uses WAL. Writes are wrapped in a per-DB-path RLock so concurrent
    threads (Streamlit re-renders + scheduler) never collide — and so two
    markets running side-by-side don't serialise on each other.
    """
    db_path = current_db_path()

    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
        return

    lock = _lock_for_path(db_path)
    with lock:
        conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
        finally:
            conn.close()


def db_health(path: str | None = None) -> dict:
    """Fast structural health check of a SQLite file (read-only, no locks).

    This is the detector for the classic Streamlit Cloud failure mode where
    the container is killed mid-write and the DB file (or its -wal/-shm
    sidecars) ends up corrupt — every later read then raises
    ``sqlite3.DatabaseError: database disk image is malformed``.

    Returns::

        {"healthy": bool, "error": str | None, "path": str, "missing": bool}

    A missing file counts as healthy — there is nothing to repair and
    ``init_db()`` will create it on first use.
    """
    p = path or current_db_path()
    if not os.path.exists(p):
        return {"healthy": True, "error": None, "path": p, "missing": True}
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5.0)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
        if row and row[0] == "ok":
            return {"healthy": True, "error": None, "path": p, "missing": False}
        return {"healthy": False,
                "error": row[0] if row else "PRAGMA quick_check failed",
                "path": p, "missing": False}
    except Exception as e:
        return {"healthy": False, "error": str(e), "path": p, "missing": False}


# ---------------------------------------------------------------------------
# SCHEMA
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    name            TEXT,
    sector          TEXT,
    signal_type     TEXT,
    entry_price     REAL NOT NULL,
    stop_loss       REAL NOT NULL,
    tp1             REAL,
    tp2             REAL,
    tp3             REAL,
    shares          INTEGER NOT NULL,
    lots            INTEGER,
    cost            REAL,
    fee             REAL,
    total_outlay    REAL,
    risk_per_share  REAL,
    actual_risk_pct REAL,
    status          TEXT NOT NULL DEFAULT 'ACTIVE',
    phase           TEXT DEFAULT 'FULL',
    outcome         TEXT,
    logged_at       TEXT NOT NULL,
    closed_at       TEXT,
    execution_type  TEXT DEFAULT 'MANUAL',
    market_regime   TEXT,
    regime_conviction REAL,
    confidence_score REAL,
    entry_reasoning TEXT,
    entry_indicators_json TEXT,
    trailing_stop   REAL,
    highest_price   REAL,
    lowest_price    REAL,
    mae_pct         REAL DEFAULT 0,
    mfe_pct         REAL DEFAULT 0,
    unrealized_pnl  REAL DEFAULT 0,
    realized_pnl    REAL DEFAULT 0,
    closed_pnl      REAL,
    exit_price      REAL,
    exit_type       TEXT,              -- v3.7: TP3/CLIMAX/SL/TP2/TP1/TIME/MANUAL
    shares_remaining INTEGER NOT NULL,
    slippage_pct    REAL DEFAULT 0,
    notes           TEXT DEFAULT '',
    tags_json       TEXT DEFAULT '[]',
    cumulative_split_factor REAL DEFAULT 1.0  -- v3.5: product of all split ratios applied to this trade
);

CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_logged_at ON trades(logged_at);

CREATE TABLE IF NOT EXISTS partial_exits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER NOT NULL,
    tp_level        TEXT,
    shares_closed   INTEGER,
    exit_price      REAL,
    exit_type       TEXT,              -- v3.7: TP3/CLIMAX/SL/TP2/TP1/TIME/MANUAL
    pnl_rm          REAL,
    net_pnl_after_fees REAL,
    exit_at         TEXT,
    reason          TEXT,
    FOREIGN KEY (trade_id) REFERENCES trades(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_partials_trade ON partial_exits(trade_id);

CREATE TABLE IF NOT EXISTS account (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    initial_capital   REAL NOT NULL,
    cash_balance      REAL NOT NULL,
    total_equity      REAL NOT NULL,
    last_updated      TEXT
);

CREATE TABLE IF NOT EXISTS parameters (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    payload     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parameter_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at  TEXT NOT NULL,
    source      TEXT,
    before_json TEXT,
    after_json  TEXT,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_paramhist_at ON parameter_history(changed_at);

CREATE TABLE IF NOT EXISTS bias_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    payload         TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bias_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at  TEXT NOT NULL,
    field       TEXT,
    before_val  REAL,
    after_val   REAL,
    trade_id    INTEGER,
    outcome     TEXT
);
CREATE INDEX IF NOT EXISTS idx_biashist_at ON bias_history(changed_at);

CREATE TABLE IF NOT EXISTS state_priors (
    state_id    INTEGER NOT NULL,
    action      TEXT NOT NULL,
    alpha       REAL NOT NULL DEFAULT 1.0,
    beta        REAL NOT NULL DEFAULT 1.0,
    n_trades    INTEGER NOT NULL DEFAULT 0,
    total_r     REAL NOT NULL DEFAULT 0,
    last_updated TEXT,
    PRIMARY KEY (state_id, action)
);

CREATE TABLE IF NOT EXISTS learning_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    description     TEXT,
    changes_json    TEXT,
    metrics_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_learnev_at ON learning_events(timestamp);

CREATE TABLE IF NOT EXISTS scheduler_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    level           TEXT NOT NULL DEFAULT 'INFO',
    event           TEXT NOT NULL,
    message         TEXT,
    duration_sec    REAL,
    payload_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_schedlog_at ON scheduler_log(timestamp);

CREATE TABLE IF NOT EXISTS scheduler_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    running         INTEGER NOT NULL DEFAULT 0,
    interval_sec    INTEGER NOT NULL DEFAULT 3600,
    last_run_at     TEXT,
    next_run_at     TEXT,
    last_heartbeat  TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    autotrade_enabled INTEGER NOT NULL DEFAULT 1,
    autoexit_enabled  INTEGER NOT NULL DEFAULT 1,
    kill_switch     INTEGER NOT NULL DEFAULT 0,
    exploration_mode INTEGER NOT NULL DEFAULT 1,
    exploration_trades_target INTEGER NOT NULL DEFAULT 50,
    owner_pid INTEGER NOT NULL DEFAULT 0,
    cycle_started_at TEXT           -- v3.1.10: set when a cycle begins,
                                    -- cleared when it ends; powers the
                                    -- runaway-cycle watchdog.
);

CREATE TABLE IF NOT EXISTS trade_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    event           TEXT NOT NULL,
    trade_id        INTEGER,
    ticker          TEXT,
    actor           TEXT NOT NULL DEFAULT 'USER',
    payload_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tradelog_at ON trade_log(timestamp);

CREATE TABLE IF NOT EXISTS data_quality_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    ticker          TEXT,
    severity        TEXT,
    issue           TEXT,
    detail_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_dq_at ON data_quality_log(timestamp);

CREATE TABLE IF NOT EXISTS scan_cache (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    payload     TEXT NOT NULL,
    market_regime_json TEXT,
    updated_at  TEXT NOT NULL
);

-- v3.1: Live trigger / notification system

CREATE TABLE IF NOT EXISTS live_trigger_config (
    id                       INTEGER PRIMARY KEY CHECK (id = 1),
    enabled                  INTEGER NOT NULL DEFAULT 0,
    min_confidence           REAL    NOT NULL DEFAULT 70.0,
    exploit_mode_only        INTEGER NOT NULL DEFAULT 0,
    alert_on_entry           INTEGER NOT NULL DEFAULT 1,
    alert_on_full_exit       INTEGER NOT NULL DEFAULT 1,
    alert_on_stop_loss       INTEGER NOT NULL DEFAULT 1,
    alert_on_trailing_stop   INTEGER NOT NULL DEFAULT 1,
    alert_on_partial_exit    INTEGER NOT NULL DEFAULT 0,
    alert_on_risk_rejected   INTEGER NOT NULL DEFAULT 0,
    telegram_enabled         INTEGER NOT NULL DEFAULT 1,
    email_enabled            INTEGER NOT NULL DEFAULT 0,
    email_recipients         TEXT    NOT NULL DEFAULT '',
    actor_filter             TEXT    NOT NULL DEFAULT 'AGENT',
    updated_at               TEXT
);

CREATE TABLE IF NOT EXISTS alert_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    trade_id       INTEGER,
    ticker         TEXT,
    channel        TEXT,
    status         TEXT,
    message        TEXT,
    error          TEXT,
    payload_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_at ON alert_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_alert_trade ON alert_log(trade_id);

-- v3.1.1: Idempotency guard for daily maintenance tasks
CREATE TABLE IF NOT EXISTS maintenance_state (
    task_name      TEXT PRIMARY KEY,
    last_ran_date  TEXT NOT NULL,   -- YYYY-MM-DD (MYT)
    last_ran_at    TEXT NOT NULL,   -- full timestamp
    owner_pid      INTEGER,
    result         TEXT
);

-- v3.1.4: regime history for trend analysis in cycle explanations
CREATE TABLE IF NOT EXISTS regime_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    regime          TEXT NOT NULL,
    conviction      REAL NOT NULL,
    trend_score     REAL,
    ema_200_vs_price REAL,
    klci_rsi        REAL
);
CREATE INDEX IF NOT EXISTS idx_regime_at ON regime_history(timestamp);

-- v3.1.9: meta table for key/value pairs that must survive container resets
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT
);

-- v3.1.9: custom_watchlist was previously created ad-hoc in watchlist.py.
-- Moved into schema so init_db() creates it for fresh / restored DBs.
CREATE TABLE IF NOT EXISTS custom_watchlist (
    ticker      TEXT PRIMARY KEY,
    name        TEXT,
    sector      TEXT,
    added_at    TEXT
);

-- Risk parameters (was lazy-created by risk_manager, now in schema for consistency)
-- v3.5: Corporate actions (splits, bonus issues, dividends)
-- corporate_actions_processed: idempotency guard. Prevents applying the same
-- split twice across multiple cycles. Key = (ticker, ex_date, event_type).
CREATE TABLE IF NOT EXISTS corporate_actions_processed (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    ex_date         TEXT NOT NULL,
    event_type      TEXT NOT NULL,  -- 'SPLIT' | 'DIVIDEND' | 'BONUS'
    ratio           REAL,           -- for SPLIT/BONUS: new_shares / old_shares (e.g. 5.0 for 1-for-5)
    amount_per_share REAL,          -- for DIVIDEND: cash per share in RM
    source          TEXT,           -- 'moomoo' | 'yfinance'
    detected_at     TEXT NOT NULL,
    action_taken    TEXT NOT NULL,  -- 'ADJUSTED' | 'ALERTED_ONLY' | 'SKIPPED_NO_POSITION' | 'FAILED'
    affected_trade_ids_json TEXT DEFAULT '[]',
    error_message   TEXT,
    UNIQUE(ticker, ex_date, event_type)
);
CREATE INDEX IF NOT EXISTS idx_corp_actions_ticker ON corporate_actions_processed(ticker);
CREATE INDEX IF NOT EXISTS idx_corp_actions_ex_date ON corporate_actions_processed(ex_date);

CREATE TABLE IF NOT EXISTS risk_params (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    payload     TEXT,
    updated_at  TEXT
);

-- NOOP phase: structured decision journal. One row per evaluated setup per
-- cycle, across ALL tiers (A/B/C/D), whether or not it would be executed.
-- This is the measurement layer that lets the agent learn from setups it does
-- NOT take (Tier B/C shadow outcomes). Per-(market,mode) DB, backed up via Gist
-- automatically (whole DB is backed up). Resolver fills the outcome_* columns.
CREATE TABLE IF NOT EXISTS decision_journal (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id               TEXT,
    decided_at             TEXT NOT NULL,
    review_at              TEXT,
    market                 TEXT,
    mode                   TEXT,
    ticker                 TEXT,
    name                   TEXT,
    sector                 TEXT,
    tier                   TEXT NOT NULL,           -- A / B / C / D
    would_execute          INTEGER NOT NULL DEFAULT 0,
    signal                 TEXT,
    confidence             REAL,
    regime                 TEXT,
    regime_threshold       REAL,
    state_id               INTEGER,
    entry                  REAL,
    stop_loss              REAL,
    tp1                    REAL,
    tp2                    REAL,
    tp3                    REAL,
    rsi                    REAL,
    vol_ratio              REAL,
    atr                    REAL,
    reasoning              TEXT,
    expected_scenario      TEXT,
    invalidation_condition TEXT,
    what_proves_wrong      TEXT,
    status                 TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN / RESOLVED / SKIPPED
    outcome                TEXT,                            -- WIN / LOSS / FLAT / UNKNOWN
    outcome_r              REAL,
    max_favorable_pct      REAL,
    max_adverse_pct        REAL,
    resolved_at            TEXT,
    resolver_notes         TEXT
);
"""


def _profile_default_capital() -> float:
    """Seed-account capital for the active market.

    MY → RM 20,000 (preserved v3.3 default)
    US → USD 5,000
    """
    try:
        from market_profiles import active_profile
        return float(active_profile().default_capital)
    except Exception:
        return 20_000.0


def init_db():
    """Create tables if missing, run column migrations, and seed singleton rows.

    Idempotent — safe to call on every import.
    """
    with connect() as c:
        c.executescript(SCHEMA)
        # ---- Lightweight column migrations (v2 → v3 → v3.6) ----
        # All wrapped individually in try/except — ALTER TABLE ... ADD COLUMN
        # raises if the column already exists, which is the no-op case.
        for sql in (
            "ALTER TABLE scheduler_state ADD COLUMN exploration_mode INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE scheduler_state ADD COLUMN exploration_trades_target INTEGER NOT NULL DEFAULT 50",
            "ALTER TABLE scheduler_state ADD COLUMN owner_pid INTEGER NOT NULL DEFAULT 0",
            # v3.1.10: cycle_started_at lets the watchdog detect runaway cycles.
            "ALTER TABLE scheduler_state ADD COLUMN cycle_started_at TEXT",
            "ALTER TABLE trades ADD COLUMN executed_in_window TEXT",
            # v3.5: corporate-action audit trail. Default 1.0 means "never split".
            "ALTER TABLE trades ADD COLUMN cumulative_split_factor REAL DEFAULT 1.0",
            "ALTER TABLE trades ADD COLUMN exit_type TEXT",  # v3.7: TP3/CLIMAX/SL/TP2/TP1/TIME/MANUAL
            # v3.5: toggle for auto-adjustment behaviour. Default ON.
            "ALTER TABLE scheduler_state ADD COLUMN corp_action_autoadjust INTEGER NOT NULL DEFAULT 1",
            # v3.5: last time we scanned for corporate actions (ISO timestamp).
            "ALTER TABLE scheduler_state ADD COLUMN last_corp_action_scan_at TEXT",
            # v3.6: broker execution mode for this market's DB.
            # 'NOOP' (default — notify only) / 'SIMULATE' / 'REAL'.
            # MY ignores anything other than NOOP today; US respects all three.
            "ALTER TABLE scheduler_state ADD COLUMN broker_mode TEXT NOT NULL DEFAULT 'NOOP'",
            "ALTER TABLE scheduler_state ADD COLUMN last_wfo_run_at TEXT",  # v3.7: WFO tracker
            # v3.6: last reconciliation drift (broker vs internal) in absolute currency,
            # plus the timestamp it was last computed. Surfaces in Settings tab.
            "ALTER TABLE scheduler_state ADD COLUMN last_reconcile_at TEXT",
            "ALTER TABLE scheduler_state ADD COLUMN last_reconcile_drift REAL DEFAULT 0",
        ):
            try:
                c.execute(sql)
            except Exception:
                pass  # column already exists
        # ---- NOOP decision_journal indexes (idempotent) ----
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_journal_status "
            "ON decision_journal (status)",
            "CREATE INDEX IF NOT EXISTS idx_journal_review "
            "ON decision_journal (status, review_at)",
            "CREATE INDEX IF NOT EXISTS idx_journal_decided "
            "ON decision_journal (decided_at)",
            "CREATE INDEX IF NOT EXISTS idx_journal_tier "
            "ON decision_journal (tier)",
        ):
            try:
                c.execute(idx_sql)
            except Exception:
                pass
        # Seed scheduler_state
        c.execute(
            "INSERT OR IGNORE INTO scheduler_state "
            "(id, running, interval_sec, autotrade_enabled, autoexit_enabled, "
            " exploration_mode, exploration_trades_target) "
            "VALUES (1, 0, 3600, 1, 1, 1, 50)"
        )
        # Seed account — capital comes from active market profile.
        seed_cap = _profile_default_capital()
        c.execute(
            "INSERT OR IGNORE INTO account "
            "(id, initial_capital, cash_balance, total_equity, last_updated) "
            "VALUES (1, ?, ?, ?, ?)",
            (seed_cap, seed_cap, seed_cap, myt_iso()),
        )
        # Seed parameters
        param_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "ai_parameters.json"
        )
        default_params = {
            "ema_trend": 200, "ema_fast": 10, "ema_slow": 20,
            "rsi_oversold_pullback": 40.0, "rsi_overbought": 70.0,
            "volume_surge_ratio": 1.5, "breakout_period": 20,
            "atr_period": 14, "atr_multiplier_stop": 1.5,
            "min_price": 0.30, "max_price": 4.00,
        }
        if os.path.exists(param_path):
            try:
                with open(param_path) as f:
                    default_params.update(json.load(f))
            except Exception:
                pass
        # Market-specific price-range overrides.
        # These ensure the screener covers the active market's actual price
        # levels. ai_parameters.json may carry MY-centric values (e.g.
        # max_price=4.00) that filter out every blue-chip in other markets.
        try:
            from market_profiles import active_market_code
            _mkt = active_market_code()
            if _mkt == "US":
                # Leveraged ETFs run USD 10-900; mega-caps up to ~900.
                # Cover the full range so SPY/QQQ/META are not filtered.
                default_params["min_price"] = 5.00
                default_params["max_price"] = 2000.00
            elif _mkt == "MY":
                # Bursa blue-chips range RM 1-30+.  The old 0.30-4.00 band
                # filtered out Maybank, Tenaga, CIMB, IHH — every liquid name.
                default_params["min_price"] = 0.30
                default_params["max_price"] = 200.00
        except Exception:
            pass
        c.execute(
            "INSERT OR IGNORE INTO parameters (id, payload, updated_at) VALUES (1, ?, ?)",
            (json.dumps(default_params), myt_iso()),
        )
        # Seed bias_state
        default_bias = {
            "breakout_bias": 1.0, "pullback_bias": 1.0,
            "sector_biases": {}, "system_win_rate": 0.5,
            "strategy_stats": {}, "sector_stats": {},
            "total_closed_trades": 0,
        }
        c.execute(
            "INSERT OR IGNORE INTO bias_state (id, payload, updated_at) VALUES (1, ?, ?)",
            (json.dumps(default_bias), myt_iso()),
        )
        # v3.1: seed live trigger config (disabled by default)
        c.execute(
            "INSERT OR IGNORE INTO live_trigger_config "
            "(id, enabled, updated_at) VALUES (1, 0, ?)",
            (myt_iso(),),
        )


# ---------------------------------------------------------------------------
# Helpers exposed to callers
# ---------------------------------------------------------------------------


def execute(sql, args=()):
    with connect() as c:
        return c.execute(sql, args)


# v3.1.9: meta key/value helpers — survive container resets via Gist backup

def get_meta(key: str) -> str | None:
    """Read a value from the meta table. Returns None if missing or table absent."""
    try:
        with connect(readonly=True) as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
    except Exception:
        return None


def set_meta(key: str, value: str) -> None:
    """Upsert a key/value pair into the meta table."""
    try:
        with connect() as c:
            c.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, value, myt_iso()),
            )
    except Exception:
        # Table might not exist in a DB restored from an older backup
        try:
            with connect() as c:
                c.execute(
                    "CREATE TABLE IF NOT EXISTS meta ("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)"
                )
                c.execute(
                    "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (key, value, myt_iso()),
                )
        except Exception:
            pass


# Initialize on import so callers never have to remember.
try:
    init_db()
except Exception as e:
    print(f"[db] init warning: {e}")
