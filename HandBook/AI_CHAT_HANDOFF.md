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

**Status:** v3.7 complete + hotfixes applied. **611 tests passing in one `pytest tests/` run — zero failures.** Merged to `main`.

**Repo location:** GitHub `autonomous_bursa_agentV3.3`, branch `main`
(https://github.com/fongway94/autonomous_bursa_agentV3.3)

---

## LIVE DEPLOYMENT STATUS (as of 2026-06-01)

### What is running right now

| | Streamlit Cloud | Local PC (user's Windows machine) |
|---|---|---|
| MY SWING | ✅ Running 24/7 | ✅ Also works |
| US SWING | ⚠️ NOOP only (no OpenD) | ✅ SIMULATE mode active |
| US INTRADAY | ⚠️ No OpenD on cloud | ✅ Paper mode ready |
| OpenD | ❌ Not reachable | ✅ Running on port 11111 |
| Book Trader mirror | ❌ | ✅ SIMULATE wired to Moomoo Book Trader |

### Local PC setup (confirmed working)
- **Moomoo Desktop** — running and logged in
- **Moomoo OpenD** — running on `127.0.0.1:11111` ✅
- **NASDAQ Basic quote subscription** — active, all 6 curated tickers served via Moomoo ✅
- **Book Trader (paper trading account)** — active in Moomoo ✅
- **US SWING → SIMULATE** — broker mirroring wired and confirmed connected ✅
- **US INTRADAY** — paper mode ready, real 5m data from OpenD via NASDAQ Basic ✅
- **Local secrets** — `.streamlit/secrets.toml` configured with GITHUB_TOKEN, TELEGRAM etc.
- **Local dashboard** — `streamlit run app.py` → `http://localhost:8501`

### What the user needs to do every time they open their PC
```
1. Open Moomoo Desktop → log in
2. Launch OpenD (separate tray app) → wait for green status
3. cd C:\Users\USER\Project\autonomous_bursa_agentV3.3
4. streamlit run app.py
5. Browser: http://localhost:8501
6. Sidebar: 🇺🇸 US → SWING → SIMULATE (or INTRADAY)
```

---

## HOTFIXES APPLIED (post v3.7 merge)

### Hotfix 1 — INTRADAY mode switch OperationalError (db.py + app.py)
**Bug:** Clicking INTRADAY in the sidebar crashed with `sqlite3.OperationalError: no such table: scheduler_state`.

**Root cause 1:** `_migrate_v36_db_if_needed()` in `db.py` was orphaned floating code — never ran. v3.6 DB files (`bursa_agent_MY.db`, `bursa_agent_US.db`) were never renamed to the v3.7 scheme (`bursa_agent_MY_SWING.db`, `bursa_agent_US_SWING.db`).

**Root cause 2:** `app.py` called `update_scheduler_state()` before `init_db()` ran on the brand-new INTRADAY DB file.

**Fix:** Converted orphaned code to `_migrate_v36_db_if_needed()` function (migrates ALL markets, not just active one). Added `init_db()` call in `app.py` before `update_scheduler_state()` on mode switch.

**Regression test:** `tests/test_intraday_mode_switch.py` — 6 tests, all passing.

### Hotfix 2 — Broker mode switch shows "disconnected" (app.py)
**Bug:** After clicking "Switch broker mode to SIMULATE", sidebar showed "🔴 Disconnected" even though OpenD was running.

**Root cause:** `app.py` called `reset_adapter_cache()` twice (once inside `set_broker_mode()` + once explicitly) leaving `_CACHED_ADAPTER = None`. Badge then showed no adapter = disconnected.

**Fix:** Removed duplicate `reset_adapter_cache()` call. Added eager `connect()` after mode switch so badge shows real state immediately.

### Files changed in hotfixes
- `db.py` — `_migrate_v36_db_if_needed()` function (proper, not orphaned)
- `app.py` — `init_db()` before mode-switch write + removed duplicate cache reset + eager connect
- `tests/test_intraday_mode_switch.py` — NEW, 6 regression tests

**Test count after hotfixes: 611 passed, 0 failed.**

---

## DATA SOURCE REALITY

| Ticker | Exchange | NASDAQ Basic | Status |
|---|---|---|---|
| TQQQ | NASDAQ | ✅ | Served by OpenD |
| GOOGL | NASDAQ | ✅ | Served by OpenD |
| MSTR | NASDAQ | ✅ | Served by OpenD |
| PLTR | NASDAQ | ✅ | Served by OpenD |
| SOXL | NYSE Arca | ✅ | Confirmed working |
| TNA | NYSE Arca | ✅ | Confirmed working |

User confirmed all 6 curated tickers return `source=moomoo` with ~390 rows of 5m data. NASDAQ Basic subscription covers all 6 — **no additional subscription needed for INTRADAY**.

For SWING daily scanning → yfinance fallback is fine (always was, since v3.3).

---

## WHAT IS NEXT (after weeks of paper trading)

The user is now running paper trades and will return after collecting data. When they come back, the agenda is:

### Review checklist (after 4-6 weeks)

**US SWING SIMULATE:**
- [ ] Does Portfolio tab match Moomoo Book Trader? (ticker, shares, direction)
- [ ] Sharpe > 1.0 over 50+ trades?
- [ ] Profit Factor > 1.5?
- [ ] Calibration chart: does 80% confidence → ~80% win rate?
- [ ] Max drawdown stayed under 8%?
- [ ] If all good → consider switching to REAL mode

**US INTRADAY (paper only):**
- [ ] 100 closed intraday trades reached? (explorer → exploit switch)
- [ ] Monthly hit rate still ≥ 65% on live paper data?
- [ ] Expectancy positive after real slippage?
- [ ] Force-flat firing correctly at 15:55 ET every day?
- [ ] If good → build Block 8 (INTRADAY broker mirroring)

### Block 8 — INTRADAY broker mirroring (when ready)
**Estimated effort: 1-2 hours.**

The hard parts are already built. Just need to wire mirror hooks into `intraday_engine.py`:
- `execute_intraday_entry()` → call `mirror_entry_to_broker()` 
- `auto_settle_intraday()` → call `mirror_exit_to_broker()` on each settled trade
- `force_flat_all_intraday()` → call `mirror_exit_to_broker()` for each forced close

Prerequisites before building Block 8:
1. ✅ 100 intraday paper trades completed
2. ✅ Live paper expectancy positive
3. ✅ SWING SIMULATE validated (orders match Book Trader)
4. ✅ OpenD quote subscription confirmed working (NASDAQ Basic ✅)

### Future roadmap (after Block 8)
- Rolling-window learning (fade priors older than N months)
- Short ORB (only when bear-market data validates it)
- HK market profile (architecture supports it — one new `hk_profile.py`)
- Stooq as 2nd free data fallback for redundancy

---

## Data-source contract (the most-asked thing — read this) ⭐

ONE mechanism, gated per market by `MarketProfile.moomoo_available`:
- **Both markets fall back to yfinance** when Moomoo OpenD is absent.
- **US** uses Moomoo when OpenD is connected; yfinance when it isn't.
- **MY** always uses yfinance today (`moomoo_available=False`) — the Moomoo path is *gated off, not deleted*. The day Moomoo adds Bursa: flip that one flag in `my_profile.py` + connect OpenD → MY auto-goes-live. Guarded by `tests/test_data_provider.py::TestMarketGating`.

**v3.7 addition:** `get_history()` now accepts `interval=` (default `"1d"` = byte-identical to v3.6). Intraday is US-only today (`supports_intraday=True` for US, `False` for MY). On Streamlit Cloud (no OpenD), intraday mode refuses to trade and shows "intraday unavailable" banner.

---

## Architecture (high level)

```
Streamlit Cloud (24/7)          Local PC (when user is at desk)
─────────────────────           ────────────────────────────────
MY SWING → yfinance             US SWING → SIMULATE → Book Trader
                                US INTRADAY → paper → brain learning
                                Both backed up to Gist (shared brain)

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
SQLite WAL — PER (MARKET, MODE):
  bursa_agent_MY_SWING.db
  bursa_agent_MY_INTRADAY.db
  bursa_agent_US_SWING.db      ← SIMULATE active, mirroring to Book Trader
  bursa_agent_US_INTRADAY.db   ← paper only (v1), brain learning
↓ (every closed trade + hourly heartbeat)
persistence.py → GitHub Gist (private, per-market-mode files) ← restored on boot
↓
data_provider (Moomoo OpenD ↔ yfinance) ; broker_adapter (Noop | MoomooUSAdapter)
↓
Streamlit dashboard (8 tabs + sidebar market/mode switchers, light theme)
↓
notifier → Telegram + Email (when live_trigger fires)
```

---

## Key design decisions (don't violate without asking)

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
25. **INTRADAY broker mirroring is NOT yet built (v3.7)** — INTRADAY is paper-only. Block 8 will add `mirror_entry_to_broker()` / `mirror_exit_to_broker()` hooks into `intraday_engine.py` after live paper validation.
26. **Book Trader = Paper Trading** — Moomoo uses these names interchangeably. Book Trader is the simulated account that SIMULATE mode mirrors to.
27. **Agent capital ($5k) ≠ Book Trader capital ($999,999)** — intentional. Agent sizes on $5k (1% risk = $50/trade). Book Trader uses its own balance but receives the same share quantities. Do NOT sync them — it would make position sizes unrealistically large.

---

## Intraday ORB Strategy Parameters (round-4 validated, do not change without re-running validate_intraday_edge.py)

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

---

## Defaults (live, per-market)

- Initial paper capital: MY RM 20,000 · US USD 5,000
- Max risk per trade: 1%
- Max concurrent positions: MY 8/5/3 · US 6/4/2 (BULL/NEUTRAL/BEAR)
- Drawdown warn: 8%, hard stop: 15%
- Daily trade limit: 5 new entries
- Trading window: MY 09:00-17:00 MYT (cutoff 16:00) · US 09:30-16:00 ET (cutoff 15:30)
- Cycle interval: 60 min (SWING) / 5 min (INTRADAY)
- Explorer target: 50 trades (SWING) / 100 trades (INTRADAY)
- Auto-trade ON, auto-exit ON, live alerts OFF
- Broker mode: MY=NOOP (fixed) · US=SIMULATE (currently active locally)
- Gist backup: every closed trade + hourly heartbeat (per-market-mode files)
- Watchdog: cycle timeout 10 min, tick every 60 s

---

## Module map (33 modules + test files)

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
| `db.py` | Per-(market,mode) SQLite schema + WAL connection; `_resolve_db_path()` (basename override rule); `_migrate_v36_db_if_needed()` (hotfix) |
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
| `intraday_engine.py` | v3.7 Block 4: Entry execution, auto-settle, force-flat invariant, session status (5 states). No broker mirroring yet (Block 8). |
| `ui_mode_helpers.py` | v3.7 Block 6: Pure UI helper functions for mode-aware rendering |
| `market_profiles/__init__.py` | active_profile(), set_active_market(), active_trading_mode(), set_trading_mode(), is_intraday(), resolver + display helpers |
| `market_profiles/base.py` | MarketProfile Protocol + TradingSession/TickerSpec + supports_intraday + intraday fields |
| `market_profiles/my_profile.py` | MY_PROFILE singleton (Bursa, supports_intraday=False) |
| `market_profiles/us_profile.py` | US_PROFILE singleton (NYSE/NASDAQ, supports_intraday=True, intraday params) |

---

## What's working

- ✅ Hourly SWING scanning during active market's sessions (MY + US)
- ✅ 5-min INTRADAY scanning during US RTH (OR_WINDOW → ACTIVE → FORCE_FLAT → POSTMARKET)
- ✅ Dual-market switching (v3.6): sidebar switcher, separate DB/brain/account per market
- ✅ Dual-mode switching (v3.7): TRADING_MODE env → .trading_mode marker file → default SWING
- ✅ Per-(market,mode) DB isolation: 4 DB files, zero cross-contamination
- ✅ Pluggable data source: Moomoo OpenD auto-detect → yfinance fallback
- ✅ US SWING broker execution: MoomooUSAdapter SIMULATE active, mirroring to Book Trader
- ✅ NASDAQ Basic quote subscription covering all 6 curated intraday tickers via OpenD
- ✅ Corporate-actions handling (v3.5): splits/bonus auto-adjust; cash dividends alerted
- ✅ Intraday ORB screener (v3.7 Block 3): 15-min opening range, VWAP, rel-vol, EMA-200 trend
- ✅ Intraday engine (v3.7 Block 4): entries, settles, force-flat at 15:55 ET invariant
- ✅ Intraday scheduler dispatch (v3.7 Block 5): 5-min cadence, session-state aware, OpenD-gated
- ✅ Mode-aware UI (v3.7 Block 6): sidebar mode switcher, intraday scanner/robo/settings panels
- ✅ Local secrets via `.streamlit/secrets.toml` (GITHUB_TOKEN, TELEGRAM etc.)
- ✅ 611 tests — full suite green in one `pytest tests/` run, zero failures

## Known gaps / deferred

- ❌ INTRADAY broker mirroring — paper only in v1; Block 8 when paper validation complete
- ❌ MY intraday — gated off until Moomoo adds Bursa to OpenAPI
- ❌ Short ORB — gated off (hurt in all tested regimes; revisit with bear-market data)
- ❌ Rolling-window learning (stale priors could accumulate)
- ❌ HK profile (architecture supports it — one new `hk_profile.py`)
- ⚠️ INTRADAY edge is narrow — +0.090R, curated-6 only; paper-only until 100 trades

---

## Working principles I expect from you

- **Read PROJECT_HANDBOOK.md first** for any non-trivial change
- **Run tests before claiming success** — `pytest tests/ -q` should show **611 passing, 0 failing** in one run
- **Bug fix = write failing test first**, then fix, then test passes
- **Output complete files**, not diffs
- **Don't change defaults without asking**
- **Push back if I ask for something risky** (raising risk to 5%, disabling drawdown breaker, etc.)
- **Every external HTTP call must have an explicit `timeout=`**

---

## Streamlit Cloud Secrets

```
GITHUB_TOKEN      — classic PAT with gist scope
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
GIST_ID
ALERT_SMTP_HOST, _PORT, _USER, _PASSWORD, _FROM
```

Local secrets → `.streamlit/secrets.toml` (NOT committed to GitHub, in .gitignore)

---

## Files in the repo (main branch)

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
tests/ (41 test files, 611 tests, 0 failures)
HandBook/ (PROJECT_HANDBOOK.md, AI_CHAT_HANDOFF.md, REVISION_HISTORY.md,
           USER_GUIDE.md, LIVE_TRIGGER_GUIDE.md, SETUP_GUIDE.md,
           FINAL_EVALUATION.md, orb_backtest_results.md)
```

---

## NOW HERE'S WHAT I WANT TO WORK ON

[← Replace this with your specific request to the new AI]

_Status at last handoff (2026-06-01): v3.7 complete + 2 hotfixes merged to `main`.
611 tests, 0 failures. System is live and paper trading:_

_- MY SWING: Streamlit Cloud, yfinance, NOOP (notify only)_
_- US SWING: Local PC, yfinance data, SIMULATE mode → mirroring to Moomoo Book Trader_
_- US INTRADAY: Local PC, OpenD real 5m data (NASDAQ Basic), paper only (no broker mirror yet)_

_User is leaving it to run for 4-6 weeks to collect paper trade data before next session._

_Next session agenda:_
_1. Review US SWING SIMULATE — does Book Trader match Portfolio tab?_
_2. Review US INTRADAY paper — 100 trades reached? Expectancy still positive?_
_3. If SWING validated → consider REAL mode_
_4. If INTRADAY validated → build Block 8 (broker mirroring for intraday)_
_5. Update PROJECT_HANDBOOK and REVISION_HISTORY with hotfix details_
