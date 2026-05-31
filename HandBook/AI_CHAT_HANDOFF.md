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
- When making changes, output the complete file for direct copy-paste to GitHub (no diffs)
- Question infrastructure assumptions early — for long-running systems, ask "what kills the data?" and "what kills the loop?" before adding features

### Project: BursaAI Swing Agent v3.7 (multi-market: MY + US, dual-mode: SWING + INTRADAY)

**Mission:** Autonomous paper-trading agent that scans a market's universe hourly (SWING mode) or every 5 minutes (INTRADAY mode), picks setups, manages exits via SL/TP, and sends Telegram alerts. As of v3.7 it runs two modes: SWING (daily, both MY + US) and INTRADAY (US-only via Moomoo OpenD, ORB strategy on curated-6 universe). Self-learns via Bayesian posteriors, **separate brain per (market, mode)**.

**Status:** v3.7 complete. **605 tests passing in one `pytest tests/` run — zero failures.** Branch: `feat/intraday`.

**Repo location:** GitHub `autonomous_bursa_agentV3.3`, branch `feat/intraday`
(https://github.com/fongway94/autonomous_bursa_agentV3.3/tree/feat/intraday)

### Data-source contract (the most-asked thing — read this) ⭐

ONE mechanism, gated per market by `MarketProfile.moomoo_available`:
- **Both markets fall back to yfinance** when Moomoo OpenD is absent.
- **US** uses Moomoo when OpenD is connected; yfinance when it isn't.
- **MY** always uses yfinance today (`moomoo_available=False`) — the Moomoo path is *gated off, not deleted*. The day Moomoo adds Bursa: flip that one flag in `my_profile.py` + connect OpenD → MY auto-goes-live. Guarded by `tests/test_data_provider.py::TestMarketGating`.

**v3.7 addition:** `get_history()` now accepts `interval=` (default `"1d"` = byte-identical to v3.6). Intraday is US-only today (`supports_intraday=True` for US, `False` for MY). On Streamlit Cloud (no OpenD), intraday mode refuses to trade and shows "intraday unavailable" banner.

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
  bursa_agent_US_SWING.db      ← live today (SWING)
  bursa_agent_US_INTRADAY.db   ← live today (INTRADAY)
↓ (every closed trade + hourly heartbeat)
persistence.py → GitHub Gist (private, per-market-mode files) ← restored on boot
↓
data_provider (Moomoo OpenD ↔ yfinance) ; broker_adapter (Noop | MoomooUSAdapter)
↓
Streamlit dashboard (8 tabs + sidebar market/mode switchers, light theme)
↓
notifier → Telegram + Email (when live_trigger fires)
```

### Key design decisions (don't violate without asking)

1. **Bayesian Beta(α,β) posteriors, NOT Q-learning** — correct for small samples. EXPLORE (Thompson) → EXPLOIT (LCB) at 50 closed trades (swing) / 100 trades (intraday).
2. **SQLite with WAL** over JSON files — kills race conditions.
3. **PID-based scheduler ownership** — evicts ghost threads from Streamlit Cloud redeploys.
4. **Boot debounce** — scheduler sleeps until next boundary on startup.
5. **Auto-trade ON, auto-exit ON by default.**
6. **1% max risk per trade** (swing and intraday).
7. **Light theme locked** via config + CSS.
8. **Lot enforcement is per-market** — 100 (Bursa board lot) / 1 (US).
9. **Volume-aware slippage**: per-market (MY 5–80 bps, US 2–35 bps).
10. **Session/holidays are per-market** — MY hardcoded through 2027; US auto-extends via `pandas_market_calendars`.
11. **Regime-adjusted thresholds**: BULL 60% / NEUTRAL 70% / BEAR 80%.
12. **Execution is per-market** — MY always NoopAdapter; US has full `MoomooUSAdapter`.
13. **Cash conservation invariant** must hold to within 1.00 (currency-aware).
14. **Every closed trade feeds the learner** — separate brain per (market, mode).
15. **Every external HTTP call must have explicit `timeout=`** — watchdog is the safety net.
16. **Simplified scheduler lifecycle (v3.2)** — `start()` orphans all stale threads and spawns fresh. No ADOPT_THREAD path.
17. **Gist backup is critical and per-(market,mode)** — `persistence.py` backs up each DB to a private GitHub Gist. Without `GITHUB_TOKEN`, the brain wipes on every container reset.
18. **All tables created by `init_db()`** — schema exists once per (market, mode) DB file.
19. **Corporate actions auto-adjust trades atomically** (v3.5).
20. **Multi-market via `market_profiles/` (v3.6)** — active market resolved by env `MARKET_MODE` → `.active_market` marker file → default MY.
21. **`db.DB_PATH` override detection is by BASENAME (v3.6)** — auto values are `bursa_agent_<CODE>_<MODE>.db`; only foreign names count as test overrides.
22. **Force-flat invariant (v3.7)** — every intraday position MUST be closed by 15:55 ET. No overnight risk. Tested at unit level.
23. **Local-only intraday enforcement (v3.7)** — on Streamlit Cloud (no OpenD), intraday mode refuses new entries and shows a banner.
24. **Curated-6 universe for intraday (v3.7)** — TNA, GOOGL, TQQQ, MSTR, SOXL, PLTR. Adding structural losers destroys the edge (proven by 360-day backtest). User can expand via Settings at their own risk.

### Intraday ORB Strategy Parameters (round-4 validated, do not change without re-running validate_intraday_edge.py)

| Parameter | Value | Rationale |
|---|---|---|
| Universe | TNA, GOOGL, TQQQ, MSTR, SOXL, PLTR | Curated-6; full-20 produces no edge (+0.012R) |
| OR minutes | 15 | Round-2 winner; stable across 10/20 min neighbors |
| Target | 2.0R | Round-4 best; stable from 1.5R–3.0R |
| Rel-vol | 1.2× | Baseline |
| VWAP support | Required | Cheap insurance |
| EMA length | 200 (daily) | The critical filter |
| Direction | Longs only | Shorts hurt in all tested regimes |
| Flat-by | 15:55 ET | Hard invariant — non-negotiable |
| Cycle | 300 s (5 min) | Matches bar size |
| Explorer target | 100 trades | ~3-4 weeks before EXPLOIT mode |

Honest caveats: +0.090R expectancy (just under +0.10R; realistic post-slippage: ~+0.07R). 83% monthly hit rate on curated-6. Max consecutive losers = 8. Use paper-trading explorer mode for first 100 trades before trusting the edge.

### Defaults (live, per-market)

- Initial paper capital: MY RM 20,000 · US USD 5,000
- Max risk per trade: 1%
- Max concurrent positions: MY 8/5/3 · US 6/4/2 (BULL/NEUTRAL/BEAR)
- Drawdown warn: 8%, hard stop: 15%
- Daily trade limit: 5 new entries
- Trading window: MY 09:00-17:00 MYT (cutoff 16:00) · US 09:30-16:00 ET (cutoff 15:30)
- Cycle interval: 60 min (SWING) / 5 min (INTRADAY)
- Explorer target: 50 trades (SWING) / 100 trades (INTRADAY)
- Auto-trade ON, auto-exit ON, live alerts OFF
- Broker mode default: NOOP
- Gist backup: every closed trade + hourly heartbeat (per-market-mode files)
- Watchdog: cycle timeout 10 min, tick every 60 s
- Screener ThreadPool: `fut.result(timeout=30)`

### Module map (33 modules: 23 top-level + 4 in market_profiles/ + 6 intraday)

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
| `broker_adapter.py` | Noop + MoomooUSAdapter (full, v3.6) + MoomooMY stub; mirror hooks |
| `data_provider.py` | Moomoo OpenD ↔ yfinance auto-fallback, per-market gated (v3.4/v3.6), v3.7 interval= support |
| `corporate_actions.py` | Split / bonus / dividend detection + atomic trade adjustment (v3.5) |
| `reconciliation.py` | Broker↔internal drift checker + Telegram alerts (v3.6) |
| `verify_moomoo.py` | Standalone diagnostic for local Moomoo OpenD setup |
| `persistence.py` | Per-(market,mode) Gist-backed DB + ML backup/restore |
| `maintenance_reminders.py` | Holiday/PAT/WFO renewal reminders (MY) |
| `intraday_backtest.py` | ORB simulator + CLI (research only — not imported by live runtime) |
| `intraday_backtest_v2.py` | Round-2 tuning (EMA trend filter + short ORB option) |
| `intraday_backtest_v3.py` | Round-4 parameter grid sweep (EMA-100/200, curated-6, 1.5/2.0R) |
| `validate_intraday_edge.py` | OpenD-backed multi-year edge validator (read-only) |
| `intraday_screener.py` | v3.7 Block 3: ORB breakout scanner, outputs same signal shape as swing screener + source="INTRADAY" |
| `intraday_engine.py` | v3.7 Block 4: Entry execution, auto-settle, force-flat invariant, session status (5 states) |
| `ui_mode_helpers.py` | v3.7 Block 6: Pure UI helper functions for mode-aware rendering |
| `market_profiles/__init__.py` | active_profile(), set_active_market(), active_trading_mode(), set_trading_mode(), is_intraday(), resolver + display helpers |
| `market_profiles/base.py` | MarketProfile Protocol + TradingSession/TickerSpec + supports_intraday + intraday fields |
| `market_profiles/my_profile.py` | MY_PROFILE singleton (Bursa, supports_intraday=False) |
| `market_profiles/us_profile.py` | US_PROFILE singleton (NYSE/NASDAQ, supports_intraday=True, intraday params) |

### What's working

- ✅ Hourly SWING scanning during active market's sessions (MY + US)
- ✅ 5-min INTRADAY scanning during US RTH (OR_WINDOW → ACTIVE → FORCE_FLAT → POSTMARKET)
- ✅ Dual-market switching (v3.6): sidebar switcher, separate DB/brain/account per market
- ✅ Dual-mode switching (v3.7): TRADING_MODE env → .trading_mode marker file → default SWING
- ✅ Per-(market,mode) DB isolation: 4 DB files, zero cross-contamination
- ✅ Pluggable data source: Moomoo OpenD auto-detect → yfinance fallback
- ✅ US broker execution (v3.6): MoomooUSAdapter NOOP/SIMULATE/REAL + reconciliation
- ✅ Corporate-actions handling (v3.5): splits/bonus auto-adjust; cash dividends alerted
- ✅ Intraday ORB screener (v3.7 Block 3): 15-min opening range, VWAP, rel-vol, EMA-200 trend
- ✅ Intraday engine (v3.7 Block 4): entries, settles, force-flat at 15:55 ET invariant
- ✅ Intraday scheduler dispatch (v3.7 Block 5): 5-min cadence, session-state aware, OpenD-gated
- ✅ Mode-aware UI (v3.7 Block 6): sidebar mode switcher, intraday scanner/robo/settings panels
- ✅ Lunch break + public holiday awareness (per market)
- ✅ Auto-exit on SL/TP3/trailing/time (swing) + force-flat (intraday)
- ✅ Bayesian state-prior updates on every closed trade (separate brain per market × mode)
- ✅ DB + ML backed up to Gist (per-market-mode), restored on boot
- ✅ Telegram + Email alerts (currency-aware)
- ✅ BEAR regime defensive behaviour
- ✅ Scheduler self-recovers from stuck loops within 10 min via watchdog
- ✅ Start/Stop/Force Restart always works (v3.2 fix)
- ✅ **605 tests — full suite green in one `pytest tests/` run, zero failures**

### Recent changes (v3.6 → v3.7)

- **v3.7 Block 1:** `data_provider.py` gets `interval=` param. Default `"1d"` is byte-identical. 5m intraday via Moomoo OpenD or yfinance fallback. +12 tests.
- **v3.7 Backtest Harness:** ORB simulator (`intraday_backtest.py`) + 27 unit tests + round-2/3/4 research scripts. 360-day OpenD validation → curated-6 + EMA-200 filter.
- **v3.7 Block 2:** Trading mode resolver + `supports_intraday` flags + per-(market,mode) DB split. +9 tests.
- **v3.7 Block 3:** Intraday ORB screener. Signal format matches swing screener + `source: "INTRADAY"`. +32 tests.
- **v3.7 Block 4:** Intraday engine: `execute_intraday_entry()`, `auto_settle_intraday()`, `force_flat_all_intraday()` (THE invariant), `get_active_intraday_tickers()`, `intraday_session_status()` (5 states). +29 tests.
- **v3.7 Block 5:** Scheduler 5-min intraday cycle path. `_run_intraday_cycle()` dispatches on session state. Force-flat at 15:55 ET. Local-only guard (no OpenD = refuse entries). +15 tests.
- **v3.7 Block 6:** `app.py` mode-aware UI + `ui_mode_helpers.py`. Sidebar mode switcher, intraday scanner/robo/settings panels, unavailability banner. +10 tests.
- **v3.7 Block 7:** Fixed 2 full-suite-only `TestScreenIntraday` runner test failures. Wrote `PROJECT_HANDBOOK §15`. Updated `REVISION_HISTORY.md`. **605 tests, 0 failures.**
- **v3.6:** MULTI-MARKET (MY + US). `market_profiles/`, per-market DB files, sidebar market switcher, `MoomooUSAdapter`, `reconciliation.py`. US data via Moomoo OpenD when connected, yfinance fallback; MY stays yfinance-only. 471 tests.

### Known gaps (deliberately deferred)

- MY intraday is gated off (`supports_intraday=False`) until Moomoo adds Bursa — flip the flag the day it happens
- Intraday edge is narrow — +0.090R, curated-6 only; validated paper-only with 100-trade explorer before exploiting
- Slippage is heuristic, not real fills
- Public holiday list expires after 2027 (MY only; US auto-extends via `pandas_market_calendars`)
- GitHub PAT expires yearly
- No rolling-window learning (stale priors could accumulate)
- No HK profile yet (architecture supports it — one new `hk_profile.py`)
- Short ORB gated off (hurt in all tested regimes; add as separate block when bear-market data is available)

### Working principles I expect from you

- **Read PROJECT_HANDBOOK.md first** for any non-trivial change
- **Run tests before claiming success** — `pytest tests/ -q` should show **605 passing, 0 failing** in one run
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
reconciliation.py, verify_moomoo.py, ui_mode_helpers.py,
ai_parameters.json, requirements.txt, .streamlit/config.toml

intraday_backtest.py, intraday_backtest_v2.py, intraday_backtest_v3.py,
validate_intraday_edge.py, intraday_screener.py, intraday_engine.py

market_profiles/ (__init__.py, base.py, my_profile.py, us_profile.py)
tests/ (38 test files, 605 tests, 0 failures)
HandBook/ (PROJECT_HANDBOOK.md, AI_CHAT_HANDOFF.md, REVISION_HISTORY.md,
           FINAL_EVALUATION.md, nextrecommendation.txt, orb_backtest_results.md)
SETUP_GUIDE.md, USER_GUIDE.md, LIVE_TRIGGER_GUIDE.md
```

> **Branch:** `feat/intraday`. `requirements.txt` includes `pandas_market_calendars`
> (US holidays) and `moomoo-api` (optional, local-only for US execution + intraday).

### To get full context

Read `HandBook/PROJECT_HANDBOOK.md` — it has every design decision, defaults
table, operational runbook, bug history, schema, the v4 roadmap, §14 Multi-Market
Architecture, and **§15 Intraday Architecture (v3.7)** (the canonical reference
for the ORB strategy, session dispatch, force-flat invariant, and DB isolation).

Read `HandBook/orb_backtest_results.md` — the full 4-round backtest write-up
with parameter sweeps and honest caveats about the intraday edge.

---

## NOW HERE'S WHAT I WANT TO WORK ON

[← Replace this with your specific request to the new AI]

_Status at last handoff (2026-05-31): v3.7 Blocks 1-7 complete on `feat/intraday`.
605 tests, 0 failures in full suite. Intraday edge validated (curated-6, +0.090R,
83% monthly hit rate). Next: let it paper-trade for 3-4 weeks in INTRADAY EXPLORER
mode (100 trades) before reviewing calibration; then decide whether to move to
EXPLOIT mode or tune further. Defer further v4 work (HK profile, short ORB,
rolling-window learning) until paper signal is validated._
