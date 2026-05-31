# BursaAI Swing Agent — User Guide (v3.7, multi-market + dual-mode)

A quick-reference manual for running the autonomous trading agent across
**🇲🇾 Bursa Malaysia (KLSE)** and **🇺🇸 US (NYSE/NASDAQ)** in either
**SWING** (daily, hourly scanner) or **INTRADAY** (5-minute ORB, US-only) mode.

> **Important:** The default mode is **paper trading** — simulated P&L using
> realistic fees and slippage, with Telegram/email alerts so you can mirror
> manually. US additionally supports real Moomoo execution (SIMULATE / REAL)
> when running locally with Moomoo OpenD. MY is notification-only. Always
> validate with your own research before risking real money.

---

## Table of Contents

1. [First Launch](#1-first-launch)
2. [Switching Markets (MY ↔ US)](#2-switching-markets-my--us)
3. [Switching Trading Mode (SWING ↔ INTRADAY)](#3-switching-trading-mode-swing--intraday)
4. [SWING Mode — What the Robo-Trader Does](#4-swing-mode--what-the-robo-trader-does)
5. [INTRADAY Mode — ORB Strategy](#5-intraday-mode--orb-strategy)
6. [How the Self-Learning Works](#6-how-the-self-learning-works)
7. [US Execution & Reconciliation](#7-us-execution--reconciliation)
8. [Daily Workflow](#8-daily-workflow)
9. [Key Controls — Where to Find Everything](#9-key-controls--where-to-find-everything)
10. [Common Operations](#10-common-operations)
11. [Important Notes & Cautions](#11-important-notes--cautions)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. First Launch

```bash
streamlit run app.py
```

Browser opens at `http://localhost:8501`.

**The Robo-Trader starts itself the moment the page loads.** No buttons to click.

| Time | What you see |
|---|---|
| 0 s | Light-themed dashboard loads, 8 tabs + sidebar **🌐 Market** and **🧭 Trading Mode** switchers |
| 1 s | Sidebar shows active market flag (🇲🇾/🇺🇸), current mode (SWING/INTRADAY), `🤖 Robo-Trader 🟢 RUNNING`, `Brain mode 🔬 EXPLORE` |
| 2 s | First HEARTBEAT logged in 📜 Logs → Robo-Trader scheduler |
| ≤ 1 h | First SWING scan runs at the next top-of-hour (during market hours) |
| Immediately | If INTRADAY mode is active and OpenD is connected, intraday cycles start within 5 min |

Default on first boot: **🇲🇾 MY · 📈 SWING**. Click
**🤖 Robo-Trader → ⚡ Run Cycle Now** to fire an immediate cycle without waiting.

---

## 2. Switching Markets (MY ↔ US)

Each market has its **own** database, brain, account, and parameters — switching
never mixes Bursa and US data.

### How to switch

- **Sidebar → 🌐 Market dropdown** → pick 🇲🇾 MY or 🇺🇸 US → page reloads. *(recommended)*
- **Env var** (sets default on boot): `MARKET_MODE=US streamlit run app.py`
- **Marker file**: `echo US > ~/.bursa_agent_data/.active_market`

Resolution order: **env var > marker file > default (MY)**.

### What's different per market

| Setting | 🇲🇾 MY (Bursa) | 🇺🇸 US (NYSE/NASDAQ) |
|---|---|---|
| Currency | RM | $ |
| Lot size | 100 (board lot) | 1 |
| Default capital | RM 20,000 | $ 5,000 |
| Sessions | 09:00–12:30 + 14:30–17:00 **MYT** | 09:30–16:00 **ET** |
| Safe-entry cutoff | 16:00 MYT | 15:30 ET (≈03:30 MYT) |
| Regime ticker | `^KLSE` | `SPY` |
| Fees | 0.15% per side | 0% (commission-free) |
| Max positions | BULL 8 / NEUTRAL 5 / BEAR 3 | BULL 6 / NEUTRAL 4 / BEAR 2 |
| SWING universe | ~74 Bursa + Shariah filter | leveraged ETFs + mega-caps |
| INTRADAY | ❌ Not available (Moomoo has no Bursa feed) | ✅ Curated-6 ORB |
| Data source | yfinance only | Moomoo OpenD when connected, else yfinance |
| Broker execution | Notify-only (NOOP) | NOOP / SIMULATE / REAL |

### 🌏 Timezone display for Malaysia-based traders

US session and cutoff times show in native **ET** with the **MYT** equivalent
in brackets — e.g. `09:30–16:00 ET (21:30–04:00 MYT)`. When editing the Trading
Window in Settings for the US market, **enter values in ET** (labels say so).
The agent's internal clock and 01:00 nightly maintenance always stay MYT.

---

## 3. Switching Trading Mode (SWING ↔ INTRADAY)

> **INTRADAY is US-only.** The 🧭 Trading Mode dropdown only shows INTRADAY
> when the 🇺🇸 US market is active. If you select MY, the dropdown shows SWING
> only, and the app auto-reverts to SWING if INTRADAY is somehow set on MY.

### Where to switch — 3 ways

| Method | How |
|---|---|
| **Sidebar → 🧭 Trading Mode** | Primary UI. Pick `📈 SWING — hourly scanner` or `⚡ INTRADAY — 5m ORB`. App reloads, scheduler restarts with the correct cadence. |
| **Env var** | `TRADING_MODE=INTRADAY streamlit run app.py` (or `TRADING_MODE=SWING`) |
| **Marker file** | `echo INTRADAY > ~/.bursa_agent_data/.trading_mode` (written automatically by the sidebar) |

Resolution order: **env var > marker file > default (SWING)**.

### What changes when you switch modes

| | SWING | INTRADAY |
|---|---|---|
| Scan frequency | Every 60 min (during market hours) | Every 5 min (US RTH only) |
| Strategy | GOLD BUY breakout / pullback (EMA + RSI + volume) | Opening Range Breakout (VWAP + rel-vol + EMA-200) |
| Universe | Full market watchlist (~74 MY / ~25 US) | Curated-6: TNA, GOOGL, TQQQ, MSTR, SOXL, PLTR |
| Position hold | Days to weeks (with trailing stop) | Same session only (force-flat at 15:55 ET) |
| Database | `bursa_agent_US_SWING.db` | `bursa_agent_US_INTRADAY.db` |
| Brain | Swing Bayesian priors | Separate intraday Bayesian priors |
| Explorer target | 50 closed trades | 100 closed trades |
| Tab labels | 🔍 Scanner / 🤖 Robo-Trader | ⚡ Intraday Scanner / ⚡ Intraday Robo-Trader |
| Scanner columns | RSI, change%, RS signal | VWAP, rel-vol, OR high/low/range |

### Requirement for INTRADAY

**Moomoo OpenD must be running locally on port 11111.** Without it:
- The sidebar shows a yellow warning banner: *"Intraday unavailable, data source insufficient. Moomoo OpenD must be connected locally."*
- The agent refuses to open new intraday entries (it will still force-flat anything already open)
- On Streamlit Cloud this banner always shows — intraday is a local-PC-only feature

---

## 4. SWING Mode — What the Robo-Trader Does

### Default settings

| Setting | Default | Effect |
|---|---|---|
| **Auto-exit** | ✅ ON | Closes positions at SL / TP / trailing stop / time exit |
| **Auto-entry** | ✅ ON | Opens new positions on high-confidence GOLD BUY signals |
| **Cycle interval** | 60 min | Hourly during that market's hours |
| **Brain mode** | 🔬 EXPLORE | Thompson sampling for the first 50 closed trades (per market) |
| **Max risk / trade** | 1.0% | Conservative for unsupervised trading |
| **Broker mode** | NOOP | Notify-only (MY fixed; US can switch to SIMULATE/REAL) |

### The hourly SWING cycle

```
1. HEARTBEAT logged
2. Corporate actions: auto-adjust open trades for splits/bonus (before anything else)
3. Fetch fresh regime (^KLSE or SPY) → BULL / NEUTRAL / BEAR
4. Scan active market's universe (parallel, 30 s timeout per ticker)
5. Cache results in scan_cache
6. Auto-exit: settle TPs / SLs / trailing / time exits on active trades
7. Auto-entry: open GOLD BUYs above regime-adjusted confidence threshold
8. Learn: every closed trade updates that market's SWING Bayesian brain
9. Reconcile (US execute modes only): compare to broker, alert on drift
10. Backup to GitHub Gist (if configured)
11. Sleep until next top-of-hour
```

### Regime-adjusted thresholds (SWING)

| Regime | Confidence threshold | Max positions (MY / US) | Max hold |
|---|---|---|---|
| BULL | 60% | 8 / 6 | 14 days |
| NEUTRAL | 70% | 5 / 4 | 7 days |
| BEAR | 80% | 3 / 2 | 5 days |

Doing fewer trades in BEAR is correct behaviour.

---

## 5. INTRADAY Mode — ORB Strategy

> **Prerequisite:** 🇺🇸 US market selected + Moomoo OpenD running locally on port 11111.

### What INTRADAY does every 5 minutes

```
Session states the agent recognises:

09:30–09:45 ET  OR_WINDOW       → scan starts, opening range builds (15 min)
09:45–15:55 ET  ACTIVE_TRADING  → scan + settle + auto-entry
15:55–16:00 ET  FORCE_FLAT      → close ALL open intraday positions (no exceptions)
Outside hours   PRE/POSTMARKET  → idle, wait 5 min, try again
```

### The ORB strategy (locked, validated over 360 days of real OpenD data)

1. **Opening Range (OR)** = high/low of the first 15 minutes (09:30–09:45 ET)
2. After 09:45, scan each 5-minute bar. A **LONG entry** fires on the **first bar** where ALL are true:
   - Close > OR_high (breakout above the range)
   - Session relative volume ≥ 1.2× average (volume confirms)
   - Close > session VWAP (price supports direction)
   - Prior daily close > daily EMA-200 (macro trend filter — this is the key one)
3. **Stop** = OR_low (structural invalidation, never moved)
4. **Targets** = entry + 1.5× OR_range (TP1), + 2.0× (TP2), + 2.5× (TP3)
5. **Force-flat at 15:55 ET** — every intraday position is closed no matter what. Zero overnight risk.
6. **One trade per ticker per session.** If TNA fires at 10:05, no second TNA entry that day.

### Intraday settings — where to see them

All intraday parameters are **read-only** in the UI — they were locked by the
round-4 360-day validation and should not be changed without rerunning the
backtest. To view them:

**⚙️ Settings tab → ⚡ Intraday Mode Defaults (validated, read-only)**

You'll see:

| Setting | Value |
|---|---|
| Universe | TNA, GOOGL, TQQQ, MSTR, SOXL, PLTR |
| Opening range | 15 min |
| Target | 2.0R |
| Rel-volume | 1.2× |
| Trend filter | EMA-200 daily |
| VWAP support | Required |
| Direction | Longs only |
| Force-flat | 15:55 ET |
| Explorer target | 100 trades |

> **Why read-only?** Adding structural losers (FNGU, MARA, IBIT, NVDA, etc.)
> destroys the edge — the full-20 universe produces only +0.012R vs curated-6's
> +0.090R. Changing parameters requires rerunning `validate_intraday_edge.py`
> on your local OpenD first (see ⚙️ Settings → 🧪 Intraday Research Tools).

### Signal grades

| Grade | What it means |
|---|---|
| **GOLD BUY (ORB)** | All filters pass: OR breakout + rel-vol ≥ 1.2× + VWAP support + EMA-200 UP |
| **SILVER BUY (ORB)** | Most filters pass, one minor weakness |

### Intraday session status card (Scanner tab)

When INTRADAY mode is active and you open the ⚡ Intraday Scanner tab, a
colour-coded card shows the current session state:

| State colour | Meaning |
|---|---|
| 🟡 Orange — OR_WINDOW | Opening range building (09:30–09:45 ET). No entries yet. |
| 🟢 Green — ACTIVE_TRADING | Agent is scanning + entering (09:45–15:55 ET). |
| 🔴 Red — FORCE_FLAT_WINDOW | Closing all positions (15:55–16:00 ET). |
| Grey — PRE/POSTMARKET | Outside US trading hours. Agent is idle. |

### Intraday brain and learning

- Intraday trades go into the **separate** `bursa_agent_US_INTRADAY.db` brain.
- They **never** mix with the SWING brain (`bursa_agent_US_SWING.db`).
- Explorer target is **100 trades** (not 50 like SWING) — intraday accumulates
  faster (~5-6 trades/day) but the state space needs more samples to converge.
- At 100 closed intraday trades (~3-4 weeks), the mode auto-switches to EXPLOIT.
- **Do not trust the EXPLOIT mode until 100 trades are closed** — during EXPLORE
  the agent is learning, not performing.

### Honest caveats

- +0.090R expectancy per trade (just under the +0.10R "strong" threshold)
- After realistic slippage: approximately +0.07R
- 83% of months were net-positive in the 360-day validation
- Max consecutive losers: 8 — survivable but uncomfortable
- The strategy was validated in a mostly-bullish period (Jun 2025–May 2026).
  Bear-market performance needs separate validation.

---

## 6. How the Self-Learning Works

> Each **market** × **mode** has its own brain:
> `MY_SWING`, `MY_INTRADAY`, `US_SWING`, `US_INTRADAY`. Switching never mixes learning.

### Phase 1 — EXPLORATION 🔬

| Mode | Explorer target | Duration (approx.) |
|---|---|---|
| SWING | First 50 closed trades | 1–6 months depending on market activity |
| INTRADAY | First 100 closed trades | ~3–4 weeks at normal US RTH volume |

- Agent uses **Thompson sampling** — tries a variety of setups to gather data fast
- Some losses during this phase are **by design** (losses are training data)
- Keep risk at **0.5–1.0%** during exploration

### Phase 2 — EXPLOITATION 🎯 (automatic after target is reached)

- Switches to **lower confidence bound (LCB)** — only acts on statistically proven setups
- Sidebar badge changes to **🎯 EXPLOIT**
- You can force either mode in **🤖 Robo-Trader → 🧪 Learning Mode**

### What every closed trade teaches (both modes)

1. **State's Beta(α,β) posterior** — α += win_weight or β += loss_weight
2. **Strategy bias** — breakout_bias / pullback_bias adjusted with Bayesian shrinkage
3. **Sector bias** — per-sector multiplier adjusted

### Where to see it
- **🧠 AI Learning** → Bayesian State Priors table, ML feature importance
- **📜 Logs** → Learning events, Bias updates

### Brain persistence (per market × mode)

Each DB (`bursa_agent_MY_SWING.db`, `bursa_agent_US_SWING.db`, `bursa_agent_US_INTRADAY.db`, etc.)
is backed up to a private GitHub Gist after every closed trade and every hourly heartbeat.

**Without `GITHUB_TOKEN` configured, the brain is lost on every container reset.**

---

## 7. US Execution & Reconciliation

US is the only market with real broker execution (Moomoo OpenAPI has no Bursa
coverage, so MY stays notify-only). Broker mode applies to **SWING only** —
INTRADAY currently uses paper execution regardless.

### Broker modes (US, set in ⚙️ Settings → 🎯 Execution Mode)

- **NOOP** *(default)* — paper trades + Telegram alerts only; you mirror manually.
- **SIMULATE** — agent places real orders in Moomoo's paper-trading account (no real money). **Recommended for the first 4–6 weeks.**
- **REAL** — live orders with real money. Requires `MOOMOO_TRADING_PWD` env var + Moomoo OpenD running + unlocked trading session.

**Requirements for SIMULATE/REAL:** run on your local PC with **Moomoo Desktop + OpenD enabled** (Settings → API → port 11111). On Streamlit Cloud the agent is always NOOP.

### Reconciliation (US execute modes)

Every SWING cycle the agent compares its internal positions/cash to the broker
and sends a Telegram **RECONCILE_DRIFT** alert if equity drift exceeds **0.5%**
or any position quantity differs. Small drift (<0.5%) is normal. See
**⚙️ Settings → 🔄 Broker Reconciliation** for status + "Run now" button.

---

## 8. Daily Workflow

### MY SWING (typical Malaysia day)

| Time (MYT) | What happens |
|---|---|
| Pre-market | Scheduler sleeps |
| 09:00 | Bursa opens, first hourly cycle |
| 09:00–12:30 | Morning session — scans + entries |
| 12:30–14:00 | Lunch break — no scans |
| 14:30–16:00 | Afternoon — scans + entries |
| 16:00–17:00 | Exits still run, no new entries |
| 17:00 | Bursa closes, scheduler skips |
| 01:00 | Nightly: ML retrain + log prune |

### US SWING (overnight from Malaysia)

| Time (MYT) | Time (ET) | What happens |
|---|---|---|
| 21:30 MYT | 09:30 ET | US opens, first hourly cycle |
| 21:30–04:00 MYT | 09:30–16:00 ET | US session — scans + entries + exits |
| 04:00 MYT | 16:00 ET | US closes, scheduler skips |

### US INTRADAY (5-min cycles, overnight from Malaysia)

| Time (MYT) | Time (ET) | What happens |
|---|---|---|
| 21:30–21:45 MYT | 09:30–09:45 ET | Opening range builds (15 min, no entries) |
| 21:45–03:55 MYT | 09:45–15:55 ET | Active trading — scan every 5 min + settle + entry |
| 03:55–04:00 MYT | 15:55–16:00 ET | **Force-flat: all positions closed** |
| 04:00 MYT | 16:00 ET | Postmarket — agent idles |

> ⚠️ **The US INTRADAY session runs while you're asleep in Malaysia.** The
> force-flat at 15:55 ET (03:55 MYT) guarantees no overnight positions — you
> wake up with a clean slate every morning.

**You only need to touch the dashboard for:**
- Reviewing overnight intraday results (📊 Performance / 💼 Portfolio)
- Switching mode (Sidebar → 🧭 Trading Mode)
- Adjusting risk (⚙️ Settings)
- Emergency stop (🚨 Kill-Switch)

---

## 9. Key Controls — Where to Find Everything

### Sidebar (always visible)

| Control | What it does |
|---|---|
| **🌐 Market** | Switch between 🇲🇾 MY and 🇺🇸 US (reloads on switch) |
| **🧭 Trading Mode** | Switch between 📈 SWING (hourly) and ⚡ INTRADAY (5-min ORB). **INTRADAY only shown when 🇺🇸 US is active.** |
| **Initial Capital (RM/$)** | Starting paper-trade balance for the active market |
| **🤖 Robo-Trader badge** | Status + last/next run + current mode + interval + brain mode |
| **🔕/🟢 Broker badge** | Current broker mode + connection light (US) |
| **Intraday availability banner** | Yellow warning if OpenD is not connected (INTRADAY mode) or market doesn't support intraday (MY) |
| **Start/Stop button** | Toggle the scheduler |
| **➕ Add Custom Stock** | Append a ticker outside the curated list |

### ⚡ Intraday Scanner tab (INTRADAY mode only)

- Session state card (OR_WINDOW / ACTIVE_TRADING / FORCE_FLAT / PRE/POST) with colour coding
- **⚡ SCAN INTRADAY** — manual scan of the curated-6 ORB watchlist
- Signal filter + signal cards with VWAP, rel-vol, OR high/low/range, EMA-200 trend
- 5-minute candlestick chart with OR high/low reference lines
- **✅ EXECUTE INTRADAY ORDER** button (manual entry with risk check)

### 🔍 Scanner tab (SWING mode)

- **🔥 SCAN MARKET** — manual swing scan
- Filter by signal type; click any ticker → 90-day chart, 5-day tape, execute panel

### 💼 Portfolio tab

- Active positions with live P&L, MAE/MFE; sector exposure heatmap
- Manual close / partial exit; closed trades history
- Shows both SWING and INTRADAY trades for the active market

### 🧠 AI Learning tab

- Strategy + sector performance, Bayesian state priors, ML feature importance + OOS accuracy, walk-forward
- Reflects the **active (market, mode)** brain

### 📊 Performance tab

- Sharpe / Sortino / Max Drawdown / Profit Factor / Expectancy (R)
- Equity curve vs benchmark; calibration chart; per-regime hit rate
- Reflects the **active (market, mode)** database

### ⚡ Intraday Robo-Trader tab (INTRADAY mode) / 🤖 Robo-Trader tab (SWING)

| Control | Action |
|---|---|
| ▶ Start / 🛑 Stop | Daemon thread on/off |
| ♻ Force Restart | Stop + start with correct cadence for current mode |
| ⚡ Run Cycle Now | Immediate cycle (intraday or swing depending on mode) |
| 🚨 Kill-Switch | Emergency stop; won't restart until cleared |
| Auto-exit | Settle SL/TP/trailing/time (SWING) or settle intraday exits (INTRADAY) |
| Auto-entry | Open GOLD BUY entries (SWING) or ORB entries (INTRADAY) |
| Cycle interval | 15/30/60/120 min (SWING only) — locked to 5 min in INTRADAY |
| Watchdog & Cycle Health | Watchdog status, recent timeouts |
| Learning Mode | Force EXPLORE or EXPLOIT; progress bar to target |
| Trading-Time Window | (SWING) shows window + status; (INTRADAY) shows session state |

### ⚙️ Settings tab

| Panel | Notes |
|---|---|
| **⚡ Intraday Mode Defaults** | Read-only — visible when INTRADAY mode active. Shows locked ORB parameters. |
| Scanner Parameters | Governs the SWING scanner. In INTRADAY mode a note explains these don't affect intraday. |
| Risk Parameters + Trading Window | Per market; US shows ET + MYT mirror |
| 📡 Data Source | Active provider + re-probe button |
| 🎯 Execution Mode | US: NOOP/SIMULATE/REAL + connection test |
| 🔄 Broker Reconciliation | Status + run-now button (US SWING) |
| 🏢 Corporate Actions | Auto-adjust toggle + manual scan |
| 🧪 Intraday Research Tools | (INTRADAY mode only) Commands to rerun the backtest scripts locally |
| Custom Watchlist | Add/remove tickers from the SWING scanner |
| Kill-switch clear | Appears when kill-switch is engaged |
| Persistent Backup | Gist backup status + backup/restore buttons |
| Long-term Maintenance Status | Holiday list, PAT rotation, walk-forward schedule |
| Reset Capital / Delete Trades | Destructive actions — all per active (market, mode) DB |

---

## 10. Common Operations

### A. Switch to INTRADAY mode

1. Sidebar → **🌐 Market** → 🇺🇸 US (INTRADAY is US-only)
2. Make sure **Moomoo OpenD is running** on your local PC (port 11111)
3. Sidebar → **🧭 Trading Mode** → `⚡ INTRADAY — 5m ORB`
4. Page reloads. Sidebar shows a green banner: *"Intraday ready: curated-6 ORB · 5-min cadence · force-flat 15:55"*
5. The scheduler restarts automatically with the 5-min cadence

If you see a yellow warning instead: OpenD is not connected — open Moomoo Desktop, enable OpenD, wait for the green dot, then refresh.

### B. Switch back to SWING

Sidebar → **🧭 Trading Mode** → `📈 SWING — hourly scanner`. The agent restarts on the hourly cadence.

### C. Switch markets

Sidebar → **🌐 Market** → pick MY or US. Each market keeps its own trades and brain.

### D. Check INTRADAY results in the morning

1. **📊 Performance** → Equity curve and expectancy reflect last night's trades
2. **💼 Portfolio** → Active positions (should be empty after force-flat) + closed trades
3. **📜 Logs → Trade executions** → filter `AGENT` to see every ORB entry/exit

### E. Check current session state

**⚡ Intraday Scanner tab** → session state card at the top. Or check the
Robo-Trader tab → *"Session state: ACTIVE_TRADING — ..."*

### F. Adjust risk per trade

**⚙️ Settings → Risk Parameters → Max risk / trade %** → 💾 Save. (Per market.)

| Value | Stance |
|---|---|
| 0.5% | Very conservative — first 50–100 trades (explorer mode) |
| **1.0%** | **Default** — balanced |
| 2.0% | Aggressive — only after 100+ validated intraday or 50+ SWING trades |

### G. Turn on US live/sim execution (SWING)

1. Local PC: install Moomoo Desktop + enable OpenD (port 11111)
2. Switch to 🇺🇸 US + 📈 SWING in the sidebar
3. **⚙️ Settings → 🎯 Execution Mode → 🔌 Test broker connection**
4. Switch to **SIMULATE** → run 4+ weeks → only then consider REAL

### H. Emergency stop

**🤖/⚡ Robo-Trader → 🚨 Kill-Switch**. Clears via **⚙️ Settings → Kill-Switch → Clear**.

### I. Force an immediate cycle

**🤖/⚡ Robo-Trader → ⚡ Run Cycle Now**. Works in both modes. In INTRADAY mode it runs one full intraday cycle (checks session state, scans if appropriate).

### J. Headless / no-browser operation

```bash
# SWING (MY, default)
python -m scheduler --interval 3600

# SWING (US)
MARKET_MODE=US python -m scheduler --interval 3600

# INTRADAY (US, 5-min)
MARKET_MODE=US TRADING_MODE=INTRADAY python -m scheduler --interval 300
```

### K. Reset one (market, mode) and start fresh

```bash
# Delete only the US INTRADAY brain
rm ~/.bursa_agent_data/bursa_agent_US_INTRADAY.db

# Delete only the US SWING brain
rm ~/.bursa_agent_data/bursa_agent_US_SWING.db

# Delete ALL data
rm -rf ~/.bursa_agent_data/
```

Or via UI: **⚙️ Settings → ⚠️ Destructive actions → Delete all trades + scan cache** (deletes the active (market, mode) DB only).

### L. Rerun the intraday backtest (after changing parameters)

On your local PC with OpenD running:

```bash
# 360-day edge validator (OpenD needed for full history)
python validate_intraday_edge.py

# Parameter grid sweep (EMA-100/200, curated-6 vs full-20, 1.5/2.0R)
python intraday_backtest_v3.py | Tee-Object -FilePath v3_report.txt

# Quick 60-day check (yfinance, no OpenD needed)
python intraday_backtest.py --tickers TNA,GOOGL,TQQQ,MSTR,SOXL,PLTR
```

See `HandBook/orb_backtest_results.md` for the original 4-round validation results.

---

## 11. Important Notes & Cautions

### ⚠️ Expect losses during EXPLORE mode

- **SWING:** first 50 closed trades per market — agent tries unknown setups by design
- **INTRADAY:** first 100 closed trades — more trades/day but more states to explore
- Keep risk at **0.5–1.0%** during this phase. Losses are training data.

### ⚠️ Four separate brains, four separate validations

`MY_SWING`, `US_SWING`, `MY_INTRADAY`, `US_INTRADAY` each learn independently.
A calibrated US SWING brain says nothing about US INTRADAY — each needs its own
sample before you trust its signals.

### ⚠️ INTRADAY runs while you sleep

The US intraday session is 09:45–15:55 ET = 21:45–03:55 MYT. Force-flat at
15:55 ET guarantees no overnight positions. You wake up with a clean slate.
Review results in the morning via the Performance and Portfolio tabs.

### ⚠️ INTRADAY requires Moomoo OpenD locally

On Streamlit Cloud or any PC without OpenD, INTRADAY mode shows a warning
banner and refuses new entries. It still runs the 5-min loop (useful for
monitoring and force-flat), but no new positions are opened.

### ⚠️ INTRADAY edge is narrow — use explorer mode first

+0.090R expectancy was validated on curated-6. Realistic post-slippage is
approximately +0.07R. Do not widen the universe or switch to EXPLOIT mode
before completing 100 intraday paper trades and reviewing the calibration chart.

### ⚠️ Where each config should run

| Config | Where |
|---|---|
| MY SWING | Streamlit Cloud or local; yfinance; runs 24/7 fine |
| US SWING | Local PC with OpenD for SIMULATE/REAL; Streamlit Cloud for NOOP |
| US INTRADAY | **Local PC with OpenD only** — Streamlit Cloud can't do real intraday |

### ⚠️ Risk gates are always active

Even with auto-entry ON, every trade passes `run_full_risk_check`: drawdown >8%
halves size, >15% blocks all trading, max positions / position-cost / sector
caps / daily limit / trading-hours all enforced.

### ⚠️ INTRADAY parameters are locked

The curated-6 universe and ORB parameters were validated over 360 days of real
Moomoo OpenD data. Changing them requires rerunning `validate_intraday_edge.py`.
The ⚙️ Settings panel shows them as read-only for this reason.

---

## 12. Troubleshooting

| Symptom | Fix |
|---|---|
| Scheduler shows STOPPED | ♻️ Force Restart in 🤖/⚡ Robo-Trader tab |
| Kill-switch stuck on | ⚙️ Settings → Clear kill-switch |
| 🧭 Trading Mode only shows SWING | Switch to 🇺🇸 US first — INTRADAY is US-only |
| Intraday yellow warning banner | OpenD is not connected. Open Moomoo Desktop, enable OpenD (port 11111), wait for green status, refresh |
| Intraday "PREMARKET" all the time | It's outside US RTH (09:30–16:00 ET). Normal. |
| No INTRADAY signals at 10:00 ET | OR_WINDOW just closed. Signals only fire after 09:45 ET when the 15-min opening range is set. |
| No GOLD BUY (ORB) signals | Daily EMA-200 filter may be blocking (prior close < EMA-200). Check the session state card. |
| Force-flat didn't close positions | Verify OpenD was connected at 15:55 ET. If offline, positions may survive — manually close via Portfolio tab. |
| No SWING GOLD BUY signals | BEAR regime raises threshold to 80%; check Scanner regime banner |
| Auto-entries not happening (SWING) | Check: market hours? past safe-entry cutoff? at max positions? See Logs |
| US tab shows RM / wrong currency | Hard refresh (Ctrl+Shift+R); ensure latest `app.py` deployed |
| US Settings times look odd | Expected: US shows native ET with MYT in brackets — enter window values in ET |
| `no such table: account` after switching | Pull latest code; market switch now calls `init_db()` automatically |
| Moomoo OpenD "not listening" | Open Moomoo Desktop AND enable OpenD (Settings → API → port 11111) |
| REAL mode says "MOOMOO_TRADING_PWD not set" | Add the secret + restart so env vars reload |
| Reconciliation drift alert | Expected if <0.5% — internal heuristic slippage vs real fills |
| Brain lost after redeploy | Set `GITHUB_TOKEN` (+ `GIST_ID`) in Streamlit Secrets |
| `ModuleNotFoundError: scipy` / `pandas_market_calendars` | `pip install -r requirements.txt` |
| Want to delete everything | `rm -rf ~/.bursa_agent_data/` then restart |
| Intraday backtest shows no trades | Ensure `--days` ≤ 60 for yfinance (5m limited) or use OpenD for longer history |

---

## Quick Reference

```
START (local):
  streamlit run app.py

HEADLESS:
  SWING MY:       python -m scheduler --interval 3600
  SWING US:       MARKET_MODE=US python -m scheduler --interval 3600
  INTRADAY US:    MARKET_MODE=US TRADING_MODE=INTRADAY python -m scheduler --interval 300

TESTS:
  pytest tests/ -q        (605 tests, ~53s, 0 failures, full suite green in one run)

DATABASES (per market × mode):
  ~/.bursa_agent_data/bursa_agent_MY_SWING.db
  ~/.bursa_agent_data/bursa_agent_US_SWING.db
  ~/.bursa_agent_data/bursa_agent_US_INTRADAY.db

MARKERS:
  ~/.bursa_agent_data/.active_market    ("MY" or "US")
  ~/.bursa_agent_data/.trading_mode     ("SWING" or "INTRADAY")

LOGS:
  ~/.bursa_agent_data/logs/bursa_agent.log

KEY CONTROLS:
  Market           → Sidebar → 🌐 Market
  Trading Mode     → Sidebar → 🧭 Trading Mode  ← INTRADAY is here
  Auto-entry       → 🤖/⚡ Robo-Trader → Auto-Trading Toggles
  Risk per trade % → ⚙️ Settings → Risk Parameters
  Cycle interval   → 🤖 Robo-Trader (SWING only; INTRADAY locked to 5 min)
  Broker mode (US) → ⚙️ Settings → Execution Mode
  Shariah filter   → ⚙️ Settings → Scanner Parameters
  Brain mode       → 🤖/⚡ Robo-Trader → Learning Mode
  Intraday params  → ⚙️ Settings → ⚡ Intraday Mode Defaults (read-only)
  Kill-Switch      → 🤖/⚡ Robo-Trader → Controls

DEFAULTS:
  Auto-entry        ON
  Auto-exit         ON
  Risk per trade    1.0%
  Broker mode       NOOP (MY fixed; US can SIMULATE/REAL)
  Default market    MY
  Default mode      SWING
  Capital           MY RM 20,000 / US $ 5,000
  Max positions     MY 8/5/3 · US 6/4/2 (BULL/NEUTRAL/BEAR)
  Drawdown warn     8%   (halve size)
  Drawdown stop     15%  (block all trading)
  SWING sessions    MY 09:00–17:00 MYT · US 09:30–16:00 ET
  INTRADAY session  09:30–16:00 ET (09:45 first entry, 15:55 force-flat)
  Safe-entry cutoff MY 16:00 MYT · US 15:30 ET (SWING only)
  Brain backup      Every closed trade + hourly (per-market-mode Gist)
  SWING explorer    50 closed trades per market
  INTRADAY explorer 100 closed trades
```

---

**Start small. Trust the data, not the hope.** 🚀

Four brains. Four databases. One agent.
