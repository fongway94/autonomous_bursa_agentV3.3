# learner.py
"""
Self-Learning Engine — Bayesian per-state win-rate tracker + bias system.

Why Bayesian, not Q-Learning?
-----------------------------
For swing trading on ~80 tickers with ~5-20 closed trades/state at maturity,
a Beta(α,β) per-(state, action) posterior is statistically the right tool:

* Cold-start: Beta(1,1) → uniform prior, agent defaults to neutral.
* Each WIN → α += 1 + reward_weight, each LOSS → β += 1 + reward_weight.
* Action selection uses Thompson sampling (or posterior-mean during scan).
* Confidence intervals come for free; never overclaim with 5 trades.
* No discount factor / next-state bootstrapping to worry about (each trade
  outcome is independent — there is no sequential decision sub-structure
  within a position because exits are rule-based).

This module also handles:
* Strategy-bias multipliers (breakout vs pullback) with Bayesian shrinkage.
* Sector-bias multipliers.
* Walk-forward optimization with proper train/test separation.
* ML setup classifier with TimeSeriesSplit CV and probability calibration.

FIX 2 summary:
  - Issue #1:  State space reduced 128→27 (3×3×3 bins, no MACD split)
  - Issue #2:  EXPLOIT mode uses E[R] = lo × min(avg_R, 3.0) — real expected value
  - Issue #3:  Walk-forward optimizer uses active market's watchlist
  - Issue #4:  Redundant regime multiplier removed — market_analyzer owns regime
  - Issue #6:  ML classifier uses active market's watchlist tickers
"""

import os
import json
import threading
import numpy as np
import pandas as pd
import yfinance as yf

from sklearn.ensemble import GradientBoostingClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.model_selection import TimeSeriesSplit

from db import connect, myt_iso, DATA_DIR
# v3.7: exploration threshold — only start exploiting after N closed trades
DEFAULT_EXPLORATION_TRADES = 75
# v3.7: WFO minimum closed trades — lowered from 100 to 30 so WFO runs
# during the 6-month noop period. At 30-50 trades/year, this triggers
# in 8-12 months, then monthly after.
WFO_MIN_TRADES = 30
from repository import (
    load_parameters, save_parameters,
    load_bias_state, save_bias_state, closed_trades,
)
from logger import log_learning_event, log_bias_change, get_logger

log = get_logger("learner")

FILE_LOCK = threading.RLock()

# -------------------------------------------------------------------------
# State discretization
# -------------------------------------------------------------------------
# FIX #1: Reduced from 4×4×4×2=128 states to 3×3×3=27 states.
# At 50 exploration trades: 50/27=1.85 per state (vs 50/128=0.39 previously).
# This is the minimum viable granularity for the Bayesian brain to learn.
# MACD split removed — it was contributing noise on sparse data.
# -------------------------------------------------------------------------

RSI_BINS       = [0, 40, 60, 100]      # 3 bins: oversold / neutral / overbought
VOL_RATIO_BINS = [0, 0.8, 1.5, 100]    # 3 bins: dry / normal / spike
TREND_BINS     = [-100, 0, 100]        # 3 bins: below EMA / cross / above EMA


def discretize_state(rsi, vol_ratio, ema_fast_vs_slow_pct, macd_hist) -> int:
    """
    FIX #1: Reduced to 27 states (3×3×3) from 128 (4×4×4×2).

    MACD hist is kept as a feature signal for the screener/ML but
    is NOT a discrete axis — two MACD bins added too much noise on
    sparse data and the brain never had enough trades to populate
    all 128 states reliably.
    """
    def _bin(v, bins):
        for i, u in enumerate(bins):
            if v < u:
                return i
        return len(bins) - 1
    rsi_s = _bin(rsi, RSI_BINS)
    vol_s = _bin(vol_ratio, VOL_RATIO_BINS)
    tr_s  = _bin(ema_fast_vs_slow_pct, TREND_BINS)
    # States: rsi_s ∈ {0,1,2}, vol_s ∈ {0,1,2}, tr_s ∈ {0,1,2}
    # Combined state_id = rsi * 9 + vol * 3 + tr  (range 0–26)
    return rsi_s * 9 + vol_s * 3 + tr_s


# -------------------------------------------------------------------------
# Bayesian posterior
# -------------------------------------------------------------------------

WIN_WEIGHT_CAP = 3.0
LOSS_WEIGHT_CAP = 3.0


def _get_prior(c, state_id: int, action: str) -> dict:
    """
    Retrieve the Beta(α, β) posterior for a (state, action).

    Cold-start priors are *weakly optimistic on BUY* so the agent will
    try a never-seen-before setup at least a few times before deciding
    it's bad. AVOID and HOLD use Beta(1, 1) — neutral.
    """
    row = c.execute(
        "SELECT alpha, beta, n_trades, total_r FROM state_priors "
        "WHERE state_id=? AND action=?",
        (state_id, action),
    ).fetchone()
    if row is None:
        if action == "BUY":
            return {"alpha": 2.0, "beta": 1.0, "n": 0, "total_r": 0.0}
        return {"alpha": 1.0, "beta": 1.0, "n": 0, "total_r": 0.0}
    return {"alpha": row["alpha"], "beta": row["beta"],
            "n": row["n_trades"], "total_r": row["total_r"]}


def _save_prior(c, state_id: int, action: str, prior: dict):
    c.execute(
        "INSERT INTO state_priors (state_id, action, alpha, beta, "
        "n_trades, total_r, last_updated) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(state_id, action) DO UPDATE SET "
        "alpha=excluded.alpha, beta=excluded.beta, "
        "n_trades=excluded.n_trades, total_r=excluded.total_r, "
        "last_updated=excluded.last_updated",
        (state_id, action, prior["alpha"], prior["beta"],
         prior["n"], prior["total_r"], myt_iso()),
    )


def posterior_win_prob(prior: dict, ci: float = 0.05) -> tuple[float, float, float]:
    """Returns (mean, lower_ci, upper_ci) of Beta posterior."""
    from scipy.stats import beta as beta_dist
    a, b = max(prior["alpha"], 1e-6), max(prior["beta"], 1e-6)
    mean = a / (a + b)
    try:
        lo = float(beta_dist.ppf(ci / 2, a, b))
        hi = float(beta_dist.ppf(1 - ci / 2, a, b))
    except Exception:
        lo, hi = mean, mean
    return mean, lo, hi


def _exploration_active() -> bool:
    """
    Exploration mode is ON until the agent has closed N trades (default 50).
    Switched off automatically by the scheduler when the threshold is met.
    """
    from repository import get_scheduler_state, closed_trades
    state = get_scheduler_state()
    if not state.get("exploration_mode", 0):
        return False
    target = state.get("exploration_trades_target", DEFAULT_EXPLORATION_TRADES)
    return len(closed_trades()) < target


# v3.8: training-universe size. 30 sector-stratified names balances
# statistical coverage against wall-clock (each name = one 3y fetch).
TRAINING_UNIVERSE_SIZE = 30


def _classifier_tickers_for_active_market(
        max_tickers: int = TRAINING_UNIVERSE_SIZE) -> list[str]:
    """
    Return the training universe for walk-forward optimisation and the ML
    setup classifier.

    v3.8 FIX — train on what we actually trade
    ------------------------------------------
    The previous implementation returned ``active_profile().default_watchlist[:10]``.
    For MY that was the first 10 entries of ``MY_PROFILE`` — 6 banks + 4 telcos
    (and 5347/1023 are both CIMB, so really 9 distinct names). Three problems:

      1. The LIVE scanner screens ``watchlist.get_all_tickers()`` (109 MY
         symbols), not the 30-name profile list. Every parameter the brain
         learned was fitted on ~9% of the universe it trades.
      2. The 10 were 2 sectors out of 9. ATR-based stops and volume-surge
         ratios do not transfer from a RM 10 bank to a RM 0.50 small cap.
      3. Most of them trade ABOVE the ``max_price`` band, so the optimiser
         scored setups the live agent is structurally forbidden from entering
         (``is_in_price_range`` gates every BUY branch in screener.py).

    Now: same universe as the scanner, sampled round-robin across sectors so
    no single sector dominates. Price-band filtering happens at fetch time in
    the callers (``_price_band_ok``), because it needs the actual close and we
    do not want to spend extra network calls just to pre-filter.

    Honours ``shariah_only`` so the brain never learns from names the scanner
    would refuse to trade.
    """
    tickers: list[str] = []
    try:
        from watchlist import (
            get_all_tickers, get_all_tickers_shariah_only, get_ticker_sector,
        )
        from repository import load_parameters

        try:
            shariah_only = bool(load_parameters().get("shariah_only"))
        except Exception:
            shariah_only = False

        all_t = (get_all_tickers_shariah_only() if shariah_only
                 else get_all_tickers())

        if all_t:
            # Bucket by sector, then round-robin so the sample spans sectors
            # instead of taking an alphabetical head (which is what [:10] did).
            by_sector: dict[str, list[str]] = {}
            for t in all_t:
                by_sector.setdefault(get_ticker_sector(t), []).append(t)
            for bucket in by_sector.values():
                bucket.sort()

            ordered = sorted(by_sector.keys())
            i = 0
            while len(tickers) < max_tickers:
                added = False
                for sec in ordered:
                    if i < len(by_sector[sec]):
                        tickers.append(by_sector[sec][i])
                        added = True
                        if len(tickers) >= max_tickers:
                            break
                if not added:
                    break          # every sector exhausted
                i += 1
    except Exception as e:
        log.warning("training universe resolution failed: %s", e)

    if tickers:
        return tickers

    # Fallback 1: the market profile (previous behaviour, minus the [:10]).
    try:
        from market_profiles import active_profile
        prof_t = [t.yf_symbol
                  for t in active_profile().default_watchlist[:max_tickers]]
        if prof_t:
            log.warning("training universe: falling back to market profile")
            return prof_t
    except Exception:
        pass

    # Fallback 2: hardcoded MY names, only if everything above failed.
    log.warning("training universe: falling back to hardcoded MY list")
    return ["1155.KL", "5347.KL", "6742.KL", "5398.KL",
            "0166.KL", "0138.KL", "5296.KL", "8583.KL",
            "7113.KL", "7108.KL"]


def _price_band_ok(df: pd.DataFrame, params: dict) -> bool:
    """
    True if the ticker's recent price sits inside the tradeable band.

    ``screener.analyze_stock_setup`` gates EVERY buy branch on
    ``min_price <= close <= max_price``. Training on names outside that band
    teaches the brain about setups it can never act on, so we drop them here.

    Uses the MEDIAN of the last 60 closes rather than the last close: a single
    spike shouldn't include/exclude a whole ticker, and the median reflects
    where the name spent the training window.
    """
    try:
        if df is None or df.empty or "Close" not in df.columns:
            return False
        recent = df["Close"].dropna().iloc[-60:]
        if recent.empty:
            return False
        median_close = float(recent.median())
        lo = float(params.get("min_price", 0.0))
        hi = float(params.get("max_price", 1e9))
        return lo <= median_close <= hi
    except Exception:
        return False


def compute_state_action_score(state_id: int, confidence_score: float,
                               regime: str, sector_strength: float) -> dict:
    """
    FIX #2 & #4: Returns the agent's recommendation for a state.

    Changes from original:
      - FIX #4: Regime multiplier REMOVED. market_analyzer already encodes
        regime via confidence_threshold, max_positions, position_size_mult.
        Applying a second layer of regime adjustment on sparse data was
        double-counting and distorting the signal.
      - FIX #2: EXPLOIT mode uses expected value = lo × min(avg_R, 3.0).
        This is the actual expected profit per trade, not just win probability.
        A state with 50% win rate but avg R=2.0 now scores 2× higher than
        a 60% win rate with avg R=0.5 (true expected value: 1.0R vs 0.3R).

    Mode selection:
      * EXPLORATION (first 50 closed trades) — Thompson sampling: draw one
        win-rate sample from each (state, action) Beta posterior and pick
        the highest. Encourages trying new setups quickly.
      * EXPLOITATION (after 50 closed trades) — Expected-value bound:
        conservative estimate so the agent doesn't act on tiny samples.
        E[V] = P(win) × E[R] ≈ lo × min(avg_R, 3.0)
    """
    try:
        from scipy.stats import beta as beta_dist  # noqa: F401
    except Exception:
        return {"action": "HOLD", "confidence_modifier": 1.0,
                "q_scores": {"BUY": 50, "HOLD": 50, "AVOID": 50},
                "reasoning": "scipy unavailable — neutral score"}

    with connect() as c:
        priors = {a: _get_prior(c, state_id, a)
                  for a in ("BUY", "HOLD", "AVOID")}

    explore = _exploration_active()
    scored = {}
    for action, p in priors.items():
        mean, lo, hi = posterior_win_prob(p)
        avg_r = (p["total_r"] / p["n"]) if p["n"] > 0 else 1.0
        # FIX #2: Cap avg_R at 3.0 to prevent outlier trades from distorting
        # the score. A single 10R winner shouldn't override 40 losing 0.3R trades.
        effective_r = min(avg_r, 3.0)

        if explore:
            # Thompson sample: one draw from Beta(α, β) — purely win rate
            try:
                ts = float(beta_dist.rvs(
                    max(p["alpha"], 1e-6), max(p["beta"], 1e-6), size=1)[0])
            except Exception:
                ts = mean
            decision_score = ts
        else:
            # FIX #2: EXPLOIT — expected value = P(win) × E[R]
            # Uses lower confidence bound on P(win) for conservatism
            decision_score = lo * effective_r

        scored[action] = {
            "mean": mean, "lcb": lo, "ucb": hi,
            "decision": decision_score,
            "n": p["n"],
            "avg_r": avg_r,
        }

    # FIX #4: REMOVED — regime_mult was double-counting regime.
    # market_analyzer already encodes regime via threshold, position size, etc.
    # Only keep the sector strength nudge — it reflects actual performance, not market macro.
    sector_mult = {}
    for a in scored:
        sector_mult[a] = 1.0
    if sector_strength > 0.3:
        sector_mult["BUY"] *= 1.05
    elif sector_strength < -0.3:
        sector_mult["BUY"] *= 0.92

    # Composite score 0-100 (no regime multiplier — FIX #4)
    composite = {a: scored[a]["decision"] * 100 * sector_mult[a] for a in scored}

    # Shrink toward 50 when sample is tiny — milder during exploration
    shrink_w = 0.25 if explore else 0.5
    for a, s in scored.items():
        if s["n"] < 5:
            composite[a] = (1 - shrink_w) * composite[a] + shrink_w * 50.0

    best = max(composite, key=composite.get)
    best_score = composite[best]

    modifier = round(min(max(best_score / 100.0, 0.3), 1.3), 3)

    mode_tag = "EXPLORE/Thompson" if explore else "EXPLOIT/E[V]=LCB×avgR"
    reasoning = (
        f"State#{state_id} [{mode_tag}]: "
        + " | ".join(f"{a}: μ={scored[a]['mean']:.2f} "
                     f"R̄={scored[a]['avg_r']:.2f} "
                     f"n={scored[a]['n']}"
                     for a in ("BUY", "HOLD", "AVOID"))
        + f" → {best} ({best_score:.1f})"
    )

    return {
        "action": best,
        "confidence_modifier": modifier,
        "q_scores": {a: round(composite[a], 1) for a in composite},
        "state_priors": {a: {k: round(v, 3) for k, v in s.items()}
                         for a, s in scored.items()},
        "reasoning": reasoning,
        "state_id": state_id,
    }


# -------------------------------------------------------------------------
# Learning loop
# -------------------------------------------------------------------------

def learn_from_trade_outcome(trade: dict) -> dict:
    """
    Update Bayesian posterior + biases based on a closed trade.

    Called from:
        * trading_engine.execute_full_exit
        * scheduler auto-settle
        * dashboard manual close buttons
    """
    with FILE_LOCK:
        ind = trade.get("entry_indicators", {})
        rsi = ind.get("rsi", 50)
        vol = ind.get("vol_ratio", 1.0)
        ema_d = ind.get("ema_trend_distance", 0)
        macd_h = ind.get("macd_hist", 0)
        state_id = discretize_state(rsi, vol, ema_d, macd_h)

        outcome = trade.get("outcome", "UNKNOWN")
        pnl_pct = 0.0
        if trade.get("closed_pnl") is not None and trade.get("cost"):
            pnl_pct = trade["closed_pnl"] / trade["cost"] * 100

        # R-multiple = pnl / initial risk
        rps = trade.get("risk_per_share") or 0
        shares = trade.get("shares") or 1
        initial_risk = (rps * shares) if rps and shares else 0
        r_mult = (trade.get("closed_pnl") or 0) / initial_risk \
            if initial_risk > 0 else 0

        signal = trade.get("signal_type", "")
        if "BREAKOUT" in signal or "PULLBACK" in signal:
            action = "BUY"
        elif "SELL" in signal:
            action = "AVOID"
        else:
            action = "HOLD"

        with connect() as c:
            prior = _get_prior(c, state_id, action)
            if outcome == "WIN":
                prior["alpha"] += 1.0
            elif outcome == "LOSS":
                prior["beta"] += 1.0
            else:
                prior["beta"] += 0.5
            prior["n"] += 1
            # FIX #3-10: Only update total_r when initial_risk > 0.
            # Zero r_mult (e.g., missing risk_per_share at manual entry)
            # would drag avg_R toward 0, corrupting exploit-mode scoring.
            if initial_risk > 0:
                prior["total_r"] += float(r_mult)
            _save_prior(c, state_id, action, prior)

        _update_strategy_bias(trade)

        # v3.7: capture exit_type for learning quality analysis
        exit_type = trade.get("exit_type", "UNKNOWN")
        log_learning_event(
            "BAYES_UPDATE",
            f"State#{state_id} {action} → {outcome} [{exit_type}] (R={r_mult:+.2f})",
            changes={"state_id": state_id, "action": action,
                     "alpha": round(prior["alpha"], 2),
                     "beta": round(prior["beta"], 2)},
            metrics={"r_multiple": round(r_mult, 3),
                     "pnl_pct": round(pnl_pct, 2),
                     "n_trades": prior["n"],
                     "exit_type": exit_type},
        )
        return {
            "state_id": state_id, "action": action,
            "alpha": prior["alpha"], "beta": prior["beta"],
            "n_trades": prior["n"], "r_mult": r_mult,
        }


def _update_strategy_bias(trade: dict):
    """Bayesian-shrunk strategy + sector bias multipliers."""
    biases = load_bias_state()
    sig = trade.get("signal_type", "")
    outcome = trade.get("outcome", "")
    strat = ("breakout" if "BREAKOUT" in sig else
             "pullback" if "PULLBACK" in sig else "other")

    stats = biases.setdefault("strategy_stats", {})
    stats.setdefault(strat, {"wins": 0, "losses": 0, "total": 0})
    stats[strat]["total"] += 1
    if outcome == "WIN":
        stats[strat]["wins"] += 1
    elif outcome == "LOSS":
        stats[strat]["losses"] += 1

    for key, s in stats.items():
        prior_a, prior_b = 5, 5
        wr_shrunk = (s["wins"] + prior_a) / (s["total"] + prior_a + prior_b)
        bias_key = f"{key}_bias"
        if bias_key in biases:
            before = biases[bias_key]
            after = float(np.clip(wr_shrunk / 0.5, 0.75, 1.30))
            biases[bias_key] = after
            log_bias_change(bias_key, before, after,
                            trade_id=trade.get("id"), outcome=outcome)

    sector = trade.get("sector", "")
    if sector:
        sec_stats = biases.setdefault("sector_stats", {})
        sec_stats.setdefault(sector, {"wins": 0, "losses": 0, "total": 0})
        sec_stats[sector]["total"] += 1
        if outcome == "WIN":
            sec_stats[sector]["wins"] += 1
        elif outcome == "LOSS":
            sec_stats[sector]["losses"] += 1
        prior_a, prior_b = 5, 5
        s = sec_stats[sector]
        if s["total"] >= 3:
            wr = (s["wins"] + prior_a) / (s["total"] + prior_a + prior_b)
            before = biases.get("sector_biases", {}).get(sector, 1.0)
            after = float(np.clip(wr / 0.5, 0.80, 1.20))
            biases.setdefault("sector_biases", {})[sector] = after
            log_bias_change(f"sector:{sector}", before, after,
                            trade_id=trade.get("id"), outcome=outcome)

    closed = closed_trades()
    if closed:
        wins = sum(1 for t in closed if t.get("outcome") == "WIN")
        biases["total_closed_trades"] = len(closed)
        biases["system_win_rate"] = round(wins / len(closed), 4)

    save_bias_state(biases)


# -------------------------------------------------------------------------
# Walk-forward optimisation (with proper train/test split)
# -------------------------------------------------------------------------

def _simulate_trades(df: pd.DataFrame, params: dict) -> list[dict]:
    from screener import compute_indicators
    if df is None or df.empty or len(df) < params.get("ema_trend", 200):
        return []
    df = compute_indicators(df, params)
    if df.empty or "RSI" not in df.columns:
        return []
    TRANSACTION_COST = 0.0015
    trades = []
    in_trade = False
    entry = stop = tp2 = 0.0
    entry_idx = 0
    for i in range(params.get("ema_trend", 200), len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        if in_trade:
            if row["Low"] <= stop:
                pnl_pct = (stop - entry) / entry * 100 - TRANSACTION_COST * 100
                trades.append({"outcome": "LOSS", "pnl_pct": pnl_pct})
                in_trade = False
            elif row["High"] >= tp2:
                pnl_pct = (tp2 - entry) / entry * 100 - TRANSACTION_COST * 100
                trades.append({"outcome": "WIN", "pnl_pct": pnl_pct})
                in_trade = False
            elif i - entry_idx >= 15:
                pnl_pct = (row["Close"] - entry) / entry * 100 - TRANSACTION_COST * 100
                trades.append({
                    "outcome": "WIN" if pnl_pct > 0 else "LOSS",
                    "pnl_pct": pnl_pct,
                })
                in_trade = False
            continue
        close = row["Close"]
        ema_fast = row["EMA_Fast"]; ema_slow = row["EMA_Slow"]
        ema_trend = row["EMA_Trend"]; vol_ratio = row["Vol_Ratio"]
        atr = row["ATR"] if not pd.isna(row["ATR"]) else close * 0.03
        is_up = close > ema_trend
        vol_spike = vol_ratio >= params.get("volume_surge_ratio", 1.5)
        is_breakout = close > prev["EMA_Fast"] and prev["Close"] <= prev["EMA_Fast"]
        is_pullback = (is_up and close > ema_slow
                       and row["Low"] <= ema_slow * 1.01 and close >= ema_slow)
        if is_up and ((is_breakout and vol_spike) or is_pullback):
            in_trade = True
            entry = float(close) * (1 - TRANSACTION_COST)
            entry_idx = i
            stop = float(close - atr * params.get("atr_multiplier_stop", 1.5))
            risk = entry - stop
            tp2 = float(entry + 2.0 * risk)
    return trades


def _score_param_set(trades: list[dict]) -> dict:
    """
    Score a param set's trades. Returns win_rate, profit_factor,
    combined score, n_trades, AND sharpe-like (annualised risk-adjusted R).
    
    Sharpe-like = mean(pnl_pct per trade) / std(pnl_pct) * sqrt(252)
    A value < 0.3 means the strategy's edge is statistically questionable.
    """
    if len(trades) < 5:
        return {"win_rate": 0, "profit_factor": 0,
                "combined": 0, "n_trades": len(trades),
                "sharpe_like": 0.0}
    wins = [t for t in trades if t["outcome"] == "WIN"]
    losses = [t for t in trades if t["outcome"] == "LOSS"]
    wr = len(wins) / len(trades)
    tp = sum(t["pnl_pct"] for t in wins)
    tl = abs(sum(t["pnl_pct"] for t in losses))
    pf = tp / (tl + 1e-9)
    combined = wr * 0.4 + min(pf / 2.0, 1.0) * 0.6
    
    # v3.7: Sharpe-like (annualised risk-adjusted R)
    pnl_vals = [t["pnl_pct"] for t in trades]
    mean_pnl = sum(pnl_vals) / len(pnl_vals)
    variance = sum((p - mean_pnl) ** 2 for p in pnl_vals) / max(len(pnl_vals) - 1, 1)
    std_pnl = variance ** 0.5
    sharpe_like = (mean_pnl / std_pnl) * (252 ** 0.5) if std_pnl > 0 else 0.0
    
    return {"win_rate": wr, "profit_factor": pf,
            "combined": combined, "n_trades": len(trades),
            "sharpe_like": round(sharpe_like, 4)}


def run_walk_forward_optimization(progress_callback=None) -> tuple[dict, float, float]:
    """
    FIX #3: Walk-forward optimizer now uses the active market's watchlist
    instead of hardcoded MY tickers.

    Proper walk-forward optimisation:
      * Each window: optimise on TRAIN, evaluate on TEST.
      * Pick params with best out-of-sample TEST score, averaged across
        all windows.
      * Requires at least 30 OOS trades in the winning grid; else reject.
    """
    # FIX #3: Use active market's watchlist — not hardcoded MY tickers.
    # When US is active, this picks SPY, QQQ, TQQQ, SOXL, etc.
    # Instead of MY stocks that have entirely different volatility profiles.
    tickers = _classifier_tickers_for_active_market()

    search_grid = [
        {"ema_fast": 9, "ema_slow": 21, "rsi_oversold_pullback": 35.0,
         "volume_surge_ratio": 1.3, "atr_multiplier_stop": 1.5},
        {"ema_fast": 10, "ema_slow": 20, "rsi_oversold_pullback": 40.0,
         "volume_surge_ratio": 1.5, "atr_multiplier_stop": 1.5},
        {"ema_fast": 12, "ema_slow": 26, "rsi_oversold_pullback": 42.0,
         "volume_surge_ratio": 1.8, "atr_multiplier_stop": 2.0},
        {"ema_fast": 5, "ema_slow": 15, "rsi_oversold_pullback": 45.0,
         "volume_surge_ratio": 1.2, "atr_multiplier_stop": 1.5},
        {"ema_fast": 8, "ema_slow": 18, "rsi_oversold_pullback": 38.0,
         "volume_surge_ratio": 1.4, "atr_multiplier_stop": 1.5},
        {"ema_fast": 7, "ema_slow": 21, "rsi_oversold_pullback": 36.0,
         "volume_surge_ratio": 1.6, "atr_multiplier_stop": 1.8},
    ]

    base_params = load_parameters()

    # v3.8: fetch via data_provider (respects Moomoo/yfinance dispatch and the
    # provider's timeout contract) instead of calling yf.Ticker directly, and
    # drop names outside the tradeable price band — the optimiser must not
    # score setups the live screener would refuse on price alone.
    from data_provider import get_history

    dfs: dict[str, pd.DataFrame] = {}
    skipped_band = 0
    for t in tickers:
        try:
            d = get_history(t, period="3y", timeout=15)
            if d is None or d.empty:
                continue
            if not _price_band_ok(d, base_params):
                skipped_band += 1
                continue
            dfs[t] = d
        except Exception:
            pass

    if skipped_band:
        log.info("WFO: skipped %d/%d tickers outside price band %.2f-%.2f",
                 skipped_band, len(tickers),
                 base_params.get("min_price", 0), base_params.get("max_price", 0))

    if not dfs:
        log_learning_event(
            "WALK_FORWARD_REJECTED",
            "No tickers survived the price-band filter — params unchanged.",
            metrics={"candidates": len(tickers), "skipped_band": skipped_band},
        )
        return None, 0, 0

    grid_scores: dict[int, list[dict]] = {i: [] for i in range(len(search_grid))}

    n_windows = 4
    for w in range(n_windows):
        train_end = -w * 60 - 60        # leave 60-day OOS gap
        train_start = train_end - 252   # 1y train
        test_start = train_end
        test_end = train_end + 60

        if test_end > 0 or train_start > -250 - n_windows * 60:
            continue
        for gi, g in enumerate(search_grid):
            params = {**base_params, **g}
            if progress_callback:
                pct = (w * len(search_grid) + gi) / (n_windows * len(search_grid))
                progress_callback(pct,
                                  f"WF window {w+1}/{n_windows} | grid {gi+1}/{len(search_grid)}")

            all_trades = []
            for t, df in dfs.items():
                if df.empty or len(df) < 300:
                    continue
                train_df = df.iloc[train_start:train_end] if train_end else df.iloc[train_start:]
                test_df = df.iloc[test_start:test_end] if test_end else df.iloc[test_start:]
                train_score = _score_param_set(_simulate_trades(train_df, params))
                if train_score["n_trades"] < 2:
                    continue
                test_trades = _simulate_trades(test_df, params)
                all_trades.extend(test_trades)
            grid_scores[gi].append(_score_param_set(all_trades))

    # v3.7: Sharpe gate minimum
    SHARPE_GATE = 0.3
    best_idx, best_score, best_pf, best_wr, best_n, best_sharpe = None, -1, 0, 0, 0, 0.0
    aggregated = []
    for gi, scores in grid_scores.items():
        if not scores:
            continue
        n_total = sum(s["n_trades"] for s in scores)
        avg_combined = float(np.mean([s["combined"] for s in scores]))
        avg_wr = float(np.mean([s["win_rate"] for s in scores]))
        avg_pf = float(np.mean([s["profit_factor"] for s in scores]))
        avg_sharpe = float(np.mean([s["sharpe_like"] for s in scores]))
        aggregated.append({"grid": gi, "params": search_grid[gi],
                           "avg_combined": avg_combined, "avg_wr": avg_wr,
                           "avg_pf": avg_pf, "n_total": n_total,
                           "avg_sharpe": round(avg_sharpe, 4)})
        # v3.7: Sharpe-like gate — reject lucky grids with high combined but no real edge
        if n_total >= 20 and avg_combined > best_score and avg_sharpe >= SHARPE_GATE:  # v3.7: was 30, lowered to 20
            best_score = avg_combined; best_idx = gi
            best_pf = avg_pf; best_wr = avg_wr; best_n = n_total
            best_sharpe = avg_sharpe

    if best_idx is None:
        log_learning_event(
            "WALK_FORWARD_REJECTED",
            "No parameter set passed the Sharpe-like ≥0.3 gate; params unchanged.",
            metrics={"grids_tested": len(search_grid)},
        )
        return None, 0, 0

    new_params = {**base_params, **search_grid[best_idx]}
    save_parameters(new_params, source="WALK_FORWARD",
                    reason=f"Best OOS combined={best_score:.3f} "
                           f"WR={best_wr*100:.1f}% PF={best_pf:.2f} N={best_n}")
    log_learning_event(
        "WALK_FORWARD_OPTIMIZATION",
        f"Selected grid #{best_idx} (OOS WR={best_wr*100:.1f}%, PF={best_pf:.2f}, "
        f"N={best_n})",
        changes={"new_params": search_grid[best_idx]},
        metrics={"combined": round(best_score, 3),
                 "win_rate": round(best_wr * 100, 2),
                 "profit_factor": round(best_pf, 2),
                 "oos_trades": best_n,
                 "all_grids": aggregated},
    )
    return new_params, best_wr, best_pf


# -------------------------------------------------------------------------
# ML setup classifier — TimeSeriesSplit + Calibration
# -------------------------------------------------------------------------

_CLASSIFIER_PATH = os.path.join(DATA_DIR, "setup_classifier.pkl")
_CLASSIFIER_META_PATH = os.path.join(DATA_DIR, "setup_classifier_meta.json")
_clf_model = None


def train_setup_classifier():
    """
    Train the ML setup classifier on the active market's TRADEABLE universe.

    FIX #6 (v3.6): use the active market's watchlist, not hardcoded MY tickers.
      US 3× ETFs (TQQQ, SOXL) have entirely different volatility/volume
      signatures than MY stocks; training on MY and applying to US produced
      misaligned probability estimates.

    v3.8: the universe is now the SCANNER's universe (sector-stratified sample
      of ``watchlist.get_all_tickers()``), filtered to the active price band —
      previously it was ``profile.default_watchlist[:10]``, i.e. 9 large-cap
      banks/telcos that mostly trade above ``max_price`` and so could never be
      entered live. See ``_classifier_tickers_for_active_market``.
    """
    from screener import compute_indicators
    tickers = _classifier_tickers_for_active_market()
    params = load_parameters()
    feature_names = ["RSI", "VolRatio", "MACDHist",
                     "EMA_Fast_Dist", "EMA_Slow_Dist", "EMA_Trend_Dist",
                     "BB_Width"]

    # v3.8: route through data_provider and apply the same price-band filter
    # the live screener applies, so the classifier's calibrated probabilities
    # describe the distribution the agent actually trades.
    from data_provider import get_history

    rows = []  # (date, features, label)
    skipped_band = 0
    for t in tickers:
        try:
            df = get_history(t, period="3y", timeout=15)
            if df is None or df.empty or len(df) < 250:
                continue
            if not _price_band_ok(df, params):
                skipped_band += 1
                continue
            df = compute_indicators(df, params)
            if df.empty or "RSI" not in df.columns:
                continue
            for i in range(100, len(df) - 10):
                row = df.iloc[i]; future = df["High"].iloc[i + 1:i + 11].values
                cur = row["Close"]
                if pd.isna(cur) or cur == 0:
                    continue
                target = int(any(
                    ((fp - cur) / cur >= 0.05)
                    for fp in future if not pd.isna(fp)))
                feats = [
                    float(row["RSI"]) if not pd.isna(row["RSI"]) else 50,
                    float(row["Vol_Ratio"]) if not pd.isna(row["Vol_Ratio"]) else 1,
                    float(row["MACD_Hist"]) if not pd.isna(row["MACD_Hist"]) else 0,
                    float((row["Close"] - row["EMA_Fast"]) / row["EMA_Fast"] * 100)
                    if not (pd.isna(row["EMA_Fast"]) or row["EMA_Fast"] == 0) else 0,
                    float((row["Close"] - row["EMA_Slow"]) / row["EMA_Slow"] * 100)
                    if not (pd.isna(row["EMA_Slow"]) or row["EMA_Slow"] == 0) else 0,
                    float((row["Close"] - row["EMA_Trend"]) / row["EMA_Trend"] * 100)
                    if not (pd.isna(row["EMA_Trend"]) or row["EMA_Trend"] == 0) else 0,
                    float((row["BB_Upper"] - row["BB_Lower"]) / row["Close"] * 100)
                    if not (pd.isna(row["BB_Upper"]) or pd.isna(row["BB_Lower"])
                            or pd.isna(row["Close"]) or row["Close"] == 0) else 0,
                ]
                feats = [float(np.nan_to_num(f, nan=0)) for f in feats]
                rows.append((df.index[i], feats, target))
        except Exception:
            continue

    if skipped_band:
        log.info("ML classifier: skipped %d/%d tickers outside price band",
                 skipped_band, len(tickers))

    if len(rows) < 200:
        log.warning("ML classifier: only %d samples (<200) — not training", len(rows))
        return None

    rows.sort(key=lambda r: r[0])  # ensure chronological for TimeSeriesSplit
    X = np.array([r[1] for r in rows])
    y = np.array([r[2] for r in rows])

    split = int(len(rows) * 0.9)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    base = GradientBoostingClassifier(
        n_estimators=120, max_depth=4, learning_rate=0.08, random_state=42)

    tscv = TimeSeriesSplit(n_splits=3)
    clf = CalibratedClassifierCV(base, method="isotonic", cv=tscv)
    clf.fit(X_train, y_train)

    train_acc = float(clf.score(X_train, y_train))
    test_acc = float(clf.score(X_test, y_test)) if len(y_test) else 0.0

    try:
        importances = []
        for cclf in clf.calibrated_classifiers_:
            est = getattr(cclf, "estimator", None) or getattr(cclf, "base_estimator", None)
            if est is not None and hasattr(est, "feature_importances_"):
                importances.append(est.feature_importances_)
        importance = np.mean(importances, axis=0).tolist() if importances else [0] * len(feature_names)
    except Exception:
        importance = [0] * len(feature_names)

    import joblib
    joblib.dump(clf, _CLASSIFIER_PATH)
    meta = {
        "model_type": "CalibratedGradientBoosting",
        "train_accuracy": round(train_acc, 4),
        "holdout_accuracy": round(test_acc, 4),
        "importance": {n: round(float(v), 4)
                       for n, v in zip(feature_names, importance)},
        "feature_names": feature_names,
        "trained_at": myt_iso(),
        "n_train": len(X_train), "n_test": len(X_test),
        "class_ratio": round(float(np.mean(y_train)), 3),
        "market_code": _active_market_code_from_profile(),
        # v3.8: record WHICH universe produced this model. Without this you
        # cannot tell a model trained on the old 9-large-cap list from one
        # trained on the real scanner universe.
        "n_tickers_candidate": len(tickers),
        "n_tickers_used": len(tickers) - skipped_band,
        "price_band": [params.get("min_price"), params.get("max_price")],
    }
    with open(_CLASSIFIER_META_PATH, "w") as f:
        json.dump(meta, f, indent=2)

    log_learning_event(
        "ML_CLASSIFIER_TRAINED",
        f"Train acc={train_acc:.3f} | OOS test acc={test_acc:.3f} "
        f"[{_active_market_code_from_profile()}]",
        metrics={"n_train": len(X_train), "n_test": len(y_test),
                 "importance": meta["importance"]},
    )
    return clf, test_acc, importance


def _active_market_code_from_profile() -> str:
    try:
        from market_profiles import active_market_code
        return active_market_code()
    except Exception:
        return "MY"


def _load_classifier():
    global _clf_model
    if _clf_model is not None:
        return _clf_model
    import joblib
    if os.path.exists(_CLASSIFIER_PATH):
        try:
            _clf_model = joblib.load(_CLASSIFIER_PATH)
            return _clf_model
        except Exception:
            pass
    trained = train_setup_classifier()
    if trained:
        _clf_model = trained[0]
    return _clf_model


def get_ml_score(rsi, vol_ratio, macd_hist, close, ema_fast, ema_slow,
                 ema_trend, bb_upper, bb_lower) -> float:
    """Calibrated probability (0–100) that this setup will gain >=5% in 10d."""
    clf = _load_classifier()
    if clf is None:
        return 50.0 + (15 if 40 < rsi < 65 else 0) + \
               (15 if vol_ratio > 1.5 else 0)
    feats = np.array([[
        float(np.nan_to_num(rsi, nan=50)),
        float(np.nan_to_num(vol_ratio, nan=1)),
        float(np.nan_to_num(macd_hist, nan=0)),
        float(np.nan_to_num((close - ema_fast) / ema_fast * 100
                            if ema_fast else 0, nan=0)),
        float(np.nan_to_num((close - ema_slow) / ema_slow * 100
                            if ema_slow else 0, nan=0)),
        float(np.nan_to_num((close - ema_trend) / ema_trend * 100
                            if ema_trend else 0, nan=0)),
        float(np.nan_to_num((bb_upper - bb_lower) / close * 100
                            if close else 0, nan=0)),
    ]])
    try:
        prob = clf.predict_proba(feats)[0][1]
        return float(prob * 100)
    except Exception:
        return 50.0


def get_classifier_meta() -> dict:
    if os.path.exists(_CLASSIFIER_META_PATH):
        try:
            with open(_CLASSIFIER_META_PATH) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


# -------------------------------------------------------------------------
# Read helpers for dashboard
# -------------------------------------------------------------------------

def get_strategy_performance_report() -> dict:
    trades = closed_trades()
    if not trades:
        return {"summary": {"total_trades": 0, "wins": 0, "losses": 0,
                            "breakeven": 0, "win_rate": 0,
                            "total_pnl_rm": 0, "avg_win_rm": 0,
                            "avg_loss_rm": 0},
                "by_strategy": {}, "by_sector": {}, "by_month": {}}

    wins = [t for t in trades if t.get("outcome") == "WIN"]
    losses = [t for t in trades if t.get("outcome") == "LOSS"]
    be = [t for t in trades if t.get("outcome") == "BREAKEVEN"]

    by_strat = _group(trades, "signal_type")
    by_sector = _group(trades, "sector")
    by_month = _group_month(trades)

    return {
        "summary": {
            "total_trades": len(trades),
            "wins": len(wins), "losses": len(losses), "breakeven": len(be),
            "win_rate": round(len(wins) / len(trades) * 100, 1),
            "total_pnl_rm": round(sum(t.get("realized_pnl") or t.get("closed_pnl") or 0
                                      for t in trades), 2),
            "avg_win_rm": round(np.mean([t.get("realized_pnl") or 0 for t in wins])
                                if wins else 0, 2),
            "avg_loss_rm": round(np.mean([t.get("realized_pnl") or 0 for t in losses])
                                 if losses else 0, 2),
        },
        "by_strategy": by_strat,
        "by_sector": by_sector,
        "by_month": dict(sorted(by_month.items())),
    }


def _group(trades, key):
    out = {}
    for t in trades:
        k = t.get(key) or "Unknown"
        d = out.setdefault(k, {"wins": 0, "losses": 0, "total": 0,
                               "total_pnl": 0, "win_rate": 0})
        d["total"] += 1
        d["total_pnl"] += t.get("realized_pnl") or t.get("closed_pnl") or 0
        if t.get("outcome") == "WIN":
            d["wins"] += 1
        elif t.get("outcome") == "LOSS":
            d["losses"] += 1
    for k, v in out.items():
        if v["total"] > 0:
            v["win_rate"] = round(v["wins"] / v["total"] * 100, 1)
    return out


def _group_month(trades):
    out = {}
    for t in trades:
        c = t.get("closed_at") or t.get("logged_at") or ""
        m = c[:7]
        if not m:
            continue
        d = out.setdefault(m, {"wins": 0, "losses": 0, "total": 0,
                               "pnl": 0, "win_rate": 0})
        d["total"] += 1
        d["pnl"] += t.get("realized_pnl") or t.get("closed_pnl") or 0
        if t.get("outcome") == "WIN":
            d["wins"] += 1
        elif t.get("outcome") == "LOSS":
            d["losses"] += 1
        if d["total"] > 0:
            d["win_rate"] = round(d["wins"] / d["total"] * 100, 1)
    return out


def get_learning_history() -> list[dict]:
    from logger import get_learning_events
    return get_learning_events(limit=200)



def cleanup_orphaned_state_priors() -> dict:
    """
    FIX #3-16: One-time cleanup of orphaned state IDs.

    Before fix_2, the state formula produced IDs 0-127 (128 states).
    Fix_2 reduced it to 0-26 (27 states). Any state_ids >= 27 in
    state_priors are from the old formula and will never be queried
    again. This function removes them so the brain only tracks valid states.

    Idempotent: safe to run multiple times. Returns count of orphaned rows.
    Called once during daily maintenance after fix_2 deployment.
    """
    try:
        from market_profiles import active_market_code
        max_valid = 26
    except Exception:
        max_valid = 26

    with connect() as c:
        cur = c.execute(
            "SELECT COUNT(*) FROM state_priors WHERE state_id > ?",
            (max_valid,),
        )
        orphaned = cur.fetchone()[0]
        if orphaned > 0:
            c.execute(
                "DELETE FROM state_priors WHERE state_id > ?",
                (max_valid,),
            )
            log_learning_event(
                "STATE_PRIORS_CLEANUP",
                f"Removed {orphaned} orphaned state IDs (formula changed 128->27).",
                changes={"orphaned_count": orphaned, "max_valid_state_id": max_valid},
            )
        return {"orphaned_removed": orphaned, "max_valid_state_id": max_valid}

def decay_priors(decay_factor: float = 0.95):
    """
    Applies an exponential decay factor to all historical alpha and beta values
    in the state_priors table to mitigate market non-stationarity.
    Ensures recent trade feedback has higher weight than ancient history.

    NOTE: This function exists but is NOT called from scheduler daily maintenance
    (FIX #4 removed the call). It will be re-enabled only after empirical
    calibration with real closed-trade data (expected after 200+ trades).
    """
    if not (0.5 <= decay_factor < 1.0):
        return
    with connect() as c:
        c.execute(
            "UPDATE state_priors SET "
            "alpha = MAX(1.0, alpha * ?), "
            "beta = MAX(1.0, beta * ?)",
            (decay_factor, decay_factor)
        )
    log_learning_event(
        "PRIORS_DECAY",
        f"Applied decay factor of {decay_factor} to state_priors",
        changes={"decay_factor": decay_factor}
    )


def decay_priors_if_warranted(decay_factor: float = 0.95) -> dict:
    """
    v3.7: Conditional decay — only runs when we have enough data to make
    the decision meaningful. Prevents destroying the prior with insufficient
    signal.

    Conditions:
      * ≥5 distinct states have ≥20 trades each.

    This prevents the decay from accidentally flattening priors when the
    brain only has 10 trades across 2 states.
    """
    with connect() as c:
        cur = c.execute(
            """SELECT COUNT(*) FROM state_priors
               WHERE n_trades >= 20"""
        )
        rich_states = cur.fetchone()[0]

        cur = c.execute("SELECT COUNT(DISTINCT state_id) FROM state_priors")
        total_states = cur.fetchone()[0]

    if rich_states < 5 or total_states < 5:
        return {
            "decayed": False,
            "reason": f"Only {rich_states}/5 required rich states, "
                      f"{total_states} total states",
            "rich_states": rich_states,
            "total_states": total_states,
        }

    if not (0.5 <= decay_factor < 1.0):
        return {"decayed": False, "reason": "Invalid decay_factor"}

    with connect() as c:
        c.execute(
            "UPDATE state_priors SET "
            "alpha = MAX(1.0, alpha * ?), "
            "beta = MAX(1.0, beta * ?)",
            (decay_factor, decay_factor)
        )

    log_learning_event(
        "PRIORS_DECAY_CONDITIONAL",
        f"Conditional decay applied (factor={decay_factor}) "
        f"because {rich_states} states had ≥20 trades.",
        changes={"decay_factor": decay_factor,
                 "rich_states": rich_states,
                 "total_states": total_states},
    )
    return {
        "decayed": True,
        "decay_factor": decay_factor,
        "rich_states": rich_states,
        "total_states": total_states,
    }
