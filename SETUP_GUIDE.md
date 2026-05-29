# BursaAI Swing Agent — Setup Guide

**Autonomous · Self-learning · Light-themed · Audited**

Python 3.9+ · Windows / macOS / Linux / Streamlit Cloud

---

## Prerequisites

```bash
python --version       # 3.9 or higher
pip --version
```

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

**`requirements.txt` installs:** streamlit, yfinance, pandas, numpy, plotly, scikit-learn, scipy, joblib, pytest, requests.

---

## Run the dashboard

```bash
streamlit run app.py
```

Open http://localhost:8501. The Robo-Trader starts automatically.

A persistent SQLite DB is created at `~/.bursa_agent_data/bursa_agent.db`.
Rotating text logs at `~/.bursa_agent_data/logs/bursa_agent.log`.

---

## Run the test suite

```bash
pytest tests/ -q
```

**191 tests** should pass in ~40 seconds. The suite uses an isolated temp directory and never touches your real DB.

---

## Run the agent without Streamlit (headless)

```bash
python -m scheduler --interval 3600
```

This boots the same loop without launching the UI. Pair with cron / systemd / Docker for true 24/7 autonomy.

---

## Deploy to Streamlit Cloud

1. Push the repo to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io) → New app → select your repo
3. **Manage app → Secrets** — add the following (optional but recommended):

```toml
GITHUB_TOKEN = "ghp_..."       # Classic PAT with gist scope (for brain persistence)
TELEGRAM_BOT_TOKEN = "..."     # From @BotFather (for live alerts)
TELEGRAM_CHAT_ID = "..."       # From @userinfobot
```

Without `GITHUB_TOKEN`, the brain wipes on every container reset. See `LIVE_TRIGGER_GUIDE.md` for Telegram/Email setup.

---

## Tabs at a glance

| Tab | What it does |
|---|---|
| 🔍 **Scanner** | Live market scan; click a row to see chart + 5-day tape + execute |
| 💼 **Portfolio** | Active + closed trades, sector heatmap, manual close, partial exit |
| 🧠 **AI Learning** | Bayesian state priors, biases, ML classifier metrics, walk-forward |
| 📊 **Performance** | Sharpe / Sortino / drawdown / calibration / KLCI benchmark |
| 🤖 **Robo-Trader** | Start/Stop/Restart, kill-switch, auto-trade toggles, watchdog health |
| 📜 **Logs** | Trade executions · Scheduler · Learning · Bias updates · Data quality (CSV download) |
| 🔔 **Live Alerts** | Telegram + Email notification config and alert history |
| ⚙️ **Settings** | Scanner params, risk params, custom watchlist, persistent backup, maintenance status |

---

## Module map (19 modules)

```
autonomous_bursa_agent/
├── app.py                    ← Streamlit dashboard (8 tabs, light theme)
├── scheduler.py              ← Robo-Trader daemon + watchdog (v3.2 lifecycle)
├── screener.py               ← Indicators + GOLD BUY signal classifier
├── trading_engine.py         ← Entry / partial / full exit + slippage + lots
├── risk_manager.py           ← Drawdown / position / sector / time-window gates
├── learner.py                ← Bayesian priors + walk-forward + ML classifier
├── market_analyzer.py        ← KLCI regime + sector momentum + RS
├── market_calendar.py        ← Bursa sessions + public holidays (through 2027)
├── evaluation.py             ← Sharpe / drawdown / calibration / benchmarks
├── data_quality.py           ← OHLCV validator
├── repository.py             ← All SQL access (repository pattern)
├── db.py                     ← SQLite schema + WAL connection
├── logger.py                 ← 6 log streams + rotating text file
├── watchlist.py              ← ~74 Bursa tickers + custom user list
├── notifier.py               ← Telegram (plain text) + Email (HTML)
├── live_trigger.py           ← Filter + dedup + format trade alerts
├── broker_adapter.py         ← Moomoo stub (v4-ready)
├── persistence.py            ← Gist-backed DB backup + restore
├── maintenance_reminders.py  ← Holiday / PAT / WFO renewal banners
├── ai_parameters.json        ← Default scanner params
├── requirements.txt
├── .streamlit/config.toml    ← Light theme enforcement
├── tests/                    ← 191 tests across 24 files
└── HandBook/                 ← PROJECT_HANDBOOK.md, AI_CHAT_HANDOFF.md
```

---

## SQLite schema (21 tables)

Single file at `~/.bursa_agent_data/bursa_agent.db`:

| Table | Purpose |
|---|---|
| `trades` | Full trade journal (active + closed) |
| `partial_exits` | TP partial-exit child rows |
| `account` | Singleton: capital, cash, equity |
| `parameters` | Singleton: scanner params (JSON blob) |
| `parameter_history` | Every param change with before/after |
| `bias_state` | Singleton: strategy + sector bias multipliers |
| `bias_history` | Every bias update with before/after |
| `state_priors` | Per-(state_id, action) Beta(α,β) priors |
| `learning_events` | Bayes updates, ML training, walk-forward |
| `scheduler_log` | Every robo-trader cycle event |
| `scheduler_state` | Singleton: running, owner_pid, heartbeat, toggles |
| `trade_log` | Every trade action (entry/exit/reject) |
| `data_quality_log` | Per-ticker data validation issues |
| `scan_cache` | Most recent screener output |
| `risk_params` | Risk parameter overrides |
| `custom_watchlist` | User-added tickers |
| `live_trigger_config` | Telegram/Email filter config |
| `alert_log` | Every alert sent/skipped/failed |
| `maintenance_state` | Daily-task idempotency |
| `regime_history` | Per-cycle KLCI regime snapshots |
| `meta` | Key/value store (Gist marker, PAT timestamp) |

---

## Configuration constants

### Slippage model (`trading_engine.py`)
```python
TRANSACTION_COST_PCT = 0.0015     # 0.15% per leg (brokerage + stamp + clearing)
SLIPPAGE_BASE_BPS = 5             # 0.05% minimum market-impact
SLIPPAGE_K_RM = 50_000            # linear component scales with order size
SLIPPAGE_LIQUIDITY_CAP_BPS = 80   # hard cap at 0.80%
LOT_SIZE = 100                    # Bursa board lot
```

### Risk defaults (`risk_manager.py`)
```python
max_risk_per_trade_pct = 1.0      # 1% of capital per trade
max_drawdown_pct = 8.0            # warn + halve size
max_drawdown_strict_pct = 15.0    # hard stop all trading
max_concurrent_positions = 8      # (3 in BEAR regime)
max_trades_per_day = 5
```

All adjustable via **⚙️ Settings → Risk Parameters** in the dashboard.

---

## Files written outside the repo

| Path | Contains |
|---|---|
| `~/.bursa_agent_data/bursa_agent.db` | All trades, state, logs, brain |
| `~/.bursa_agent_data/logs/bursa_agent.log` | Rotating text log (5 × 2 MB) |
| `~/.bursa_agent_data/setup_classifier.pkl` | Calibrated ML model |
| `~/.bursa_agent_data/regime_classifier.pkl` | KLCI regime model |
| `~/.bursa_agent_data/market_regime_cache.json` | 2-hour TTL cache |
| `~/.bursa_agent_data/sector_momentum.json` | 2-hour TTL cache |
| `~/.bursa_agent_data/.gist_marker.json` | Gist backup metadata |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: scipy` | `pip install scipy>=1.10` |
| Scheduler shows STOPPED | Click ♻️ Force Restart in 🤖 Robo-Trader tab |
| Kill-switch stuck on | ⚙️ Settings → Clear kill-switch |
| Tests fail in CI | Ensure `HOME` env var is writable — `conftest.py` uses it |
| yfinance returns empty for `^KLSE` | Retry; or wait — yfinance has periodic outages |
| Reset everything | `rm -rf ~/.bursa_agent_data/` then restart |
| Brain lost after Streamlit Cloud redeploy | Set `GITHUB_TOKEN` in Secrets for Gist backup |

---

**Paper first. Always.** 🚀
