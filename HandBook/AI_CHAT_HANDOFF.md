# AI Chat Handoff — Copy this into a fresh chat to continue work

Paste everything below the line into a new AI conversation when you want to continue developing this project. It gives the new assistant enough context to be immediately useful without you re-explaining.

---

## CONTEXT FOR NEW AI ASSISTANT

I'm building an autonomous AI swing-trading agent for Bursa Malaysia (KLSE). The project is live on Streamlit Cloud and has been through multiple version iterations with previous AI assistants. I need you to act as a senior software engineer and senior swing trader to help me continue maintenance and development.

### Role & expectations
- Senior SWE mindset: ask before assuming, think tradeoffs, call out risks, prefer boring proven tech
- Senior swing trader mindset: Bursa-specific conventions, realistic execution, risk-first
- Always run tests before claiming a fix works
- When fixing bugs, write a regression test for it
- When making changes, output the **complete file** for direct copy-paste to GitHub (no diffs)
- **Question infrastructure assumptions early** — for long-running systems, ask "what kills the data?" and "what kills the loop?" before adding features

### Project: BursaAI Swing Agent v3.6 (multi-market: MY + US)

**Mission:** Autonomous paper-trading agent that scans a market's universe hourly, picks GOLD BUY breakout/pullback setups, manages exits via SL/TP/trailing stops, and sends Telegram alerts. **As of v3.6 it runs two markets:** 🇲🇾 Bursa (notify-only on yfinance — Moomoo OpenAPI has no MY coverage) and 🇺🇸 US (full Moomoo OpenD execution: NOOP/SIMULATE/REAL when run locally; yfinance fallback otherwise). Self-learns via Bayesian posteriors, separate brain per market. Designed to run **indefinitely** with growing memory.

**Status:** v3.6 live. **471 tests passing in ~46 s** (full suite green in one `pytest tests/` run), **~14,350 LOC** across **27 Python modules** (23 top-level + 4 in `market_profiles/`).

**Repo location:** GitHub `autonomous_bursa_agentV3.3`, branch `feat/us-market` (https://github.com/fongway94/autonomous_bursa_agentV3.3/tree/feat/us-market)

### Data-source contract (the most-asked thing — read this) ⭐
ONE mechanism, gated per market by `MarketProfile.moomoo_available`:
- **Both markets fall back to yfinance** when Moomoo OpenD is absent.
- **US** uses Moomoo when OpenD is connected; yfinance when it isn't.
- **MY** always uses yfinance today (`moomoo_available=False`) — the Moomoo path is *gated off, not deleted*. The day Moomoo adds Bursa: flip that one flag in `my_profile.py` + connect OpenD → MY auto-goes-live. Guarded by `tests/test_data_provider.py::TestMarketGating` (incl. a flag-flip test).

### Architecture (high level)
```
Sidebar market switcher → market_profiles.active_profile()  (MY | US)
↓
Robo-Trader thread (scheduler.py, hourly, PID-owned, self-healing,
boot-debounced, v3.2 simplified lifecycle)
+ Watchdog thread (scheduler.py, every 60s, evicts runaway cycles)
+ Reconciliation step (v3.6, broker↔internal drift, US execute modes)
↓
market_calendar → market_analyzer → screener → risk_manager → trading_engine → learner
   (all dispatch on active_profile(): sessions, tz, lot, fee, slippage, universe, regime ticker)
↓
SQLite WAL — ONE FILE PER MARKET (~/.bursa_agent_data/bursa_agent_MY.db | _US.db)
↓ (every closed trade + hourly heartbeat)
persistence.py → GitHub Gist (private, per-market files) ← restored on boot
↓
data_provider (Moomoo OpenD ↔ yfinance) ; broker_adapter (Noop | MoomooUSAdapter)
↓
Streamlit dashboard (8 tabs + sidebar market switcher, light theme)
↓
notifier → Telegram + Email (when live_trigger fires)
```

### Key design decisions (don't violate without asking)

1. **Bayesian Beta(α,β) posteriors, NOT Q-learning** — correct for ~74 tickers with small samples. EXPLORE (Thompson) → EXPLOIT (LCB) auto-switch at 50 closed trades.
2. **SQLite with WAL** over JSON files — kills race conditions, 1000+ writes/sec.
3. **PID-based scheduler ownership** — evicts ghost threads from Streamlit Cloud redeploys.
4. **Boot debounce** — scheduler sleeps until next boundary on startup (no GitHub-push-storm scanning).
5. **Auto-trade ON, auto-exit ON by default**.
6. **1% max risk per trade** (lowered from 2% because auto-trade is on).
7. **Light theme locked** via config + CSS.
8. **Lot enforcement is per-market** — 100 (Bursa board lot) / 1 (US). From `active_profile().lot_size`.
9. **Volume-aware slippage**: per-market (MY 5–80 bps, US 2–35 bps), via `active_profile().slippage_fn`.
10. **Session/holidays are per-market** — MY hardcoded Bursa list through 2027; US auto-extends via `pandas_market_calendars`. `market_calendar` dispatches on the active profile.
11. **Regime-adjusted thresholds**: BULL 60% / NEUTRAL 70% / BEAR 80%. Regime ticker is per-market (`^KLSE` / `SPY`).
12. **Execution is per-market** — MY always NoopAdapter (notify-only); US has full `MoomooUSAdapter` (NOOP/SIMULATE/REAL). `broker_mode` is a `scheduler_state` column.
13. **Cash conservation invariant** must hold to within 1.00 (currency-aware) — there's a test.
14. **Every closed trade feeds the learner** — α/β updates per (state, action). Separate brain per market DB.
15. **Every external HTTP call must have explicit `timeout=`** — watchdog is the safety net, not the first line of defence.
16. **Simplified scheduler lifecycle (v3.2)** — `start()` orphans all stale threads and spawns fresh. No ADOPT_THREAD path. `stop()` does NOT set `kill_switch` (only `engage_kill_switch()` does). `ensure_started()` is just `if not is_running(): start()`.
17. **Gist backup** is critical and **per-market** — `persistence.py` backs up each market's DB (+ML pkl) to a private GitHub Gist. Without `GITHUB_TOKEN`, the brain wipes on every container reset.
18. **All tables created by `init_db()`** — and the **whole schema exists once per market DB** (`bursa_agent_MY.db` + `bursa_agent_US.db`).
19. **Corporate actions auto-adjust trades atomically** (v3.5) — splits/bonus mutate qty×ratio + prices÷ratio in a single SQLite transaction; cash-conservation invariant verified within 1.00 or rolled back. `corporate_actions_processed` table guarantees idempotency via UNIQUE(ticker, ex_date, event_type).
20. **Multi-market via `market_profiles/` (v3.6)** — business modules import `active_profile()` instead of hardcoding Bursa constants. Active market resolved by env `MARKET_MODE` → `.active_market` marker file → default MY. Adding HK/SG = one new `<code>_profile.py`. See PROJECT_HANDBOOK §14.
21. **`db.DB_PATH` override detection is by BASENAME (v3.6)** — auto values are `bursa_agent_<CODE>.db`; only foreign names (`fake.db`/`test.db`) count as test overrides. Don't revert this — it's what keeps the full pytest suite green in one run.

### Defaults (live, per-market)

- Initial paper capital: **MY RM 20,000 · US USD 5,000**
- Max risk per trade: 1%
- Max concurrent positions: **MY 8/5/3 · US 6/4/2** (BULL/NEUTRAL/BEAR)
- Drawdown warn: 8%, hard stop: 15%
- Daily trade limit: 5 new entries
- Trading window: **MY 09:00-17:00 MYT (cutoff 16:00) · US 09:30-16:00 ET (cutoff 15:30)** — UI shows ET + MYT mirror for the Malaysia-based user
- Cycle interval: 60 minutes
- Exploration target: 50 closed trades before EXPLOIT mode
- Auto-trade ON, auto-exit ON, live alerts OFF
- Broker mode default: NOOP (MY can only be NOOP; US can be NOOP/SIMULATE/REAL)
- Gist backup: every closed trade + hourly heartbeat (per-market files)
- Watchdog: cycle timeout 10 min, tick every 60 s
- Screener ThreadPool: `fut.result(timeout=30)`

### Module map (27 modules: 23 top-level + 4 in market_profiles/)

| Module | What it does |
|---|---|
| `app.py` | Streamlit UI, 8 tabs + sidebar market switcher, light theme |
| `scheduler.py` | Background daemon, hourly cycle, PID-owned, watchdog, v3.2 lifecycle, v3.6 reconciliation step |
| `screener.py` | Indicators + GOLD BUY classifier, ThreadPool with timeout (profile-aware universe) |
| `trading_engine.py` | execute_entry/exit, cash math, slippage, lots (`lot_size()`/`fee_rate()` per profile) |
| `risk_manager.py` | run_full_risk_check, drawdown breaker, time windows (currency-aware messages) |
| `learner.py` | Bayesian posteriors, walk-forward, ML classifier |
| `market_analyzer.py` | Regime (^KLSE/SPY via `_regime_ticker()`), sector momentum, RS |
| `market_calendar.py` | Sessions + holidays, dispatched on active profile (Bursa / NYSE) |
| `evaluation.py` | Sharpe, drawdown, calibration, benchmarks |
| `data_quality.py` | OHLCV validator |
| `repository.py` | All SQL access |
| `db.py` | Per-market SQLite schema + WAL connection; `_resolve_db_path()` (basename override rule) |
| `logger.py` | 6 log streams + dedupe helpers |
| `watchlist.py` | Profile-aware universe (MY Bursa+Shariah / US ETFs+megacaps) |
| `notifier.py` | Telegram (plain text) + Email (HTML) |
| `live_trigger.py` | Filter+dedup+format trade alerts (currency-aware) |
| `broker_adapter.py` | Noop + **MoomooUSAdapter (full, v3.6)** + MoomooMY stub; mirror hooks |
| `data_provider.py` | Moomoo OpenD ↔ yfinance auto-fallback, per-market gated (v3.4/v3.6) |
| `corporate_actions.py` | Split / bonus / dividend detection + atomic trade adjustment (v3.5) |
| `reconciliation.py` | **Broker↔internal drift checker + Telegram alerts (v3.6)** |
| `verify_moomoo.py` | Standalone diagnostic for local Moomoo OpenD setup |
| `persistence.py` | Per-market Gist-backed DB + ML backup/restore |
| `maintenance_reminders.py` | Holiday/PAT/WFO renewal reminders (MY) |
| `market_profiles/__init__.py` | `active_profile()`, `set_active_market()`, resolver + display-helper re-exports |
| `market_profiles/base.py` | `MarketProfile` Protocol + `format_session_window()`/`format_time_with_user_local()` |
| `market_profiles/my_profile.py` | `MY_PROFILE` singleton (Bursa) |
| `market_profiles/us_profile.py` | `US_PROFILE` singleton (NYSE/NASDAQ) |

### What's working

- Hourly scanning during the active market's sessions (MY 09:00-12:30/14:30-17:00 MYT; US 09:30-16:00 ET)
- **Dual-market switching** (v3.6): sidebar switcher, separate DB/brain/account per market, zero cross-contamination
- **Pluggable data source**: Moomoo OpenD auto-detect → yfinance fallback. US uses Moomoo live; MY gated to yfinance until OpenAPI adds Bursa.
- **US broker execution** (v3.6): `MoomooUSAdapter` NOOP/SIMULATE/REAL + reconciliation drift alerts
- **Corporate-actions handling** (v3.5): splits/bonus auto-adjust; cash dividends alerted; idempotent
- Lunch break + public holiday awareness (per market)
- Auto-exit on SL/TP3/trailing/time
- Bayesian state-prior updates on every closed trade (separate brain per market)
- DB + ML backed up to Gist (per-market), restored on boot
- Telegram + Email alerts (currency-aware)
- BEAR regime defensive behaviour
- Scheduler self-recovers from stuck loops within 10 min via watchdog
- Start/Stop/Force Restart always works (v3.2 fix)
- **All 471 tests pass — full suite green in one `pytest tests/` run**

### Recent changes (v3.2 → v3.6)

- **v3.6: MULTI-MARKET (MY + US).** NEW `market_profiles/` (base Protocol + MY/US profiles) — business modules now dispatch on `active_profile()` instead of hardcoded Bursa constants. Per-market DB files (`bursa_agent_MY.db` / `_US.db`), per-market Gist backups, sidebar market switcher. NEW `broker_adapter.MoomooUSAdapter` (full NOOP/SIMULATE/REAL execution, cherry-picked from the WallTrading-Bot reference). NEW `reconciliation.py` (broker↔internal drift + Telegram alerts). US data via Moomoo OpenD when connected, yfinance fallback; MY stays yfinance-only (OpenAPI gap, gated by `moomoo_available` flag). Settings: Trading Window panel + alerts now currency/timezone-aware (US shows `$` and `ET (… MYT)` for the Malaysia-based user). `db._resolve_db_path()` override detection switched to basename. **471 tests, full suite green in one run.**
- v3.4: NEW `data_provider.py` — Moomoo OpenD ↔ yfinance auto-fallback abstraction. `screener.py`, `market_analyzer.py`, `scheduler.py`, `app.py` migrated. Raw TCP port pre-check prevents moomoo SDK reconnect-thread spam on Streamlit Cloud. Added 📡 Data Source panel in Settings tab. 25 new tests.
- v3.3: Unused import cleanup (9 imports across 8 modules), `risk_params` added to schema, `screener.py` `fut.result(timeout=30)`, `db.executemany()` removed
- v3.2: Scheduler lifecycle refactor — removed ADOPT_THREAD, simplified start/stop/ensure_started, separated kill_switch from stop()
- Test file renames: removed version numbers from test file names
- Documentation rewrite: USER_GUIDE.md, SETUP_GUIDE.md, REVISION_HISTORY.md (replaces CHANGES_V2_TO_V3.md + CHANGES_V3_TO_V3_1.md)

### Known gaps (deliberately deferred)
- ~~Single data source (yfinance)~~ → **solved in v3.4/v3.6**: pluggable via `data_provider.py`, per-market. (Stooq as a 2nd free fallback still open.)
- ~~No corporate actions handling~~ → **v3.5**: splits/bonus auto-adjusted, cash dividends alerted. Full dividend P&L credit + rights issues deferred.
- ~~Moomoo broker adapter is stubbed~~ → **solved for US in v3.6** (`MoomooUSAdapter`). MY execution blocked by Moomoo OpenAPI's lack of Bursa coverage.
- **MY remains notify-only** until Moomoo adds Bursa to OpenAPI (flip `MY_PROFILE.moomoo_available=True` that day).
- Slippage is heuristic, not real fills
- Public holiday list expires after 2027 (MY only; US auto-extends via `pandas_market_calendars`)
- GitHub PAT expires yearly
- No rolling-window learning (stale priors could accumulate)
- No HK profile yet (architecture supports it — one new `hk_profile.py`)

### Working principles I expect from you
- **Read PROJECT_HANDBOOK.md first** for any non-trivial change
- **Run tests before claiming success** — `pytest tests/ -q` should show **471 passing** (the FULL suite must be green in ONE run, not just per-file)
- **Bug fix = write failing test first**, then fix, then test passes
- **Output complete files**, not diffs
- **Don't change defaults without asking**
- **Push back if I ask for something risky** (raising risk to 5%, disabling drawdown breaker, etc.)
- **Every external HTTP call must have an explicit `timeout=`**

### Streamlit Cloud Secrets I have configured
- `GITHUB_TOKEN` — classic PAT with `gist` scope
- `TELEGRAM_BOT_TOKEN` — from @BotFather
- `TELEGRAM_CHAT_ID` — from @userinfobot
- `ALERT_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_FROM` — Gmail app password

### Files in the repo
```
app.py, scheduler.py, screener.py, trading_engine.py, risk_manager.py,
learner.py, market_analyzer.py, market_calendar.py, evaluation.py,
data_quality.py, repository.py, db.py, logger.py, watchlist.py,
notifier.py, live_trigger.py, broker_adapter.py, persistence.py,
maintenance_reminders.py, data_provider.py, corporate_actions.py,
reconciliation.py, verify_moomoo.py, ai_parameters.json, requirements.txt,
.streamlit/config.toml

market_profiles/ (__init__.py, base.py, my_profile.py, us_profile.py)
tests/ (35 test files, 471 tests)
HandBook/ (PROJECT_HANDBOOK.md, AI_CHAT_HANDOFF.md, FINAL_EVALUATION.md, nextrecommendation.txt)
SETUP_GUIDE.md, USER_GUIDE.md, LIVE_TRIGGER_GUIDE.md, REVISION_HISTORY.md
```

> **Branch:** active development is on `feat/us-market`. `requirements.txt` now
> includes `pandas_market_calendars` (US holidays) and `moomoo-api` (optional,
> local-only for US execution).

### To get full context

Read `HandBook/PROJECT_HANDBOOK.md` — it has every design decision, defaults
table, operational runbook, bug history, schema, the v4 roadmap, and the new
**§14 Multi-Market Architecture** (the canonical reference for how MY/US are
kept separate and the data-source gating contract).

---

## NOW HERE'S WHAT I WANT TO WORK ON

[← Replace this with your specific request to the new AI]

_Status at last handoff (2026-05-30): v3.6 multi-market complete and on
`feat/us-market`; 471 tests green in one run; PROJECT_HANDBOOK §14 +
this handoff updated. Next candidates per `nextrecommendation.txt`: let it
run ~2 months to gather trades, then review calibration; defer further v4 work
(HK profile, live capital tracking, Stooq fallback) until the paper signal is
validated._
