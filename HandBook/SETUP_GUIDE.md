# BursaAI Swing Agent — Setup Guide (v3.6)

**Multi-market · Autonomous · Self-learning · Light-themed · Audited**

Python 3.9+ · Windows / macOS / Linux / Streamlit Cloud
Markets: 🇲🇾 Bursa Malaysia (KLSE) · 🇺🇸 NYSE/NASDAQ

---

## What v3.6 added vs v3.5

- **Multi-market support** — switch between MY (Bursa) and US (NYSE/NASDAQ) from the sidebar dropdown
- **Per-market isolated databases** — `bursa_agent_MY.db` / `bursa_agent_US.db`, separate trade history, Bayesian brain, risk params
- **Full Moomoo US execution adapter** — NOOP / SIMULATE / REAL broker modes
- **Per-cycle reconciliation** — internal vs broker drift alerts via Telegram
- **Symmetric data fallback** — Moomoo OpenD ↔ yfinance for both markets (MY gated to yfinance until Moomoo OpenAPI adds Bursa)
- **Timezone-aware Settings** — because you trade from Malaysia, US session/cutoff times show as both native exchange time **and** the MYT equivalent (e.g. `09:30–16:00 ET (21:30–04:00 MYT)`)

---

## Prerequisites

```bash
python --version       # 3.9 or higher
pip --version
```

For US trading execution (optional, only needed for SIMULATE / REAL modes):
- **Moomoo Desktop** installed on the machine running the agent
- **OpenD** enabled inside Moomoo Desktop (Settings → API → Enable OpenAPI on port 11111)

---

## Installation

```bash
# 1. Clone the repo
git clone <your-repo-url>
cd autonomous_bursa_agent

# 2. Virtual environment (recommended)
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

# 3. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

**`requirements.txt` installs:** streamlit, yfinance, pandas, numpy, plotly, scikit-learn, scipy, joblib, pytest, requests, pandas_market_calendars (NYSE calendar), moomoo-api (optional — for OpenD execution).

---

## Run the dashboard

```bash
streamlit run app.py
```

Open http://localhost:8501. The Robo-Trader starts automatically.

Per-market persistent SQLite DBs are created at:
- `~/.bursa_agent_data/bursa_agent_MY.db`
- `~/.bursa_agent_data/bursa_agent_US.db`

Rotating text logs at `~/.bursa_agent_data/logs/bursa_agent.log`.

> 🔄 **Upgrading from v3.3-v3.5?** Your existing `bursa_agent.db` is auto-migrated to `bursa_agent_MY.db` on first boot. All your trades, brain, and history are preserved.

---

## 🌐 Switching markets

### In-app (recommended)
Sidebar → **🌐 Market** dropdown → choose 🇲🇾 MY or 🇺🇸 US → page reloads.

### Via env var (sets default on boot)
```bash
export MARKET_MODE=US   # or MY
streamlit run app.py
```

### Via marker file
```bash
echo US > ~/.bursa_agent_data/.active_market
```

**Resolution order:** env var > marker file > default (MY).

### Confirming you're on the right market
The sidebar always shows the active flag (🇲🇾 or 🇺🇸) + market name + currency symbol (RM or $). Each market has fully isolated trades, brain, account, and parameters — no cross-contamination possible.

---

## Run the test suite

```bash
pytest tests/ -q
```

**471 tests** should pass in ~45 seconds — and the **full suite must pass in one `pytest tests/` run** (not just per-file). The suite uses isolated temp directories and never touches your real DBs.

---

## Run the agent without Streamlit (headless)

```bash
# Default: MY market
python -m scheduler --interval 3600

# US market
MARKET_MODE=US python -m scheduler --interval 3600
```

Pair with cron / systemd / Docker for true 24/7 autonomy.

---

## 🔐 Secrets — complete reference

You can set these in either:
- **Streamlit Cloud:** Manage app → Settings → Secrets (TOML format)
- **Local:** `~/.streamlit/secrets.toml` or shell environment variables

### Required for ALL setups (both markets)

| Secret | Required? | Purpose |
|---|---|---|
| `GITHUB_TOKEN` | ⚠️ Recommended | Classic PAT with **`gist`** scope. Without it, your brain wipes on every container reset / 7-day sleep. [How to create →](#how-to-create-a-github-pat-for-backups) |
| `GIST_ID` | ⚠️ Recommended | Set after first successful backup. Acts as a backup pointer so the agent can find your Gist even if the local marker file is wiped during a container reset. |

### For notification-only / paper-trading (both markets, default mode)

| Secret | Required? | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Optional | From @BotFather — enables Telegram alerts |
| `TELEGRAM_CHAT_ID` | Optional | From @userinfobot — your Telegram destination |
| `ALERT_SMTP_HOST` | Optional | e.g. `smtp.gmail.com` |
| `ALERT_SMTP_PORT` | Optional | e.g. `587` |
| `ALERT_SMTP_USER` | Optional | Your sender email |
| `ALERT_SMTP_PASSWORD` | Optional | Gmail App Password (NOT your normal Gmail password) |
| `ALERT_SMTP_FROM` | Optional | From: header — defaults to ALERT_SMTP_USER |

### For US SIMULATE / REAL broker execution (US market only)

| Secret | When needed | Purpose |
|---|---|---|
| `MARKET_MODE` | Optional | Set to `US` to make US the default market on app boot. Otherwise defaults to MY and you switch via the sidebar. |
| `MOOMOO_HOST` | Optional | OpenD host. Defaults to `127.0.0.1`. Only change if OpenD is on a different machine than the agent. |
| `MOOMOO_PORT` | Optional | OpenD port. Defaults to `11111`. |
| **`MOOMOO_TRADING_PWD`** | ⚠️ REQUIRED for REAL mode | Your moomoo **trading password** (the one used to unlock orders, NOT your login password). Required to call `unlock_trade()`. Without this, REAL mode fails loudly. |
| `MOOMOO_SECURITY_FIRM` | Optional | Defaults to `FUTUINC` (US accounts). Other options: `FUTUSECURITIES` (HK), `FUTUSG` (SG), `FUTUAU` (AU). |
| `BURSA_DATA_PROVIDER` | Optional | Defaults to `auto`. Force to `yfinance` to skip moomoo data-source attempts (useful for debugging). |

### Recommended setup by deployment

#### 🏠 Local PC — your primary execution machine
Create `~/.streamlit/secrets.toml`:

```toml
# Persistence (CRITICAL — without this you lose your brain on every restart)
GITHUB_TOKEN = "ghp_..."
GIST_ID = "your-gist-id-here"

# Notifications
TELEGRAM_BOT_TOKEN = "..."
TELEGRAM_CHAT_ID = "..."

# Email alerts (Gmail example)
ALERT_SMTP_HOST = "smtp.gmail.com"
ALERT_SMTP_PORT = "587"
ALERT_SMTP_USER = "you@gmail.com"
ALERT_SMTP_PASSWORD = "your-16-char-app-password"
ALERT_SMTP_FROM = "you@gmail.com"

# Moomoo defaults — leave commented unless OpenD is non-standard
# MOOMOO_HOST = "127.0.0.1"
# MOOMOO_PORT = "11111"
# MOOMOO_SECURITY_FIRM = "FUTUINC"

# ONLY uncomment when you're ready to enable REAL trading
# (after ~4 weeks of SIMULATE validation)
# MOOMOO_TRADING_PWD = "your-trading-password"
```

#### ☁️ Streamlit Cloud — monitoring / yfinance paper-trade only
Manage app → Secrets:

```toml
# Same as local minus moomoo (OpenD is unreachable from Streamlit Cloud)
GITHUB_TOKEN = "ghp_..."
GIST_ID = "your-gist-id-here"
TELEGRAM_BOT_TOKEN = "..."
TELEGRAM_CHAT_ID = "..."
ALERT_SMTP_HOST = "smtp.gmail.com"
ALERT_SMTP_PORT = "587"
ALERT_SMTP_USER = "you@gmail.com"
ALERT_SMTP_PASSWORD = "..."
ALERT_SMTP_FROM = "you@gmail.com"
```

On Streamlit Cloud the agent always runs in NOOP mode (paper trades + Telegram alerts only). Moomoo OpenD execution requires the local PC setup above.

---

## How to create a GitHub PAT for backups

1. Go to https://github.com/settings/tokens (**classic tokens** — NOT `?type=beta`)
2. Click **"Generate new token (classic)"**
3. Note: `bursa-ai-backup` (include the year if you want, e.g. `bursa-ai-backup-2027`)
4. Expiration: 1 year (longer if available)
5. **Select ONLY the `gist` scope** — don't check anything else
6. Generate → copy the token (starts with `ghp_...`) — you only see it once
7. Paste into Streamlit Cloud Secrets / local `secrets.toml` as `GITHUB_TOKEN`
8. Restart app → ⚙️ Settings → 🗄️ Persistent Backup → click **💾 Backup now**
9. Once successful, copy the gist ID from the success message → set as `GIST_ID` secret too

> ⚠️ **Important:** Fine-grained tokens (`?type=beta`) do NOT support the Gist API. You must use classic tokens.

---

## How to set up Moomoo OpenD (for US SIMULATE / REAL trading)

1. Install **Moomoo Desktop** from https://www.moomoo.com/download
2. Sign in to your moomoo account
3. Open Moomoo Desktop → Settings (⚙️) → API
4. Toggle **"Enable OpenAPI"** ON
5. Note the port — usually **11111** (this is what `MOOMOO_PORT` defaults to)
6. **Keep Moomoo Desktop running** while the agent runs — OpenD needs the desktop process alive

Verify it's working:
- In the agent: ⚙️ Settings tab → 🎯 Execution Mode → click **🔌 Test broker connection**
- Should show: `✅ Connected as moomoo_us in NOOP mode`

To flip to SIMULATE:
- Same panel → dropdown → SIMULATE → click **💾 Switch broker mode to SIMULATE**
- Recommend 4+ weeks of SIMULATE before considering REAL

To flip to REAL (only after SIMULATE has matched paper-trade expectations):
- Set `MOOMOO_TRADING_PWD` in secrets / env
- Restart agent
- Settings → Execution Mode → dropdown → REAL → confirm warning → save

---

## Deploy to Streamlit Cloud

1. Push the repo to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io) → New app → select your repo + branch
3. **Manage app → Secrets** — paste the Streamlit Cloud config from the "Recommended setup" section above
4. Save → app redeploys

---

## Tabs at a glance

| Tab | What it does |
|---|---|
| 🔍 **Scanner** | Live market scan; click a row to see chart + 5-day tape + execute |
| 💼 **Portfolio** | Active + closed trades, sector heatmap, manual close, partial exit |
| 🧠 **AI Learning** | Bayesian state priors, biases, ML classifier metrics, walk-forward |
| 📊 **Performance** | Sharpe / Sortino / drawdown / calibration / benchmark vs market |
| 🤖 **Robo-Trader** | Start/Stop/Restart, kill-switch, auto-trade toggles, watchdog health |
| 📜 **Logs** | Trade executions · Scheduler · Learning · Bias updates · Data quality · Corporate actions (CSV download) |
| 🔔 **Live Alerts** | Telegram + Email notification config and alert history |
| ⚙️ **Settings** | Scanner params, risk params, market switcher, broker execution mode, reconciliation, persistent backup, maintenance status |

---

## Module map (23 top-level modules + market_profiles/ package = 27 total)

```
autonomous_bursa_agent/
├── app.py                        ← Streamlit dashboard (8 tabs, light theme)
├── scheduler.py                  ← Robo-Trader daemon + watchdog + reconciliation hook
├── screener.py                   ← Indicators + GOLD BUY signal classifier
├── trading_engine.py             ← Entry / exits + slippage + lots (profile-aware)
├── risk_manager.py               ← Drawdown / position / sector / time-window gates
├── learner.py                    ← Bayesian priors + walk-forward + ML classifier
├── market_analyzer.py            ← KLCI/SPY regime + sector momentum + RS
├── market_calendar.py            ← Bursa + NYSE sessions / holidays (dispatched)
├── evaluation.py                 ← Sharpe / drawdown / calibration / benchmarks
├── data_quality.py               ← OHLCV validator
├── repository.py                 ← All SQL access (repository pattern)
├── db.py                         ← Per-market SQLite + WAL + auto-migration
├── logger.py                     ← 6 log streams + rotating text file
├── watchlist.py                  ← MY+US universe (profile-aware)
├── notifier.py                   ← Telegram (plain text) + Email (HTML)
├── live_trigger.py               ← Filter + dedup + format trade alerts
├── broker_adapter.py             ← Noop + MoomooMY (stub) + MoomooUS (full)
├── data_provider.py              ← Moomoo OpenD ↔ yfinance fallback
├── corporate_actions.py          ← Split / bonus / dividend detection + adjust
├── reconciliation.py             ← Internal-vs-broker drift checker (v3.6)
├── persistence.py                ← Per-market Gist backup + restore
├── maintenance_reminders.py      ← Holiday / PAT / WFO renewal banners
├── verify_moomoo.py              ← Standalone OpenD diagnostic
├── market_profiles/              ← Multi-market abstraction (v3.6)
│   ├── __init__.py               ← active_profile() resolver
│   ├── base.py                   ← MarketProfile Protocol + helpers
│   ├── my_profile.py             ← MY_PROFILE singleton (Bursa)
│   └── us_profile.py             ← US_PROFILE singleton (NYSE/NASDAQ)
├── ai_parameters.json            ← Default scanner params
├── requirements.txt
├── .streamlit/config.toml        ← Light theme enforcement
├── tests/                        ← 471 tests across 35 files
└── HandBook/                     ← PROJECT_HANDBOOK.md, AI_CHAT_HANDOFF.md
```

---

## Per-market data layout

Each market has its OWN SQLite database file with the full schema (~22 tables):

```
~/.bursa_agent_data/
├── .active_market                          ← marker file: "MY" or "US"
├── .gist_marker.json                       ← Gist backup pointer (shared)
├── bursa_agent_MY.db                       ← MY market: trades, brain, account, params
├── bursa_agent_US.db                       ← US market: trades, brain, account, params
├── setup_classifier.pkl                    ← ML model (shared filename, market-tagged inside)
├── regime_classifier.pkl                   ← KLCI/SPY regime model
├── market_regime_cache.json                ← 2-hour TTL cache
├── sector_momentum.json                    ← 2-hour TTL cache
└── logs/
    └── bursa_agent.log                     ← Rotating text log (5 × 2 MB)
```

### How brains stay separated

- **`account` table** (cash, equity, capital) → separate per DB
- **`trades` table** → separate per DB
- **`state_priors`** (Bayesian Beta α/β posteriors — the actual "brain") → separate per DB
- **`bias_state`** (strategy + sector bias multipliers) → separate per DB
- **`risk_params`** (drawdown caps, max positions) → separate per DB
- **`scheduler_state`** (broker_mode, autotrade toggles, reconciliation status) → separate per DB
- **`live_trigger_config`** (Telegram/Email filter) → separate per DB

### Gist backup is per-(market, mode) too

Inside your single private Gist:
```
bursa_agent_MY_SWING_db.b64.gz            ← MY SWING DB (gzipped + base64)
setup_classifier_MY_SWING.pkl.b64.gz      ← MY SWING ML model
bursa_agent_US_SWING_db.b64.gz            ← US SWING DB
setup_classifier_US_SWING.pkl.b64.gz      ← US SWING ML model
bursa_agent_US_INTRADAY_db.b64.gz         ← US INTRADAY DB
setup_classifier_US_INTRADAY.pkl.b64.gz    ← US INTRADAY ML model
```

---

## Per-market configuration

The `MarketProfile` for each market lives in `market_profiles/<code>_profile.py`. Key differences:

| Setting | 🇲🇾 MY (Bursa) | 🇺🇸 US (NYSE/NASDAQ) |
|---|---|---|
| Currency | MYR (RM) | USD ($) |
| Lot size | 100 shares (board lot) | 1 share |
| Default capital | RM 20,000 | $ 5,000 |
| Sessions | 09:00-12:30 + 14:30-17:00 MYT (lunch break) | 09:30-16:00 ET (RTH only) — UI also shows MYT mirror |
| Timezone | Asia/Kuala_Lumpur | America/New_York (Settings input is in ET; MYT shown in brackets) |
| Safe-entry cutoff | 16:00 MYT | 15:30 ET (≈03:30 MYT) |
| Holidays | Hardcoded set (update yearly) | Auto-extending via `pandas_market_calendars` |
| Regime ticker | `^KLSE` | `SPY` |
| Per-trade fee | 0.15% per side | 0% (moomoo US commission-free) |
| Slippage | 5-80 bps (volume-aware Bursa) | 2-35 bps (tighter US ETFs) |
| Min risk per trade | RM 50 | $ 20 |
| Max positions | BULL 8 / NEUTRAL 5 / BEAR 3 | BULL 6 / NEUTRAL 4 / BEAR 2 |
| Default universe | 74 Bursa tickers + Shariah filter | 25 leveraged ETFs + mega-caps |
| Moomoo execution | ❌ Not yet (OpenAPI gap) | ✅ Full (SIMULATE + REAL) |

---

## Configuration constants

### Risk defaults (`risk_manager.py`)
```python
max_risk_per_trade_pct = 1.0          # 1% of capital per trade
max_drawdown_pct = 8.0                # warn + halve size
max_drawdown_strict_pct = 15.0        # hard stop all trading
max_concurrent_positions = 8 (MY) / 6 (US)
max_trades_per_day = 5
```

All adjustable per market via **⚙️ Settings → Risk Parameters** in the dashboard.

### Reconciliation (`reconciliation.py`)
```python
DEFAULT_DRIFT_ALERT_THRESHOLD = 0.005   # 0.5% of equity
POSITION_QTY_TOLERANCE_ABS = 1          # 1 share
POSITION_QTY_TOLERANCE_PCT = 0.01       # 1%
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: scipy` | `pip install scipy>=1.10` |
| `ModuleNotFoundError: pandas_market_calendars` | `pip install pandas_market_calendars>=4.4.0` (needed for NYSE holidays) |
| Scheduler shows STOPPED | Click ♻️ Force Restart in 🤖 Robo-Trader tab |
| Kill-switch stuck on | ⚙️ Settings → Clear kill-switch |
| Tests fail in CI | Ensure `HOME` env var is writable — `conftest.py` uses it |
| yfinance returns empty for `^KLSE` / `SPY` | Retry; or wait — yfinance has periodic outages |
| Reset everything for ONE market | Delete that specific DB: `rm ~/.bursa_agent_data/bursa_agent_US.db` |
| Reset everything for BOTH markets | `rm -rf ~/.bursa_agent_data/` then restart |
| Brain lost after Streamlit Cloud redeploy | Set `GITHUB_TOKEN` + `GIST_ID` in Secrets |
| `sqlite3.OperationalError: no such table: account` after switching market | Fixed in v3.6 — pull latest `market_profiles/__init__.py` + `app.py` |
| US tab still shows RM currency | Fixed in v3.6 — pull latest `app.py` + `live_trigger.py` (alerts are currency-aware too) |
| US Settings shows times in MYT only / confusing timezone | Expected in v3.6: US session + cutoff render as native `ET` with the `MYT` equivalent in brackets. Enter window values in **ET**. |
| Moomoo OpenD "not listening" on US SIMULATE | Make sure Moomoo Desktop is open AND OpenD is enabled in Settings → API |
| Moomoo REAL mode says "MOOMOO_TRADING_PWD not set" | Add the secret + restart the agent process so env vars reload |
| Switching market doesn't show new flag | Hard refresh (Ctrl+Shift+R) — Streamlit caches the sidebar across tab switches |
| Reconciliation drift alert when broker fills differently than internal | Expected — internal uses heuristic slippage, broker uses real NBBO. Drift <0.5% is normal |

---

## Long-term maintenance

The agent runs indefinitely, but two items need annual attention:

| Task | When | Detailed runbook |
|---|---|---|
| Append next year's Bursa public holidays to `market_calendar.MY_PUBLIC_HOLIDAYS` | Every January (Bursa publishes in late November) | Settings tab shows a banner from October each year |
| Regenerate `GITHUB_TOKEN` and update Streamlit Secrets | ~11 months after token creation | Settings tab warns at 11 months, errors at 12+ |
| Review walk-forward optimization | Quarterly | 🧠 AI Learning tab → Run Walk-Forward Optimization |

> **Good news:** US holidays auto-extend via `pandas_market_calendars` — no annual maintenance needed for the US market.

---

**Paper first. Always.** 🚀

Switch markets via the sidebar. Two brains, two databases, one agent.
