# AI Chat Handoff — Copy this into a fresh chat to continue work

Paste everything below the line into a new AI conversation when you want to continue developing this project. It gives the new assistant enough context to be immediately useful without you re-explaining.

---

## CONTEXT FOR NEW AI ASSISTANT

I'm building an autonomous AI swing-trading agent for Bursa Malaysia (KLSE) that also supports US markets. The project is live on Streamlit Cloud and has been through multiple version iterations. I need you to act as a senior software engineer and senior swing trader to help me continue maintenance and development.

### Role & expectations
- Senior SWE mindset: ask before assuming, think tradeoffs, call out risks, prefer boring proven tech
- Senior swing trader mindset: Bursa-specific conventions, realistic execution, risk-first
- Always run tests before claiming a fix works
- When fixing bugs, write a regression test for it
- When making changes, output the **complete file** for direct copy-paste to GitHub (no diffs)
- **Question infrastructure assumptions early** — for long-running systems, ask "what kills the data?" and "what kills the loop?" before adding features

### Project: BursaAI Swing Agent v3.7 (multi-market: MY + US, intraday in progress)

**Mission:** Autonomous paper-trading agent that scans a market's universe hourly (swing mode) or every 5 minutes (intraday mode), picks setups, manages exits via SL/TP, and sends Telegram alerts. **As of v3.7 it runs two modes:** SWING (daily, both MY + US) and INTRADAY (US-only via Moomoo OpenD, ORB strategy on curated-6 universe). Self-learns via Bayesian posteriors, separate brain per market AND per trading mode.

**Status:** v3.7 in progress. Blocks 1-5 complete. **593 tests** (578 pass, 2 known split-brain failures in TestScreenIntraday runner tests — they pass in isolation).

**Repo location:** GitHub `autonomous_bursa_agentV3.3`, branch `feat/intraday`

### v3.7 Block Progress

| Block | What | Status | Files |
|-------|------|--------|-------|
| **Block 1** | `data_provider.py` interval= support (daily byte-identical, 5m intraday via Moomoo OpenD or yfinance fallback) | ✅ Complete | `data_provider.py`, `tests/test_data_provider.py` (+12 tests) |
| **Backtest Harness** | ORB simulator with VWAP, rel-vol, opening range, R-multiple math | ✅ Complete | `intraday_backtest.py`, `tests/test_intraday_backtest.py` (+27 tests) |
| **Round 2-4 Research** | EMA trend filter, universe curation, parameter sweep | ✅ Complete | `intraday_backtest_v2.py`, `v3.py`, `validate_intraday_edge.py` |
| **Results** | Backtest report | ✅ Complete | `HandBook/orb_backtest_results.md` |
| **Block 2** | Trading mode resolver + supports_intraday flags + per-(market,mode) DB split | ✅ Complete | `market_profiles/base.py`, `my_profile.py`, `us_profile.py`, `__init__.py`, `db.py`, `tests/conftest.py`, `tests/test_multi_market_dispatch.py` (+9 tests) |
| **Block 3** | Intraday ORB screener with EMA-200 trend filter, VWAP, rel-vol | ✅ Complete | `intraday_screener.py`, `tests/test_intraday_screener.py` (+32 tests) |
| **Block 4** | Intraday engine: entry execution, settle, force-flat invariant | ✅ Complete | `intraday_engine.py`, `tests/test_intraday_engine.py` (+29 tests) |
| **Block 5** | Scheduler 5-min intraday cycle path + force-flat dispatch | ✅ Complete | `scheduler.py` (modified), `tests/test_scheduler_intraday.py` (+15 tests) |
| **Block 6** | UI: sidebar mode switcher + intraday tabs + backtest harness | ⛔ Not started | — |
| **Block 7** | Final tests + docs (PROJECT_HANDBOOK §15 + REVISION_HISTORY) | ⛔ Not started | — |

### Data-source contract (the most-asked thing — read this) ⭐
ONE mechanism, gated per market by `MarketProfile.moomoo_available`:
- **Both markets fall back to yfinance** when Moomoo OpenD is absent.
- **US** uses Moomoo when OpenD is connected; yfinance when it isn't.
- **MY** always uses yfinance today (`moomoo_available=False`) — the Moomoo path is *gated off, not deleted*. The day Moomoo adds Bursa: flip that one flag in `my_profile.py` + connect OpenD → MY auto-goes-live. Guarded by `tests/test_data_provider.py::TestMarketGating`.

**v3.7 addition:** Intraday is US-only today (`supports_intraday=True` for US, `False` for MY). On Streamlit Cloud (no OpenD), intraday mode refuses to trade and shows "intraday unavailable" banner.

### Architecture (high level)
```
Sidebar market switcher → market_profiles.active_profile()  (MY | US)
Sidebar mode switcher   → market_profiles.active_trading_mode()  (SWING | INTRADAY)
↓
Robo-Trader thread (scheduler.py)
├── SWING PATH: hourly cycle, scan → settle → entry (v3.6, unchanged)
└── INTRADAY PATH: 5-min cadence, US RTH only (v3.7)
    ├── OR_WINDOW (09:30-09:45 ET): scan, build opening range
    ├── ACTIVE_TRADING (09:45-15:55 ET): scan + settle + entry
    ├── FORCE_FLAT (15:55-16:00 ET): close ALL intraday positions
    └── PRE/POSTMARKET: idle
+ Watchdog thread (scheduler.py, every 60s, evicts runaway cycles)
+ Reconciliation step (v3.6, broker↔internal drift, US execute modes)
↓
market_calendar → market_analyzer → screener → risk_manager → trading_engine → learner
   (SWING path — dispatch on active_profile())
↓
intraday_screener.py → intraday_engine.py → learner
   (INTRADAY path — separate brain, separate DB)
↓
SQLite WAL — PER (MARKET, MODE):
  bursa_agent_MY_SWING.db
  bursa_agent_MY_INTRADAY.db
  bursa_agent_US_SWING.db
  bursa_agent_US_INTRADAY.db
↓ (every closed trade + hourly heartbeat)
persistence.py → GitHub Gist (private, per-market-mode files) ← restored on boot
↓
data_provider (Moomoo OpenD ↔ yfinance) ; broker_adapter (Noop | MoomooUSAdapter)
↓
Streamlit dashboard (8 tabs + sidebar market/mode switchers, light theme)
↓
notifier → Telegram + Email (when live_trigger fires)
```

Communication between modules happens via **SQLite**, not in-memory objects. Scheduler thread + UI re-renders never deadlock.

### Key design decisions (don't violate without asking)

1. **Bayesian Beta(α,β) posteriors, NOT Q-learning** — correct for small samples. EXPLORE → EXPLOIT auto-switch at 50 closed trades (swing) / 100 trades (intraday).
2. **SQLite with WAL** over JSON files — kills race conditions, 1000+ writes/sec.
3. **PID-based scheduler ownership** — evicts ghost threads from Streamlit Cloud redeploys.
4. **Boot debounce** — scheduler sleeps until next boundary on startup.
5. **Auto-trade ON, auto-exit ON by default**.
6. **1% max risk per trade** (swing); 1% for intraday too.
7. **Light theme locked** via config + CSS.
8. **Lot enforcement is per-market** — 100 (Bursa board lot) / 1 (US).
9. **Volume-aware slippage**: per-market (MY 5–80 bps, US 2–35 bps).
10. **Session/holidays are per-market** — MY hardcoded Bursa list through 2027; US auto-extends via `pandas_market_calendars`.
11. **Regime-adjusted thresholds**: BULL 60% / NEUTRAL 70% / BEAR 80%.
12. **Execution is per-market** — MY always NoopAdapter; US has full `MoomooUSAdapter` (NOOP/SIMULATE/REAL).
13. **Cash conservation invariant** must hold to within 1.00 (currency-aware).
14. **Every closed trade feeds the learner** — separate brain per (market, mode).
15. **Every external HTTP call must have explicit `timeout=`** — watchdog is the safety net.
16. **Simplified scheduler lifecycle (v3.2)** — `start()` orphans all stale threads. No ADOPT_THREAD path.
17. **Gist backup** is critical and **per-(market,mode)** — survives container resets.
18. **All tables created by `init_db()`** — schema exists once per DB file.
19. **Corporate actions auto-adjust trades atomically** (v3.5).
20. **Multi-market via `market_profiles/` (v3.6)** — one new `<code>_profile.py` to add a market.
21. **Trading mode via `market_profiles/__init__.py` (v3.7)** — `active_trading_mode()` resolves from env `TRADING_MODE` → `.trading_mode` marker file → default `SWING`.
22. **DB override detection is by BASENAME (v3.6)** — auto values are `bursa_agent_<CODE>_<MODE>.db`; foreign names are test overrides.
23. **Force-flat invariant (v3.7)** — every intraday position MUST be closed by 15:55 ET. No overnight risk. Tested and enforced.
24. **Local-only intraday enforcement (v3.7)** — on Streamlit Cloud (no OpenD), intraday mode refuses to trade and logs a warning.

### Intraday ORB Strategy Parameters (round-4 validated)
Locked from 360-day Moomoo OpenD validation with stricter parameters:

| Parameter | Value | Source |
|-----------|-------|--------|
| Universe | `["TNA", "GOOGL", "TQQQ", "MSTR", "SOXL", "PLTR"]` (curated-6) | Round-4 sweep |
| OR window | 15 min | Round-2 winner, stable across neighbors |
| Target | 2.0R | Round-4 best config |
| Rel-vol threshold | 1.2x | Baseline |
| VWAP support | True | Cheap insurance |
| EMA trend length | 200 (daily) | Round-4 sweep winner |
| Allow shorts | False | Shorts hurt in all tested regimes |
| Flat by | 15:55 ET | Per US profile |
| Cycle interval | 300 sec (5 min) | Scheduler dispatch |
| Explorer target | 100 trades | Intraday accumulates faster |
| Max risk/trade | 1% | Same as swing |

**Honest caveats:**
- 83% monthly hit rate (10/12 months positive) on curated-6 vs 50% on full-20
- +0.090R/trade expectancy (just under the +0.10R threshold; realistic post-slippage ~+0.07R)
- Max consecutive losers = 8 (on the line)
- June 2025 had a -7.22R drawdown month
- The universe curation does the heavy lifting — EMA length barely matters on full-20

### Defaults (live, per-market)

- Initial paper capital: **MY RM 20,000 · US USD 5,000**
- Max risk per trade: 1%
- Max concurrent positions: **MY 8/5/3 · US 6/4/2** (BULL/NEUTRAL/BEAR)
- Drawdown warn: 8%, hard stop: 15%
- Daily trade limit: 5 new entries
- Trading window: **MY 09:00-17:00 MYT (cutoff 16:00) · US 09:30-16:00 ET (cutoff 15:30)**
- Cycle interval: 60 minutes (swing) / 5 minutes (intraday)
- Exploration target: 50 closed trades (swing) / 100 trades (intraday)
- Auto-trade ON, auto-exit ON, live alerts OFF
- Broker mode default: NOOP
- Gist backup: every closed trade + hourly heartbeat (per-market-mode)
- Watchdog: cycle timeout 10 min, tick every 60 s
- Screener ThreadPool: `fut.result(timeout=30)`

### Module map (30 modules: 23 top-level + 4 in market_profiles/ + 3 intraday)

| Module | What it does |
|---|---|
| `app.py` | Streamlit UI, 8 tabs + sidebar market/mode switchers, light theme |
| `scheduler.py` | Background daemon, dual-path (swing hourly / intraday 5-min), PID-owned, watchdog, v3.2 lifecycle, v3.6 reconciliation, v3.7 intraday dispatch |
| `screener.py` | Indicators + GOLD BUY classifier, ThreadPool with timeout (profile-aware universe) |
| `trading_engine.py` | execute_entry/exit, cash math, slippage, lots (per profile) |
| `risk_manager.py` | run_full_risk_check, drawdown breaker, time windows (currency-aware messages) |
| `learner.py` | Bayesian posteriors, walk-forward, ML classifier |
| `market_analyzer.py` | Regime (^KLSE/SPY via `_regime_ticker()`), sector momentum, RS |
| `market_calendar.py` | Sessions + holidays, dispatched on active profile (Bursa / NYSE) |
| `evaluation.py` | Sharpe, drawdown, calibration, benchmarks |
| `data_quality.py` | OHLCV validator |
| `repository.py` | All SQL access |
| `db.py` | Per-(market,mode) SQLite schema + WAL connection; `_resolve_db_path()` (basename override rule) |
| `logger.py` | 6 log streams + dedupe helpers |
| `watchlist.py` | Profile-aware universe (MY Bursa+Shariah / US ETFs+megacaps) |
| `notifier.py` | Telegram (plain text) + Email (HTML) |
| `live_trigger.py` | Filter+dedup+format trade alerts (currency-aware) |
| `broker_adapter.py` | Noop + **MoomooUSAdapter (full, v3.6)** + MoomooMY stub; mirror hooks |
| `data_provider.py` | Moomoo OpenD ↔ yfinance auto-fallback, per-market gated (v3.4/v3.6), v3.7 interval= support |
| `corporate_actions.py` | Split / bonus / dividend detection + atomic trade adjustment (v3.5) |
| `reconciliation.py` | **Broker↔internal drift checker + Telegram alerts (v3.6)** |
| `verify_moomoo.py` | Standalone diagnostic for local Moomoo OpenD setup |
| `persistence.py` | Per-(market,mode) Gist-backed DB + ML backup/restore |
| `maintenance_reminders.py` | Holiday/PAT/WFO renewal reminders (MY) |
| `intraday_backtest.py` | ORB simulator + CLI for backtesting (research tool) |
| `intraday_backtest_v2.py` | Round-2 tuning (EMA trend filter + shorts option) |
| `intraday_backtest_v3.py` | Round-4 parameter grid sweep (EMA-100/200, curated-6, 1.5/2.0R) |
| `validate_intraday_edge.py` | OpenD-backed multi-year edge validator (read-only) |
| `intraday_screener.py` | **v3.7 Block 3:** ORB breakout scanner, outputs same signal shape as swing screener + `source: "INTRADAY"` |
| `intraday_engine.py` | **v3.7 Block 4:** Entry execution, auto-settle, force-flat invariant, session status (5 states) |
| `market_profiles/__init__.py` | `active_profile()`, `set_active_market()`, `active_trading_mode()`, `set_trading_mode()`, `is_intraday()`, resolver + display helpers |
| `market_profiles/base.py` | `MarketProfile` Protocol + `TradingSession`/`TickerSpec` + `supports_intraday` + intraday fields |
| `market_profiles/my_profile.py` | `MY_PROFILE` singleton (Bursa, `supports_intraday=False`) |
| `market_profiles/us_profile.py` | `US_PROFILE` singleton (NYSE/NASDAQ, `supports_intraday=True`, intraday params) |

### What's working

- ✅ Hourly SWING scanning during active market's sessions (MY + US)
- ✅ 5-min INTRADAY scanning during US RTH (OR_WINDOW → ACTIVE → FORCE_FLAT → POSTMARKET)
- ✅ **Dual-market switching** (v3.6): sidebar switcher, separate DB/brain/account per market
- ✅ **Dual-mode switching** (v3.7): `TRADING_MODE` env → `.trading_mode` marker file → default `SWING`
- ✅ **Per-(market,mode) DB isolation**: 4 DB files, zero cross-contamination
- ✅ **Pluggable data source**: Moomoo OpenD auto-detect → yfinance fallback
- ✅ **US broker execution** (v3.6): `MoomooUSAdapter` NOOP/SIMULATE/REAL + reconciliation
- ✅ **Corporate-actions handling** (v3.5): splits/bonus auto-adjust; cash dividends alerted
- ✅ **Intraday ORB screener** (v3.7 Block 3): 15-min opening range, VWAP, rel-vol, EMA-200 trend
- ✅ **Intraday engine** (v3.7 Block 4): entries, settles, force-flat at 15:55 ET invariant
- ✅ **Intraday scheduler dispatch** (v3.7 Block 5): 5-min cadence, session-state aware, OpenD-gated
- ✅ Lunch break + public holiday awareness (per market)
- ✅ Auto-exit on SL/TP3/trailing/time (swing) + force-flat (intraday)
- ✅ Bayesian state-prior updates on every closed trade (separate brain per market × mode)
- ✅ DB + ML backed up to Gist (per-market-mode), restored on boot
- ✅ Telegram + Email alerts (currency-aware)
- ✅ BEAR regime defensive behaviour
- ✅ Scheduler self-recovers from stuck loops within 10 min via watchdog
- ✅ Start/Stop/Force Restart always works (v3.2 fix)
- ✅ **593 tests** (578 pass in full suite, 2 known split-brain failures in TestScreenIntraday runner tests — they pass in isolation)

### Recent changes (v3.6 → v3.7)

- **v3.7 Block 1:** `data_provider.py` interval= support (daily byte-identical). 5m intraday via Moomoo OpenD or yfinance fallback. +12 tests.
- **v3.7 Backtest Harness:** ORB simulator (`intraday_backtest.py`) + 27 unit tests. Research tool only — not imported by live runtime.
- **v3.7 Rounds 2-4:** Parameter sweep via `intraday_backtest_v2.py`, `v3.py`, `validate_intraday_edge.py`. 360-day OpenD validation → curated-6 universe + EMA-200 trend filter.
- **v3.7 Block 2:** Trading mode resolver + supports_intraday flags + per-(market,mode) DB split. +9 tests.
- **v3.7 Block 3:** Intraday ORB screener with EMA-200 trend filter, VWAP, rel-vol. Signal format matches swing screener + `source: "INTRADAY"`. +32 tests.
- **v3.7 Block 4:** Intraday engine: `execute_intraday_entry()`, `auto_settle_intraday()`, `force_flat_all_intraday()` (THE invariant), `get_active_intraday_tickers()`, `intraday_session_status()` (5 states). +29 tests.
- **v3.7 Block 5:** Scheduler 5-min intraday cycle path. `_run_intraday_cycle()` dispatches on session state. Force-flat at 15:55 ET. Local-only guard (no OpenD = refuse entries). +15 tests.
- **v3.6: MULTI-MARKET (MY + US).** NEW `market_profiles/` (base Protocol + MY/US profiles) — business modules dispatch on `active_profile()`. Per-market DB files, per-market Gist backups, sidebar market switcher. NEW `broker_adapter.MoomooUSAdapter` (full NOOP/SIMULATE/REAL). NEW `reconciliation.py` (broker↔internal drift + Telegram alerts). US data via Moomoo OpenD when connected, yfinance fallback; MY stays yfinance-only. Settings: Trading Window panel + alerts now currency/timezone-aware. `db._resolve_db_path()` override detection switched to basename. **471 tests, full suite green in one run.**
- v3.5: Corporate actions auto-adjust splits/bonuses; cash dividends alert-only.
- v3.4: NEW `data_provider.py` — Moomoo OpenD ↔ yfinance auto-fallback. Raw TCP port pre-check prevents SDK reconnect-thread spam. +25 tests.
- v3.3: Unused import cleanup, `risk_params` added to schema, `screener.py` `fut.result(timeout=30)`.

### Known gaps (deliberately deferred)

- ~~Single data source (yfinance)~~ → **solved in v3.4/v3.6**: pluggable via `data_provider.py`, per-market. (Stooq as a 2nd free fallback still open.)
- ~~No corporate actions handling~~ → **v3.5**: splits/bonus auto-adjusted, cash dividends alerted. Full dividend P&L credit + rights issues deferred.
- ~~Moomoo broker adapter is stubbed~~ → **solved for US in v3.6** (`MoomooUSAdapter`). MY execution blocked by Moomoo OpenAPI's lack of Bursa coverage.
- **MY remains notify-only** until Moomoo adds Bursa to OpenAPI (flip `MY_PROFILE.moomoo_available=True` that day).
- **Intraday is US-only today** — MY has `supports_intraday=False`. The day Moomoo adds Bursa: flip the flag, and intraday auto-enables for MY (with the same ORB strategy).
- **Intraday edge is narrow** — validated on curated-6 only (TNA, GOOGL, TQQQ, MSTR, SOXL, PLTR). Adding structural losers dilutes edge to break-even. +0.090R expectancy (just under +0.10R threshold; realistic post-slippage ~+0.07R).
- **Force-flat not yet tested live** — the invariant exists and is tested in unit tests, but hasn't been validated with paper trading.
- Slippage is heuristic, not real fills.
- Public holiday list expires after 2027 (MY only; US auto-extends via `pandas_market_calendars`).
- GitHub PAT expires yearly.
- No rolling-window learning (stale priors could accumulate).
- No HK profile yet (architecture supports it — one new `hk_profile.py`).

### Working principles I expect from you
- **Read PROJECT_HANDBOOK.md first** for any non-trivial change
- **Run tests before claiming success** — `pytest tests/ -q` should show all passing (full suite green in ONE run)
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

intraday_backtest.py, intraday_backtest_v2.py, intraday_backtest_v3.py,
validate_intraday_edge.py, intraday_screener.py, intraday_engine.py

market_profiles/ (__init__.py, base.py, my_profile.py, us_profile.py)
tests/ (38 test files, ~600 tests)
HandBook/ (PROJECT_HANDBOOK.md, AI_CHAT_HANDOFF.md, FINAL_EVALUATION.md,
           nextrecommendation.txt, orb_backtest_results.md)
SETUP_GUIDE.md, USER_GUIDE.md, LIVE_TRIGGER_GUIDE.md, REVISION_HISTORY.md
```

> **Branch:** active development is on `feat/intraday`. `requirements.txt` now
> includes `pandas_market_calendars` (US holidays) and `moomoo-api` (optional,
> local-only for US execution + intraday).

### To get full context

Read `HandBook/PROJECT_HANDBOOK.md` — it has every design decision, defaults
table, operational runbook, bug history, schema, the v4 roadmap, and the new
**§14 Multi-Market Architecture** (the canonical reference for how MY/US are
kept separate and the data-source gating contract).

Read `HandBook/orb_backtest_results.md` — the full backtest write-up with
round-1 through round-4 results, parameter sweeps, and the honest caveats
about the intraday edge.

---

## NOW HERE'S WHAT I WANT TO WORK ON

[← Replace this with your specific request to the new AI]

_Status at last handoff (2026-05-31): v3.7 Blocks 1-5 complete on
`feat/intraday` branch. 593 tests (578 pass full suite, 2 known
split-brain in TestScreenIntraday). Next: Block 6 (UI mode switcher +
intraday tabs) or Block 7 (final tests + docs). Intraday edge is real but
narrow (curated-6, +0.090R, 83% monthly hit rate) — proceed with
explorer-only mode (100 trades) before letting it exploit._
