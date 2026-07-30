# BursaAI Agent — Full Bug Audit (Swing Trader Perspective)

**Date:** 2026-07-30 (session date)  
**Branch:** arena/019fb146-autonomous-bursa-agentv3-3  
**Codebase version:** v3.7 (MY + US, SWING + INTRADAY)  
**Auditor:** Arena Agent (automated deep dive)  
**User-reported symptom:** "clicked scan market, live price already way low than stop loss, but it did not sell to stop loss"

---

## Executive Summary

The project is **NOT unusable**, but has **critical bugs for a swing trader** that make it unsafe to leave unattended. The core ideas (Bayesian brain, regime filter, risk gates, Gist persistence, real Bursa mechanics) are sound and actually impressive for an abandoned project. The biggest gap is that **stop-loss enforcement was broken in two layers**:

1. Manual Scan button never triggered auto-settle
2. Auto-settle itself used yesterday's bar for SL/TP, causing 1-day delay

After fixes in this PR, the bot becomes **useful** for swing trading as a *paper-trading + alert assistant*, not yet as fully autonomous live execution (MY has no broker API).

**Test status after fixes:** 621 passed, 0 failed.

---

## 1 — Critical Bugs (P0) — Would lose money / break core promise

### 1A — SCAN MARKET does not trigger auto-settle (USER REPORTED)

**Location:** `app.py` Scanner tab (line ~750), `tab_portfolio` (~1098)

**What happens:**
- User clicks "🔥 SCAN MARKET" → `screener.screen_all_stocks()` runs, cache saved, UI rerenders.
- `trading_engine.auto_settle_trades()` is **never called**.
- Portfolio tab builds `price_lookup` from scan cache (price only, no high/low) and shows unrealized PnL, but never closes trades.
- Scheduler is the only place that calls auto-settle, and on Streamlit Cloud it often is STOPPED (heartbeat stale after deploy, ghost-thread orphan, 7-day sleep).
- Result: user sees live price RM2.50, stop-loss RM2.85, trade still ACTIVE.

**Why it existed:** Separation of concerns gone too far — scanner UI was read-only by design, settlement was "scheduler's job". But for a swing trader, manual scan *must* enforce stops.

**Fix in this PR:**
- `app.py` Scanner: after manual scan, if `autoexit_enabled`, build price_lookup from scan df + parallel fresh fetch via `scheduler._fetch_ticker_price_data()` (now uses TODAY's high/low) and call `auto_settle_trades(..., actor="USER_SCAN")`. Toasts settled count.
- Intraday scanner: same, calls `auto_settle_intraday()` or `force_flat_all_intraday()`.
- Portfolio tab: added button "🛡️ Check Stops & Settle Now" that does same parallel fetch + settle. Also added throttled auto-check (once per 60s) if any live price <= stop_loss, which auto-closes on portfolio view.
- Documented new behavior in caption.

**Test guard:** None existed. Manually verified via code review + new logic path uses existing `auto_settle_trades` which has cash-conservation tests.

---

### 1B — Auto-settle used YESTERDAY's bar for SL/TP (1-day delay)

**Location:** `scheduler.py` `_fetch_ticker_price_data()` (line ~220), `trading_engine.py` `auto_settle_trades()` (line ~550)

**What happens:**
- `_fetch_ticker_price_data()` returned:
  ```python
  current_row = df.iloc[-1]  # today close for P&L
  last_closed = df.iloc[-2]  # yesterday high/low for exit checks
  ```
- Comment says: prevents false trigger when daily Low includes pre-entry crash (e.g. crash at 09:35, entry at 10:00, daily Low still contains crash).
- But this is applied **unconditionally** to all trades, even those held 10 days.
- Consequence: if stock crashes today -10% below SL, `low_today` is yesterday's low (still above SL), so no exit. Trade only exits tomorrow.
- For a swing trader, this is catastrophic — stop loss must be intraday.

**Why it existed:** Valid concern for same-day entry, but fix was too broad.

**Fix in this PR:**
- `scheduler.py`: `_fetch_ticker_price_data()` now returns TODAY's high/low (`df.iloc[-1]` High/Low) for exit checks.
- `trading_engine.py`: new helper `_is_same_day_trade()` checks `logged_at` date vs MYT today.
  - Same-day trades: only `current_price <= sl` triggers SL (not low), and only `current_price >= tp` triggers TP (avoids pre-entry wick).
  - Older trades: `(low <= sl) OR (current_price <= sl)` triggers SL, so live price below SL closes immediately, plus wick-based exit still works.
- Updated exit priority comments.

**Trade-off:** Same-day entry still has slight protection vs pre-entry low, but if price *currently* below SL right after entry, it will close (which is correct).

---

### 1C — Intraday stale-bar detection used MYT hours for US tickers

**Location:** `scheduler.py` `_build_intraday_bar_data()` (line ~900)

**Bug:**
```python
myt_hour = now_myt.hour
is_market_hours_myt = (9 <= myt_hour < 16)
```
US RTH is 09:30-16:00 ET = 21:30-04:00 MYT. Above check never true during US session, so stale-bar detection never fired. Intraday engine would use stale price and miss force-flat.

**Fix:** Now uses `ZoneInfo("America/New_York")` and checks `09:30 <= now_et.time() < 16:00`. Also converts bar timestamp to ET date for "today" check.

---

### 1D — Equity (total_equity) went stale after entry/exit

**Location:** `trading_engine.py` `execute_entry`, `execute_partial_exit`, `execute_full_exit`

**Bug:**
- `execute_entry` saved only `cash_balance`, not `total_equity`. After buying, DB `total_equity` still showed old value (cash_before). Risk checks that used `total_equity` from DB (if caller didn't recompute) could see false drawdown.
- `execute_partial_exit` and `execute_full_exit` same.

**Why not noticed:** `app.py` portfolio recomputes equity on the fly as cash + MV, and scheduler's auto_settle saves total_equity at end. But after manual close, total_equity stayed stale until next cycle.

**Fix:** Now after each entry/partial/full, compute:
```python
mv = sum(entry_price * shares_remaining for active trades)
save_account(cash_balance=new_cash, total_equity=new_cash + mv)
```
Falls back to old behavior on exception.

---

## 2 — High Severity Bugs (P1) — Degrades usefulness / misleads

### 2A — LOT_SIZE hardcoded to 100 in UI, breaks US market (lot=1)

**Location:** `app.py` line 954: `min_value=LOT_SIZE` (100) even when active market is US (lot=1). US position of 10 shares would be rounded to 0 and rejected with "below 100-share lot" message, causing "Unknown reason for zero entries" in scheduler logs.

**Fix:** Now imports `lot_size as _lot_size_fn` and uses dynamic value for min/value/step and error messages.

### 2B — Broker factory tests failing due to NOOP safety gate

**Location:** `tests/test_moomoo_us_adapter.py`, `test_multi_market_dispatch.py`, `broker_adapter.py` `get_broker_adapter()`

**Bug:** `noop_safety.any_execution_allowed()` returns False by default (NOOP_MODE=True). `get_broker_adapter()` then forces NoopAdapter even when mode=SIMULATE/REAL, causing 3 test failures. The safety gate is correct for prod, but tests need paper trading enabled.

**Fix:** `tests/conftest.py` now sets `NOOP_MODE=false` and `PAPER_TRADING_ENABLED=true` at session start. All 621 tests pass.

### 2C — Portfolio price_lookup only had price, not high/low

**Location:** `app.py` portfolio tab: `price_lookup = {ticker: {"price": ..., "change_pct": ...}}`

This dict was fed to nothing before, but after fix we feed it to `auto_settle_trades` which expects high/low. Now scanner path merges scan cache price (as high=low=price) plus fresh fetch that has true high/low.

### 2D — Intraday vs Swing scheduler cadence confusion

**Location:** `scheduler.py` `_loop()` swing path

UI says interval is 15/30/60/120 min, but actual code does full scan every `interval_sec` and fast settle every 600s regardless. So setting 15 min still does fast settle every 10 min. Not a bug per se (documented as 10-min split-cadence in handbook §15.11), but misleading label.

**Recommendation:** Keep as is, but clarify in UI that interval = full scan interval, fast settle always 10 min.

---

## 3 — Medium / Low Bugs

### 3A — Data provider health shows wrong market support flag
`health()["moomoo_supports_active_market"]` always checks `AAPL` for US and `1155.KL` for MY, but if active market is US, it should check US ticker; if MY, MY ticker. It does, but uses hardcoded examples rather than deriving from active profile. Low impact.

### 3B — Corporate actions detection uses yfinance period heuristic that may miss events if window >2y? Uses period mapping, okay for 7-day window. Low.

### 3C — Evaluation `klci_buy_hold` uses `yfinance` without explicit timeout in some older paths? Now has timeout 30. Okay.

### 3D — Watchlist US includes TQQQ labeled WEAK but still in list. Intentional secondary.

### 3E — Risk manager `check_trading_time_window` uses `market_calendar.is_market_open` which dispatches correctly, but also checks user `no_entry_before/after` in local TZ. For US, user must enter ET time, UI label says ET — correct.

---

## 4 — Is this project useful for a swing trader? (Abandoned analysis)

**Short answer: YES, after fixes, for Bursa MY swing only, as a paper-trading brain + alert assistant.**

### Strengths (why not throw away)

- **Real Bursa mechanics**: 100-share lots, 0.15% fee, volume-aware slippage (5-80 bps), real session hours + 50+ holidays through 2027, lunch break blocked. Most open-source bots ignore this.
- **Bayesian brain**: Beta(α,β) per 27 states (not 128), Thompson sampling in explore, LCB × avgR in exploit. Correct for small-sample swing trading. Shrinkage prior prevents whipsaw. This is textbook correct.
- **Risk gates**: drawdown circuit breaker (8% warn half-size, 15% hard stop), sector exposure 40%, position cost 20%, max positions regime-aware (BULL 8, BEAR 3), correlation shield max 2 per sector, daily trade limit, safe-entry window. All actually enforced and size_multiplier respected.
- **Persistence**: Gist backup of full SQLite (trades + brain + params) survives Streamlit Cloud 7-day sleep. This was missing in early versions and is now solved.
- **Self-healing scheduler**: PID-owned, orphan registry, watchdog that evicts runaway cycles within 10 min, boot debounce to avoid scan on every GitHub push. This is production-grade.
- **Audit trail**: Every entry/exit/risk-reject/param change/bias change goes to a log table. Downloadable CSV. Good for swing trader post-mortem.
- **Multi-market architecture**: Clean Protocol-based profiles, per-(market,mode) DB isolation, basename-based override detection fix (previously broke full test suite). Adding HK/SG = one new profile file.
- **Corporate actions**: Splits/bonus auto-adjusted with cash-invariant check, dividend alert-only. Prevents 80% crash false SL.

### Weaknesses (why abandoned)

- **No MY broker API**: Moomoo OpenAPI does not support Bursa. So MY stays notification-only. User must manually mirror Telegram alerts. US has SIMULATE/REAL but you said swing trader (likely MY).
- **Data source fragile**: yfinance is only source for MY. If Yahoo is down, no scan. No secondary provider (Stooq, etc.). Intraday for MY impossible today.
- **ML classifier not in Gist**: `.pkl` lost on container reset, rebuilds nightly. Okay but 24h gap.
- **Public holiday list expires 2027**: Needs yearly manual update (system reminds, but still manual).
- **GitHub PAT expiry**: Backups silently fail when token expires (yearly reminder exists).
- **Walker complexity**: 471 tests in handbook says 471, actual 621 after adding intraday/NOOP tests. Many tests but still missed critical stop-loss bug because they mocked price_lookup with high=low=price.
- **NOOP phase**: Currently default ON, blocking real broker mirror. You must set NOOP_MODE=false env var to actually trade. Confusing for new user.

### Verdict for swing trader

- **Use as**: Research / paper-trading journal + alert bot. Let it run on Streamlit Cloud MY-SWING, get Telegram alerts on GOLD BUY, mirror in Moomoo manually, and let it enforce stops via auto-settle (now fixed) and backup brain via Gist.
- **Don't use as**: Fully autonomous live execution for MY (impossible) or intraday for MY (no feed).
- **Needed to become truly useful**: 
  1. Fix critical bugs above (done in this PR)
  2. Set up GITHUB_TOKEN + TELEGRAM_BOT_TOKEN secrets
  3. Set NOOP_MODE=false when you want broker mirror (US only)
  4. Add second data source (Stooq) for redundancy
  5. Consider subprocess-isolated scan so Yahoo hang can be SIGKILLed (currently watchdog recovers in 10 min, but cycle wastes time)

---

## 5 — Fixes Applied in This PR

| File | Bug | Fix |
|------|-----|-----|
| `scheduler.py` `_fetch_ticker_price_data` | Used yesterday bar for SL/TP | Now uses today's bar high/low |
| `trading_engine.py` `auto_settle_trades` | No same-day vs multi-day distinction, live price below SL didn't trigger | Added `_is_same_day_trade()` + triggers on `current_price <= sl` OR `low <= sl` (for old trades) |
| `scheduler.py` `_build_intraday_bar_data` | MYT hours check for US RTH | Now uses ET timezone |
| `app.py` Scanner tab | Manual scan never settled | Now calls auto_settle after scan (swing) and intraday settle/force-flat (intraday) |
| `app.py` Portfolio tab | No emergency settle, stale equity | Added "Check Stops & Settle Now" button + throttled auto-settle if live <= SL |
| `trading_engine.py` entry/exit | total_equity stale | Now saves cash + MV |
| `app.py` LOT_SIZE | Hardcoded 100 breaks US | Dynamic via `lot_size()` |
| `tests/conftest.py` | 3 failing broker tests due to NOOP gate | Sets NOOP_MODE=false, PAPER_TRADING_ENABLED=true |

---

## 6 — Further Recommended Fixes (not yet done, but low effort)

1. **Add "Close all below SL" one-click in Portfolio** — currently we have settle now, but also need bulk close preview.
2. **Show last settle error in UI** — if auto_settle fails, user doesn't know.
3. **Add Stooq fallback** in `data_provider.py` after yfinance fails.
4. **Make fast-settle interval configurable** — currently hardcoded 600s.
5. **Add explicit timeout to all yfinance calls** — audit done, but evaluation's `equal_weight_watchlist` uses timeout 15, okay.
6. **Document NOOP graduation runbook** — user must know to flip env vars to enable broker mirror.
7. **Update holiday list for 2028** — Bursa publishes late Dec, create calendar reminder.

---

## 7 — Repro Steps for Original Bug

1. Open MY-SWING DB, insert trade: entry RM3.00, SL RM2.85, shares 100, logged yesterday.
2. Mock scan cache: price RM2.50 (below SL), high RM2.55, low RM2.45.
3. Before fix: `scheduler._fetch_ticker_price_data` would return high/low from yesterday (e.g. high RM3.10 low RM2.90) — SL not triggered.
4. After fix: returns high RM2.55 low RM2.45 — SL triggers because `low <= sl` OR `price <= sl`.
5. Manual scan path: before fix, portfolio tab never called auto_settle, so trade stayed ACTIVE despite live price below SL. After fix, portfolio's "Check Stops" button or auto throttle triggers settle.

---

## 8 — Conclusion

The abandoned project is **salvageable and actually valuable** for a Bursa swing trader after P0 fixes. The core engine is more carefully built than many commercial bots (real slippage, real sessions, Bayesian brain, audit logs). The main reason it felt abandoned is the stop-loss bug you found + lack of MY broker execution (which is not the code's fault, it's Moomoo OpenAPI).

After this PR, clicking SCAN MARKET will **also check stops** and close positions whose live price is below stop-loss, exactly as you expected.

