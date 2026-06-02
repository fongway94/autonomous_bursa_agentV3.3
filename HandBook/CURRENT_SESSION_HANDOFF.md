# Current Session Handoff
# Date: 2026-06-02 (Monday)
# Purpose: Pick up exactly where we left off if chat is disrupted

---

## WHERE WE ARE RIGHT NOW

### Today is Day 1 of live intraday testing
- US market opened at 09:30 ET (21:30 MYT)
- INTRADAY mode running on local PC
- 0 trades fired today (correct — low volume day, no clean breakout)
- TQQQ and SOXL broke OR_high but volume only 0.36x and 0.61x (need 1.2x)

### What is running right now

```
Streamlit Cloud (24/7):
  MY SWING  → yfinance → paper → NOOP (notify only)
  US SWING  → yfinance → paper → NOOP (notify only)
  ← both auto-scanning, no human intervention needed

Local PC (user's Windows machine, must keep running):
  US SWING  → SIMULATE → mirroring to Moomoo Book Trader
  US INTRADAY → paper only (no broker mirror yet)
  OpenD running on 127.0.0.1:11111 ✅
  NASDAQ Basic quote subscription active ✅
  streamlit run app.py → http://localhost:8501
```

---

## BUGS FIXED THIS SESSION (not yet pushed to GitHub)

### Bug 1 — US SWING "Unknown reason for zero entries" ✅ FIXED

**Root cause:** `scheduler.py` hardcoded `// 100` for share rounding (Bursa board lot logic).
For US market (lot size = 1), this zeroed ALL share quantities silently:
```
$50 risk / $5 risk_per_share = 10 shares
10 // 100 * 100 = 0 → silently skipped → "Unknown reason"
```

**Fix applied:** replaced hardcoded `// 100` with `lot_size()` from `trading_engine.py`
```python
# Before (broken for US):
target_shares = (target_shares // 100) * 100
if target_shares < 100: continue

# After (correct for both MY and US):
_lot = lot_size()  # 100 for MY, 1 for US
target_shares = (target_shares // _lot) * _lot
if target_shares < max(_lot, 1): continue
```

**Files changed:**
- `scheduler.py` — two places fixed (lines ~474 and ~504)
- `tests/test_scheduler_intraday.py` — 4 regression tests added

**Test count after fix: 615 passed, 0 failed**

**⚠️ NOT YET PUSHED TO GITHUB — push these 2 files:**
```
scheduler.py
tests/test_scheduler_intraday.py
```
Commit message: `fix: US SWING zero entries — use lot_size() instead of hardcoded 100 for share rounding`

---

## DIAGNOSTIC SCRIPTS CREATED THIS SESSION

These files are in the repo root (already pushed or created locally):

```
check_intraday.py      — EMA-200 trend filter check for curated-6
check_intraday2.py     — Deep ORB diagnostic (OR_high, VWAP, rel-vol per ticker)
check_rvol.py          — Rel-vol bar-by-bar breakdown for TQQQ
check_historical_signals.py — 60-day historical backtest to prove strategy fires
check_swing.py         — US SWING 0-entry diagnostic
```

**Add these to .gitignore or delete after debugging — they are not part of the live system.**

---

## TODAY'S DIAGNOSTIC RESULTS (2026-06-02, 11:31 ET)

### EMA-200 Trend Filter Status
```
TNA    → UP  ✅ (longs allowed)
GOOGL  → UP  ✅ (longs allowed)
TQQQ   → UP  ✅ (longs allowed)
MSTR   → DOWN ❌ (longs BLOCKED — below EMA-200)
SOXL   → UP  ✅ (longs allowed)
PLTR   → UP  ✅ (longs allowed)
```
MSTR correctly blocked. 5 tickers eligible today.

### ORB Status at 11:31 ET
```
TNA    close=66.980  OR_high=67.330 → ❌ not broken out
GOOGL  close=375.730 OR_high=377.380 → ❌ not broken out
TQQQ   close=85.335  OR_high=85.140 → ✅ broke out BUT vol=0.36x ❌
SOXL   close=227.630 OR_high=226.000 → ✅ broke out BUT vol=0.61x ❌
PLTR   close=159.605 OR_high=160.800 → ❌ not broken out
```
All conditions met except VOLUME. Market moving but no conviction.

### Historical Signal Check (last 60 days, yfinance)
```
TNA    19 trades  58% win  +2.61R  avg +0.137R ✅
GOOGL  20 trades  55% win  +3.83R  avg +0.191R ✅
TQQQ   24 trades  54% win  +4.08R  avg +0.170R ✅
SOXL   22 trades  23% win  +0.45R  avg +0.020R ⚠️ weak
PLTR    2 trades   0% win  -0.18R  avg -0.091R ❌ bad

TOTAL: 87 trades in 60 days = ~10.9 trades/week
→ Strategy DOES fire. Today was just a quiet day.
```
⚠️ NOTE: SOXL and PLTR underperforming. Watch after 4 weeks live — may need to remove.

---

## KEY DECISIONS MADE THIS SESSION

### 1. Pre-training the brain — DECIDED NOT TO DO
**Decision:** Don't pre-train the INTRADAY brain with historical simulated data.
**Reason:** Day 1 of live testing. Need clean baseline. Brain should learn from real conditions.
**Revisit:** After 4-6 weeks if trade count is too low.

### 2. Explorer target — KEEP AT 50 (lowered from 100)
**Decision:** Lower explorer target from 100 to 50 trades.
**How:** Dashboard → ⚡ Intraday Robo-Trader → Learning Mode → Change target to 50
**Reason:** Speeds up explorer→exploit transition. At ~10 trades/week → exploit in ~5 weeks.
**⚠️ User still needs to do this — not done yet.**

### 3. Block 9 — DO NOT BUILD YET
**Decision:** Block 9 (enhanced ORB) comes after live baseline is established.
**Reason:** No data to improve from yet. Need 4-6 weeks of live trades first.
**When:** After Block 8, after 4-6 weeks of live data.

### 4. Universe — KEEP CURATED-6 FOR NOW
**Decision:** Keep the 6-ticker watchlist as-is.
**Reason:** Backtest proved edge on these 6. Full-20 destroys edge.
**Revisit:** After live data shows SOXL/PLTR continuing to underperform.

---

## WHAT THE USER NEEDS TO DO BEFORE NEXT CHAT

### Immediate (today):
```
□ Push scheduler.py fix to GitHub (see Bug 1 above)
□ Push tests/test_scheduler_intraday.py to GitHub
□ Lower intraday explorer target to 50:
    Dashboard → ⚡ Intraday Robo-Trader → Learning Mode
    Change target from 100 → 50 → Force EXPLORE
□ Keep local PC running tonight (US market hours 21:30–04:00 MYT)
□ Keep Moomoo Desktop + OpenD running
```

### This week:
```
□ Check Portfolio tab each morning — did any SWING or INTRADAY trades fire?
□ Check Moomoo Book Trader — do positions match Portfolio tab?
□ Check 📜 Logs → AUTO_ENTRY_END — is US SWING now entering trades?
□ Run python check_intraday2.py occasionally to see ORB status
```

### Before next development session (4-6 weeks):
```
□ Collect: How many INTRADAY trades fired?
□ Collect: How many US SWING trades fired?
□ Collect: Do Book Trader positions match Portfolio?
□ Collect: Win rate and P&L for each mode
□ Screenshot or export: 📊 Performance tab for US SWING
□ Screenshot or export: ⚡ Intraday Scanner session logs
```

---

## NEXT DEVELOPMENT SESSION AGENDA

When you return in 4-6 weeks, bring the live trade data and we will:

### Step 1 — Review live results
```
US SWING SIMULATE:
  □ Matches Moomoo Book Trader? (ticker, shares, direction)
  □ Sharpe > 1.0 over trades so far?
  □ Expectancy positive?
  □ If yes → consider switching to REAL mode

US INTRADAY paper:
  □ How many trades in 4-6 weeks?
  □ Win rate vs backtest expectation?
  □ SOXL/PLTR still underperforming?
  □ If SOXL/PLTR bad → remove from watchlist
```

### Step 2 — Build Block 8 (INTRADAY broker mirroring)
```
Estimated effort: 1-2 hours
What it does: Mirror INTRADAY entries/exits to Moomoo Book Trader
Prerequisites:
  ✅ 50+ intraday paper trades completed
  ✅ Force-flat confirmed working every day at 15:55 ET
  ✅ SWING SIMULATE validated
```

### Step 3 — Build Block 9 (Enhanced ORB strategy)
```
Estimated effort: 1-2 weeks including backtest

Enhancements planned:
  1. Historical volume baseline
     Current: compare bar vs today's session average
     Better:  compare bar vs same-time historical 5m average
     Why:     midday bars naturally quiet → unfairly penalised now

  2. QQQ/NQ confirmation filter
     Current: no market-wide check
     Better:  only enter if QQQ trending same direction
     Why:     reduces false breakouts on weak market days

  3. Tighter stop placement
     Current: always stop at OR_low (sometimes too wide)
     Better:  stop below breakout bar low, VWAP, or OR midpoint
     Why:     better R:R, more capital efficient

  4. Volume reclaim — second chance entry
     Current: hard 1.2x cutoff, never re-evaluates
     Better:  if price stays above OR_high and volume picks up → enter
     Why:     catches delayed breakouts like today's TQQQ/SOXL

Process for Block 9:
  → Build enhanced version
  → Backtest on 360 days (same dataset)
  → Compare expectancy vs current (+0.090R baseline)
  → Only deploy if improvement is meaningful
```

---

## FULL PROJECT STATUS

### Code — main branch on GitHub
```
Tests:    615 passed, 0 failed (after pushing scheduler fix)
Blocks:   1-7 complete
Hotfixes: 3 applied (INTRADAY mode switch, broker cache reset, lot-size)
```

### Architecture summary
```
4 separate brains, 4 separate databases:
  bursa_agent_MY_SWING.db     RM 20,000  — Streamlit Cloud
  bursa_agent_US_SWING.db     $ 5,000    — Local PC + SIMULATE
  bursa_agent_US_INTRADAY.db  $ 5,000    — Local PC + paper
  bursa_agent_MY_INTRADAY.db  RM 20,000  — empty (MY not supported)

Brain capital ≠ Moomoo Book Trader capital
  Agent: $5,000 (1% risk = $50/trade)
  Book Trader: $999,999 (receives same share quantities, different balance)
  This is intentional — do NOT sync them
```

### Strategy parameters (LOCKED — do not change without re-backtesting)
```
Universe:   TNA, GOOGL, TQQQ, MSTR, SOXL, PLTR
OR window:  15 min
Target:     2.0R
Rel-vol:    1.2× session average (known limitation — see Block 9)
VWAP:       Required (close > VWAP)
EMA filter: Daily EMA-200 (longs only when above)
Force-flat: 15:55 ET every day
Cycle:      5 minutes
Explorer:   50 trades (just lowered from 100)
```

---

## IMPORTANT CONTEXT FOR NEW AI

### What this project is
Autonomous paper-trading agent for Bursa Malaysia (MY) and US markets.
Two trading modes: SWING (hourly, both markets) and INTRADAY (5-min ORB, US only).
Self-learns via Bayesian Beta(α,β) posteriors. Separate brain per (market, mode).

### What it is NOT
- Not a signal service (acts autonomously)
- Not a get-rich-quick system (edge is real but small: ~+0.07R after slippage)
- Not fully validated yet (Day 1 of live intraday testing)

### Honest edge assessment
```
US INTRADAY ORB:
  Backtest: +0.090R expectancy, 83% monthly hit rate (360-day OpenD data)
  Realistic live: +0.060R to +0.080R after slippage
  Probability edge holds in live: ~35-40%
  This is why we paper trade first

US SWING:
  Strategy: EMA + RSI + volume breakout/pullback
  Running on yfinance (daily bars)
  Just fixed lot-size bug — first live entries expected soon

MY SWING:
  Same strategy as US SWING
  yfinance only (Moomoo has no Bursa coverage)
  Notify-only (no broker execution)
```

### Key files to read for full context
```
HandBook/PROJECT_HANDBOOK.md   — full architecture, design decisions
HandBook/AI_CHAT_HANDOFF.md    — technical handoff with module map
HandBook/USER_GUIDE.md         — user-facing guide with INTRADAY section
HandBook/orb_backtest_results.md — full 4-round backtest write-up
```

### Local PC requirements (must be running for US trading)
```
1. Moomoo Desktop — open and logged in
2. OpenD — running (separate tray app, port 11111)
3. streamlit run app.py → http://localhost:8501
4. Sidebar: 🇺🇸 US → SWING/INTRADAY → SIMULATE/paper
```

### Secrets location (local)
```
.streamlit/secrets.toml  (NOT in GitHub — in .gitignore)
Contains: GITHUB_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GIST_ID
```

---

## WHAT NEW AI SHOULD DO FIRST

When user returns with this file:

1. **Ask:** "What data do you have from the last X weeks?"
2. **Ask:** "Did US SWING start entering trades after the lot-size fix?"
3. **Ask:** "How many INTRADAY trades fired?"
4. **Ask:** "Does Moomoo Book Trader match the Portfolio tab?"
5. **Then:** Review data → decide on Block 8 → decide on Block 9

Do NOT start building Block 8 or 9 without reviewing live data first.
Do NOT change strategy parameters without a backtest.
Do NOT deploy to REAL mode until SIMULATE is validated.

---

## REPO LOCATION

```
GitHub: https://github.com/fongway94/autonomous_bursa_agentV3.3
Branch: main (feat/intraday was merged via PR #2)
Tests:  pytest tests/ -q  →  615 passed, 0 failed
```

---
*Last updated: 2026-06-02 — Day 1 of live intraday paper trading*
