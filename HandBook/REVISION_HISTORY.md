[![Tests](https://github.com/fongway94/autonomous_bursa_agentV3.3/actions/workflows/tests.yml/badge.svg)](https://github.com/fongway94/autonomous_bursa_agentV3.3/actions/workflows/tests.yml)

# BursaAI Swing Agent — Revision History

Complete changelog from v1 through the current release.

---

## v3.6 (current)

**Focus:** Multi-market — the Bursa-only agent becomes a dual-market agent (🇲🇾 MY + 🇺🇸 US) on a single repo, with full Moomoo US execution. Branch: `feat/us-market`.

### Why
Moomoo OpenAPI does **not** support Bursa (MY) for real-time data or order execution. The v4 dream of "real broker execution" is therefore impossible on MY — it stays notification-only on yfinance. The path to actual automation is US/HK, where OpenAPI is fully supported. v3.6 adds US (full execution) while leaving MY behaviour byte-identical and preserving every market's brain separately.

### The data-source contract (most-asked design point)
ONE mechanism, gated per market by `MarketProfile.moomoo_available`:
- **Both markets fall back to yfinance** when Moomoo OpenD is absent.
- **US** (`moomoo_available=True`) uses Moomoo when OpenD is connected; yfinance otherwise.
- **MY** (`moomoo_available=False`) always uses yfinance today — the Moomoo path is *gated off, not deleted*. The day Moomoo adds Bursa: flip that one flag in `my_profile.py` + connect OpenD → MY auto-goes-live, no other code change. Guarded by `tests/test_data_provider.py::TestMarketGating` (incl. a flag-flip test).

### Changes

**NEW `market_profiles/` package (the multi-market spine):**
- `base.py` — `MarketProfile` Protocol + shared value types (`TradingSession`, `TickerSpec`, slippage/calendar contracts) + display helpers `format_session_window()` / `format_time_with_user_local()`
- `my_profile.py` — `MY_PROFILE` (RM, 100-share lot, 09:00–17:00 MYT, 0.15% fee, `^KLSE`, Shariah universe, `moomoo_available=False`)
- `us_profile.py` — `US_PROFILE` (USD, 1-share, 09:30–16:00 ET, 0% fee, `SPY`, leveraged-ETF + mega-cap universe, `moomoo_available=True`)
- `__init__.py` — `active_profile()` resolver (env `MARKET_MODE` → `.active_market` marker file → default MY), `set_active_market()`, `available_markets()`
- Business modules (`screener`, `trading_engine`, `risk_manager`, `market_analyzer`, `market_calendar`, `watchlist`, `data_provider`, `persistence`, `db`) all dispatch on `active_profile()` instead of hardcoded Bursa constants

**Per-market data isolation:**
- One SQLite file per market: `bursa_agent_MY.db` / `bursa_agent_US.db` (full schema each)
- Legacy `bursa_agent.db` auto-migrates to `bursa_agent_MY.db` on first boot (trades/brain/history preserved)
- Per-market Gist backups: `bursa_agent_<CODE>_db.b64.gz` + `setup_classifier_<CODE>.pkl.b64.gz`
- `db._resolve_db_path()` dispatches on the active market

**US broker execution — NEW `broker_adapter.MoomooUSAdapter` (~440 LOC):**
- Full `connect` / `unlock_trade` / `place_order` / `accinfo_query` / `position_list_query`, cherry-picked from the `WallTrading-Bot-MooMoo-Futu` reference pattern
- Modes: NOOP (notify-only, default) / SIMULATE (moomoo paper account) / REAL (live; requires `MOOMOO_TRADING_PWD`)
- MY always resolves to `NoopAdapter` regardless of mode (OpenAPI gap)
- Fire-and-forget mirror hooks (`mirror_entry_to_broker` / `mirror_exit_to_broker`), NO-OP in NOOP
- `broker_mode` is a per-market `scheduler_state` column

**NEW `reconciliation.py` (~400 LOC):**
- Compares internal positions/cash to the broker each cycle (US execute modes only)
- Telegram `RECONCILE_DRIFT` alert when equity drift > 0.5% or position qty tolerance (1 share / 1%)
- Observation-only — never mutates internal state from broker data
- `scheduler._run_reconciliation_step` runs it at cycle end

**UI (`app.py`):**
- Sidebar 🌐 market switcher (writes `.active_market`, reloads)
- Settings → 🎯 Execution Mode panel (NOOP/SIMULATE/REAL + connection test) and 🔄 Reconciliation status
- Currency-aware throughout (Portfolio/Scanner/Performance/AI Learning/Reconciliation use the active profile's symbol)
- Trading Window panel is profile-aware: description, TZ labels, defaults, and the safe-entry cutoff adapt to the active market. **Because the user trades from Malaysia, US times render as native ET + the MYT equivalent** (e.g. `09:30–16:00 ET (21:30–04:00 MYT)`)
- Sidebar broker badge + per-rerun `init_db()` safety net

**Currency/timezone correctness:**
- Telegram/email alerts (`live_trigger.py`) use the active currency symbol
- Scheduler "0 entries" log describes the active market's window via `format_session_window()`
- Corp-action help text uses `{_ccy}`

**Requirements:** added `pandas_market_calendars` (NYSE auto-extending holidays) and `moomoo-api` (optional, local-only for US execution).

### Bug fixes (test-harness hardening)
- **`db._resolve_db_path()` override detection by full path → stale path wins** ⭐ — a stale auto-computed `DB_PATH` (captured at first import vs the real `$HOME`, or left after `importlib.reload`) was mistaken for a deliberate test override and silently won, pointing the process at the wrong market DB → `no such table: account`. The full pytest suite failed (43 failures) even though every file passed alone. Fixed by detecting overrides **by basename** (`bursa_agent_<CODE>.db` = auto; foreign names like `fake.db`/`test.db` = real override).
- **Test split-brain from `del sys.modules["db"]`** — `test_multi_market_dispatch._reimport()` created a *second* `db` module object; cross-module references diverged onto different WAL connections. Fixed: reload in place with `importlib.reload()`.
- **Stale pre-v3.6 tests** updated to v3.6 reality (`test_data_provider` re-pointed to US live path + new `TestMarketGating`; `test_live_trigger` adapter contract; `test_ml_persistence` per-market filename).

### Tests
- **471 passing in ~46 s** (up from 240 mid-build), 35 test files. The full suite is green in a single `pytest tests/` run (deterministic across repeated runs), not just per-file.

### Deferred
- HK market profile (architecture supports it — one new `hk_profile.py`)
- Live capital tracking table (reconciliation foundation landed here)
- Stooq as a 2nd free data fallback
- MY execution — blocked until Moomoo OpenAPI adds Bursa coverage

---

## v3.5

**Focus:** Corporate-action handling — splits, bonus issues, and cash dividends no longer silently corrupt open trades.

### Why
Before v3.5 the agent had a silent failure mode: a stock doing a 1-for-5 split mid-position made stored `entry_price` / `stop_loss` / `qty` all wrong relative to the post-split market price. The agent would interpret this as an 80% crash and trigger a stop-loss at the wrong price; the learner would then train on garbage data. Handbook gap #2 (corporate actions handling) is now closed for splits and bonus issues; cash dividends are detected and alerted (full P&L adjustment deferred to v6).

### Changes

**Schema (Phase 1):**
- `trades.cumulative_split_factor` column (default 1.0) — audit trail for compose-ability of multiple splits on the same trade
- `corporate_actions_processed` table — idempotency guard with `UNIQUE(ticker, ex_date, event_type)`
- `scheduler_state.corp_action_autoadjust` (default ON) — Settings toggle for auto-adjustment
- `scheduler_state.last_corp_action_scan_at` — drives the scan window
- 3 ALTER TABLE migrations for live DBs (restored from Gist with old schema)

**Detection (Phase 2):** new `corporate_actions.py` module (~830 LOC)
- `CorporateAction` frozen dataclass with strict validation
- `_detect_moomoo`: `request_rehab()` via OpenD with port-pre-check, thread-join timeout, sticky-demote after 3 consecutive failures
- `_detect_yfinance`: `Stock Splits` + `Dividends` columns
- `detect_for_ticker` / `detect_for_tickers`: provider-agnostic with per-ticker isolation
- `detection_health` / `reset_detection_state`: UI diagnostics
- Symmetric with `data_provider.py`'s Moomoo→yfinance pattern

**Adjustment (Phase 3):** new `trading_engine.apply_split_to_trade()` (~200 LOC)
- Atomic single-transaction adjustment
- 10 per-share price fields divided by ratio (entry, stop, tp1/2/3, trailing, highest, lowest, exit, risk_per_share)
- 3 share-count fields multiplied by ratio (shares, shares_remaining, lots)
- Cost / fee / total_outlay / PnL / pct fields preserved (the cash invariant)
- `cumulative_split_factor` composes across multiple splits
- Cash-conservation invariant verified within RM 1.00 or refuses to apply
- Audit note appended to `trade.notes`
- Defensive rejection: nonexistent trade, non-ACTIVE status, bad ratio, race condition (closed between SELECT and UPDATE)

**Orchestrator (Phase 4):** `process_corporate_actions()` in `corporate_actions.py`
- Scans only tickers with active trades (typically 0-8, not all 74) → cheap on data-source quota
- Events sorted by `ex_date` so multiple splits compose in chronological order
- Per-event failure isolation: one bad split doesn't abort the batch
- Best-effort Telegram + Email alerts via `notifier.dispatch`
- `scheduler._run_one_cycle` calls it as Step 0 (before regime/scan/settle) via the new `_run_corporate_actions_step` helper, so stop-loss math uses post-split prices on the same cycle

**UI (Phase 5):** `app.py`
- Settings tab → 🏢 Corporate Actions panel: auto-adjust toggle, last-scan timestamp, detection-health expander, manual "Scan now" button
- Logs tab → new 🏢 Corporate Actions sub-tab: audit trail of `corporate_actions_processed` + CORP_ACTIONS scheduler events, both with CSV download

**Tests:** 113 new tests across 4 test files
- Phase 1 (data model): 27 tests
- Phase 2 (detection): 33 tests
- Phase 3 (adjustment, the riskiest phase): 29 tests including 7 parameterized cash-invariant cases
- Phase 4 (orchestrator): 13 tests
- Phase 6 (end-to-end via real scheduler): 11 tests
- conftest.py updated to truncate `corporate_actions_processed` and reset the new scheduler_state columns between tests

**Refactor (during Phase 7):** extracted the inline corp-actions block in `scheduler._run_one_cycle` into a `_run_corporate_actions_step` helper. Removed dead imports.

### Test count: **329 passing in ~41 seconds** (was 216 → +113)

### Settings defaults (live)
- `corp_action_autoadjust`: ON (set via Phase 1 schema default)
- Scan-window initial lookback: 7 days
- Sticky-demote threshold (Moomoo): 3 consecutive failures
- Cash-invariant tolerance: RM 1.00

### Bugs fixed during this release
- conftest test-isolation: `corp_action_autoadjust` and `last_corp_action_scan_at` weren't being reset between tests, causing state leak between test classes. Caught by Phase 6 integration tests.

### Known gaps for v6 (corporate actions)
- Dividend P&L credit is deferred (alert-only in v3.5)
- Rights issues are not handled (detected but treated as no-op)
- Historical trades closed before v3.5 are NOT retroactively split-adjusted (would require destructive backfill; current learner copes with stale priors via fading)

---

## v3.4

**Focus:** Pluggable data-source abstraction with Moomoo OpenD ↔ yfinance auto-fallback.

### Why
The agent was hard-wired to yfinance — a single point of failure (handbook gap #1). A yfinance outage made the scanner go blind, and there was no path to real-time data when running the agent locally with Moomoo Desktop. Both problems are solved by routing all OHLCV fetches through a single `data_provider` abstraction.

### Changes
- **NEW: `data_provider.py`** (~470 LOC) — unified market-data provider with:
  - Auto-detection of Moomoo OpenD on `127.0.0.1:11111`, sticky fallback to yfinance
  - **Raw TCP port pre-check** before instantiating `OpenQuoteContext` (prevents the moomoo SDK's internal reconnect-thread from spamming `ECONNREFUSED` on environments without OpenD — e.g. Streamlit Cloud)
  - Per-call fallback on Moomoo exceptions, sticky demotion after 5 consecutive failures
  - Thread-join timeout wraps the SDK's `request_history_kline` (the SDK has no native `timeout=` — honors handbook rule #15)
  - Internal ticker conversion: `0166.KL ↔ MY.0166`, `^KLSE → MY.800000`
  - Output shape is byte-compatible with yfinance (`Open/High/Low/Close/Volume`, tz-aware `DatetimeIndex` named `Date`) — existing `data_quality.validate_ohlcv` and all indicator code work unmodified
  - Env override: `BURSA_DATA_PROVIDER=yfinance|moomoo|auto` (default `auto`)
  - Public API: `get_history()`, `provider_name()`, `health()`, `reset()`, `ensure_probed()`
- **`screener.py`** — `yf.Ticker(t).history()` → `get_history(t)` (1 call site)
- **`market_analyzer.py`** — same swap (4 call sites)
- **`scheduler.py`** — user-facing error strings now say "data-source outage" instead of "yfinance outage" (provider-agnostic)
- **`app.py`** — new **📡 Data Source** panel in Settings tab: shows active provider, full health dict, and a re-probe button
- **`requirements.txt`** — added `moomoo-api>=8.0.0` as optional dep (auto-fallback handles missing or unreachable cleanly)
- **`evaluation.py` + `learner.py` deliberately untouched** — they pull 3y histories which may exceed Moomoo's history quota; staying on yfinance is the conservative choice

### Architecture impact
- Same code now runs in 3 environments:
  - **Streamlit Cloud** → port pre-check fails → yfinance (identical to v3.3 behaviour)
  - **Local PC, no Moomoo Desktop** → port pre-check fails → yfinance
  - **Local PC, Moomoo Desktop + OpenD running** → real-time Moomoo data, auto-detected
- Zero behavioural change on Streamlit Cloud — paper trading, learning, scanning all run exactly as before

### Test count: **216 passing** (was 191; +25 new tests in `test_data_provider.py`)

### Bugs fixed during this release
- **moomoo SDK reconnect-loop spam (v3.4 hotfix)** — discovered on first Streamlit Cloud deploy: the SDK's `OpenQuoteContext()` constructor spawns a background reconnect thread that cannot be cleanly killed and spams `ECONNREFUSED` once per second forever on environments without OpenD. Fixed by pre-checking the TCP port with a raw socket probe; only construct the context if the port is open. Regression test `TestPortPreCheck::test_port_closed_skips_opend_construction` guards this (uses a fake `OpenQuoteContext` that throws if constructed).

---

## v3.3

**Focus:** Code cleanup, system audit, unused import sweep.

### Changes
- **Removed unused imports** across 8 modules: `math` and `yfinance` from `trading_engine.py`, `numpy` from `data_quality.py`, `Any` from `repository.py` and `risk_manager.py`, `get_myt_now` from `live_trigger.py`, `myt_iso` from `persistence.py`, `datetime/timezone/timedelta` from `learner.py`
- **Added `risk_params` table to `db.py` SCHEMA** — was previously created lazily by `risk_manager._ensure_risk_row()`, now created with all other tables for consistency
- **Added `fut.result(timeout=30)`** in `screener.py` — ThreadPoolExecutor results had no timeout, violating the project's own design rule #24 (all external calls must have explicit timeouts)
- **Added regression test** `test_screener_futures_have_timeout` guarding the timeout fix
- **Removed `db.executemany()`** — zero callers across the entire codebase

### Test count: 191 passing (~40 s)

---

## v3.2

**Focus:** Scheduler lifecycle refactor — fixed the permanently-STOPPED bug.

### Root cause
The `ADOPT_THREAD` path in `start()` adopted a still-alive thread but never wrote `running=1, kill_switch=0` to the DB. Combined with `stop()` unconditionally setting `kill_switch=1`, this created a death spiral where `force_restart()` left the scheduler permanently STOPPED.

### Changes
- **`scheduler.py` rewritten** (1,359 → 1,077 LOC):
  - `start()`: 8 guards + ADOPT_THREAD → 1 guard + orphan-all + start fresh
  - `stop()`: no longer sets `kill_switch=1` (was the root cause)
  - New `engage_kill_switch()`: dedicated function for the kill-switch button
  - `ensure_started()`: simplified from 5-case tree to `if not is_running(): start()`
- **`app.py`**: Kill-Switch button calls `sched.engage_kill_switch()`, clearer Start button feedback
- **Test file renames** (removed version numbers):
  - `test_v3_features.py` → `test_defaults.py`
  - `test_v32_lifecycle_regression.py` → `test_scheduler_lifecycle.py`
  - `test_watchdog_lifecycle_v3_1_11.py` → `test_watchdog_lifecycle.py`
  - `test_schema_migration_after_restore.py` → `test_schema_migration.py`
- **7 new regression tests** in `test_scheduler_lifecycle.py`

### Test count: 190 passing

---

## v3.1.10

**Focus:** Zombie thread recovery + runaway-cycle watchdog.

### Changes
- `_ORPHANED_THREAD_IDS` registry lets `start()` skip threads that `stop()` requested to exit but couldn't kill within join timeout
- `_watchdog_loop` thread (every 60 s) detects cycles exceeding 10 min and forces clean handoff
- `cycle_started_at` column added to `scheduler_state`
- All `evaluation.py` yfinance calls given explicit `timeout=`
- `conftest.py` resets scheduler module-level state between tests

---

## v3.1.9

**Focus:** Crash-recoverable start guards.

### Changes
- `start()` Guards 2/3 only block on local alive threads, not stale DB state
- Gist ID stored in SQLite `meta` table (survives container resets)
- `GIST_ID` env var as fallback when local marker lost
- `custom_watchlist` table moved into `db.py` SCHEMA

---

## v3.1.8

**Focus:** Duplicate worker loop prevention.

### Changes
- Ghost threads from Streamlit reruns exit silently (no log spam)
- `ensure_started()` conservative — doesn't spawn when another live owner detected
- Per-minute log dedup for HEARTBEAT/SKIP storms

---

## v3.1.7

**Focus:** Long-term maintenance reminders.

### Changes
- `maintenance_reminders.py` added — surfaces banners for holiday list renewal, GitHub PAT rotation, walk-forward optimization
- "I rotated the token" button resets the PAT timer
- Maintenance status panel in Settings tab

---

## v3.1.6

**Focus:** ML classifier persistence.

### Changes
- ML classifier `.pkl` included in Gist backup alongside DB
- Auto-train on boot if `.pkl` missing (background thread)

---

## v3.1.5

**Focus:** Brain persistence via GitHub Gist.

### Changes
- `persistence.py` added — gzip + base64 encode DB → private Gist
- Backup on every closed trade + hourly heartbeat
- Auto-restore on boot before scheduler starts
- `boot_restore_once()` is idempotent — only runs once per process, skips if local DB has data

---

## v3.1.4

**Focus:** Regime trend tracking.

### Changes
- `regime_history` table for per-cycle KLCI regime snapshots
- Cycle explanations include regime trend direction (weakening/strengthening)
- KLCI 200-EMA distance shown in cycle logs

---

## v3.1.3

**Focus:** Boot debounce + dead code cleanup.

### Changes
- Scheduler sleeps until next scheduled boundary on startup (no immediate scan on GitHub push)
- Removed deprecated `learning_engine.py` shim
- Several unused helper functions deleted

---

## v3.1.2

**Focus:** Bursa-accurate market calendar.

### Changes
- `market_calendar.py` added with real sessions (09:00–12:30, 14:30–17:00)
- Lunch break (12:30–14:00) correctly blocks scans
- Public holidays hardcoded through 2027
- Safe-entry window cutoff at 16:00

---

## v3.1.1

**Focus:** Maintenance task idempotency.

### Changes
- `maintenance_state` table with SQL CAS for daily tasks
- Fixed: ML classifier was retraining 8× per night

---

## v3.1

**Focus:** Live trigger / notification system.

### Changes
- `notifier.py`, `live_trigger.py`, `broker_adapter.py` added
- Telegram (plain text) + Email (HTML) alerts on qualifying trades
- Configurable filters: confidence, mode, per-event toggles
- 🔔 Live Alerts tab added to dashboard
- `MoomooAdapter` stub for future real-broker integration
- Live alerts OFF by default (user opts in)

### Test count: 63 passing

---

## v3

**Focus:** Autonomy hardening + cold-start learning.

### Changes
- Auto-trade ON by default (`autotrade_enabled=1`)
- Exploration mode (Thompson sampling) → Exploitation (LCB) auto-switch at 50 trades
- Volume-aware slippage model (base + size-linear + liquidity penalty, capped 80 bps)
- Default `max_risk_per_trade_pct` lowered from 2.0% → 1.0%
- Shariah-compliant filter option
- Self-healing `ensure_started` (force-restart on stale heartbeat)
- No entries after 16:00 MYT
- Nightly ML retrain at 01:00 MYT

### Test count: 47 passing

---

## v2

**Focus:** Complete rebuild — honest learning, real risk management, production-grade storage.

### Changes from v1
- **Learning:** Q-learning EMA → Bayesian Beta(α,β) posteriors
- **Storage:** JSON files → SQLite with WAL mode
- **Risk:** size_multiplier actually enforced, 100-share lot rounding
- **Execution:** added slippage model, MAE/MFE tracking
- **Evaluation:** Sharpe, Sortino, max DD, profit factor, calibration, KLCI benchmark
- **Logging:** 6 dedicated audit streams (trade, scheduler, learning, bias, parameter, data quality)
- **Theme:** dark → forced light
- **Tests:** 0 → 36

---

## v1

**Original prototype.** Had "Q-learning" that was a single-step EMA, JSON file storage with race conditions, no slippage, no lot enforcement, no real risk management.
