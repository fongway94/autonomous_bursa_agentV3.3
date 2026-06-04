"""
decision_tiers.py — Pure tier classification + structured decision records.

This is the heart of the NOOP learning design. It takes a setup as produced by
the existing screeners (swing `screener.screen_all_stocks()` and
`intraday_screener`) and classifies it into one of four tiers, WITHOUT changing
any strategy logic. It then builds a fully-structured, falsifiable decision
record that can be journaled and later resolved.

The tiers (see HandBook/NOOP_TRUE_STATE.md and the audit):

    Tier A : High-confidence valid setup. GOLD BUY whose confidence >= the
             regime threshold. This is the "would-execute" tier in NOOP.
    Tier B : Good setup, one confirmation short. A GOLD/SILVER BUY that is
             close to (but below) the threshold (within TIER_B_MARGIN).
    Tier C : Weak / rejected but recognizable setup worth tracking
             (a BUY signal far below threshold, OR a SILVER buy).
    Tier D : No valid setup.

Why classify ALL tiers? Because a setup you DON'T take is still a labeled
training example once its shadow outcome is resolved. This is what dissolves the
"strict -> starvation / loose -> noise" dilemma: you get learning volume without
taking marginal trades.

This module is PURE: no DB, no network, no side effects. Deterministic given its
inputs — which is required so decision records are reproducible (tested).
"""

from __future__ import annotations

from typing import Any, Optional


# ---------------------------------------------------------------------------
# Frozen tier thresholds (DO NOT tune during a NOOP observation window).
# These describe *classification*, not strategy. They never change what the
# screener detects or what would be executed — they only bucket records.
# ---------------------------------------------------------------------------

# A GOLD BUY at >= regime threshold is Tier A.
# A BUY within this many confidence points BELOW the threshold is Tier B.
TIER_B_MARGIN: float = 10.0

# A BUY at least this many points below threshold (but still a BUY signal) is
# Tier C ("weak / rejected but tracked").
# Anything between TIER_B and TIER_C lower edge is Tier C too; D is "no signal".
TIER_C_MIN_CONFIDENCE: float = 1.0  # any positive-confidence BUY signal qualifies as >= C

VALID_TIERS = ("A", "B", "C", "D")


def _is_buy_signal(signal: Any) -> bool:
    """True if the screener signal string represents some kind of BUY setup."""
    if not signal:
        return False
    s = str(signal).upper()
    return "BUY" in s


def _is_gold(signal: Any) -> bool:
    s = str(signal or "").upper()
    return "GOLD" in s and "BUY" in s


def classify_tier(
    signal: Any,
    confidence: Optional[float],
    regime_threshold: float,
) -> str:
    """
    Classify a single setup into tier A/B/C/D.

    Args:
        signal: screener signal string (e.g. "GOLD BUY (BREAKOUT)", "SILVER BUY",
                "NO SETUP", or "" / None).
        confidence: 0-100 confidence score from the screener (None -> treated 0).
        regime_threshold: 0-100 confidence threshold for the active regime
                          (e.g. BULL 60 / NEUTRAL 70 / BEAR 80). This is read
                          from the existing regime rules; it is NOT a new knob.

    Returns:
        One of "A", "B", "C", "D".

    Rules (deterministic):
        - No BUY signal at all                                  -> D
        - GOLD BUY,   confidence >= threshold                   -> A
        - BUY (gold/silver), threshold-MARGIN <= conf < thresh  -> B
        - any other BUY with confidence > 0                      -> C
        - degenerate (BUY but confidence <= 0)                  -> D
    """
    conf = float(confidence) if confidence is not None else 0.0
    thr = float(regime_threshold)

    if not _is_buy_signal(signal):
        return "D"

    if conf <= 0.0:
        return "D"

    if _is_gold(signal) and conf >= thr:
        return "A"

    if conf >= (thr - TIER_B_MARGIN):
        # Close to qualifying — one confirmation short.
        return "B"

    if conf >= TIER_C_MIN_CONFIDENCE:
        return "C"

    return "D"


def tier_would_execute(tier: str) -> bool:
    """Only Tier A is the 'would-execute' tier in NOOP (recorded, never sent)."""
    return tier == "A"


def build_decision_record(
    setup: dict,
    *,
    market: str,
    mode: str,
    regime: str,
    regime_threshold: float,
    state_id: Optional[int],
    decided_at: str,
    review_at: str,
    cycle_id: Optional[str] = None,
) -> dict:
    """
    Build a structured, falsifiable NOOP decision record from a screener setup.

    The record is deterministic given identical inputs (no clock reads here —
    timestamps are passed in). This is what makes decision records reproducible
    and testable.

    Returns a flat dict matching the decision_journal schema (see noop_journal).
    Outcome fields are left empty/NULL — the resolver fills them later.
    """
    signal = setup.get("signal")
    confidence = setup.get("confidence")
    tier = classify_tier(signal, confidence, regime_threshold)

    entry = setup.get("entry", setup.get("price"))
    stop_loss = setup.get("stop_loss")
    tp1 = setup.get("tp1")
    tp2 = setup.get("tp2")
    tp3 = setup.get("tp3")

    # ----- Falsifiable prediction fields -----
    # Expected scenario: for a BUY, we expect price to reach TP1 before SL within
    # the review horizon. For D (no setup) we expect "no qualifying move".
    if tier in ("A", "B", "C"):
        expected_scenario = (
            f"Long setup: expect price to reach TP1 "
            f"({tp1}) before stop ({stop_loss}) by review."
        )
        invalidation = f"Close at/below stop {stop_loss} before TP1."
        proves_wrong = (
            f"Price hits stop {stop_loss} first, OR fails to make progress "
            f"toward TP1 ({tp1}) by review date."
        )
    else:
        expected_scenario = "No qualifying setup; expect no actionable move."
        invalidation = "A clear qualifying breakout that we failed to flag."
        proves_wrong = "A strong qualifying move occurs that we did not record."

    reasoning = setup.get("reasoning") or setup.get("q_reasoning") or ""

    return {
        "cycle_id": cycle_id or "",
        "decided_at": decided_at,
        "review_at": review_at,
        "market": market,
        "mode": mode,
        "ticker": setup.get("ticker"),
        "name": setup.get("name"),
        "sector": setup.get("sector"),
        "tier": tier,
        "would_execute": 1 if tier_would_execute(tier) else 0,
        "signal": str(signal) if signal is not None else "",
        "confidence": float(confidence) if confidence is not None else 0.0,
        "regime": regime,
        "regime_threshold": float(regime_threshold),
        "state_id": int(state_id) if state_id is not None else None,
        "entry": float(entry) if entry is not None else None,
        "stop_loss": float(stop_loss) if stop_loss is not None else None,
        "tp1": float(tp1) if tp1 is not None else None,
        "tp2": float(tp2) if tp2 is not None else None,
        "tp3": float(tp3) if tp3 is not None else None,
        "rsi": setup.get("rsi"),
        "vol_ratio": setup.get("vol_ratio"),
        "atr": setup.get("atr"),
        "reasoning": reasoning,
        "expected_scenario": expected_scenario,
        "invalidation_condition": invalidation,
        "what_proves_wrong": proves_wrong,
        # Outcome fields — filled by the resolver, NULL/empty until then.
        "status": "OPEN",
        "outcome": None,            # WIN / LOSS / FLAT / UNKNOWN
        "outcome_r": None,          # realized R-multiple of the shadow trade
        "max_favorable_pct": None,
        "max_adverse_pct": None,
        "resolved_at": None,
        "resolver_notes": None,
    }
