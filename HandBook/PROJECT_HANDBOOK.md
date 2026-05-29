# BursaAI Swing Agent — Project Handbook

**Living reference document.** Update as the project evolves.
Single source of truth for: architecture decisions, why things were built the way they are, known issues, operational runbooks, and the rationale behind every design choice.

Last updated: 2026-05-29 (v3.3)

---

## Table of Contents

1. [Project Mission & Core Objective](#1-project-mission--core-objective)
2. [Current Version & Live Status](#2-current-version--live-status)
3. [Architecture Overview](#3-architecture-overview)
4. [Key Design Decisions](#4-key-design-decisions)
5. [Module-by-Module Reference](#5-module-by-module-reference)
6. [Defaults & Risk Parameters](#6-defaults--risk-parameters)
7. [The Robo-Trader Lifecycle](#7-the-robo-trader-lifecycle)
8. [The Self-Learning Engine](#8-the-self-learning-engine)
9. [Operational Runbooks](#9-operational-runbooks)
10. [Bugs Fixed (chronological)](#10-bugs-fixed-chronological)
11. [Known Gaps & v4 Roadmap](#11-known-gaps--v4-roadmap)
12. [Conventions for Future Work](#12-conventions-for-future-work)
13. [Long-Term Maintenance Calendar](#13-long-term-maintenance-calendar)

---

## 1. Project Mission & Core Objective

**Mission:** Autonomous AI agent that paper-trades Bursa Malaysia swing setups, learns from outcomes over multiple years, and (eventually) sends real-broker entry/exit alerts to a human trader for manual mirroring.

**Core principles:**
- **Honest learning** — Bayesian posteriors, not fake RL theater. Statistically sound on small samples.
- **Defensive by design** — risk gates always fire. Drawdown circuit breakers protect capital.
- **Fully auditable** — every state change leaves a row in a log table.
- **Real Bursa mechanics** — 100-share lots, 0.15% fees, volume-aware slippage, real session hours, public holidays.
- **Durable memory** — the brain persists indefinitely via Gist backup, surviving every container reset.
- **Self-healing scheduler** — simplified lifecycle (v3.2): orphan-all-and-start-fresh design, runaway-cycle watchdog, no ADOPT_THREAD complexity.
- **Light theme only** — enforced by both Streamlit config and CSS override.
- **Defaults err on safety** — 1% risk/trade, auto-trade ON but with conservative thresholds.

---

## 2. Current Version & Live Status

| | |
|---|---|
| **Codebase version** | v3.3 |
| **Deployment** | Streamlit Cloud (live) |
| **Database** | SQLite WAL at `~/.bursa_agent_data/bursa_agent.db` |
| **DB persistence** | **GitHub Gist backup (private)** — survives container resets |
| **Source LOC** | ~9,065 across **19 Python modules** |
| **Test count** | **191 passing in ~40 seconds** |
| **Documentation files** | SETUP_GUIDE.md, USER_GUIDE.md, LIVE_TRIGGER_GUIDE.md, REVISION_HISTORY.md, PROJECT_HANDBOOK.md, AI_CHAT_HANDOFF.md |
| **Capital (paper)** | RM 20,000 default (user adjustable) |
| **Brokers supported** | NOOP (notification only), MoomooAdapter stub (v4 ready) |

---

## 3. Architecture Overview

```
                         ┌────────────────────────────────┐
                         │  🤖 ROBO-TRADER (scheduler.py)  │
                         │  Hourly daemon thread           │
                         │  PID-owned, self-healing        │
                         │  Boot-debounced                 │
                         │  + 🦴 Orphan-all-and-restart    │
                         │  + ⏱️ Runaway watchdog          │
                         └──────────────┬─────────────────┘
                                        │
   ┌────────────────────────────────────┼─────────────────────────────────┐
   │                                    ▼                                  │
   │  market_calendar  →  market_analyzer  →  screener   →  risk_manager   │
   │  (session/holiday) (KLCI regime detect) (80 tickers)  (gate-keeper)   │
   │                                                                       │
   │                              ↓                                        │
   │            trading_engine    →    learner (Bayesian brain)            │
   │            (fills + cash)    →    state_priors update                 │
   │                                                                       │
   │                              ↓                                        │
   │  live_trigger  →  notifier (Telegram + Email)  →  YOUR PHONE          │
   │                                                                       │
   └────────────────────────────────────┼─────────────────────────────────┘
                                        │
                                        ▼
                  ┌─────────────────────────────────────┐
                  │  SQLite (WAL) — local on container  │
                  │  trades, account, state_priors,     │
                  │  bias_state, scheduler_state        │
                  │  (incl. cycle_started_at v3.1.10),  │
                  │  scheduler_log, trade_log,          │
                  │  learning_events, parameter_history,│
                  │  alert_log, maintenance_state,      │
                  │  regime_history, meta ...           │
                  └─────────────────┬───────────────────┘
                                    │ every closed trade
                                    │ + hourly heartbeat
                                    ▼
                  ┌─────────────────────────────────────┐
                  │  persistence.py  →  PRIVATE GIST    │
                  │  (gzip + base64-encoded)            │
                  │  Survives container resets,         │
                  │  redeploys, 7-day sleeps.           │
                  └─────────────────────────────────────┘
                                    │
                                    ▼
                  Streamlit dashboard (8 tabs, light theme)
                  Scanner / Portfolio / AI Learning / Performance /
                  Robo-Trader / Logs / Live Alerts / Settings
```

Communication between modules happens via **SQLite**, not in-memory objects. This means scheduler thread + UI re-renders never deadlock or share mutable state.

The Gist backup runs out-of-band — never blocking trade execution, always degrading silently if GitHub is unreachable.

The scheduler now spawns **two** daemon threads at start: the cycle loop (`bursa-scheduler`) and the runaway-cycle watchdog (`bursa-watchdog`). The watchdog runs every 60 s and forces a clean handoff if any cycle exceeds 10 minutes (v3.1.10).

---

## 4. Key Design Decisions

Every decision below has a deliberate rationale. Don't change them without understanding why.

### 4.1 Bayesian Beta posteriors, not Q-learning
- The original v1 had "Q-learning" that was actually just an EMA of immediate rewards — no next-state bootstrapping, statistically wrong.
- Swing trading on ~80 tickers gives 5-20 trades per state at maturity. That's tiny-sample territory.
- **Bayesian Beta(α,β) is the correct tool.** Lower confidence bound for action selection during EXPLOIT mode; Thompson sampling during EXPLORE mode.
- Auto-switches EXPLORE → EXPLOIT at 50 closed trades.

### 4.2 SQLite over JSON files
- v1 had file-lock race conditions in the scheduler thread.
- SQLite WAL mode handles 1000+ concurrent writes/sec with zero errors (proven by stress test).
- Single `bursa_agent.db` file at `~/.bursa_agent_data/`.
- ACID transactions for every state change.

### 4.3 PID-based scheduler ownership
- Streamlit Cloud auto-redeploys on every push. Ghost threads from previous deploys can survive briefly.
- Each scheduler thread stamps its PID into `scheduler_state.owner_pid`.
- Every loop iteration checks if current owner matches its own PID. If not, ghost exits cleanly.
- Combined with `maintenance_state` table for SQL-CAS idempotency on daily tasks.

### 4.4 Boot debounce — no scan on startup (v3.1.3)
- Every Streamlit Cloud redeploy spawns a fresh scheduler thread.
- Without debounce, every GitHub push during market hours triggered an immediate market scan → wasteful yfinance hits, confusing logs.
- The loop now **sleeps until the next scheduled boundary** before its first cycle.
- User can still force an instant scan via the "⚡ Run Cycle Now" button or "🔥 SCAN MARKET" button — both bypass the debounce.

### 4.5 Auto-trade ON by default
- User explicitly chose this. Default is `autotrade_enabled=1`.
- Auto-exit is also ON by default (defensive).
- User can toggle either independently in the 🤖 Robo-Trader tab.

### 4.6 1% default max_risk_per_trade_pct
- Lowered from v2's 2% because auto-trade ON means agent acts without supervision.
- User-adjustable in Settings → Risk Parameters.
- Drawdown warns at 8%, hard-stops at 15%.

### 4.7 Asymmetric risk multipliers by regime
- BULL → 60% confidence threshold, 8 max positions, 14-day max hold
- NEUTRAL → 70% threshold, 5 positions, 7-day hold
- BEAR → 80% threshold, 3 positions, 5-day hold, 50% position sizing, +40% confidence penalty
- **Doing fewer trades in BEAR is correct behaviour.** Don't loosen these.

### 4.8 Volume-aware slippage
- Base 5 bps + size-linear + liquidity penalty up to 80 bps cap.
- Reads avg daily traded value from scan cache.
- Realistic for Bursa small caps (RM 0.30-4.00 universe).

### 4.9 100-share lot enforcement
- Bursa trades in board lots of 100. The engine auto-rounds down.
- 137-share order becomes 100 (not 137).

### 4.10 Bursa-native market calendar (v3.1.2)
- Real sessions: PRE_OPEN_AM (08:30-09:00), MORNING (09:00-12:30), LUNCH (12:30-14:00), PRE_OPEN_PM (14:00-14:30), AFTERNOON (14:30-16:45), PRE_CLOSE (16:45-16:50), TRADING_AT_LAST (16:50-17:00).
- Lunch break and pre-open phases correctly treated as "no fills".
- Public holidays hardcoded through 2027 in `market_calendar.MY_PUBLIC_HOLIDAYS`.
- Safe-entry window cutoff at 16:00 for new auto-entries (gives trades ≥1h to develop).

### 4.11 Light theme locked
- `.streamlit/config.toml` + inline CSS override.
- User requirement; do not change.

### 4.12 Notification-only mode for live trading (v3.1)
- Real broker orders are NOT placed. The system sends Telegram + email alerts.
- User manually mirrors trades in Moomoo.
- `broker_adapter.py` has a Moomoo stub ready for v4 when user wants direct API execution.

### 4.13 Daily maintenance idempotency (v3.1.1)
- ML classifier nightly retrain was firing 8× per night due to ghost threads.
- `maintenance_state` table + `try_claim_daily_task()` use SQL `INSERT OR IGNORE` for atomic CAS.
- Only one process per MYT date can win each daily task — proven by 20-thread concurrency test.

### 4.14 Regime trend tracking (v3.1.4)
- `regime_history` table records (regime, conviction, KLCI 200-EMA distance) on every cycle.
- `get_regime_trend()` exposes a 24-hour rolling summary (WEAKENING / STRENGTHENING / STABLE).
- Used by cycle-explanation messages so "0 entries fired" tells you whether BEAR is easing (entries may resume soon) or deepening (stay defensive).

### 4.15 Persistent backup via GitHub Gist (v3.1.5) ⭐
- **This is critical to the project's core value proposition.** Without it, every Streamlit Cloud container reset (GitHub push, manual reboot, 7-day sleep, platform maintenance) would wipe the entire database including the Bayesian brain. The "self-learning over time" promise would collapse.
- `persistence.py` backs up the full SQLite DB (gzip + base64) to a single private GitHub Gist.
- Triggered on: every closed trade (instant brain preservation), hourly heartbeat (safety net), every full exit (manual or auto).
- On boot, `boot_restore_once()` checks if local DB is empty and restores from the latest gist.
- Rate-limited to 30s minimum between backups (prevents API hammering).
- Requires user to set `GITHUB_TOKEN` (classic PAT with `gist` scope only) in Streamlit Secrets.
- **Without this token, all data is volatile.** The Settings tab shows a prominent warning if not configured.
- All backup operations wrapped in try/except — never block trade execution. Failure degrades silently to "agent still works, data still ephemeral until token is set."

### 4.16 Conservative duplicate-loop guards + silent ghost exit (v3.1.8)
- Streamlit reruns the script on every interaction. Daemon threads usually survive but module-level handles can be lost on reload.
- Before v3.1.8 each rerun could spawn a fresh scheduler thread that logged HEARTBEAT/SKIP before realizing it was a ghost — produced "10 SKIPs in 16 seconds" log spam.
- Fix: `_loop` checks `owner_pid` FIRST. If another live owner exists (heartbeat < 5 min old), exit SILENTLY — no work, no log spam (one breadcrumb GHOST_EXIT row max per process).
- `ensure_started()` is conservative: if another live owner is detected, do nothing. Adopt rather than duplicate alive threads when our local handle is lost.

### 4.17 Crash + handle-loss recovery (v3.1.9)
- Earlier guards could permanently block `start()` if the local `_THREAD` handle was lost while DB still showed a fresh heartbeat (e.g. Streamlit script reload race).
- Guards 2 and 3 now only block when there is a *local* alive thread. If the thread crashed silently, the stale DB state is ignored and start() proceeds.
- `_run_one_cycle` accepts `my_pid` so it can abort if ownership changed mid-cycle.
- `_STOP_EVENT.clear()` moved to AFTER all guards pass — previously a slow-dying thread saw the event cleared and slept for up to an hour instead of exiting.

### 4.18 Zombie thread recovery — orphan registry (v3.1.10) ⭐
- **Problem class:** a `_loop` cycle gets stuck inside a long network call (yfinance hang) or sleeps past `stop()`'s 5-second join window. The thread survives stop. On the next Start click, `start()`'s Guard 2 enumerates threads, finds the still-alive zombie named `bursa-scheduler`, ADOPTS it (since the local handle was cleared), and returns False — leaving the UI permanently on "🔴 STOPPED" with no path back to RUNNING.
- Reproduced live in the screenshot that prompted v3.1.10: heartbeat stuck at 17:00, last cycle at 12:00, Start/Force-Restart/Kill-Switch all did nothing.
- **Fix:** module-level `_ORPHANED_THREAD_IDS: set[int]`. `stop()` records `_THREAD.ident` here BEFORE the bounded join — so even if the thread is stuck, the orphan flag still applies. `start()` Guard 2 SKIPS any `bursa-scheduler` thread whose ident is in this set. The orphan still self-terminates via the existing `owner_pid` mismatch check inside `_loop` — no thread leak.
- `force_restart()` no longer blocks for 30 seconds polling for the zombie to die — useless if the thread is hung. The orphan flag does the work.
- The set is garbage-collected at the top of each `start()` so dead idents don't accumulate.
- **Tests:** `test_zombie_thread_recovery.py` (3 tests including the exact production scenario).

### 4.19 Runaway-cycle watchdog (v3.1.10) ⭐
- **Problem class:** the v3.1.10 orphan fix lets the *user* recover the UI by clicking Start. But what if no one is watching (overnight, weekend)? A single stuck cycle would still mean hours of no scans.
- **Fix:** separate `bursa-watchdog` daemon thread spawned by `start()`. Reads `scheduler_state.cycle_started_at` (new column) every 60 s. If a cycle has been running > `WATCHDOG_CYCLE_TIMEOUT_SEC` (default 600 = 10 min), the watchdog (1) logs `CYCLE_TIMEOUT`, (2) clears `cycle_started_at`, (3) bumps `owner_pid` to a sentinel value (-1), (4) marks the stuck thread as orphaned.
- The stuck `_loop` self-exits on its next wake via the existing `owner_pid` mismatch check. Next Streamlit rerun → `ensure_started()` spawns a fresh loop.
- **Soft warn:** any cycle exceeding `CYCLE_DURATION_WARN_SEC` (default 300 = 5 min) logs `CYCLE_SLOW` even if it completes. Early visibility before the watchdog has to act.
- **Critical caveat:** Python `threading` cannot interrupt blocking I/O. The watchdog cannot make the stuck cycle return faster — it only ensures the system *recovers* within 10 min instead of forever. That's why **every external call must have its own timeout**. Audited and confirmed: yfinance (15-30 s), requests (30-60 s), smtp (configurable).
- **Single source of truth:** only the owner_pid process's watchdog acts on its own cycle. Cross-process false positives are impossible.
- **Tests:** `test_watchdog_and_cycle_tracking.py` (8 tests).

---


### 4.20 Simplified scheduler lifecycle (v3.2) ⭐

The ADOPT_THREAD path in `start()` was the root cause of the permanently-STOPPED bug: it adopted a still-alive thread but never wrote `running=1, kill_switch=0` to the DB. v3.2 removes the ADOPT_THREAD path entirely:

- `start()`: orphan ALL stale threads, then spawn fresh. 1 guard instead of 8.
- `stop()`: does NOT set `kill_switch` (only `engage_kill_switch()` does).
- `ensure_started()`: just `if not is_running(): start()`. No 5-case tree.
- No ADOPT_THREAD. No multi-guard complexity. If in doubt, start fresh.

### 4.21 All external calls must have explicit timeout (reinforced v3.3)

The `screener.py` ThreadPoolExecutor `fut.result()` was missing a timeout — meaning one hung yfinance call could block the entire 74-ticker scan indefinitely. Fixed with `fut.result(timeout=30)`. The watchdog is the safety net, not the first line of defence.

### 4.22 Schema consistency (v3.3)

`risk_params` table was previously created lazily by `risk_manager._ensure_risk_row()` instead of being in `db.py` SCHEMA. Moved to SCHEMA for consistency — all 21 tables are now created by `init_db()`.

## 5. Module-by-Module Reference

| Module | Purpose | Critical functions |
|---|---|---|
| `app.py` | Streamlit UI (8 tabs) | Tab handlers, sidebar, light theme CSS, boot-restore wiring |
| `scheduler.py` | Background daemon thread + runaway watchdog (v3.1.10) | `start()`, `stop()`, `force_restart()`, `_loop()`, `_run_one_cycle()`, `_watchdog_loop()`, `_start_watchdog()`, `_stop_watchdog()`, `_find_live_non_orphan_scheduler_thread()`, `_explain_cycle_outcome()` |
| `screener.py` | Market scan, indicators, setup classifier | `screen_all_stocks()`, `analyze_stock_setup()`, `compute_indicators()`, `fetch_and_calculate()` |
| `trading_engine.py` | Execute entries + exits, cash math | `execute_entry()`, `execute_full_exit()`, `execute_partial_exit()`, `auto_settle_trades()` |
| `risk_manager.py` | Gate-keep proposed trades | `run_full_risk_check()`, `check_trading_time_window()`, `check_drawdown_circuit_breaker()` |
| `learner.py` | Bayesian brain + walk-forward + ML classifier | `compute_state_action_score()`, `learn_from_trade_outcome()`, `run_walk_forward_optimization()`, `train_setup_classifier()` |
| `market_analyzer.py` | KLCI regime detection, sector momentum, RS | `get_full_market_analysis()`, `detect_market_regime()` |
| `market_calendar.py` | Bursa session boundaries + public holidays | `is_market_open()`, `is_safe_entry_window()`, `next_session_start()` |
| `evaluation.py` | Sharpe, drawdown, calibration, benchmarks | `full_evaluation_report()`, `expectancy()` (all yfinance calls have explicit timeouts as of v3.1.10) |
| `data_quality.py` | OHLCV validator (catches bad yfinance bars) | `validate_ohlcv()` |
| `repository.py` | All SQL access for trades/account/params | `insert_trade()`, `load_account()`, `try_claim_daily_task()`, `record_regime_snapshot()`, `get_regime_trend()` |
| `db.py` | SQLite schema + connection (WAL) | `connect()`, `init_db()` (adds `cycle_started_at` v3.1.10), `get_meta()`, `set_meta()` |
| `logger.py` | All log streams + rotating text log | `log_trade_event()`, `log_scheduler_event()`, `log_learning_event()`, `dedupe_scheduler_log_at_same_second()`, `dedupe_scheduler_log_within_minute()` (v3.1.8) |
| `watchlist.py` | 80 Bursa tickers + Shariah filter | `get_all_tickers()`, `is_shariah_compliant()`, `add_custom_ticker()`, `remove_custom_ticker()` |
| `notifier.py` | Telegram + Email + dashboard alerts | `send_telegram()` (plain text default), `send_email()`, `dispatch()` |
| `live_trigger.py` | Filter+dedup+format paper-trade events into alerts | `fire()`, `send_test_alert()` |
| `broker_adapter.py` | Abstract broker interface (NOOP + Moomoo stub) | `BrokerAdapter.place_order()` (stubbed) |
| **`persistence.py`** ⭐ | **Gist-backed DB backup + restore** | `backup()`, `restore()`, `boot_restore_once()`, `get_status()` |
| `maintenance_reminders.py` | Holiday/PAT/WFO renewal reminder banners (v3.1.7) | `check_maintenance_status()`, `mark_pat_rotated()` |

**Note:** `learning_engine.py` was removed in v3.1.3 — it was a 40-line backwards-compat shim from the v1→v2 refactor with zero remaining imports.

### Scheduler invariants (v3.2+)

The scheduler module guarantees:
1. **At most one** `bursa-scheduler` thread per process is treated as "live and authoritative". Zombies in the orphan registry are excluded from this count.
2. **At most one** `bursa-watchdog` thread per process.
3. `is_running()` is True ↔ a non-orphaned alive thread exists AND `scheduler_state.running = 1`. (False on either side means the badge is honest.)
4. The UI can ALWAYS recover from STOPPED → RUNNING via the Start button. If it can't, that's a P0 bug.
5. A runaway cycle is autonomously evicted within `WATCHDOG_CYCLE_TIMEOUT_SEC + WATCHDOG_TICK_SEC` (≤ 11 min by default).

---

## 6. Defaults & Risk Parameters

These live in `risk_manager.DEFAULT_RISK_PARAMS` and are seeded into the `risk_params` SQLite table on first boot. User adjustable via **⚙️ Settings tab → Risk Parameters**.

| Parameter | Default | Where to edit |
|---|---|---|
| `max_drawdown_pct` | 8.0 | Settings → Risk Parameters |
| `max_drawdown_strict_pct` | 15.0 | Settings → Risk Parameters |
| `min_risk_per_trade_rm` | 50.0 | Settings → Risk Parameters |
| `max_risk_per_trade_pct` | **1.0** (v3 lowered from 2.0) | Settings → Risk Parameters |
| `max_position_cost_pct` | 20.0 | Settings → Risk Parameters |
| `max_sector_exposure_pct` | 40.0 | Settings → Risk Parameters |
| `max_concurrent_positions` | 8 (3 in BEAR via regime) | Settings → Risk Parameters |
| `max_trades_per_day` | 5 | Settings → Risk Parameters |
| `no_entry_before_time` | **09:00** (v3.1.2 fixed from 09:15) | Settings → Risk Parameters |
| `no_entry_after_time` | 17:00 | Settings → Risk Parameters |
| `max_stop_loss_pct` | 10.0 | Code only |
| `min_stop_loss_pct` | 1.5 | Code only |
| `trailing_stop_buffer_pct` | 0.5 | Code only |

Scheduler params live in `scheduler_state` table:

| Parameter | Default | Where to edit |
|---|---|---|
| `autotrade_enabled` | **1 (ON)** | 🤖 Robo-Trader tab |
| `autoexit_enabled` | 1 (ON) | 🤖 Robo-Trader tab |
| `interval_sec` | 3600 | 🤖 Robo-Trader tab (15/30/60/120 min) |
| `exploration_mode` | 1 (until 50 trades closed) | 🤖 Robo-Trader tab |
| `exploration_trades_target` | 50 | 🤖 Robo-Trader tab |
| `kill_switch` | 0 | 🤖 Robo-Trader tab (Settings to clear) |
| `cycle_started_at` | NULL (set during cycle, cleared after) | Internal — written by `_loop` |

Watchdog knobs (deploy-time config, hardcoded — not user-facing):

| Knob | Default | Module location | Purpose |
|---|---|---|---|
| `WATCHDOG_TICK_SEC` | 60 | `scheduler.py` | How often the watchdog wakes up |
| `WATCHDOG_CYCLE_TIMEOUT_SEC` | 600 (10 min) | `scheduler.py` | Cycle is "runaway" if it exceeds this |
| `WATCHDOG_TIMEOUT_OWNER_SENTINEL` | -1 | `scheduler.py` | Forces owner_pid mismatch for self-exit |
| `CYCLE_DURATION_WARN_SEC` | 300 (5 min) | `scheduler.py` | Soft warn (no action) for slow-but-completed cycles |

Live trigger params live in `live_trigger_config` table:

| Parameter | Default | Where to edit |
|---|---|---|
| `enabled` | **0 (OFF)** — opt-in | 🔔 Live Alerts tab |
| `min_confidence` | 70.0 | 🔔 Live Alerts tab |
| `exploit_mode_only` | 0 | 🔔 Live Alerts tab |
| `alert_on_entry` / `_full_exit` / `_stop_loss` / `_trailing_stop` | 1 | 🔔 Live Alerts tab |
| `alert_on_partial_exit` / `_risk_rejected` | 0 | 🔔 Live Alerts tab |

Persistence (v3.1.5):

| Setting | Default | Where to set |
|---|---|---|
| `GITHUB_TOKEN` | (unset) | Streamlit Cloud → Manage app → Secrets |
| Backup frequency | On every closed trade + hourly heartbeat | Hardcoded |
| Rate limit | 30 seconds minimum between backups | `persistence.MIN_BACKUP_INTERVAL_SEC` |

---

## 7. The Robo-Trader Lifecycle

### Startup sequence (every Streamlit redeploy)

```
1. Streamlit Cloud kills old process, spawns new one
2. app.py imports trigger db.init_db() (creates/migrates schema,
   incl. cycle_started_at column v3.1.10)
3. boot_restore_once() runs — if local DB is empty AND GITHUB_TOKEN set,
   restore from Gist (preserves brain across resets)
4. app.py calls sched.ensure_started() — spawns daemon thread
5. Thread immediately writes STARTED log with PID
6. start() also spawns the bursa-watchdog thread (v3.1.10)
7. Thread sleeps until next scheduled boundary (v3.1.3 DEBOUNCE)
   → prevents instant scan on redeploy
8. First real cycle runs at the next top-of-hour
```

### Wake-up sequence (every hour, after debounce)

```
1. HEARTBEAT logged (+update last_heartbeat, next_run_at, owner_pid)
2. Hourly persistence backup fires (v3.1.5, rate-limited)
3. Check kill_switch — if engaged, exit loop
4. Check owner_pid — if changed (incl. WATCHDOG sentinel -1), exit silently
5. Check market hours via market_calendar.is_market_open()
     - if closed → log SKIP with reason + next event time, sleep
6. If open:
     a. Stamp cycle_started_at = now (v3.1.10, watchdog hook)
     b. Run _run_one_cycle()
        i.   Fetch fresh KLCI regime
        ii.  Record regime snapshot to regime_history (v3.1.4)
        iii. Scan all ~80 tickers (parallel yfinance pulls, timeout=15s each)
        iv.  Validate data via data_quality
        v.   Cache results in scan_cache table
        vi.  AUTO-SETTLE if autoexit_enabled:
             - Check active trades against SL/TP/trailing/time exits
             - Close any that hit
             - Feed each closed trade to learner.learn_from_trade_outcome()
             - Trigger persistence.backup() if any trade closed (v3.1.5)
        vii. AUTO-ENTRY if autotrade_enabled AND in safe-entry window:
             - Filter scan → GOLD BUY ≥ regime threshold
             - For each: run_full_risk_check → execute_entry (if pass)
             - Log AUTO_ENTRY_END with reason if zero entries fired
               (v3.1.2 includes regime trend if BEAR + below-threshold)
     c. Clear cycle_started_at = NULL (v3.1.10, in finally block)
     d. If duration > 5 min: log CYCLE_SLOW (soft warn)
7. Daily maintenance (only at 01:00-01:05 MYT, only one process wins via try_claim_daily_task):
     - prune_logs (keep last 5000 rows per log table)
     - train_setup_classifier (nightly ML retrain)
     - exploration_mode auto-disable if ≥ target trades
8. Update last_run_at, next_run_at = top of next hour
9. Sleep until next wake-up (interruptible by stop event)
```

### Watchdog sequence (every 60 s, in parallel)

```
1. Read scheduler_state. Is cycle_started_at set?
2. Is owner_pid still our PID? (If not, skip — another process is in charge.)
3. Compute age = now - cycle_started_at.
4. If age > 600 s:
     a. Log CYCLE_TIMEOUT (ERROR level)
     b. Clear cycle_started_at, set running=0, set owner_pid=-1 (sentinel),
        set last_error with stuck duration
     c. Add the stuck scheduler thread's ident to _ORPHANED_THREAD_IDS
5. Sleep 60 s (interruptible by _WATCHDOG_STOP_EVENT)
```

### Safe-entry window (v3.1.2)

| Time MYT | Auto-exits | New entries |
|---|---|---|
| 08:30–09:00 (PRE_OPEN_AM) | ❌ | ❌ |
| **09:00–12:30 (MORNING)** | **✅** | **✅** |
| 12:30–14:00 (LUNCH_BREAK) | ❌ | ❌ |
| 14:00–14:30 (PRE_OPEN_PM) | ❌ | ❌ |
| **14:30–16:00 (AFTERNOON early)** | **✅** | **✅** |
| 16:00–16:45 (AFTERNOON late) | ✅ | ❌ (too late, <1h to develop) |
| 16:45–17:00 (PRE_CLOSE + TaL) | ✅ | ❌ |
| 17:00 onwards | ❌ | ❌ |
| Weekends + 50+ public holidays | ❌ | ❌ |

---

## 8. The Self-Learning Engine

### What evolves automatically

| Layer | Updates | When | Auto? | Persisted? |
|---|---|---|---|---|
| Bayesian state priors (α, β) | Every closed trade | Instant | ✅ | ✅ Gist |
| Strategy biases (breakout_bias, pullback_bias) | Every closed trade with that strategy | Instant | ✅ | ✅ Gist |
| Sector biases | Every closed trade in that sector | Instant | ✅ | ✅ Gist |
| ML setup classifier (calibrated GBM) | All historical data | Nightly at 01:00 MYT | ✅ | ⚠️ .pkl file NOT in Gist; rebuilds nightly |
| Scanner parameters (EMA/RSI/ATR) | Walk-forward optimization | User clicks button | ⚠️ Manual | ✅ Gist (in `parameters` table) |
| Regime history (conviction trend) | Every cycle | Instant | ✅ | ✅ Gist |

### The two-phase learning cycle

**Phase 1 — EXPLORATION (first 50 closed trades)**
- Thompson sampling from each (state, action) Beta posterior
- Optimistic Beta(2,1) prior on BUY for unseen states
- Smaller shrinkage toward 50% prior (0.25× vs 0.5×)
- Agent tries setups quickly to populate the brain

**Phase 2 — EXPLOITATION (after 50 trades, auto-switch)**
- Lower confidence bound (LCB) for action selection
- Conservative: only acts on setups with statistical evidence
- Standard shrinkage (0.5× toward 50%)

### Reward function

```python
WIN  → α += min(max(|pnl_pct|/5, 0.5), 3.0)
LOSS → β += min(max(|pnl_pct|/5, 0.5), 3.0)
BREAKEVEN → β += 0.25 (small opportunity-cost penalty)
```

R-multiple per trade = `realized_pnl / (risk_per_share × shares)`

### Bias shrinkage formula

```python
# Beta(5,5) prior — equivalent to 10 imaginary trades, prevents whipsaws
wr_shrunk = (wins + 5) / (total_trades + 10)
breakout_bias = clip(wr_shrunk / 0.5, 0.75, 1.30)
```

---

## 9. Operational Runbooks

### A. How to adjust max risk per trade
1. ⚙️ Settings tab → Risk Parameters
2. Change "Max risk / trade %" value
3. Click 💾 Save Risk Parameters
4. Logged automatically in `parameter_history`

### B. How to turn auto-entry OFF (manual approval mode)
1. 🤖 Robo-Trader tab → Auto-Trading Toggles
2. Uncheck "Auto-execute new GOLD BUY entries"
3. Click 💾 Save Robo-Trader settings
4. Scheduler force-restarts automatically

### C. Emergency stop everything
1. 🤖 Robo-Trader tab → 🚨 Kill-Switch (red button)
2. Loop exits within 60 seconds; will NOT auto-restart
3. To re-enable: ⚙️ Settings → Kill-Switch section → Clear

### D. Reset capital and trades
1. ⚙️ Settings → ⚠️ Destructive actions expander
2. Click "⛔ Delete all trades + scan cache"
3. State priors persist (preserved learning)
4. For full brain wipe: stop app, delete `~/.bursa_agent_data/bursa_agent.db`, restart

### E. Set up Telegram alerts
1. Create bot via @BotFather, get token
2. Get chat ID via @userinfobot
3. Send `/start` to your new bot
4. Streamlit Cloud → Manage app → Secrets:
   ```
   TELEGRAM_BOT_TOKEN = "..."
   TELEGRAM_CHAT_ID = "..."
   ```
5. 🔔 Live Alerts tab → check "Send to Telegram" → Save → Test alert button

### F. Set up Email alerts (Gmail)
1. Enable 2-Step Verification on Google account
2. Generate App Password at https://myaccount.google.com/apppasswords
3. Streamlit Cloud → Secrets:
   ```
   ALERT_SMTP_HOST = "smtp.gmail.com"
   ALERT_SMTP_PORT = "587"
   ALERT_SMTP_USER = "you@gmail.com"
   ALERT_SMTP_PASSWORD = "<app password, no spaces>"
   ALERT_SMTP_FROM = "you@gmail.com"
   ```
4. 🔔 Live Alerts tab → check "Send to Email" → fill recipients → Save → Test

### G. Set up persistent backup (CRITICAL for long-term operation)
1. Go to https://github.com/settings/tokens **(NOT `?type=beta`)**
2. Click **"Generate new token (classic)"**
3. Note: `bursa-ai-backup`
4. Expiration: 1 year (or longer)
5. **Select only the `gist` scope** (don't check anything else)
6. Generate → copy the token (starts with `ghp_...`)
7. Streamlit Cloud → Manage app → Secrets:
   ```
   GITHUB_TOKEN = "ghp_..."
   ```
8. Restart app → ⚙️ Settings tab → 🗄️ Persistent Backup section
9. Click "💾 Backup now" → verify success message + new gist appears at https://gist.github.com/{your-username}
10. From now on, all data persists across container resets

**Important:** Fine-grained tokens (`?type=beta`) do NOT support the Gist API. You must use classic tokens.

### H. Verify the agent is running
1. Sidebar shows 🤖 Robo-Trader 🟢 RUNNING with current heartbeat
2. 🤖 Robo-Trader tab → check last_run_at within 1 hour
3. 📜 Logs → Robo-Trader scheduler → see hourly HEARTBEAT events
4. **v3.1.10:** also look for `WATCHDOG_STARTED` once per session, and absence of `CYCLE_TIMEOUT` events. A `CYCLE_SLOW` event is informational (cycle was slow but completed).

### I. Diagnose "zero auto-entries"
The system self-explains in the AUTO_ENTRY_END log message. Common reasons:
- BEAR regime + no signal ≥ 80% confidence (message now includes regime trend — see v3.1.4)
- At max concurrent positions
- All qualifiers already held
- Outside safe-entry window
- Auto-entry toggle is OFF
- yfinance outage (data quality log will show errors)

### J. Detect and fix ghost threads
- 🤖 Robo-Trader tab auto-shows a 🧟 banner if old + new heartbeat formats coexist
- Fix: Streamlit Cloud → Manage app → ⋮ → Reboot app

### K. Force an immediate scan (without waiting for hourly cycle)
After v3.1.3 debounce, the scheduler waits until the next top-of-hour after startup. To scan immediately:
- **🤖 Robo-Trader tab → ⚡ Run Cycle Now** — full scan + settle + auto-entry
- **🔍 Scanner tab → 🔥 SCAN MARKET** — scan only (no auto-entry)

### L. Restore from backup after disaster
If your data appears wiped (DB shows 0 trades, brain reset):
1. ⚙️ Settings tab → 🗄️ Persistent Backup
2. Click "♻️ Restore from latest backup"
3. Confirm the warning prompt
4. App restarts with full data restored

### M. Run tests locally before pushing changes
```bash
cd <project_root>
pip install -r requirements.txt
pytest tests/ -q
# Expect: 168 passed in ~30 seconds (v3.1.10)
```

### N. Renew the public holiday calendar (every January)

The agent uses `market_calendar.MY_PUBLIC_HOLIDAYS` to skip trading on
Bursa Malaysia holidays. This set must be extended yearly. The system
shows a maintenance reminder banner from October each year, and an
OVERDUE banner if January arrives without the new year's holidays.

**When the banner appears:**

1. Go to the **official Bursa Malaysia Trading Holidays page**:
   https://www.bursamalaysia.com/trade/our_products_services/equities/trading_holidays
   (typically updated late November / early December)
2. Open `market_calendar.py` in your GitHub repo
3. Find the `MY_PUBLIC_HOLIDAYS` set (around line 100)
4. Add the new year's block following the existing comment style:
   ```python
       # ---- YYYY ----
       "YYYY-01-01",  # New Year's Day
       "YYYY-MM-DD",  # Chinese New Year (verify exact date — lunar calendar)
       "YYYY-MM-DD",  # Chinese New Year (day 2)
       "YYYY-MM-DD",  # Thaipusam
       "YYYY-MM-DD",  # Nuzul Al-Quran
       "YYYY-MM-DD",  # Hari Raya Aidilfitri
       "YYYY-MM-DD",  # Hari Raya Aidilfitri (day 2)
       "YYYY-05-01",  # Labour Day
       "YYYY-MM-DD",  # Wesak Day
       "YYYY-MM-DD",  # Yang di-Pertuan Agong's Birthday
       "YYYY-MM-DD",  # Hari Raya Aidiladha
       "YYYY-MM-DD",  # Awal Muharram
       "YYYY-08-31",  # National Day
       "YYYY-MM-DD",  # Maulidur Rasul
       "YYYY-09-16",  # Malaysia Day
       "YYYY-MM-DD",  # Deepavali
       "YYYY-12-25",  # Christmas Day
   ```
5. Push to GitHub — Streamlit Cloud auto-redeploys
6. Verify the banner disappears + check **⚙️ Settings → 🗓️ Long-Term Maintenance Status**
7. Should now show: **"✅ Public holiday list — current year covered"**

**Critical:** Lunar/Islamic dates (Chinese New Year, Hari Raya, Thaipusam,
Deepavali, Wesak, Aidiladha, Awal Muharram, Maulidur Rasul) shift each
year. Don't guess — use Bursa's published dates. Fixed dates are only
New Year (Jan 1), Labour Day (May 1), National Day (Aug 31), Malaysia
Day (Sep 16), Christmas (Dec 25).

### O. Renew the GitHub Personal Access Token (every ~12 months)

The Gist backup requires a GitHub PAT in Streamlit Cloud Secrets. Tokens
expire (typically 1 year). When they do, backups silently fail.

The system warns automatically:
- **At 11 months** — yellow banner (give yourself buffer time)
- **At 12+ months** — red banner with "I rotated the token" button

**Steps when the banner appears:**

1. Go to https://github.com/settings/tokens **(NOT `?type=beta` — classic only)**
2. Either:
   - Click **"Regenerate"** on the existing `bursa-ai-backup` token, OR
   - Delete the old one + click **"Generate new token (classic)"**
3. Set:
   - **Note:** `bursa-ai-backup` (or include the year, e.g. `bursa-ai-backup-2027`)
   - **Expiration:** 1 year
   - **Scope:** check ONLY ☑ **`gist`**
4. **Copy the new token** immediately (starts with `ghp_...`) — you only see it once
5. Streamlit Cloud → your app → **Manage app** → **Secrets**
6. Replace the `GITHUB_TOKEN` value:
   ```
   GITHUB_TOKEN = "ghp_NEW_TOKEN_HERE"
   ```
7. Save (Streamlit auto-restarts within ~30s)
8. Open the app
9. If the red overdue banner is showing → click **"✅ I rotated the token"**
   - This resets the 11-month timer internally
   - Banner disappears
10. Verify: **⚙️ Settings → 🗄️ Persistent Backup → click 💾 Backup now**
    - Should succeed with new gist revision
    - Token reset confirmed

**If you missed the renewal window and backups have been failing:**

The agent continues running normally — trades, brain, learning all happen
in the local SQLite DB. The only risk is if a Streamlit Cloud container
reset (push, reboot, 7-day sleep) happens before you renew the token,
the data accumulated since the last successful backup is lost.

So: when the banner appears, treat it as a high-priority task. The fix
is 5 minutes; the cost of ignoring it can be weeks of lost brain learning.

### P. Diagnose a stuck scheduler (v3.1.10)

**Symptoms:**
- 🤖 Robo-Trader status badge stays 🔴 STOPPED even after clicking Start
- Heartbeat is older than 5 minutes
- `last_run_at` hasn't advanced
- Clicking Start / Force Restart / Kill-Switch + Start does nothing

**Diagnosis order:**

1. **Check the cycle log** — `📜 Logs → Robo-Trader scheduler`. Look for:
   - `CYCLE_TIMEOUT` (ERROR) → the watchdog already detected a stuck cycle. The system should self-recover within the next Streamlit rerun.
   - `CYCLE_SLOW` (WARN) → a recent cycle was slow but completed. Yahoo Finance may be degraded.
   - `START_REJECT` / `ADOPT_THREAD` (INFO) → the duplicate-loop guards are firing. Should NOT prevent recovery in v3.1.10 (the orphan registry handles this).
   - `BACKUP_FAIL` (WARN) → the Gist backup is failing. Check PAT.
2. **Check `last_error`** in the 🤖 Robo-Trader tab — if it mentions "Watchdog forced handoff", that's the v3.1.10 recovery doing its job.
3. **Click ▶️ Start** once. It should succeed even if a zombie thread is alive in the background. If it returns False, file a bug — the v3.1.10 orphan registry should have made this impossible.
4. **If nothing else works** — Streamlit Cloud → Manage app → ⋮ → Reboot app. This kills the entire Python process, eliminating any zombie threads.

---

## 10. Bugs Fixed (chronological)

Each bug has a regression test guarding against its return.

| Version | Bug | Test guarding it |
|---|---|---|
| v2.0 | Cash invariant drift from missing entry-fee accounting | `test_cash_conservation_full_cycle_tp3` |
| v2.0 | Breakout threshold off by 2% | `test_compute_indicators_columns` (indirect) |
| v2.0 | risk_check size_multiplier computed but never applied | `test_full_risk_check_applies_size_multiplier` |
| v3.0 | Default risk too aggressive (2%) for autonomous trading | `test_default_risk_per_trade_is_one_percent` |
| v3.0 | "Q-learning" was just EMA, no real RL semantics | `test_thompson_sampling_used_in_exploration_mode`, `test_exploit_mode_deterministic` |
| v3.0 | Walk-forward had data leakage (train slice unused) | Built-in 30-trade OOS minimum rejection |
| v3.1 | Telegram rejected `<br>` HTML tags | `test_send_telegram_does_not_send_br_tag` |
| v3.1 | Scheduler ghost threads from Streamlit redeploys | `test_ghost_thread_evicted_when_new_owner_claims` |
| v3.1 | Email failed silently due to filter check order | (manual fix: enable checkbox + recipients) |
| v3.1.1 | next_run_at went stale outside market hours | `test_next_run_advances_even_when_market_closed` |
| v3.1.1 | ML classifier retrained 8× per night (ghost + no idempotency) | `test_concurrent_claims_only_one_winner`, `test_dedup_collapses_daily_event_multiplications` |
| v3.1.2 | Market open was 09:15, should be 09:00 | `test_morning_open_at_9am` |
| v3.1.2 | No lunch break handling (12:30-14:00) | `test_lunch_break_is_closed` |
| v3.1.2 | No public holiday awareness | `test_public_holiday_is_not_trading_day`, `test_next_session_skips_holiday` |
| v3.1.2 | Cycle log didn't explain why 0 entries | `test_explains_below_threshold_in_bear` and 6 others |
| v3.1.3 | Every GitHub push triggered an immediate scan | `test_loop_does_not_scan_immediately_on_start`, `test_run_once_still_bypasses_debounce` |
| v3.1.4 | Cycle explanation didn't show regime trend (user couldn't tell if BEAR weakening) | `test_cycle_explanation_includes_trend_in_bear` and 6 others |
| **v3.1.5** | **DB wiped on every container reset → brain reset every redeploy → self-learning impossible long-term** | **`test_encode_decode_roundtrip`, `test_boot_restore_skips_when_local_db_has_data`, `test_backup_rate_limit` and 7 others** |
| v3.1.6 | ML classifier .pkl wasn't backed up + had no auto-train on boot → "Classifier not trained yet" indefinitely | `test_ml_persistence.py` |
| v3.1.7 | Holiday list + GitHub PAT expiry were silent failure modes | `test_maintenance_reminders.py` |
| v3.1.8 | Duplicate worker loops caused "10 SKIPs in 16 seconds" log spam from Streamlit reruns | `test_duplicate_worker_fix.py` (5 tests) |
| v3.1.9 | start() permanently blocked when local _THREAD handle lost while DB heartbeat fresh; stop()'s join window too short for in-cycle threads | `test_start_after_stop_while_mid_cycle`, `test_run_one_cycle_aborts_when_owner_changed`, `test_start_adopts_alive_thread_when_handle_lost`, `test_start_bypasses_fresh_db_when_local_thread_dead`, `test_same_process_duplicate_start_rejected_by_db_guard` |
| **v3.1.10** | **Stuck cycle (yfinance hang) survives stop() → start() Guard 2 adopts the zombie → UI permanently STOPPED with no recovery path** | **`test_zombie_thread_recovery.py` (3 tests)** |
| **v3.1.10** | **No autonomous recovery from runaway cycles — required human to click Start** | **`test_watchdog_and_cycle_tracking.py` (8 tests)** |
| **v3.1.10** | **conftest.py didn't reset scheduler module-level state → tests failed in isolation but passed in full suite (flaky)** | Fixed in `tests/conftest.py` — explicit teardown of `_THREAD`, `_ORPHANED_THREAD_IDS`, `_STOP_EVENT`, watchdog |
| **v3.1.10** | **Two yfinance calls in `evaluation.py` lacked explicit `timeout=` — Performance tab could hang indefinitely on slow Yahoo days** | Defensive fix; covered by general yfinance audit |

---


- **v3.2: ADOPT_THREAD path didn't write DB state → permanently STOPPED** ⭐ — `start()` adopted an alive thread but never set `running=1` or cleared `kill_switch=1`. Combined with `stop()` setting `kill_switch=1`, `force_restart()` left the scheduler permanently STOPPED. Fixed by removing ADOPT_THREAD entirely; `start()` now orphans all stale threads and spawns fresh.
- **v3.2: `stop()` set `kill_switch=1` unconditionally** — meant `force_restart()` → `stop()` → `start()` left `kill_switch=1` for any thread to self-kill. Fixed: `stop()` no longer sets `kill_switch`; new `engage_kill_switch()` is the only way to set it.
- **v3.2: `ensure_started()` deferred to dead containers** — Gist restore brought back `owner_pid` from a dead container with a fresh heartbeat; `ensure_started()` Case 1 deferred for up to 5 minutes. Fixed: simplified to `if not is_running(): start()`.
- **v3.3: `screener.py` `fut.result()` had no timeout** — a single hung yfinance call would block the entire scan. Fixed with `timeout=30`.
- **v3.3: `risk_params` table not in `db.py` SCHEMA** — created lazily; moved to SCHEMA for consistency.
- **v3.3: 9 unused imports across 8 modules** — removed (`math`, `yfinance`, `numpy`, `Any`, `get_myt_now`, `myt_iso`, `datetime/timezone/timedelta`).

## 11. Known Gaps & v4 Roadmap

### Known gaps (deliberately deferred)

| Gap | Impact | Why deferred |
|---|---|---|
| Single data source (yfinance) | Agent blind during outages | User explicitly deferred. Secondary source hook in `market_analyzer._try_secondary_klci()` |
| No corporate actions (splits, bonuses) | ~5% of small caps affected/year | Manual JSON workaround possible |
| Slippage model is heuristic | Real fills may differ for very thin stocks | Volume-aware version covers most cases |
| No real broker execution | Notification only | Moomoo adapter stubbed; user wants 6-month validation first |
| Public holiday list expires after 2027 | Must update yearly | Hardcoded in `market_calendar.MY_PUBLIC_HOLIDAYS` |
| GitHub PAT expires | Backups silently fail | User must rotate ~yearly |
| ML classifier .pkl not in Gist | Lost on container reset | Self-rebuilds nightly within 24h, so non-critical |
| Python `threading` cannot interrupt blocking I/O | Watchdog recovers within N min, can't cut a stuck call short | Subprocess-based scan would be a 4× code increase; per-call HTTP timeouts cover the realistic cases |

### v4 candidates (when user is ready)

1. **Moomoo OpenAPI integration** — fill in `broker_adapter.MoomooAdapter` methods, add `broker_mode = "EXECUTE"` toggle in live_trigger.py
2. **Live capital tracking** — separate `live_account` table that records real-broker mirror trades
3. **Calibration-driven auto-mode-switch** — only enable EXECUTE mode if calibration chart shows <5% deviation
4. **GitHub Actions CI** — auto-run pytest on every push
5. **Telegram interactive buttons** — APPROVE/REJECT inline keyboard for each alert
6. **Multi-account support** — track multiple paper accounts with different parameter sets
7. **Rolling-window learning** — fade brain priors older than N months so it adapts to market regime shifts
8. **ML classifier in backup** — include .pkl in Gist so it persists across resets
9. **Multi-revision restore UI** — let user pick which historical backup to restore (currently always latest)
10. **Subprocess-isolated market scan** — run `screen_all_stocks()` in a subprocess with hard `timeout=` so a Yahoo hang can be `SIGKILL`ed (currently we recover within 10 min via watchdog, but the cycle itself still wastes that time)

### Intentional "v4 scaffolding" (kept on purpose, not dead code)

These functions look unused to a casual grep but are deliberate API surface for future features:

| Function | Why kept |
|---|---|
| `broker_adapter.MoomooAdapter.*` (all methods) | v4 stub interface for real broker execution |
| `broker_adapter.get_broker_adapter()` | Factory function for v4 broker selection |
| `learner.get_ml_score()` | For future ML-confidence display in Scanner UI |
| `market_analyzer.get_market_ml_prediction()` | For future regime-prediction panel |
| `repository.get_partial_exits()` | Will surface in trade-detail UI later |
| `risk_manager.validate_stop_loss()` | Helper for future manual SL-edit UI |
| `trading_engine.add_trade_note()`, `tag_trade()` | UI extension hooks for trade annotations |

---

## 12. Conventions for Future Work

When making changes, follow these patterns to keep the system honest and maintainable.

### When fixing a bug
1. Write a failing test FIRST that reproduces the bug
2. Fix the code until the test passes
3. Don't delete the test — it's the regression guard
4. Add a row to the bug table in section 10

### When adding a feature
1. If it touches money/state, add a cash-conservation or invariant test
2. If it adds a config option, surface it in the appropriate tab
3. **If it adds a new SQLite table, ensure it's covered by the Gist backup automatically** (it is — entire DB is backed up)
4. Update `PROJECT_HANDBOOK.md` (this file) section 4 and section 6
5. Update `USER_GUIDE.md` if user-facing
6. **If it makes an external HTTP/network call, it MUST have an explicit `timeout=` kwarg** (v3.1.10 invariant; the watchdog is the safety net, not the first line of defence)

### When changing defaults
1. Update `db.py` schema seed
2. Add a column migration via `ALTER TABLE ... ADD COLUMN ... DEFAULT ...`
3. Update `risk_manager.DEFAULT_RISK_PARAMS` or equivalent
4. Update section 6 of this handbook

### When deleting code (deprecation sweep)
1. Confirm zero imports across the codebase: `grep -rn "name" --include="*.py"`
2. Confirm zero references in tests
3. Delete the code
4. Run `pytest tests/ -q` — all green = safe
5. Verify Streamlit still boots
6. Update section 5 module table if removing a whole module

### When touching the scheduler
1. **Read section 4.16 / 4.17 / 4.18 / 4.19 first.** The duplicate-loop / zombie / watchdog story is non-trivial and has accumulated multiple subtle fixes.
2. The `is_running()` badge MUST be honest — if you change it, also change the tests in `test_zombie_thread_recovery.py`.
3. If you add a new long-running operation inside `_run_one_cycle`, make sure all external I/O has explicit timeouts. The watchdog catches infinite hangs at 10 min but it shouldn't be your first line of defence.
4. Module-level state (`_THREAD`, `_ORPHANED_THREAD_IDS`, `_WATCHDOG_THREAD`) must be reset between tests — see `conftest.py`.

### When debugging on Streamlit Cloud
1. Check 📜 Logs → Robo-Trader scheduler first
2. Look for ERROR-level rows, CYCLE_ERROR, CYCLE_TIMEOUT (v3.1.10), GHOST_EXIT
3. If ghost thread suspected → Streamlit Cloud → Manage app → Reboot app
4. After significant code changes, also reboot to start fresh

### Architectural completeness checks (lesson learned in v3.1.5 + reinforced in v3.1.10)
**Always question your own infrastructure assumptions early.** Before designing any long-running system, ask:
- "Where does the data live, and what kills it?"
- "What's the cost of losing 1 week of operational data?"
- "If I had to recover from total infrastructure loss, how long would it take?"
- "What grows unbounded over the system's life?"
- "What's the longest-running scenario the design has actually been validated for?"
- "If the loop gets stuck, how long before the system recovers without human intervention?" (v3.1.10 lesson)

The v3.1.5 Gist backup should have been part of the v2 design, not a v3.1.5 hotfix. Likewise the v3.1.10 watchdog should have been part of v3.1 — for a system whose value comes from continuous unattended operation, automatic recovery is a core feature, not an ops concern.

### Code style
- Type hints on every public function
- Docstrings explain *why* not *what*
- All SQL via `repository.py` — never raw SQL in business logic
- Wrap external calls (yfinance, Telegram, SMTP, GitHub Gist) in try/except — never crash the scheduler
- **All external calls must have explicit `timeout=` kwargs** (v3.1.10)
- Log every state change to the appropriate audit table

### Testing discipline
- 168 tests, all passing, in ~30 seconds (v3.1.10; was 145 at v3.1.7)
- New features must include tests
- Bug fixes must include regression tests
- **Tests must pass both in isolation AND in the full suite** — `conftest.py` resets scheduler module state between tests (v3.1.10 fix for pre-existing flakiness)
- Run `pytest tests/ -q` before every push to GitHub

---

## 13. Long-Term Maintenance Calendar

The system is designed to run indefinitely, but a few items need annual attention.
**The agent tells you when each is due** via banners above the dashboard tabs
and in **⚙️ Settings → 🗓️ Long-Term Maintenance Status** (see v3.1.7).

| Task | Frequency | When | Detailed runbook |
|---|---|---|---|
| Append next year's Bursa public holidays to `market_calendar.MY_PUBLIC_HOLIDAYS` | Yearly | Every January (Bursa publishes in late December) | **Section 9.N** |
| Regenerate `GITHUB_TOKEN` and update Streamlit Secrets | Yearly | ~11 months after token creation (system reminds at 11 months) | **Section 9.O** |
| Review walk-forward optimization results and re-run if market regime has fundamentally shifted | Quarterly | Every 3 months (system reminds at 90+ days) | 🧠 AI Learning tab → Run Walk-Forward Optimization |
| Review Performance tab calibration chart and per-regime stats | Monthly | First weekend of each month | 📊 Performance tab |
| Verify Gist backup is still working (check 🗄️ Persistent Backup status in Settings) | Weekly | Open the app — Settings tab | ⚙️ Settings → 🗄️ Persistent Backup |
| Scan for `CYCLE_TIMEOUT` / `CYCLE_SLOW` events in scheduler log (v3.1.10) | Weekly | 📜 Logs tab | If recurring, Yahoo Finance is degraded — investigate or wait it out |

### What you DON'T need to maintain
- The scheduler thread itself (self-healing, watchdog-protected)
- Log table sizes (auto-pruned nightly at 5,000 rows per table)
- The Bayesian brain (auto-evolves with each closed trade)
- ML classifier (retrained nightly)
- Sector / strategy biases (auto-shrunk with Bayesian prior)

### What you SHOULD monitor
- Drawdown level — if it crosses 8% the agent halves position sizes; at 15% all trading pauses
- State priors growth — should keep adding new states as the agent encounters new market conditions
- Calibration chart accuracy — if "80% confidence" picks only win 50%, retune
- **Frequency of CYCLE_TIMEOUT / CYCLE_SLOW events (v3.1.10)** — occasional ones are fine (Yahoo blip); regular ones mean it's time to investigate the data source or add a secondary feed

---

## Appendix A: SQLite Schema Summary

| Table | Singleton? | Purpose |
|---|---|---|
| `trades` | No | All trade records (active + closed) |
| `partial_exits` | No | TP2 partial-exit child rows |
| `account` | Yes (id=1) | Capital, cash, equity |
| `parameters` | Yes | Scanner params (JSON blob) |
| `parameter_history` | No | Every param change with before/after |
| `bias_state` | Yes | Strategy + sector multipliers (JSON) |
| `bias_history` | No | Bias drift audit trail |
| `state_priors` | No | Per (state_id, action) Beta(α,β) |
| `learning_events` | No | Bayes updates, ML training, walk-forward |
| `scheduler_log` | No | HEARTBEAT, SKIP, CYCLE_OK, CYCLE_SLOW, CYCLE_TIMEOUT, errors |
| `scheduler_state` | Yes | Running flag, last/next run, owner_pid, toggles, **cycle_started_at (v3.1.10)** |
| `trade_log` | No | Every ENTRY/EXIT/REJECT execution event |
| `data_quality_log` | No | Per-ticker validation issues |
| `scan_cache` | Yes | Most recent screener output |
| `risk_params` | Yes | Risk parameter overrides |
| `custom_watchlist` | No | User-added tickers |
| `live_trigger_config` | Yes | Telegram/email filters + toggles |
| `alert_log` | No | Every alert sent/skipped/failed |
| `maintenance_state` | No (one row per task) | Daily-task idempotency CAS |
| `regime_history` | No | Per-cycle KLCI regime snapshots (v3.1.4) |
| `meta` | No (key/value) | Cross-container state (Gist marker, PAT rotation timestamp) — v3.1.9 |

**All tables are inside `~/.bursa_agent_data/bursa_agent.db` and are backed up to the Gist as a single file.**

---

## Appendix B: Quick Command Reference

```bash
# Start the dashboard
streamlit run app.py

# Run headless (no UI)
python -m scheduler --interval 3600

# Run tests
pytest tests/ -q                         # 191 tests, ~40s
pytest tests/test_trading_engine.py -v   # specific file
pytest tests/ -k "cash_conservation"      # match by name
pytest tests/test_zombie_thread_recovery.py -v        # v3.1.10 regression
pytest tests/test_watchdog_and_cycle_tracking.py -v   # v3.1.10 regression

# Reset everything (nuclear)
rm -rf ~/.bursa_agent_data/

# View the DB directly
sqlite3 ~/.bursa_agent_data/bursa_agent.db
> .tables
> SELECT * FROM scheduler_state;
> SELECT event, message, timestamp FROM scheduler_log
  ORDER BY id DESC LIMIT 10;

# Check rotating text log
tail -f ~/.bursa_agent_data/logs/bursa_agent.log

# Manually trigger a Gist backup (in Python REPL)
python -c "from persistence import backup; print(backup(force=True, reason='manual'))"

# Manually restore from latest Gist (in Python REPL)
python -c "from persistence import restore; print(restore())"

# Inspect the watchdog state (v3.1.10)
sqlite3 ~/.bursa_agent_data/bursa_agent.db \
  "SELECT cycle_started_at, owner_pid, running, last_error FROM scheduler_state WHERE id=1"
```

---

**This handbook supersedes any verbal description of how the system works. When in doubt, read here first.**
