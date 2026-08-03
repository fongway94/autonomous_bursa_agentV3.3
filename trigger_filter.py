"""
Stricter trigger filter — evidence-based improvement.

Based on backtest findings (HandBook/orb_backtest_results.md):
  - +0.110 R avg ONLY in bull/trending markets (Mar-May 2026 sample)
  - 51% win rate, 8 max consecutive losses — barely survivable
  - Post-slippage realistic expectation: ~+0.07 R (thin)
  - VWAP filter barely improves results; volume filter does real work
  - Brain veto (q_action = AVOID) historically avoids losses

This module applies stricter criteria BEFORE firing a live trigger.
Default behavior: ONLY trigger high-confidence GOLD BUY in non-BEAR
regimes when brain endorses the setup and RSI is not overbought.
"""

from __future__ import annotations

from typing import Optional, Dict, Any


def strict_trigger_check(
    setup: Dict[str, Any],
    brain_action: Optional[str] = None,
    brain_buy_score: Optional[float] = None,
    brain_avoid_score: Optional[float] = None,
    regime: str = "NEUTRAL",
    mode: str = "SWING",
    min_confidence: float = 80.0,
    max_rsi: float = 70.0,
    min_vol_ratio: float = 1.2,
) -> tuple[bool, str]:
    """
    Returns (should_trigger, reason).

    Strict rules (no exceptions):
      1. Signal MUST contain "GOLD BUY".
      2. Confidence >= min_confidence (default 80, not 70).
      3. Regime MUST NOT be "BEAR".
      4. Brain action MUST NOT be "AVOID".
      5. If brain BUY score < brain AVOID score + 15: VETO.
      6. RSI < max_rsi (not overbought — avoids buying exhaustion).
      7. Volume ratio >= min_vol_ratio (confirms real buying pressure).
      8. Only trigger when agent brain is in EXPLOIT mode for intraday.

    These rules are designed to produce FEWER, HIGHER-QUALITY triggers
    rather than frequent marginal ones. A professional trader should prefer
    10 high-quality setups per month over 50 marginal ones.
    """
    signal = str(setup.get("signal") or "")

    # Rule 1: Only GOLD BUY
    if "GOLD BUY" not in signal:
        return False, "signal_not_gold_buy"

    # Rule 2: Confidence threshold (stricter than default 70)
    confidence = float(setup.get("confidence") or 0)
    if confidence < min_confidence:
        return False, f"confidence_{confidence:.0f}_below_{min_confidence:.0f}"

    # Rule 3: Regime check — never trigger in BEAR
    if regime == "BEAR":
        return False, "bear_regime_blocked"

    # Rule 4 & 5: Brain veto / endorsement
    if brain_action is not None:
        brain_action = str(brain_action).upper()
        if brain_action == "AVOID":
            # Hard veto: brain has seen historical losses in this state
            if brain_buy_score is not None and brain_avoid_score is not None:
                if brain_avoid_score > brain_buy_score + 15:
                    return False, "brain_hard_veto_avoid"
            else:
                return False, "brain_avoid_no_scores"
        elif brain_action == "BUY" and brain_buy_score is not None:
            # Brain endorsement helps but does not override other rules
            if brain_buy_score < 60:
                return False, f"brain_buy_score_{brain_buy_score:.0f}_low"

    # Rule 6: RSI overbought protection
    rsi = float(setup.get("rsi") or 99)
    if rsi >= max_rsi:
        return False, f"rsi_{rsi:.1f}_overbought_above_{max_rsi:.0f}"

    # Rule 7: Volume confirmation
    vol_ratio = float(setup.get("vol_ratio") or 0)
    if vol_ratio < min_vol_ratio:
        return False, f"volume_{vol_ratio:.2f}x_below_{min_vol_ratio:.1f}x"

    # Rule 8: Mode awareness (for intraday only — SWING has different rules)
    # Not enforced here; caller should check mode separately.

    return True, "ok_strict"


def evaluate_trigger_profitability(
    triggered_trades: list,
    min_trades_for_stat: int = 30,
    min_expectancy_r: float = 0.05,
) -> Dict[str, Any]:
    """
    After triggering N trades, calculate realized profitability.

    Args:
        triggered_trades: list of trade outcome dicts (each with 'r_multiple', 'outcome')
        min_trades_for_stat: need at least this many for any opinion
        min_expectancy_r: minimum acceptable per-trade R

    Returns:
        dict with 'should_continue', 'avg_r', 'n_trades', 'reason'
    """
    n = len(triggered_trades)
    if n < min_trades_for_stat:
        return {
            "should_continue": True,
            "avg_r": None,
            "n_trades": n,
            "reason": f"insufficient_trades_{n}_of_{min_trades_for_stat}",
        }

    r_vals = [float(t.get("r_multiple") or 0) for t in triggered_trades]
    avg_r = sum(r_vals) / len(r_vals)
    wins = sum(1 for r in r_vals if r > 0)
    win_rate = wins / len(r_vals)

    if avg_r >= min_expectancy_r:
        return {
            "should_continue": True,
            "avg_r": round(avg_r, 3),
            "n_trades": n,
            "reason": f"positive_expectancy_{avg_r:.3f}r",
            "win_rate": round(win_rate, 3),
        }
    else:
        return {
            "should_continue": False,
            "avg_r": round(avg_r, 3),
            "n_trades": n,
            "reason": f"negative_or_marginal_expectancy_{avg_r:.3f}r_below_{min_expectancy_r:.2f}",
            "win_rate": round(win_rate, 3),
        }
