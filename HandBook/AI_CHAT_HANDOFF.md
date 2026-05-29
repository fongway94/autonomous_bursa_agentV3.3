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

### Project: BursaAI Swing Agent v3.5

**Mission:** Autonomous paper-trading agent that scans ~74 Bursa stocks hourly, picks GOLD BUY breakout/pullback setups, manages exits via SL/TP/trailing stops, and sends Telegram alerts so I can mirror trades in Moomoo manually. Self-learns from outcomes via Bayesian posteriors. Designed to run **indefinitely** with growing memory.

**Status:** Live on Streamlit Cloud, **329 tests passing in ~41 s**, **~11,214 LOC** across **22 Python modules**.

**Repo location:** GitHub (https://github.com/fongway94/autonomous_bursa_agentV3.1)

### Architecture (high level)
```
Robo-Trader thread (scheduler.py, hourly, PID-owned, self-healing,
boot-debounced, v3.2 simplified lifecycle)
+ Watchdog thread (scheduler.py, every 60s, evicts runaway cycles)
↓
market_calendar → market_analyzer → screener → risk_manager → trading_engine → learner
↓
SQLite WAL (~/.bursa_agent_data/bursa_agent.db)
↓ (every closed trade + hourly heartbeat)
persistence.py → GitHub Gist (private) ← restored on boot
↓
Streamlit dashboard (8 tabs: Scanner / Portfolio / AI Learning / Performance /
Robo-Trader / Logs / Live Alerts / Settings)
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
8. **100-share lot enforcement** (Bursa board lot).
9. **Volume-aware slippage**: 5 bps base + size-linear + liquidity penalty, capped 80 bps.
10. **Bursa session-aware** market_calendar with public holidays through 2027.
11. **Regime-adjusted thresholds**: BULL 60% / NEUTRAL 70% / BEAR 80%.
12. **Notification-only live mode** — `MoomooAdapter` stubbed for v4.
13. **Cash conservation invariant** must hold to within RM 1.00 — there's a test.
14. **Every closed trade feeds the learner** — α/β updates per (state, action).
15. **Every external HTTP call must have explicit `timeout=`** — watchdog is the safety net, not the first line of defence.
16. **Simplified scheduler lifecycle (v3.2)** — `start()` orphans all stale threads and spawns fresh. No ADOPT_THREAD path. `stop()` does NOT set `kill_switch` (only `engage_kill_switch()` does). `ensure_started()` is just `if not is_running(): start()`.
17. **Gist backup** is critical — `persistence.py` backs up the whole DB to a private GitHub Gist. Without `GITHUB_TOKEN`, the brain wipes on every container reset.
18. **All 21 tables created by `init_db()`** — including `risk_params` (moved from lazy creation in v3.3).
19. **Corporate actions auto-adjust trades atomically** (v3.5) — splits/bonus mutate qty×ratio + prices÷ratio in a single SQLite transaction; cash-conservation invariant verified within RM 1.00 or rolled back. `corporate_actions_processed` table guarantees idempotency via UNIQUE(ticker, ex_date, event_type). Detection symmetric with `data_provider`: Moomoo `request_rehab` preferred, yfinance Stock Splits / Dividends fallback. Toggle via `scheduler_state.corp_action_autoadjust` (default ON).

### Defaults (live)

- Initial paper capital: RM 20,000
- Max risk per trade: 1% (RM 200)
- Max concurrent positions: 8 (BULL) / 5 (NEUTRAL) / 3 (BEAR)
- Drawdown warn: 8%, hard stop: 15%
- Daily trade limit: 5 new entries
- Trading window: 09:00-17:00 MYT, safe-entry cutoff at 16:00
- Cycle interval: 60 minutes
- Exploration target: 50 closed trades before EXPLOIT mode
- Auto-trade ON, auto-exit ON, live alerts OFF
- Gist backup: every closed trade + hourly heartbeat
- Watchdog: cycle timeout 10 min, tick every 60 s
- Screener ThreadPool: `fut.result(timeout=30)`

### Module map (19 modules)

| Module | What it does |
|---|---|
| `app.py` | Streamlit UI, 8 tabs, light theme |
| `scheduler.py` | Background daemon, hourly cycle, PID-owned, boot-debounced, watchdog, v3.2 simplified lifecycle |
| `screener.py` | Indicators + GOLD BUY classifier, ThreadPool with timeout |
| `trading_engine.py` | execute_entry/exit, cash math, slippage, lots |
| `risk_manager.py` | run_full_risk_check, drawdown breaker, time windows |
| `learner.py` | Bayesian posteriors, walk-forward, ML classifier |
| `market_analyzer.py` | KLCI regime, sector momentum, RS |
| `market_calendar.py` | Bursa sessions + public holidays |
| `evaluation.py` | Sharpe, drawdown, calibration, benchmarks |
| `data_quality.py` | OHLCV validator |
| `repository.py` | All SQL access |
| `db.py` | SQLite schema (21 tables) + WAL connection |
| `logger.py` | 6 log streams + dedupe helpers |
| `watchlist.py` | ~74 tickers + Shariah filter |
| `notifier.py` | Telegram (plain text) + Email (HTML) |
| `live_trigger.py` | Filter+dedup+format trade events into alerts |
| `broker_adapter.py` | Moomoo stub (v4-ready) |
| `data_provider.py` | Pluggable market-data provider — Moomoo OpenD ↔ yfinance auto-fallback (v3.4) |
| `corporate_actions.py` | Split / bonus / dividend detection + atomic trade adjustment (v3.5) |
| `verify_moomoo.py` | Standalone diagnostic for local Moomoo OpenD setup (v3.4) |
| `persistence.py` | Gist-backed DB backup + restore |
| `maintenance_reminders.py` | Holiday/PAT/WFO renewal reminders |

### What's working

- Hourly scanning during Bursa sessions (09:00-12:30, 14:30-17:00)
- **Pluggable data source** (v3.4): Moomoo OpenD auto-detect → yfinance fallback. Same code runs on Streamlit Cloud (yfinance) and local PC (Moomoo real-time).
- **Corporate-actions handling** (v3.5): splits and bonus issues auto-adjust open positions atomically before each cycle's signal scan; cash dividends alerted to Telegram + Email; idempotent across cycles.
- Lunch break + public holiday awareness
- Auto-exit on SL/TP3/trailing/time
- Bayesian state-prior updates on every closed trade
- DB backed up to Gist, restored on boot
- Telegram + Email alerts (user configures)
- BEAR regime defensive behaviour
- Scheduler self-recovers from stuck loops within 10 min via watchdog
- Start/Stop/Force Restart always works (v3.2 fix)
- All 329 tests pass

### Recent changes (v3.2 → v3.5)

- v3.5: NEW `corporate_actions.py` — splits/bonus/dividend detection (Moomoo `request_rehab` + yfinance fallback) with atomic `trading_engine.apply_split_to_trade`. Scheduler runs corp-actions BEFORE regime/scan/settle on every cycle. Cash-conservation invariant verified within RM 1.00. 113 new tests including 7 parameterized cash-invariant cases and 11 end-to-end integration tests via real scheduler. Settings tab toggle + Logs tab audit trail.
- v3.4: NEW `data_provider.py` — Moomoo OpenD ↔ yfinance auto-fallback abstraction. `screener.py`, `market_analyzer.py`, `scheduler.py`, `app.py` migrated. Raw TCP port pre-check prevents moomoo SDK reconnect-thread spam on Streamlit Cloud. Added 📡 Data Source panel in Settings tab. 25 new tests.
- v3.3: Unused import cleanup (9 imports across 8 modules), `risk_params` added to schema, `screener.py` `fut.result(timeout=30)`, `db.executemany()` removed
- v3.2: Scheduler lifecycle refactor — removed ADOPT_THREAD, simplified start/stop/ensure_started, separated kill_switch from stop()
- Test file renames: removed version numbers from test file names
- Documentation rewrite: USER_GUIDE.md, SETUP_GUIDE.md, REVISION_HISTORY.md (replaces CHANGES_V2_TO_V3.md + CHANGES_V3_TO_V3_1.md)

### Known gaps (deliberately deferred)
- ~~Single data source (yfinance)~~ → **solved in v3.4**: pluggable via `data_provider.py`. (Adding Stooq as a 2nd free fallback for full redundancy is still open.)
- ~~No corporate actions handling~~ → **partially solved in v3.5**: splits/bonus auto-adjusted, cash dividends alerted. Full dividend P&L credit and rights issues deferred to v6.
- No corporate actions handling (splits/bonuses)
- Slippage is heuristic, not real fills
- Moomoo broker adapter is stubbed
- Public holiday list expires after 2027
- GitHub PAT expires yearly
- No rolling-window learning (stale priors could accumulate)

### Working principles I expect from you
- **Read PROJECT_HANDBOOK.md first** for any non-trivial change
- **Run tests before claiming success** — `pytest tests/ -q` should show **191 passing**
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
verify_moomoo.py, ai_parameters.json, requirements.txt,
.streamlit/config.toml

tests/ (31 test files, 329 tests)
HandBook/ (PROJECT_HANDBOOK.md, AI_CHAT_HANDOFF.md)
SETUP_GUIDE.md, USER_GUIDE.md, LIVE_TRIGGER_GUIDE.md, REVISION_HISTORY.md
```

### To get full context

Ask me to upload `PROJECT_HANDBOOK.md` — it has every design decision, defaults table, operational runbook, bug history, schema, and v4 roadmap.

---

## NOW HERE'S WHAT I WANT TO WORK ON

[← Replace this with your specific request to the new AI]
