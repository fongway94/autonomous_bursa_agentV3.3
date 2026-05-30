# BursaAI Swing Agent — User Guide

A quick-reference manual for running the autonomous Bursa Malaysia paper-trading agent.

> **Important:** This is paper trading only. The agent does **not** place real broker orders. P&L is simulated using realistic Bursa fees and slippage. Always validate with your own research before risking real money.

---

## Table of Contents

1. [First Launch](#1-first-launch)
2. [What the Robo-Trader Does by Default](#2-what-the-robo-trader-does-by-default)
3. [How the Self-Learning Works](#3-how-the-self-learning-works)
4. [Daily Workflow](#4-daily-workflow)
5. [Key Controls — Where to Find Everything](#5-key-controls--where-to-find-everything)
6. [Common Operations](#6-common-operations)
7. [Important Notes & Cautions](#7-important-notes--cautions)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. First Launch

```bash
streamlit run app.py
```

Browser opens at `http://localhost:8501`.

**The Robo-Trader starts itself the moment the page loads.** No buttons to click.

| Time | What you see |
|---|---|
| 0 s | Light-themed dashboard loads, 8 tabs visible |
| 1 s | Sidebar shows `🤖 Robo-Trader 🟢 RUNNING`, `Brain mode 🔬 EXPLORE` |
| 2 s | First HEARTBEAT logged in 📜 Logs → Robo-Trader scheduler |
| ≤ 1 h | First scan runs at the next top-of-hour (during market hours) |

Click **🤖 Robo-Trader → ⚡ Run Cycle Now** to fire an immediate cycle without waiting.

---

## 2. What the Robo-Trader Does by Default

| Setting | Default | Effect |
|---|---|---|
| **Auto-exit** | ✅ ON | Closes positions at SL / TP / trailing stop / time exit |
| **Auto-entry** | ✅ ON | Opens new positions on high-confidence GOLD BUY signals |
| **Cycle interval** | 60 min | Hourly during market hours |
| **Brain mode** | 🔬 EXPLORE | Thompson sampling for the first 50 closed trades |
| **Max risk / trade** | 1.0% | Conservative for unsupervised trading |
| **Trading window** | 09:00–17:00 MYT | Bursa hours; no new entries after 16:00 |
| **Kill-switch** | Off | One-click emergency stop available |

### The hourly cycle

```
1. HEARTBEAT logged
2. Fetch fresh KLCI regime → BULL / NEUTRAL / BEAR
3. Scan ~74 Bursa stocks (parallel, 30 s timeout per ticker)
4. Cache results in scan_cache
5. Auto-exit: settle TPs / SLs / trailing / time exits on active trades
6. Auto-entry: open GOLD BUYs above regime-adjusted confidence threshold
7. Learn: every closed trade updates the Bayesian brain
8. Backup brain to GitHub Gist (if configured)
9. Sleep until next top-of-hour
```

### Regime-adjusted thresholds

| Regime | Confidence threshold | Max positions | Max hold |
|---|---|---|---|
| BULL | 60% | 8 | 14 days |
| NEUTRAL | 70% | 5 | 7 days |
| BEAR | 80% | 3 | 5 days |

Doing fewer trades in BEAR is correct behaviour — the agent stays defensive.

---

## 3. How the Self-Learning Works

### Phase 1 — EXPLORATION 🔬 (first 50 closed trades)

- Every setup is bucketed into one of ~250 states (RSI × volume × trend × MACD).
- The agent samples a win-rate estimate from its Beta(α,β) posterior — **Thompson sampling**.
- This intentionally tries a variety of setups so the brain gathers data fast.

### Phase 2 — EXPLOITATION 🎯 (automatic after 50 trades)

- Switches to the **lower confidence bound (LCB)** — only acts on statistically proven setups.
- Badge in sidebar changes to **🎯 EXPLOIT**.

### What every closed trade teaches

Each WIN / LOSS updates three things simultaneously:

1. **State's Beta posterior** — α += win_weight (WIN) or β += loss_weight (LOSS)
2. **Strategy bias** — breakout_bias / pullback_bias adjusted with Bayesian shrinkage
3. **Sector bias** — per-sector multiplier adjusted the same way

### Where to see it

- **🧠 AI Learning** → Bayesian State Priors table
- **📜 Logs** → Learning events, Bias updates

### Brain persistence

The entire SQLite DB (including all priors, biases, and trades) is backed up to a private GitHub Gist after every closed trade and every hourly heartbeat. On container restart (Streamlit Cloud), the brain is restored automatically before the scheduler starts.

**Without `GITHUB_TOKEN` configured, the brain is lost on every container reset.**

---

## 4. Daily Workflow

The agent is fully autonomous. A typical day:

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

**You only need to touch the dashboard for:**
- Reviewing performance (📊 Performance)
- Manually closing a position (💼 Portfolio)
- Adjusting risk (⚙️ Settings)
- Emergency stop (🚨 Kill-Switch)

---

## 5. Key Controls — Where to Find Everything

### Sidebar (always visible)

| Control | What it does |
|---|---|
| **Initial Capital (RM)** | Starting paper-trade balance |
| **🤖 Robo-Trader badge** | Status + last/next run + brain mode |
| **Start/Stop button** | Toggle the scheduler |
| **➕ Add Custom Stock** | Append a ticker outside the curated list |

### 🔍 Scanner tab
- **🔥 SCAN MARKET** — manual scan (auto runs hourly anyway)
- Filter by signal type
- Click any ticker → chart, 5-day tape, execute panel

### 💼 Portfolio tab
- Active positions with live P&L, MAE/MFE
- Sector exposure heatmap
- Manual close / partial exit
- Closed trades history

### 🧠 AI Learning tab
- Strategy + sector performance breakdown
- Bayesian state priors (top 20 by sample size)
- ML classifier feature importance + OOS accuracy
- Walk-forward optimization

### 📊 Performance tab
- Sharpe / Sortino / Max Drawdown / Profit Factor / Expectancy (R)
- Equity curve vs KLCI + equal-weight benchmarks
- Calibration chart (confidence vs actual win rate)
- Per-regime hit rate

### 🤖 Robo-Trader tab

| Control | Action |
|---|---|
| ▶ Start / 🛑 Stop | Daemon thread on/off |
| ♻ Force Restart | Stop + start (clean slate) |
| ⚡ Run Cycle Now | Immediate scan + settle + entries |
| 🚨 Kill-Switch | Emergency stop; won't restart until cleared |
| Auto-exit / Auto-entry | Toggle each independently |
| Cycle interval | 15 / 30 / 60 / 120 min |
| Watchdog & Cycle Health | Shows watchdog status, recent timeouts |
| Learning Mode | Force EXPLORE or EXPLOIT |

### 📜 Logs tab
Six streams, all CSV-downloadable:
- Trade executions
- Robo-Trader scheduler
- Learning & parameter changes
- Bias updates
- Data quality issues

### 🔔 Live Alerts tab
- Master switch for Telegram + Email alerts
- Per-event toggles (entry, exit, SL, trailing, risk reject)
- Confidence and mode filters
- Alert history

### ⚙️ Settings tab
- Scanner parameters (EMA, RSI, volume, ATR, price range, Shariah filter)
- Risk parameters (drawdown, positions, risk/trade, sector cap)
- Custom watchlist management
- Kill-switch clear
- Persistent backup status
- Long-term maintenance status (holidays, GitHub PAT, walk-forward)
- Reset capital / delete trades

---

## 6. Common Operations

### A. Adjust risk per trade

1. **⚙️ Settings** → Risk Parameters
2. Change **Max risk / trade %** (default 1.0%)
3. **💾 Save Risk Parameters**

| Value | Stance |
|---|---|
| 0.5% | Very conservative — first 30 days |
| **1.0%** | **Default** — balanced |
| 2.0% | Aggressive — only after 50+ validated trades |

### B. Emergency stop

1. **🤖 Robo-Trader** → **🚨 Kill-Switch**
2. Scheduler stops and won't restart until cleared
3. Clear: **⚙️ Settings → Kill-Switch → Clear**

### C. Reset and restart learning

1. **⚙️ Settings → ⚠️ Destructive actions** → Delete all trades + scan cache
2. State priors are preserved. For full wipe: delete `~/.bursa_agent_data/`

### D. Enable Telegram alerts

See `LIVE_TRIGGER_GUIDE.md` for step-by-step setup.

### E. Shariah-only filter

1. **⚙️ Settings → Scanner Parameters**
2. Check **🕌 Shariah-compliant only**
3. Save — next scan excludes conventional banks, brewers, gaming, etc.

### F. Speed up learning (shorter cycles)

1. **🤖 Robo-Trader → Cycle interval** → 15 min
2. Save → scheduler restarts at new cadence
3. **Caveat:** yfinance updates daily bars near end-of-day; sub-hourly scans mostly see the same data

### G. Run headless (no browser needed)

```bash
python -m scheduler --interval 3600
```

---

## 7. Important Notes & Cautions

### ⚠️ Expect losses during early exploration

In EXPLORE mode (first 50 trades), the agent intentionally tries unknown setups. Some will lose. **This is by design** — losses are training data. Keep risk at 0.5–1.0% during this phase.

### ⚠️ Streamlit Cloud sleep

Streamlit Cloud sleeps apps after 7 days of inactivity. When you re-open, the scheduler self-heals and restarts automatically. Trades that should have closed during sleep won't settle until reactivation. For true 24/7: run headless or configure the Gist backup.

### ⚠️ yfinance data is daily bars

The agent uses end-of-day bars. It doesn't react to intraday spikes. This is fine for swing trading.

### ⚠️ Risk gates are always active

Even with auto-entry ON, every trade goes through `run_full_risk_check`:
- Drawdown >8% → halve position size
- Drawdown >15% → block all trading
- Max concurrent positions reached → reject
- Position cost >20% of capital → reduce size
- Sector exposure >40% → reduce or reject
- Daily trade limit (5) → reject
- Outside trading hours → reject

### ⚠️ Real trading is NOT enabled

The system places paper trades only. `broker_adapter.MoomooAdapter` is a stub for future use.

### ⚠️ Learning ≠ profit guarantee

Bayesian updates reflect what worked in your specific run. Validate Sharpe > 1.0 and Profit Factor > 1.5 over 50+ trades before trusting with real capital.

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| Scheduler shows STOPPED | ♻️ Force Restart in 🤖 Robo-Trader |
| Kill-switch stuck on | ⚙️ Settings → Clear kill-switch |
| No GOLD BUY signals | BEAR regime raises threshold to 80%; check Scanner tab regime banner |
| Auto-entries not happening | Check: market hours? After 16:00? At max positions? See Logs for rejection reason |
| Brain lost after redeploy | Set `GITHUB_TOKEN` in Streamlit Cloud Secrets |
| `ModuleNotFoundError: scipy` | `pip install scipy>=1.10` |
| Want to delete everything | `rm -rf ~/.bursa_agent_data/` then restart |

---

## Quick Reference

```
START:          streamlit run app.py
HEADLESS:       python -m scheduler --interval 3600
TESTS:          pytest tests/ -q   (191 tests, ~40s)

DB:             ~/.bursa_agent_data/bursa_agent.db
LOGS:           ~/.bursa_agent_data/logs/bursa_agent.log

KEY KNOBS:
  Auto-entry         → 🤖 Robo-Trader → Auto-Trading Toggles
  Risk per trade %   → ⚙️ Settings → Risk Parameters
  Cycle interval     → 🤖 Robo-Trader → Auto-Trading Toggles
  Shariah filter     → ⚙️ Settings → Scanner Parameters
  Brain mode         → 🤖 Robo-Trader → Learning Mode
  Kill-Switch        → 🤖 Robo-Trader → Controls

DEFAULTS:
  Auto-entry         ON
  Auto-exit          ON
  Risk per trade     1.0%
  Max positions      8 (BULL) / 5 (NEUTRAL) / 3 (BEAR)
  Drawdown warn      8%     (halve size)
  Drawdown stop      15%    (block all trading)
  Trading window     09:00–17:00 MYT
  No entries after   16:00 MYT
  Brain backup       Every closed trade + hourly (via Gist)
```

---

**Start small. Trust the data, not the hope.** 🚀
