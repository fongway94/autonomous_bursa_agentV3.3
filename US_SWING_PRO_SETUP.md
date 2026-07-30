# US SWING — Professional Paper Trading Setup (Phase 1: Train the Brain)

**Goal:** Let agent paper-trade US leveraged ETFs + mega caps for 3-6 months, become profitable, then you manually follow its GOLD BUY signals in your own broker.

> This is exactly how pro systematic traders incubate a strategy: 100% paper → walk-forward → calibration check → live.

---

## 1. Why US, not MY, for Phase 1?

| | MY (Bursa) | US (Your focus) |
|---|---|---|
| Lot | 100 shares → RM300 min per trade | 1 share → $20 min |
| Fee | 0.15% + slippage 5-80bps | 0% + 2-35bps (NBBO) |
| Universe | 74 tickers dilute brain | 26 high-beta names, dense learning |
| Data | yfinance only, gaps | yfinance reliable, plus Moomoo OpenD if local PC |
| Brain | Needs 50 trades to EXPLOIT, slow with 8 max positions | Needs 50, max 6/4/2, faster |
| Execution | NOOP only forever (Bursa not on OpenD) | NOOP for training, but can go SIMULATE/REAL later |

---

## 2. What I Fixed for Professional US Swing in This PR

### A. Inverse ETF handling (your edge case)
**Before:** BEAR regime blocked ALL longs, including SQQQ/SPXS/SOXS which go UP in BEAR. So your hedge longs were blocked.
**After:** `screener.py`:
- Regular longs (QQQ, TQQQ, SPXL) blocked in BEAR (correct, Avg R -0.067)
- Inverse bears (SQQQ, SPXS, SOXS, UVXY, VXX) **allowed in BEAR**, blocked in BULL (decay trap)

### B. Correlation Shield tightened
**Before:** max 2 per sector → you could hold SPXL + UPRO + TQQQ (all S&P500 3x) = 3x leveraged same bet = blow up.
**After:** `risk_manager.py`:
- Leveraged ETF: max 1
- Leveraged Sector: max 1
- Crypto (IBIT, MSTR, COIN, MARA): max 1
- Volatility (UVXY, VXX): max 1
- Others: max 2

### C. Stop-loss bug (critical)
Fixed yesterday-bar delay. Now live price <= SL closes immediately. Added emergency settle button in Portfolio tab.

### D. Professional Telegram alerts
`live_trigger.py` now sends:
```
🟢 GOLD BUY (BREAKOUT) — QQQ Invesco QQQ Trust
⏰ 2026-07-30 09:45:00 MYT | 🧠 🔬 EXPLORE 12/50 | 📊 Regime BULL 75% | Sector Index ETF
🎯 Confidence 78/100 | RSI 55 | Vol 1.8x | ATR 2.3
----------------------------------------
BUY 22 @ $485.300 | SL $472.100 (-2.7%) | Risk $289
TP1 $492.100 (1.5R) | TP2 $494.500 (2.0R) | TP3 $498.100 (2.5R)
----------------------------------------
🧠 Brain: Breaking above 20-day resistance | Volume surge 1.8x | Trend-aligned
----------------------------------------
📝 How to trade (manual): Place LIMIT/MARKET ...
```
Exit includes R-multiple: `TSLA WIN +1.85R $+342`.

### E. Daily Brain Summary (01:00 MYT)
New `brain_summary.py` + scheduler task `daily_brain_summary`:
- Top 8 learned states with win% and avgR
- Strategy / sector PnL
- Calibration: predicted vs realized win rate
- Recent closes with R
- Auto-sent to Telegram if enabled

Test: `python brain_summary.py` or button in Robo-Trader tab (coming).

### F. Fresh start script
`reset_us_swing.py` backs up current US_SWING DB to `~/.bursa_agent_data/backups/` then wipes trades, priors, logs, resets $5k.

---

## 3. Deploy US Paper Trading (Step-by-Step)

```bash
# 1. Clone branch
git clone ... && cd autonomous_bursa_agentV3.3
git checkout arena/...

# 2. Fresh DB (optional)
python reset_us_swing.py --yes

# 3. Run locally (no Moomoo needed, yfinance fallback)
MARKET_MODE=US TRADING_MODE=SWING streamlit run app_us.py
# OR Streamlit Cloud: set main file to app_us.py, secrets below
```

**Streamlit Cloud Secrets for US:**
```toml
MARKET_MODE = "US"
TRADING_MODE = "SWING"
GITHUB_TOKEN = "ghp_..." # classic, gist scope
TELEGRAM_BOT_TOKEN = "..."
TELEGRAM_CHAT_ID = "..."
NOOP_MODE = "true" # paper only, no broker mirror
```

**In UI:**
1. Settings → Backup → Backup now (creates private gist)
2. Robo-Trader → Start
3. Live Alerts → Enable, min_conf 60, telegram on, Entry+SL+TP+Trailing, actor AGENT → Send test
4. Scanner → SCAN MARKET (now also settles)

---

## 4. Professional Trading Rules (Hardcoded but Tunebale)

- **Risk:** 1% per trade = $50 on $5k, ATR*1.5 sizing, min $20 risk floor
- **Max positions:** BULL 6, NEUTRAL 4, BEAR 2 (prevents overtrade in BEAR)
- **Max hold:** BULL 14d, NEUTRAL 7d, BEAR 5d (leveraged decay protection)
- **Climax exit:** 30% above EMA50 (lets 3x run, US 3x stretches 25-40%)
- **Regime:** QQQ-based (not SPY) because portfolio is QQQ-heavy, 5-bar rolling score prevents whipsaw
- **Time window:** US RTH 09:30-16:00 ET, safe entry cutoff 15:30 ET (last hour no new entries)

---

## 5. How to Follow Brain Manually (Your Workflow)

1. Telegram pings GOLD BUY with entry/SL/TP/R
2. In your broker (Moomoo/IBKR), place same entry, SL, TP1/2/3
3. Position sizing: bot already sized for 1% risk — use same shares
4. If you close early in broker, also close in Dashboard → Portfolio → Manual Close as WIN/LOSS so brain learns correct R
5. Don't override SL — let bot enforce discipline

---

## 6. When is Brain Ready for Live?

Checklist (from `evaluation.py` + `brain_summary.py`):

- [ ] 50 closed trades → EXPLOIT mode auto-switch
- [ ] Win rate 45-60% (swing realistic)
- [ ] Profit factor >1.5
- [ ] Expectancy R >0.20 (avg 0.2R per trade)
- [ ] Sharpe-like >0.3 (WFO gate)
- [ ] Calibration: top confidence bucket 70-80% predicted → 65-85% realized (within 15%)
- [ ] Max consecutive losers <8
- [ ] Monthly hit rate >60%

Run `python brain_summary.py` daily or wait for 01:00 Telegram.

After that, keep NOOP_MODE=true (paper) but you now have statistical edge to follow manually. Only after 200+ trades consider SIMULATE mode (moomoo paper trading account) then REAL.

---

## 7. Commands

```bash
# Fresh start
python reset_us_swing.py --yes

# Brain summary now
python brain_summary.py

# Test stop-loss fix
python test_repro_stop_loss_fix.py

# Run tests (should be 621 passed)
python -m pytest tests/ -q

# Backup
python -c "from persistence import backup; print(backup(force=True))"
```

---

**Bottom line:** US SWING paper is ideal for phase 1. Fixes make SL work, inverse ETFs tradable, correlation shield pro, alerts pro, daily brain summary. Let it run 3-6 months, then follow its brain.
