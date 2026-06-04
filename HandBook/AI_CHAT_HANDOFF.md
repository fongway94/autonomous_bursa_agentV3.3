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

**Status:** v3.7 complete + hotfixes applied. **621 tests passing in one `pytest tests/` run — zero failures.** Merged to `main`.

**Repo location:** GitHub `autonomous_bursa_agentV3.3`, branch `main`
(https://github.com/fongway94/autonomous_bursa_agentV3.3)

---

## LIVE DEPLOYMENT STATUS (as of 2026-06-03)

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

---

## HOTFIXES & STRATEGY EDGE UPGRADES APPLIED (post v3.7 merge)

- **Per-(Market, Mode) Gist Isolation:** Separated Gist backup filenames for DB and ML classifier files (e.g. `bursa_agent_US_SWING_db.b64.gz` and `bursa_agent_US_INTRADAY_db.b64.gz`). This prevents active US Swing (local PC SIMULATE) and US Intraday (local PC paper) from overwriting each other's backups in the shared Gist.
- **Streamlit Nested Button Restore Bug Fix:** Refactored the manual restore confirmation UI in `app.py`. Streamlit's native nested-button anti-pattern was causing the confirmation block to be skipped on click. Refactored to use a persistent `st.session_state["restore_confirm_active"]` flag with separate col-based "Yes" and "Cancel" buttons.
- **Enhanced Restore Toasts:** Both manual and boot-time restore functions now fire beautiful, informative `st.toast()` notifications showing the exact Gist file name, Gist ID, and restored size.
- **Skip & Failure Toasts:** Added informative toast warnings/infos when restore is skipped on boot (e.g., when local DB already has data) or manual restore is cancelled/fails.
- **Rerun-Safe Toasts:** Manual restore toasts are preserved in `st.session_state["pending_toast"]` to survive the subsequent `st.rerun()`.
- **Multi-Generational File Fallback:** If the latest v3.7 filename is not found inside the Gist, the app now automatically attempts a 3-tier cascade fallback: v3.7 name (`bursa_agent_MY_SWING_db.b64.gz`) -> v3.6 name (`bursa_agent_MY_db.b64.gz`) -> ancient v3.3 name (`bursa_agent_db.b64.gz` for MY).
- **Market-Specific Gist ID Env Fallback:** Built support for market-specific Gist ID secrets (`GIST_ID_MY` and `GIST_ID_US`) in the resolver, allowing complete, pristine separation of your Malaysia and US environments into two separate Gists.
- **Robust `_get_secret` Helper:** Created a helper that reads secrets from `os.environ`, `st.secrets`, or parses `.streamlit/secrets.toml` directly on disk, resolving the issue where background threads on your local PC failed to read updated secrets.
- **True Intraday High/Low Exits Check:** Modified `scheduler.py` to fetch cumulative Daily High/Low prices on hourly ticks for active positions. This ensures that intraday target hits (TP) or protection drops (SL) are never missed or forgotten, even if the price pullbacks by the end of the hour.
- **MA50 > MA200 Trend Alignment Filter:** Upgraded `screener.py` to add a 50-day EMA calculation and make `EMA50 > EMA200` a mandatory alignment filter for long setups, completely excluding false "dead-cat bounce" spikes in structural bear markets.
- **Volume Dry-Up (VDU) Pullback Filter:** Refactored the "GOLD BUY (PULLBACK)" trigger to mandate dry pullback volume (`is_dry_volume`), preventing the agent from buying pullbacks during high-volume institutional selling (distribution).
- **IBD RS Percentile Leader Booster:** Top 20% market leaders (RS Percentile $\ge 80\%$) now automatically receive a `+7` boost to their confidence score inside the screener.
- **Climax Run Profit Exit:** Upgraded `trading_engine.py` to add an automatic profit-locking exit if the price of an active swing trade stretches $\ge 20\%$ above its 50-day EMA, capturing vertical momentum bursts before they collapse.
- **Progressive Exposure (The Minervini Rule):** Refactored `risk_manager.py` to audit your last 5 closed trades. It automatically halves your next trade sizes (`size_multiplier = 0.5`) if you are in a 3-consecutive-loss streak or if your recent win-rate falls $\le 40\%$.
- **ATR-Based Volatility Position Sizing:** Upgraded `scheduler.py` to calculate target shares using the Average True Range (`ATR * 1.5`) rather than support distance, ensuring every stock contributes the exact same natural volatility risk and preventing "Support Squeezing" overexposure.

---

## FUTURE ROADMAP (after Block 8)
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
├── SWING PATH: hourly cycle, scan → settle → entry
└── INTRADAY PATH: 5-min cadence, US RTH only
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
16. **Gist backup is critical and per-(market,mode)** — `persistence.py` backs up each DB to a private GitHub Gist. Without `GITHUB_TOKEN`, the brain wipes on every container reset.
17. **Force-flat invariant (v3.7)** — every intraday position MUST be closed by 15:55 ET. No overnight risk. Tested at unit level.
18. **Local-only intraday enforcement (v3.7)** — on Streamlit Cloud (no OpenD), intraday mode refuses new entries and shows a banner.
19. **Curated-6 universe for intraday (v3.7)** — TNA, GOOGL, TQQQ, MSTR, SOXL, PLTR. Adding structural losers destroys the edge (proven by 360-day backtest). User can expand via Settings at their own risk.

---

## NOW HERE'S WHAT I WANT TO WORK ON

[← Replace this with your specific request to the new AI]

_Status at last handoff (2026-06-03): v3.7 complete + Gist isolation, UI nested-button, EMA50 trend alignment, VDU pullbacks, Climax profit exits, Progressive exposure, and ATR-based volatility position sizing upgrades applied. All pushed and merged to `main`.
621 tests, 0 failures. System is live and paper trading:_

_- MY SWING: Streamlit Cloud, yfinance, NOOP (notify only)_
_- US SWING: Local PC, yfinance data, SIMULATE mode → mirroring to Moomoo Book Trader_
_- US INTRADAY: Local PC, OpenD real 5m data (NASDAQ Basic), paper only (no broker mirror yet)_

_User is leaving it to run for 4-6 weeks to collect paper trade data before next session._

_Next session agenda:_
_1. Review US SWING SIMULATE — does Book Trader match Portfolio tab?_
_2. Review US INTRADAY paper — 100 trades reached? Expectancy still positive?_
_3. If SWING validated → consider REAL mode_
_4. If INTRADAY validated → build Block 8 (broker mirroring for intraday)_
