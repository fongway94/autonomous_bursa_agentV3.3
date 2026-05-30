# BursaAI Swing Agent — User Guide (v3.6, multi-market)

A quick-reference manual for running the autonomous swing-trading agent across
**🇲🇾 Bursa Malaysia (KLSE)** and **🇺🇸 US (NYSE/NASDAQ)**.

> **Important:** The default mode is **paper trading** — simulated P&L using
> realistic fees and slippage, with Telegram/email alerts so you can mirror
> manually. The **US** market additionally supports *real* Moomoo execution
> (SIMULATE / REAL) when you run locally with Moomoo OpenD. The **MY** market is
> notification-only (Moomoo OpenAPI has no Bursa coverage yet). Always validate
> with your own research before risking real money.

---

## Table of Contents

1. [First Launch](#1-first-launch)
2. [Switching Markets (MY ↔ US)](#2-switching-markets-my--us)
3. [What the Robo-Trader Does by Default](#3-what-the-robo-trader-does-by-default)
4. [How the Self-Learning Works](#4-how-the-self-learning-works)
5. [US Execution & Reconciliation](#5-us-execution--reconciliation)
6. [Daily Workflow](#6-daily-workflow)
7. [Key Controls — Where to Find Everything](#7-key-controls--where-to-find-everything)
8. [Common Operations](#8-common-operations)
9. [Important Notes & Cautions](#9-important-notes--cautions)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. First Launch

```bash
streamlit run app.py
```

Browser opens at `http://localhost:8501`.

**The Robo-Trader starts itself the moment the page loads.** No buttons to click.

| Time | What you see |
|---|---|
| 0 s | Light-themed dashboard loads, 8 tabs + sidebar **🌐 Market** switcher |
| 1 s | Sidebar shows the active market flag (🇲🇾/🇺🇸), `🤖 Robo-Trader 🟢 RUNNING`, `Brain mode 🔬 EXPLORE` |
| 2 s | First HEARTBEAT logged in 📜 Logs → Robo-Trader scheduler |
| ≤ 1 h | First scan runs at the next top-of-hour (during that market's hours) |

The default market on first boot is **🇲🇾 MY**. Click
**🤖 Robo-Trader → ⚡ Run Cycle Now** to fire an immediate cycle without waiting.

---

## 2. Switching Markets (MY ↔ US)

Each market has its **own** database, brain, account, and parameters — switching
never mixes Bursa and US data.

### How to switch
- **Sidebar → 🌐 Market dropdown** → pick 🇲🇾 MY or 🇺🇸 US → the page reloads on the new market. *(recommended)*
- **Env var** (sets default on boot): `MARKET_MODE=US streamlit run app.py`
- **Marker file**: `echo US > ~/.bursa_agent_data/.active_market`

Resolution order: **env var > marker file > default (MY)**.

### What's different per market

| Setting | 🇲🇾 MY (Bursa) | 🇺🇸 US (NYSE/NASDAQ) |
|---|---|---|
| Currency shown | RM | $ |
| Lot size | 100 (board lot) | 1 |
| Default capital | RM 20,000 | $ 5,000 |
| Sessions | 09:00–12:30 + 14:30–17:00 **MYT** (lunch break) | 09:30–16:00 **ET** |
| Safe-entry cutoff | 16:00 MYT | 15:30 ET (≈03:30 MYT) |
| Regime ticker | `^KLSE` | `SPY` |
| Fees | 0.15% per side | 0% (commission-free) |
| Max positions | BULL 8 / NEUTRAL 5 / BEAR 3 | BULL 6 / NEUTRAL 4 / BEAR 2 |
| Universe | ~74 Bursa + Shariah filter | leveraged ETFs + mega-caps |
| Data source | yfinance only (Moomoo gap) | Moomoo OpenD when connected, else yfinance |
| Execution | Notify-only | NOOP / SIMULATE / REAL |

### 🌏 You're in Malaysia trading US — timezone display
Because the app knows you run from Malaysia, **US session and cutoff times show
in native ET *with* the MYT equivalent in brackets** — e.g.
`09:30–16:00 ET (21:30–04:00 MYT)` and cutoff `15:30 ET (03:30 MYT)`. When you
edit the Trading Window in Settings for the US market, **enter the values in ET**
(the input labels say so). The agent's internal clock and the 01:00 nightly
maintenance always stay MYT regardless of market.

---

## 3. What the Robo-Trader Does by Default

| Setting | Default | Effect |
|---|---|---|
| **Auto-exit** | ✅ ON | Closes positions at SL / TP / trailing stop / time exit |
| **Auto-entry** | ✅ ON | Opens new positions on high-confidence GOLD BUY signals |
| **Cycle interval** | 60 min | Hourly during that market's hours |
| **Brain mode** | 🔬 EXPLORE | Thompson sampling for the first 50 closed trades (per market) |
| **Max risk / trade** | 1.0% | Conservative for unsupervised trading |
| **Broker mode** | NOOP | Notify-only (MY can only be NOOP; US can switch to SIMULATE/REAL) |
| **Kill-switch** | Off | One-click emergency stop available |

### The hourly cycle

```
1. HEARTBEAT logged
2. Corporate actions: auto-adjust open trades for splits / bonus (before anything else)
3. Fetch fresh regime (^KLSE or SPY) → BULL / NEUTRAL / BEAR
4. Scan the active market's universe (parallel, 30 s timeout per ticker)
5. Cache results in scan_cache
6. Auto-exit: settle TPs / SLs / trailing / time exits on active trades
7. Auto-entry: open GOLD BUYs above regime-adjusted confidence threshold
8. Learn: every closed trade updates that market's Bayesian brain
9. Reconcile (US execute modes only): compare to broker, alert on drift
10. Backup brain to GitHub Gist (if configured)
11. Sleep until next top-of-hour
```

### Regime-adjusted thresholds

| Regime | Confidence threshold | Max positions (MY / US) | Max hold |
|---|---|---|---|
| BULL | 60% | 8 / 6 | 14 days |
| NEUTRAL | 70% | 5 / 4 | 7 days |
| BEAR | 80% | 3 / 2 | 5 days |

Doing fewer trades in BEAR is correct behaviour — the agent stays defensive.

---

## 4. How the Self-Learning Works

> Each market has a **separate brain** (separate `state_priors`, biases, ML
> model). Switching markets does not share learning between MY and US.

### Phase 1 — EXPLORATION 🔬 (first 50 closed trades, per market)

- Every setup is bucketed into one of ~250 states (RSI × volume × trend × MACD).
- The agent samples a win-rate estimate from its Beta(α,β) posterior — **Thompson sampling**.
- This intentionally tries a variety of setups so the brain gathers data fast.

### Phase 2 — EXPLOITATION 🎯 (automatic after 50 trades)

- Switches to the **lower confidence bound (LCB)** — only acts on statistically proven setups.
- Badge in sidebar changes to **🎯 EXPLOIT**.

### What every closed trade teaches

1. **State's Beta posterior** — α += win_weight (WIN) or β += loss_weight (LOSS)
2. **Strategy bias** — breakout_bias / pullback_bias adjusted with Bayesian shrinkage
3. **Sector bias** — per-sector multiplier adjusted the same way

### Where to see it
- **🧠 AI Learning** → Bayesian State Priors table
- **📜 Logs** → Learning events, Bias updates

### Brain persistence (per market)

Each market's SQLite DB (`bursa_agent_MY.db` / `bursa_agent_US.db`) — plus its ML
model — is backed up to a private GitHub Gist after every closed trade and every
hourly heartbeat, and restored automatically on container restart before the
scheduler starts.

**Without `GITHUB_TOKEN` configured, the brain is lost on every container reset.**

---

## 5. US Execution & Reconciliation

US is the only market with real broker execution (Moomoo OpenAPI has no Bursa
coverage, so MY stays notify-only).

### Broker modes (US, set in ⚙️ Settings → 🎯 Execution Mode)
- **NOOP** *(default)* — paper trades + Telegram alerts only; you mirror manually.
- **SIMULATE** — the agent places real orders in Moomoo's paper-trading account (no real money). **Recommended for the first 4–6 weeks.**
- **REAL** — live orders with real money. Requires `MOOMOO_TRADING_PWD` set + Moomoo OpenD running + an unlocked trading session. Only after SIMULATE has matched paper outcomes.

**Requirements for SIMULATE/REAL:** run on your local PC with **Moomoo Desktop +
OpenD enabled** (Settings → API → port 11111). See `SETUP_GUIDE.md` for the full
walkthrough. On Streamlit Cloud the agent is always NOOP (OpenD unreachable).

### Reconciliation (US execute modes)
Every cycle the agent compares its internal positions/cash to the broker and
sends a Telegram **RECONCILE_DRIFT** alert if equity drift exceeds **0.5%** or any
position quantity differs (>1 share / >1%). It **never** overwrites internal state
from broker data — drift is observation-only. Small drift (<0.5%) is normal
(internal uses heuristic slippage; the broker uses real fills). See
**⚙️ Settings → 🔄 Broker Reconciliation** for status + a "Run now" button.

---

## 6. Daily Workflow

The agent is fully autonomous. A typical **MY** day:

| Time (MYT) | What happens | What you do |
|---|---|---|
| Pre-market | Scheduler sleeps | Glance at Performance tab |
| 09:00 | Bursa opens, first cycle | (nothing) |
| 09:00–12:30 | Morning session — scans + entries | Optional: check Portfolio |
| 12:30–14:00 | Lunch break — no scans | (nothing) |
| 14:30–16:00 | Afternoon session — scans + entries | (nothing) |
| 16:00–17:00 | Exits still run, no new entries | (nothing) |
| 17:00 | Bursa closes, scheduler skips | Check Performance |
| 01:00 | Nightly: ML retrain + log prune | (nothing) |

For **US**, the active session is 09:30–16:00 ET (≈21:30–04:00 MYT) — so the US
agent does its work overnight from your point of view. The dashboard shows both
times so you know when to watch.

**You only need to touch the dashboard for:**
- Reviewing performance (📊 Performance)
- Manually closing a position (💼 Portfolio)
- Adjusting risk (⚙️ Settings)
- Switching markets (sidebar 🌐)
- Changing US broker mode (⚙️ Settings → Execution Mode)
- Emergency stop (🚨 Kill-Switch)

---

## 7. Key Controls — Where to Find Everything

### Sidebar (always visible)

| Control | What it does |
|---|---|
| **🌐 Market** | Switch between 🇲🇾 MY and 🇺🇸 US (reloads on new market) |
| **Initial Capital (RM/$)** | Starting paper-trade balance for the active market |
| **🤖 Robo-Trader badge** | Status + last/next run + brain mode |
| **🔕/🟢 Broker badge** | Current broker mode + connection light (US) |
| **Start/Stop button** | Toggle the scheduler |
| **➕ Add Custom Stock** | Append a ticker outside the curated list |

### 🔍 Scanner tab
- **🔥 SCAN MARKET** — manual scan (auto runs hourly anyway)
- Filter by signal type; click any ticker → chart, 5-day tape, execute panel
- Prices show in the active market's currency

### 💼 Portfolio tab
- Active positions with live P&L, MAE/MFE; sector exposure heatmap
- Manual close / partial exit; closed trades history

### 🧠 AI Learning tab
- Strategy + sector performance, Bayesian state priors, ML feature importance + OOS accuracy, walk-forward

### 📊 Performance tab
- Sharpe / Sortino / Max Drawdown / Profit Factor / Expectancy (R)
- Equity curve vs benchmark; calibration chart; per-regime hit rate

### 🤖 Robo-Trader tab

| Control | Action |
|---|---|
| ▶ Start / 🛑 Stop | Daemon thread on/off |
| ♻ Force Restart | Stop + start (clean slate) |
| ⚡ Run Cycle Now | Immediate scan + settle + entries |
| 🚨 Kill-Switch | Emergency stop; won't restart until cleared |
| Auto-exit / Auto-entry | Toggle each independently |
| Cycle interval | 15 / 30 / 60 / 120 min |
| Watchdog & Cycle Health | Watchdog status, recent timeouts |
| Learning Mode | Force EXPLORE or EXPLOIT |

### 📜 Logs tab
CSV-downloadable streams: Trade executions · Robo-Trader scheduler · Learning &
parameter changes · Bias updates · Data quality · **🏢 Corporate Actions**.

### 🔔 Live Alerts tab
- Master switch for Telegram + Email; per-event toggles; confidence/mode filters; alert history
- Alert prices use the active market's currency

### ⚙️ Settings tab
- Scanner parameters (EMA, RSI, volume, ATR, price range, Shariah filter)
- Risk parameters + **Trading Window** (profile-aware; US shows ET + MYT)
- 📡 Data Source (active provider + re-probe)
- **🎯 Execution Mode** (US: NOOP/SIMULATE/REAL + connection test)
- **🔄 Broker Reconciliation** (status + run now)
- 🏢 Corporate Actions (auto-adjust toggle + manual scan)
- Custom watchlist, Kill-switch clear, Persistent backup, Long-term maintenance status, Reset capital / delete trades

---

## 8. Common Operations

### A. Switch markets
Sidebar → 🌐 Market → pick MY or US. (Or set `MARKET_MODE` / the `.active_market`
file.) Each market keeps its own trades and brain.

### B. Adjust risk per trade
**⚙️ Settings → Risk Parameters → Max risk / trade %** → 💾 Save. (Per market.)

| Value | Stance |
|---|---|
| 0.5% | Very conservative — first 30 days |
| **1.0%** | **Default** — balanced |
| 2.0% | Aggressive — only after 50+ validated trades |

### C. Turn on US live/sim execution
1. Local PC: install Moomoo Desktop + enable OpenD (port 11111)
2. Switch to 🇺🇸 US in the sidebar
3. ⚙️ Settings → 🎯 Execution Mode → **🔌 Test broker connection**
4. Switch to **SIMULATE** → run 4+ weeks → only then consider REAL (needs `MOOMOO_TRADING_PWD`)

### D. Emergency stop
**🤖 Robo-Trader → 🚨 Kill-Switch**. Clear via **⚙️ Settings → Kill-Switch → Clear**.

### E. Reset and restart learning (one market)
**⚙️ Settings → ⚠️ Destructive actions → Delete all trades + scan cache**. For a
full wipe of one market: `rm ~/.bursa_agent_data/bursa_agent_US.db`. Both markets:
`rm -rf ~/.bursa_agent_data/`.

### F. Enable Telegram alerts
See `LIVE_TRIGGER_GUIDE.md` for step-by-step setup.

### G. Shariah-only filter (MY)
**⚙️ Settings → Scanner Parameters → 🕌 Shariah-compliant only** → Save.

### H. Run headless (no browser needed)
```bash
python -m scheduler --interval 3600              # default MY
MARKET_MODE=US python -m scheduler --interval 3600   # US
```

---

## 9. Important Notes & Cautions

### ⚠️ Expect losses during early exploration
In EXPLORE mode (first 50 trades **per market**), the agent intentionally tries
unknown setups. Some will lose — by design (losses are training data). Keep risk
at 0.5–1.0% during this phase.

### ⚠️ Two brains, two validations
MY and US learn independently. A calibrated MY brain says nothing about US — each
needs its own 50+ trades before you trust its signals.

### ⚠️ Where each market should run
- **MY** → Streamlit Cloud or local; yfinance only; runs fine 24/7 on the cloud.
- **US** → for SIMULATE/REAL you must run **locally with Moomoo OpenD**. When you're offline, the US tab shows "disconnected" and falls back to yfinance with no execution.

### ⚠️ Streamlit Cloud sleep
Streamlit Cloud sleeps apps after 7 days of inactivity. On re-open the scheduler
self-heals and restarts. For true 24/7, run headless and/or configure the Gist
backup (`GITHUB_TOKEN` + `GIST_ID`).

### ⚠️ Risk gates are always active
Even with auto-entry ON, every trade passes `run_full_risk_check`: drawdown >8%
halves size, >15% blocks all trading, max positions / position-cost / sector caps
/ daily limit / trading-hours all enforced.

### ⚠️ Real trading is opt-in and US-only
MY is paper/notify-only. US REAL mode requires explicit setup + `MOOMOO_TRADING_PWD`
and should only follow a successful SIMULATE period.

### ⚠️ Learning ≠ profit guarantee
Validate Sharpe > 1.0 and Profit Factor > 1.5 over 50+ trades (per market) before
trusting with real capital.

---

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| Scheduler shows STOPPED | ♻️ Force Restart in 🤖 Robo-Trader |
| Kill-switch stuck on | ⚙️ Settings → Clear kill-switch |
| No GOLD BUY signals | BEAR regime raises threshold to 80%; check Scanner regime banner |
| Auto-entries not happening | Check: market hours? past the safe-entry cutoff? at max positions? See Logs for the reason |
| US tab shows RM / wrong currency | Hard refresh (Ctrl+Shift+R); ensure latest `app.py` + `live_trigger.py` |
| US Settings times look odd | Expected: US shows native **ET** with **MYT** in brackets — enter window values in ET |
| `no such table: account` after switching market | Fixed in v3.6 — pull latest `market_profiles/__init__.py` + `app.py` |
| Moomoo OpenD "not listening" (US) | Open Moomoo Desktop AND enable OpenD (Settings → API) |
| REAL mode says "MOOMOO_TRADING_PWD not set" | Add the secret + restart so env vars reload |
| Reconciliation drift alert | Expected if <0.5% — internal heuristic slippage vs real fills |
| Brain lost after redeploy | Set `GITHUB_TOKEN` (+ `GIST_ID`) in Streamlit Secrets |
| `ModuleNotFoundError: scipy` / `pandas_market_calendars` | `pip install -r requirements.txt` |
| Want to delete everything | `rm -rf ~/.bursa_agent_data/` then restart |

---

## Quick Reference

```
START:          streamlit run app.py
HEADLESS (MY):  python -m scheduler --interval 3600
HEADLESS (US):  MARKET_MODE=US python -m scheduler --interval 3600
TESTS:          pytest tests/ -q   (471 tests, ~46s, full suite green in one run)

DBs:            ~/.bursa_agent_data/bursa_agent_MY.db
                ~/.bursa_agent_data/bursa_agent_US.db
MARKER:         ~/.bursa_agent_data/.active_market   ("MY" or "US")
LOGS:           ~/.bursa_agent_data/logs/bursa_agent.log

KEY KNOBS:
  Market             → Sidebar → 🌐 Market
  Auto-entry         → 🤖 Robo-Trader → Auto-Trading Toggles
  Risk per trade %   → ⚙️ Settings → Risk Parameters
  Cycle interval     → 🤖 Robo-Trader → Auto-Trading Toggles
  Broker mode (US)   → ⚙️ Settings → Execution Mode
  Shariah filter     → ⚙️ Settings → Scanner Parameters
  Brain mode         → 🤖 Robo-Trader → Learning Mode
  Kill-Switch        → 🤖 Robo-Trader → Controls

DEFAULTS:
  Auto-entry         ON
  Auto-exit          ON
  Risk per trade     1.0%
  Broker mode        NOOP (MY locked to NOOP; US can SIMULATE/REAL)
  Default market     MY
  Capital            MY RM 20,000 / US $ 5,000
  Max positions      MY 8/5/3 · US 6/4/2 (BULL/NEUTRAL/BEAR)
  Drawdown warn      8%     (halve size)
  Drawdown stop      15%    (block all trading)
  Sessions           MY 09:00–17:00 MYT · US 09:30–16:00 ET
  Safe-entry cutoff  MY 16:00 MYT · US 15:30 ET
  Brain backup       Every closed trade + hourly (per-market Gist)
```

---

**Start small. Trust the data, not the hope.** 🚀

Two brains, two databases, one agent.
