# Data-Source Validation — Is yfinance OK to train this brain?

**Date:** 2026-08-03
**Scope:** MY SWING on Streamlit Cloud (yfinance path only — Moomoo is gated off for MY)
**Method:** static trace of every `get_history` / `yf.Ticker` call site + numeric reproduction
of the indicator maths. Yahoo endpoints are firewalled in the analysis sandbox, so
live-response checks are marked **[unverified]** and need one run on your machine.

---

## Short answer

**yfinance is good enough to train this brain — but not in the way it is currently wired.**

The feed itself is fine for MY daily swing. Yahoo's Bursa EOD OHLCV is the same
data most retail Bursa tooling uses, and daily bars for liquid KLSE names are
adequate for a 27-state Bayesian tracker learning off ~30–50 trades/year.

The problems I found are **not really about yfinance**. They are about how the
code consumes it. Three of them will corrupt the brain's priors regardless of
which data vendor you plug in, and one of them is worse than every yfinance
limitation combined.

Ranked by how much they damage learning:

| # | Finding | Severity | yfinance's fault? |
|---|---|---|---|
| 1 | Brain trains on 10 stocks it is forbidden from trading | 🔴 Critical | No — config bug |
| 2 | Silent partial scans are indistinguishable from "no setups" | 🔴 Critical | Partly |
| 3 | EMA200 is computed on 246 bars — 8.6% seed contamination | 🟠 High | No — code bug |
| 4 | ~351 Yahoo calls/cycle where ~133 would do | 🟠 High | Partly |
| 5 | `yfinance>=0.2.30` unpinned on a platform that rebuilds on push | 🟠 High | Yes |
| 6 | Volume not split-adjusted → false `Vol_Ratio` spikes | 🟡 Medium | Yes |
| 7 | `auto_adjust=True` trains on prices that never traded | 🟡 Medium | Yes |

---

## 1. 🔴 The brain trains on stocks it can never trade

This is the headline finding and it has nothing to do with data quality.

`learner._classifier_tickers_for_active_market()` (learner.py:164) returns
**the first 10 tickers** of `MY_PROFILE.default_watchlist`:

```
1155 Maybank    1066 RHB      5347 CIMB Group   1015 AMMB    1295 Public Bank
1023 CIMB Grp   6947 DiGi     4863 TM           6012 Maxis   6888 Axiata
```

Those 10 tickers are the **entire training set** for both:
- `train_setup_classifier()` — the ML setup classifier (learner.py:662)
- `run_walk_forward_optimization()` — the parameter optimiser (learner.py:547)

Three things are wrong with that list.

**a) It is 6 banks and 4 telcos.** Nine sectors exist in the profile. The
optimiser tunes `ema_fast`, `volume_surge_ratio`, `atr_multiplier_stop` on
low-beta large-cap defensives, then those parameters are applied live to
construction penny stocks and tech small caps. ATR-based stops in particular do
not transfer between a RM 10 bank and a RM 0.50 small cap.

**b) The live scanner screens 109 tickers, not 30.** `watchlist.BURSA_WATCHLIST`
has 109 unique `.KL` symbols; `MY_PROFILE.default_watchlist` has 30. The learner
reads the 30-list, the scanner reads the 109-list. **Every parameter the brain
learns is fitted on 9% of the universe it actually trades.**

**c) Most of the training set is outside the tradeable price band.** [unverified — confirm with live prices]
`min_price`/`max_price` is `0.30–4.00` (ai_parameters.json), enforced in
`analyze_stock_setup` via `is_in_price_range`, which gates *every* BUY branch
(screener.py:297). Maybank, Public Bank, CIMB, TM and Nestlé all trade well above
RM 4.00. So the walk-forward optimiser is measuring profit factor on price action
the live agent is structurally forbidden from entering.

> **Net effect:** the WFO "Sharpe ≥ 0.3 gate" and the ML classifier's calibrated
> probabilities are being computed on a distribution that does not overlap the
> live one. A better data feed does not fix this. This is the single highest-value
> thing to fix before you trust any learned parameter.

**Fix:** point `_classifier_tickers_for_active_market()` at the same universe the
scanner uses (`watchlist.get_all_tickers()`), sample across sectors rather than
taking `[:10]`, and filter to the active `min_price`/`max_price` band. Also note
`5347` and `1023` are both labelled "CIMB Group" in `my_profile.py` — one is a
duplicate/mislabel, so the effective training set is 9 names, not 10.

---

## 2. 🔴 Silent partial scans poison the priors

`data_provider._fetch_yfinance()` swallows every exception and returns an empty
DataFrame (data_provider.py:665). There is **no retry and no backoff anywhere in
the module** — I grepped for `retry|backoff|429|sleep` in `data_provider.py` and
got zero hits.

The failure chain:

```
Yahoo 429/timeout → empty DataFrame → screener.fetch_and_calculate returns None
  → ticker silently dropped from scan
  → save_scan_cache() overwrites the cache with the PARTIAL result
  → brain scores states from whatever subset Yahoo happened to serve
```

Nothing distinguishes "Yahoo throttled us and we only got 40 of 109 tickers"
from "we scanned all 109 and found no setups". `scan_count` is logged, but no
threshold triggers an alert — the only surface is an explanation string in the UI
(`scheduler.py:227`), and the scheduler records `CYCLE_OK` either way.

For a Bayesian learner this is the worst possible failure mode: it does not crash,
it just quietly biases the posterior toward whichever tickers Yahoo serves most
reliably (the liquid large caps — the same ones from finding #1).

**Fix (cheap, high value):**
- Add a coverage gate in `_run_one_cycle`: if `scan_count < 0.8 × len(tickers)`,
  log `SCAN_DEGRADED`, **skip `save_scan_cache`**, and skip auto-entry for that cycle.
- Add exponential backoff + 2 retries in `_fetch_yfinance` on `429`/timeout.
- Surface a `DATA_DEGRADED` notification through `notifier.dispatch`.

---

## 3. 🟠 EMA200 is contaminated — and it gates every buy

`screener.fetch_and_calculate` pulls `period="1y"` (~246 trading bars), then
`compute_indicators` computes `EMA_Trend` with `span=200, adjust=False`.

An `adjust=False` EMA is seeded with the **first bar's value**. I reproduced the
decay numerically:

```
period="1y"    bars= 246   seed weight still in EMA200 =  8.63%
period="2y"    bars= 494   seed weight still in EMA200 =  0.72%
period="3y"    bars= 740   seed weight still in EMA200 =  0.06%
```

**8.63% of your 200-day trend line is one arbitrary price from 12 months ago.**

On a synthetic series the resulting EMA200 was off by ~1.2% versus a converged
value. That sounds small until you look at how it is used:

```python
is_long_term_uptrend = (close > ema_trend) and is_bullish_alignment
```

`is_long_term_uptrend` is the master gate on the BREAKOUT branch, the PULLBACK
branch, and `_simulate_trades` in the walk-forward optimiser. A 1.2% error in the
trend line flips that boolean for any stock sitting near its 200-day EMA — which
is exactly where pullback setups live. The brain then learns from a state label
that was assigned by an artefact of the fetch window.

The same bug is in `_simulate_trades` (learner.py:430), which requires
`len(df) >= ema_trend` and then computes indicators over a 252-bar train slice.

**Fix:** fetch `period="2y"`, compute indicators on the full frame, then slice to
the window you need. Costs nothing extra in call count — one call either way.

---

## 4. 🟠 Call volume is ~2.6× higher than necessary

Counted per full swing cycle at 109 MY tickers:

```
screener (1y)        109
RS per-stock (3mo)   109
RS benchmark         109   <-- refetches the SAME ^KLSE 109 times
sector momentum       17   (2h cached)
regime                 1
settle prices (3mo)    6
--------------------------
TOTAL / cycle        351
hourly over 8h MY session:  2,808 calls/day
minimum actually needed:    ~133 / cycle
```

The `109` benchmark refetches are a straight bug. `screener.py:553` calls
`rank_stocks_by_relative_strength(tickers)` **without** `klci_df`, so inside
`calculate_relative_strength` the `if klci_df is None:` branch (market_analyzer.py:369)
re-downloads `^KLSE` on every single ticker.

Community reports put unofficial Yahoo throttling somewhere around
~2,000 req/hour/IP with 429s well before that under bursty load
([discussion](https://www.reddit.com/r/algotrading/comments/1mntfnv/whats_the_rate_limit_on_yahoo_finance_unofficial/)).
You are bursting 8 concurrent workers (`ThreadPoolExecutor(max_workers=8)`) from a
**shared Streamlit Cloud egress IP** — you are sharing that quota with every other
Streamlit app on the same node. That materially raises your 429 odds, which feeds
straight back into finding #2.

**Fix:** hoist the benchmark fetch (`klci_df = get_regime_benchmark_data()` once,
pass it in) → −109 calls. Then reuse the screener's 1y frame for RS instead of a
second 3mo pull → −109 more. That is 351 → ~133 with no loss of information.

---

## 5. 🟠 Unpinned yfinance on an auto-rebuilding platform

```
yfinance>=0.2.30
```

Streamlit Cloud reinstalls dependencies whenever `requirements.txt` changes and
rebuilds on every push. `>=` means you get whatever yfinance published most
recently — and yfinance is not a stable dependency:

- The current release resolves to **1.5.2**, seven minor generations past your floor.
- Between 0.2.30 and now the HTTP stack was **swapped from `requests` to `curl_cffi`**
  (0.2.30 required `requests>=2.31`; current requires `curl_cffi`) because Yahoo
  began TLS-fingerprinting and blocking plain `requests` traffic in 2025.
- The `Ticker.history` signature is now `(*args, **kwargs)` — parameters are no
  longer introspectable, so a renamed kwarg fails at runtime, not import time.

A silent major bump on a redeploy is exactly the kind of thing that turns into
"the agent stopped finding signals last Tuesday and nobody noticed" — see #2.

**Fix:** pin exactly (`yfinance==1.5.2`), and bump deliberately after a green CI run.
Your `.github/workflows/tests.yml` already gives you the safety net for that.

---

## 6. 🟡 Volume is not split-adjusted

With `auto_adjust=True` (yfinance's default, and you never override it), **prices**
are back-adjusted for splits and dividends but **volume is not**. Across a split
date the raw share count jumps by the split ratio while `Vol_Avg20` still holds
pre-split values, so:

```python
df["Vol_Ratio"] = df["Volume"] / (df["Vol_Avg20"] + 1e-9)
```

...produces a fake spike for ~20 bars after any split. `Vol_Ratio` is one of only
**three axes** in `discretize_state` (RSI × VolRatio × Trend, 27 states) and is the
trigger for `is_volume_spike`. So a corporate action silently mislabels the state
for a month and the brain books the outcome under the wrong bucket.

`corporate_actions.py` already detects splits properly — it just doesn't reconcile
the volume series. Low frequency on Bursa large caps, higher on the small caps
inside your 0.30–4.00 band.

---

## 7. 🟡 `auto_adjust=True` means training on prices that never traded

Dividend back-adjustment rewrites the entire history every time a stock goes
ex-div. For high-yield Bursa names (banks, REITs, plantations — well represented
in your watchlist) the adjusted price from 3 years ago can sit materially below
the price that actually traded.

Consequences:
- `_simulate_trades` in the WFO backtests fills at prices that never existed.
- The `min_price`/`max_price` band is applied to adjusted history in backtests but
  to near-actual prices live — so the historical tradeable set ≠ the live tradeable set.

This is defensible for a momentum/trend strategy (adjusted series preserve returns,
which is what EMAs and RSI care about) but it is *not* defensible for an absolute
price filter. Either drop `auto_adjust` for the price-band check or apply the band
to unadjusted closes.

---

## Verdict

**Keep yfinance.** For MY daily swing it is adequate, and the alternatives are
worse: Moomoo has no Bursa coverage, and paid Bursa EOD feeds are not worth it
at this stage. The `data_provider.py` abstraction is well built — when a second
source appears you can slot it in without touching callers.

But **do not trust anything the brain has learned so far.** Findings #1 and #3
mean the current priors, the WFO parameters and the ML classifier were all fitted
on the wrong universe with a contaminated trend gate. I would clear the learned
state and restart the 6-month noop period after fixing #1–#4.

**Suggested order:**

1. #1 training universe — nothing else matters until this is right
2. #2 coverage gate + backoff — stops future corruption
3. #3 fetch 2y for EMA200 — one-line change, removes a systematic bias
4. #4 hoist the benchmark fetch — 351 → 133 calls, reduces 429 exposure
5. #5 pin yfinance
6. #6/#7 — track, fix opportunistically

Items 2–5 are small, self-contained diffs. Item 1 needs a decision from you about
what the training universe *should* be (see the question at the end of the chat).

---

## Appendix — Streamlit Cloud sleep

**UptimeRobot cannot keep a Streamlit app awake, and no HTTP-based monitor can.**

Streamlit's own docs are explicit: *"All apps without traffic for 12 hours go to
sleep"* ([Manage your app](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app#app-hibernation)).
Note it is **12 hours**, not the 7 days quoted in `FINAL_EVALUATION.md:108`.

The reason a ping does nothing is architectural:

```
HTTP GET  →  static HTML shell (~4 KB), returns 200  →  Python never starts
             JS must execute  →  WebSocket to /_stcore/stream  →  app boots
```

UptimeRobot stops at step one. It gets its 200, reports "up", and the container
stays asleep — [documented in detail here](https://zenn.dev/shogaku/articles/streamlit-keepalive-playwright?locale=en),
and confirmed on the Streamlit forum where an Azure availability test failed the
same way ([thread](https://discuss.streamlit.io/t/app-sleeps-after-2-days-of-no-traffic-instead-of-7-days/66866)).

### Fix shipped in this branch

`scripts/keepalive.py` + `scripts/keepalive.workflow.yml` — a Playwright headless
Chromium visit that executes JS, opens the real WebSocket, and clicks
*"Yes, get this app back up!"* if the app is already asleep. Runs every 4 hours,
timed so the app is warm before the 09:00 MYT open.

**Two steps to activate:**

1. Move the workflow into place — it ships under `scripts/` because the GitHub
   App pushing this branch lacks the `workflows` permission:

   ```bash
   git mv scripts/keepalive.workflow.yml .github/workflows/keepalive.yml
   git commit -m "ci: enable Streamlit keepalive workflow"
   git push
   ```

2. Add the app URL: **Settings → Secrets and variables → Actions → Variables →
   New variable**, name `STREAMLIT_APP_URL`, value `https://your-app.streamlit.app`.

Then run it once manually from the Actions tab to confirm. You can also test
locally:

```bash
pip install playwright && playwright install chromium --with-deps
STREAMLIT_APP_URL="https://your-app.streamlit.app" python scripts/keepalive.py
```

### But the real fix is to stop depending on Streamlit for the brain

Keepalive treats the symptom. The deeper issue is that your **trading scheduler
lives inside a web UI process** on a free tier with no SLA, unpredictable
container recycling, and a shared egress IP that worsens finding #4.

You are already 90% of the way to a better architecture: `persistence.py` backs
the whole SQLite brain to a private Gist, `scheduler.py` has a `__main__` CLI
entry, and `run_once()` exists. So a GitHub Actions cron could:

```
restore from Gist  →  scheduler.run_once()  →  backup to Gist
```

That gives you guaranteed execution on a real cron, a fresh IP per run (better
Yahoo behaviour), free compute, and logs in the Actions tab. Streamlit then
becomes a pure read-only viewer of the Gist-backed DB — and it no longer matters
whether it sleeps.

Not built here since it changes where trades actually get decided, which is your
call. Happy to implement it next.
