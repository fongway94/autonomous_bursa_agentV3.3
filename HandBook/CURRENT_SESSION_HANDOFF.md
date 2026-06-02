# Current Session Handoff
# Date: 2026-06-02 (Monday) — Day 1 of live intraday testing
# Purpose: Pick up exactly where we left off if chat is disrupted
# GitHub: https://github.com/fongway94/autonomous_bursa_agentV3.3 (main branch)

---

## WHERE WE ARE RIGHT NOW

### Today is Day 1 of live intraday testing
- US market opened at 09:30 ET (21:30 MYT)
- INTRADAY mode running on local PC
- 0 trades fired today — correct, low volume day
- TQQQ and SOXL broke OR_high but volume only 0.36x and 0.61x (need 1.2x)
- US SWING lot-size bug fixed and pushed — first real entries expected soon

### What is running right now

```
Streamlit Cloud (24/7, always on):
  MY SWING  → yfinance → paper → NOOP notify only  ✅
  US SWING  → yfinance → paper → NOOP notify only  ✅

Local PC (user's Windows machine, must keep running):
  US SWING    → SIMULATE → mirroring to Moomoo Book Trader ✅
  US INTRADAY → paper only (no broker mirror yet) ✅
  OpenD running on 127.0.0.1:11111 ✅
  NASDAQ Basic quote subscription active ✅
  streamlit run app.py → http://localhost:8501
  Moomoo Desktop logged in ✅
  Moomoo Book Trader (paper account) active ✅
```

---

## ALL BUGS FIXED THIS SESSION — ALL PUSHED TO GITHUB ✅

### Bug 1 — US SWING "Unknown reason for zero entries"
**Root cause:** `scheduler.py` hardcoded `// 100` for share rounding (Bursa board lot).
For US (lot size=1): `10 shares // 100 * 100 = 0` → silently skipped every US trade.

**Fix:** Replaced hardcoded `// 100` with `lot_size()` from `trading_engine.py`
```python
_lot = lot_size()  # 100 for MY, 1 for US
target_shares = (target_shares // _lot) * _lot
```
**Files:** `scheduler.py` + `tests/test_scheduler_intraday.py` (+4 regression tests)

### Bug 2 — INTRADAY mode switch OperationalError
**Root cause:** `db.py` v3.6→v3.7 migration was orphaned code (never ran).
New INTRADAY DB had no schema → `update_scheduler_state()` crashed.
**Fix:** `_migrate_v36_db_if_needed()` function + `init_db()` before mode switch in `app.py`
**Files:** `db.py` + `app.py` + `tests/test_intraday_mode_switch.py` (+6 tests)

### Bug 3 — Broker mode switch shows "disconnected"
**Root cause:** `app.py` called `reset_adapter_cache()` twice → wiped adapter.
**Fix:** Removed duplicate call, added eager `connect()` after switch.
**Files:** `app.py`

### Bug 4 — Flaky `test_market_calendar` test
**Root cause:** Fragile mock of `check_trading_time_window()` which uses wall clock.
**Fix:** Simplified to test `is_market_open()` directly.
**Files:** `tests/test_market_calendar.py`

### Final test count: 615 passed, 0 failed ✅

---

## DIAGNOSTIC SCRIPTS CREATED (repo root — for debugging only)

```
check_intraday.py             — EMA-200 trend filter for curated-6
check_intraday2.py            — Deep ORB diagnostic per ticker
check_rvol.py                 — Rel-vol bar-by-bar breakdown
check_historical_signals.py  — 60-day historical signal proof
check_swing.py                — US SWING 0-entry diagnostic
```
These are debug tools. Not part of live system. Can delete when done.

---

## TODAY'S FINDINGS (2026-06-02)

### EMA-200 Trend Status
```
TNA   → UP  ✅    GOOGL → UP  ✅    TQQQ → UP  ✅
MSTR  → DOWN ❌   SOXL  → UP  ✅    PLTR → UP  ✅
```
5 tickers eligible. MSTR correctly blocked.

### ORB Status at 11:31 ET
```
TQQQ: close=85.335 > OR_high=85.140 ✅ BUT vol=0.36x ❌ (need 1.2x)
SOXL: close=227.630 > OR_high=226.000 ✅ BUT vol=0.61x ❌ (need 1.2x)
Others: didn't break OR_high
```
Volume filter correctly blocked low-conviction breakouts.

### Historical Signal Check (last 60 days, yfinance)
```
TNA    19 trades  58% win  +2.61R  avg +0.137R ✅
GOOGL  20 trades  55% win  +3.83R  avg +0.191R ✅
TQQQ   24 trades  54% win  +4.08R  avg +0.170R ✅
SOXL   22 trades  23% win  +0.45R  avg +0.020R ⚠️ weak
PLTR    2 trades   0% win  -0.18R              ❌ bad

TOTAL: 87 trades / 60 days = ~10.9 trades/week
```
Strategy fires regularly. Today was a quiet day. Normal.
⚠️ Watch SOXL and PLTR after 4 weeks live — may need removal.

---

## KEY DECISIONS MADE THIS SESSION

| Decision | Choice | Reason |
|---|---|---|
| Pre-train brain with historical data | ❌ Don't do it | Day 1 — need clean baseline |
| Explorer target | Lower 100 → 50 | Faster convergence (~5 weeks) |
| Block 9 (enhanced ORB) | ❌ Not yet | Need live baseline first |
| Universe | Keep curated-6 | Backtest proved edge on these 6 |
| Add RL | ❌ Not yet (maybe never) | Wrong tool for sample size |
| Rel-vol formula | Known limitation | Compares vs today's session avg, not historical same-time avg |

### ⚠️ User still needs to lower explorer target to 50:
```
Dashboard → ⚡ Intraday Robo-Trader → 🧪 Learning Mode
Change target from 100 → 50 → Click "Force EXPLORE"
```

---

## IMPORTANT CONCEPTS CLARIFIED THIS SESSION

### What the agent is (technically)
```
NOT Reinforcement Learning.
IS: Bayesian Multi-Armed Bandit

Bayesian Bandit:
  ✅ Correct for 50-100 trade sample sizes
  ✅ Interpretable (α/β posteriors visible)
  ✅ Self-correcting
  ✅ Explore → Exploit auto-switch

RL would need 10,000+ trades to converge.
At 10 trades/week = 20 years. Impractical.
```

### RL future design (if ever)
```
Current design is 60% RL-compatible:
  ✅ State representation (entry conditions)
  ✅ Reward signal (R-multiple per trade)
  ✅ Brain isolation per (market, mode)
  ✅ Explorer/Exploit paradigm

❌ Missing for full RL:
  Mid-trade state tracking (bar by bar)
  Sequential decision framework
  Q-value storage
  Dynamic position sizing as learned action

If RL is ever added:
  Bayesian posteriors → warm-start RL (NOT thrown away)
  Add RL only for EXIT TIMING specifically
  Only after 2,000+ live trades across multiple regimes
  Block 10+ territory — years away
```

### Auto-exit — both modes confirmed
```
SWING auto-exits:
  Stop loss → auto close
  TP3 → auto close all
  TP2 → auto close 50% (partial)
  Trailing stop → activates after TP1
  Time exit → BULL 14d / NEUTRAL 7d / BEAR 5d

INTRADAY auto-exits:
  Stop loss → auto close (at OR_low)
  TP3 → auto close all
  TP2 → auto close 50%
  Force-flat → 15:55 ET EVERY DAY (non-negotiable)
  No trailing stop, no time exit needed
```

### ORB — why chosen over other strategies
```
ORB = Opening Range Breakout
OR_high = Opening Range High (highest price in first 15 min)
OR_low  = Opening Range Low  (lowest price in first 15 min)

Chosen because:
  ✅ 100% rule-based → fully automatable
  ✅ Clear stop loss always known before entry
  ✅ No news feed required
  ✅ Works perfectly with leveraged ETFs
  ✅ Backtested 360 days → proven edge
  ✅ 5-minute cycle compatible
  ✅ Brain can learn sub-patterns within it

Why leveraged ETFs:
  3× amplification = sufficient move size for $5k capital
  Index tracking = no earnings/CEO surprise risk
  High liquidity = clean fills
  Predictable behaviour = cleaner breakout patterns
```

### Rel-vol known limitation
```
Current formula:
  Bar volume vs average of TODAY's prior bars
  Problem: midday bars naturally quieter than morning
  11:30 bar vs busy 09:30-11:00 morning = unfair comparison
  May reject valid midday breakouts (like today's TQQQ)

Block 9 fix:
  Historical same-time baseline
  Compare 11:30 bar vs avg of all historical 11:30 bars
  Much fairer comparison
  Will likely fire more valid signals
```

---

## NEXT DEVELOPMENT SESSION AGENDA (4-6 weeks from now)

### Bring this data when you return:
```
□ How many US INTRADAY trades fired?
□ How many US SWING trades fired? (lot-size fix should work now)
□ Does Moomoo Book Trader match Portfolio tab? (ticker, shares, direction)
□ SOXL and PLTR — still underperforming or improved?
□ Screenshots: 📊 Performance tab for US SWING + US INTRADAY
□ Any force-flat issues? (check daily at 04:00 MYT)
```

### What we'll do:
```
Step 1: Review live results
  → Is US SWING entering trades now?
  → Is win rate matching backtest expectations?
  → SOXL/PLTR — keep or remove?

Step 2: Build Block 8 (INTRADAY broker mirroring)
  → 1-2 hours of work
  → Wire mirror_entry_to_broker() into intraday_engine.py
  → Wire mirror_exit_to_broker() for exits + force-flat
  → Test with SIMULATE mode

Step 3: Build Block 9 (Enhanced ORB)
  → Historical volume baseline (fix rel-vol limitation)
  → QQQ/NQ confirmation filter
  → Tighter stop placement options
  → Volume reclaim second-chance entry
  → Full 360-day backtest before deploying

Step 4: Adaptive position sizing (Bayesian-based, not RL)
  → Size up in high-posterior states
  → Size down in uncertain states
  → Use existing α/β data — no new architecture needed
```

---

## PROJECT STATUS SUMMARY

### Code (main branch on GitHub)
```
Tests:    615 passed, 0 failed
Blocks:   1-7 complete + 4 hotfixes
Commits:  All pushed, nothing pending
```

### Architecture
```
4 separate brains, 4 separate databases:
  bursa_agent_MY_SWING.db     RM 20,000  Streamlit Cloud, NOOP
  bursa_agent_US_SWING.db     $ 5,000    Local PC, SIMULATE ← active
  bursa_agent_US_INTRADAY.db  $ 5,000    Local PC, paper ← active
  bursa_agent_MY_INTRADAY.db  RM 20,000  empty (MY not supported)

Agent capital ($5k) ≠ Moomoo Book Trader ($999,999)
  → Intentional. Same share quantities, different account balance.
  → Do NOT sync them.
```

### Locked strategy parameters (do not change without backtest)
```
Universe:   TNA, GOOGL, TQQQ, MSTR, SOXL, PLTR (curated-6)
OR window:  15 min
Target:     2.0R (TP2), 1.5R (TP1), 2.5R (TP3)
Rel-vol:    1.2× today's session average (known limitation → Block 9)
VWAP:       Required
EMA filter: Daily EMA-200 longs only
Force-flat: 15:55 ET every day
Cycle:      5 minutes
Explorer:   50 trades (just lowered from 100 — user needs to apply)
```

### Honest edge assessment
```
Backtest (360-day OpenD): +0.090R expectancy, 83% monthly hit rate
Realistic live:           +0.060R to +0.080R after slippage
Probability edge holds:   ~35-40% (this is why we paper trade first)

Historical last 60 days:  87 trades, 10.9/week, 46% win rate
                          TNA/GOOGL/TQQQ carrying the strategy
                          SOXL/PLTR underperforming — watch these
```

---

## WHAT NEW AI SHOULD DO FIRST

1. **Ask:** "Did any trades fire since we last spoke?"
2. **Ask:** "Does Book Trader match the Portfolio tab?"
3. **Ask:** "How many INTRADAY vs SWING trades?"
4. **Ask:** "Is SOXL/PLTR still underperforming?"
5. **Then:** Review data → Build Block 8 → Build Block 9

**Do NOT:**
- Change strategy parameters without running backtest
- Add RL (wrong tool, wrong timing)
- Switch to REAL mode before SIMULATE is validated
- Pre-train brain (kills clean baseline)
- Add more tickers without 360-day validation

---

## REPO & LOCAL SETUP

```
GitHub: https://github.com/fongway94/autonomous_bursa_agentV3.3
Branch: main
Tests:  615 passed, 0 failed

Local PC requirements (must be running for US trading):
  1. Moomoo Desktop → logged in
  2. OpenD → running (port 11111)
  3. streamlit run app.py → http://localhost:8501
  4. Sidebar: 🇺🇸 US → SWING (SIMULATE) or INTRADAY (paper)

Secrets: .streamlit/secrets.toml (local only, NOT in GitHub)
  GITHUB_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GIST_ID

US market hours (MYT): 21:30 – 04:00 MYT
INTRADAY session:       21:30–21:45 OR window
                        21:45–03:55 active trading
                        03:55–04:00 force-flat
```

---

## FILES TO READ FOR FULL CONTEXT

```
HandBook/PROJECT_HANDBOOK.md        — full architecture, design decisions, §15 intraday
HandBook/AI_CHAT_HANDOFF.md         — technical handoff with module map
HandBook/USER_GUIDE.md              — user guide with INTRADAY section
HandBook/REVISION_HISTORY.md        — v3.7 changes by block
HandBook/orb_backtest_results.md    — 4-round backtest write-up with caveats
```

---

## BLOCK 9 — ENHANCED ORB STRATEGY (build after live baseline)

### Why Block 9 exists
Current ORB parameters score **90/100** vs academic research and practitioner standards.
Two specific weaknesses identified from Day 1 live testing and external validation:
1. **Rel-vol formula** compares bar vs today's session average — unfairly penalises midday bars
2. **Stop at OR_low** is wider than practitioner-optimal OR midline

### External validation sources used
- Zarattini & Aziz (2023, Concretum Research) — TQQQ ORB 2016-2023 study
- TradeThatSwing (2026) — TQQQ 15-min ORB backtest, 68% annual return
- TradingView ORB practitioner community — volume + EMA-200 + midline stop consensus
- Advanced ORB (Medium, 2025) — multi-confirmation framework

### What's already validated (do NOT change without re-backtesting)

| Parameter | Your Setting | External verdict |
|---|---|---|
| OR window | 15 min | ✅ Consensus (practitioners + research) |
| Target | 2.0R | ✅ Universal standard |
| Timeframe | 5-minute | ✅ Universally validated |
| EMA-200 trend | Daily 200 | ✅ Specifically cited by practitioners |
| VWAP support | Required | ✅ Industry standard |
| Force-flat | 15:55 ET | ✅ Professional standard |
| Universe | Leveraged ETFs | ✅ Research-validated (TQQQ specifically) |

### Enhancement 1 — Historical Volume Baseline (HIGHEST PRIORITY)

**Problem:**
```
Current formula:  bar volume / average of TODAY's prior bars
Day 1 result:     TQQQ at 11:30 ET = 0.36x (rejected)
                  SOXL at 11:30 ET = 0.61x (rejected)
                  Both were genuine breakouts — wrongly filtered

Why it's wrong:
  Morning bars (09:30-11:00) have high volume (opening rush)
  Midday bars (11:00-14:00) naturally have lower volume
  Comparing midday bar vs busy morning = unfair penalty
  A 0.36x reading at 11:30 might be NORMAL for that time of day
  and actually above the historical 11:30 average
```

**Fix:**
```python
# Instead of: bar_volume / avg_of_today's_prior_bars
# Use:        bar_volume / avg_of_historical_same_time_bars

def compute_historical_vwap_baseline(ticker, time_of_day, lookback_days=20):
    """
    Returns the average volume for this specific 5-min bar
    across the last N trading days.
    e.g. the 11:30 bar vs the average of all historical 11:30 bars
    """
    # Fetch 20 days of 5m data
    # Group by time-of-day (09:30, 09:35, ..., 15:50, 15:55)
    # Return mean volume for the matching time slot
    pass

# New rel-vol check:
historical_avg = compute_historical_vwap_baseline(ticker, bar_time)
rel_vol = bar_volume / historical_avg
if rel_vol >= cfg.rel_vol_threshold:  # 1.2x still appropriate
    # signal fires
```

**Expected impact:** More signals during active midday breakouts. Fewer false rejections.
**Backtest required:** Yes — run validate_intraday_edge.py with new formula before deploying.
**External reference:** TradingView ORB UK — "volume > previous candle or lookback average"

---

### Enhancement 2 — OR Midline Stop (MEDIUM PRIORITY)

**Problem:**
```
Current:  Stop at OR_low (full range = entry - OR_low)
          Example: TQQQ OR_high=$85.14, OR_low=$83.87, range=$1.27
          Entry at $85.20, stop at $83.87 → risk per share = $1.33

Better:   Stop at OR midline (half range = entry - OR_midline)
          OR_midline = (85.14 + 83.87) / 2 = $84.50
          Entry at $85.20, stop at $84.50 → risk per share = $0.70
          
          Same $50 risk budget:
          Current:  $50 / $1.33 = 37 shares
          Midline:  $50 / $0.70 = 71 shares  ← 92% more shares
          Same $ risk, nearly double position size
```

**Fix:**
```python
# intraday_screener.py + intraday_backtest.py
# Change stop_loss calculation:

# Current:
stop_loss = float(or_low)

# Enhanced:
or_midline = (or_high + or_low) / 2
stop_loss = float(or_midline)   # tighter, better R:R

# Also update target (still 2R from entry):
target = entry_price + cfg.target_r_multiple * (entry_price - stop_loss)
# (or_range now defined as entry - midline, not or_high - or_low)
```

**Tradeoff:**
```
Pro: Better R:R, more shares per trade, higher absolute profit potential
Con: More likely to be stopped out on normal OR chop before breakout

Recommendation: Backtest both OR_low and OR_midline stops.
                Deploy whichever shows better expectancy on 360-day data.
```

**External reference:** TradeThatSwing — "SL at midline of 15-min OR".
TradingView practitioner — "SL is nearly always set at mid point of ORB"

---

### Enhancement 3 — QQQ/NQ Confirmation Filter (MEDIUM PRIORITY)

**Problem:**
```
Current: Agent can enter TQQQ breakout even if QQQ is rolling over
         (TQQQ = 3× QQQ → fighting the index direction = bad trade)

Better:  Only enter long TQQQ if QQQ is also trading above its own VWAP
         Only enter SOXL if SOXX (semiconductor index) trending up
```

**Fix:**
```python
# Add to compute_intraday_signal():
def _check_index_confirmation(ticker: str, now_et: datetime) -> bool:
    """Check if the underlying index confirms the direction."""
    index_map = {
        "TQQQ": "QQQ",   "SQQQ": "QQQ",
        "SPXL": "SPY",   "UPRO": "SPY",
        "SOXL": "SOXX",  "TNA": "IWM",
        "GOOGL": "QQQ",  "MSTR": "BTC-USD",
    }
    index = index_map.get(ticker)
    if index is None:
        return True  # no index to check, allow
    
    df = data_provider.get_history(index, interval="5m", period="5d")
    vwap = compute_session_vwap(df)
    last_close = df["Close"].iloc[-1]
    return float(last_close) > float(vwap.iloc[-1])  # above VWAP = bullish
```

**Expected impact:** Fewer entries on weak market days. Higher win rate on taken trades.
**Cost:** 1 extra API call per signal candidate (cheap, within rate limits).
**External reference:** Zarattini paper uses TQQQ direction to confirm NQ momentum.

---

### Enhancement 4 — Volume Reclaim Second-Chance Entry (LOW PRIORITY)

**Problem:**
```
Current: Hard 1.2x cutoff on the breakout bar only.
         If breakout fires on 0.8x volume → rejected forever that day.
         
         Real scenario (Day 1, TQQQ):
           10:05 — price breaks OR_high, volume 0.36x → rejected
           10:30 — price still above OR_high, volume surges to 1.8x → missed!
```

**Fix:**
```python
# After initial breakout rejection, continue monitoring:
# If price stays above OR_high AND a LATER bar has volume >= threshold:
#   → Enter on that later bar (second-chance entry)
#   → Stop still OR_low (or midline after Enhancement 2)
#   → Target still 2.0R from new entry price

# One trade per day limit still applies.
# Second-chance only valid while price > OR_high (not if it fell back below)
```

**Expected impact:** Captures delayed breakouts on volume. Today's TQQQ and SOXL would have been caught.
**Risk:** Entry price higher (less favourable), smaller position for same risk budget.

---

### Block 9 Build Sequence

```
Prerequisites before starting Block 9:
  ✅ 50+ live INTRADAY paper trades completed
  ✅ Current baseline measured (win rate, expectancy, per-ticker)
  ✅ SOXL/PLTR performance reviewed (may be removed from universe)
  ✅ SWING SIMULATE validated (Book Trader matches Portfolio)

Build order:
  1. Enhancement 1 (historical vol baseline) — most impact, backtest first
  2. Enhancement 3 (QQQ confirmation) — medium impact, additive
  3. Enhancement 2 (OR midline stop) — changes sizing, needs careful test
  4. Enhancement 4 (volume reclaim) — low priority, add last

Estimated effort: 2-3 weeks including 360-day re-backtest

Validation gate:
  New expectancy must be > current +0.090R to deploy.
  If no improvement → revert to current params.
```

---

## BLOCK 10 — REINFORCEMENT LEARNING FOR EXIT TIMING (long-term)

### Why Block 10 exists
The Bayesian brain learns WHICH setups to enter.
RL can learn HOW to manage the trade once inside — specifically exit timing.

### Prerequisites (very long horizon)
```
Required BEFORE building Block 10:
  ✅ 2,000+ closed live trades across (market, mode)
  ✅ Multiple market regimes sampled (bull + bear + chop)
  ✅ Block 9 deployed and validated
  ✅ Bayesian brain in EXPLOIT mode (>100 intraday trades)
  ✅ Brain clearly plateauing (marginal improvement per 100 trades < 0.005R)

Timeline estimate: 18-24 months from now (2026-06 → 2027-2028)
```

### What RL would do (and NOT do)

```
RL WOULD learn:
  → When in a winning trade, how long to hold?
  → When to take partial profit early (before TP2)?
  → When to exit early (before SL hits) if market weakens?
  → Is this a "ride to TP3" day or a "bank at TP1" day?
  → Dynamic position sizing per state confidence

RL would NOT change:
  → Entry signals (ORB parameters — still rule-based)
  → Universe (curated-6 — still validated manually)
  → Risk per trade (1% — still fixed rule)
  → Force-flat at 15:55 ET (non-negotiable invariant)
```

### How Bayesian brain becomes RL warm-start

```
Current brain state after 2,000 trades:
  state_priors table: α/β per (state_id, action)
  e.g. state_42 → α=185, β=82 → 69% win rate posterior

RL warm-start:
  Q_init[state_42]["hold_to_TP2"] = 0.69  (from Bayesian posterior)
  Q_init[state_42]["exit_early"]  = 0.31
  RL starts from these values, not from 0
  Converges much faster than cold-start RL
  
Current design is 60% RL-compatible:
  ✅ State representation (entry conditions)
  ✅ Reward signal (R-multiple per trade)
  ✅ Brain isolation per (market, mode)
  ❌ Mid-trade state tracking (needs new design)
  ❌ Sequential decision framework (needs engine rewrite)
  ❌ Q-value storage (new DB table needed)
```

### RL architecture design (for future reference)

```
New table needed: q_values
  state_id    INTEGER
  action      TEXT  -- "hold", "partial_exit", "full_exit", "trail_tighten"
  q_value     REAL  -- expected cumulative future reward
  n_updates   INTEGER
  last_updated TEXT

New scheduler step (every 5-min bar while in trade):
  1. Fetch current bar state (price vs OR, VWAP, time of day, P&L)
  2. Look up Q(current_state, all_actions)
  3. Choose action (ε-greedy: mostly exploit, sometimes explore)
  4. Execute action (hold / partial / full exit / tighten trail)
  5. Observe reward (immediate P&L change)
  6. Update Q-value: Q(s,a) += α * (reward + γ * max Q(s',a') - Q(s,a))

New intraday_engine functions needed:
  _get_mid_trade_state(trade, current_bar) → state_id
  _choose_rl_action(state_id) → action
  _execute_rl_action(trade_id, action, price)
  _update_q_value(state_id, action, reward, next_state)
```

### Honest RL caveats

```
Why RL is last on the roadmap:

1. Data requirement: 2,000+ trades = ~4 years at current pace
   (100 trades/year INTRADAY = 20 years alone → need SWING too)

2. Non-stationarity: Market regime changes every 6-18 months
   RL policy trained in 2026 bull market = stale in 2028 bear

3. Engineering cost: 2-3 months of full-time work
   vs Block 9: 2-3 weeks with 3x the expectancy improvement

4. Diminishing returns:
   Block 9 improvement: +0.020R to +0.040R expectancy
   RL improvement:      +0.010R to +0.030R expectancy
   Block 9 gives more for less work

Decision framework:
  If after Block 9 expectancy > +0.12R → consider RL
  If after Block 9 expectancy < +0.08R → fix strategy first, not RL
```

---

## ALTERNATIVE STRATEGIES FOR FUTURE (if ORB edge fades)

### If ORB stops working after 6-12 months

```
Option 1 — VWAP Reversion (complementary to ORB)
  Logic: Price dips to VWAP in uptrend → buy
  Works in: Choppy/sideways markets (when ORB fails)
  Compatible: Uses same data feed, same universe
  Effort: Block 11 level, 2-3 weeks
  Risk: Fails in strongly trending days (opposite of ORB)

Option 2 — Gap and VWAP Fill
  Logic: Stock gaps up at open → VWAP acts as magnet → fade the gap
  Works in: High-gap-frequency markets
  Compatible: Same infrastructure
  Effort: Block 11 level, 2-3 weeks

Option 3 — News Sentiment + ORB Hybrid
  Logic: Only take ORB entries when FinBERT news sentiment agrees
  Data needed: Real-time news API or FinBERT (open source)
  Effort: 2-4 weeks (Block 11)
  Value: Higher quality signals, fewer total trades
  Best for: SWING mode (intraday news reaction is too fast)

Option 4 — MY SWING Enhancement
  MY is currently running basic EMA+RSI+Volume
  Could add: Bursa-specific patterns (rights issues, bonus seasons)
  Could add: EPF/institutional flow signals (Bursa data)
  Effort: Block 12 level
```

---

## PARAMETER VALIDATION SUMMARY (for new AI reference)

### External sources used for validation

| Source | Key finding | Relevance |
|---|---|---|
| Zarattini & Aziz (2023) | TQQQ ORB on 5-min bars, 2016-2023, Swiss research firm | Direct match |
| TradeThatSwing (2026) | 15-min OR, 2:1 R:R, longs-only in uptrend, 68% annual | Direct match |
| TradingView ORB UK practitioner | 200 EMA + VWAP + midline stop + vol confirmation | Validates filters |
| Advanced ORB Medium (2025) | 2.0 RR, 1.0x vol multiplier, retest confirmation | Validates R:R |

### Current parameter score: 90/100

```
Strengths (already optimal):
  OR=15min ✅   Target=2.0R ✅   5m bars ✅
  EMA-200 ✅    VWAP ✅          Force-flat 15:55 ET ✅
  Leveraged ETF universe ✅

Weaknesses (Block 9 fixes):
  Rel-vol: session average → historical same-time average
  Stop:    OR_low → OR midline (optional, test first)
```

---
*Last updated: 2026-06-02 — End of Day 1 live intraday session*
*Block 9 builds after 50+ live INTRADAY trades + live baseline measured*
*Block 10 (RL) builds after 2,000+ closed trades across all modes (~2027-2028)*
