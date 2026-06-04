"""
noop_safety.py — Central safety gates for the NOOP learning phase.

This module is the single source of truth for "what the agent is allowed to do"
during the NOOP (no-operation) learning phase. The whole point of NOOP is:

    Observe -> classify -> decide -> explain -> record -> track outcome
    WITHOUT placing real or paper trades, and WITHOUT auto-changing rules.

Everything here is conservative by default. Flags are read from the environment
so they can be flipped deliberately in a controlled deployment, but the *default*
in code is always the safe value. Nothing in this module ever raises on import.

Design rules:
  * Default = safe. NOOP is ON; live/paper execution is OFF; self-modification OFF.
  * Pure-Python, no heavy imports, no DB writes at import time.
  * One obvious place a reviewer can read to understand the safety posture.
"""

from __future__ import annotations

import os
from typing import Final


# ---------------------------------------------------------------------------
# Environment flag helpers
# ---------------------------------------------------------------------------

def _env_true(name: str, default: bool) -> bool:
    """Read a boolean-ish env var. Absent -> default. Safe parsing."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Phase constants
# ---------------------------------------------------------------------------

# The whole agent is in NOOP learning mode by default. This is intentionally
# *on* unless someone explicitly graduates the system. NOOP being ON means:
#   - decisions are recorded to the journal
#   - NO real or paper broker orders are placed
NOOP_MODE_DEFAULT: Final[bool] = True

# Paper trading is a SEPARATE, later phase. It must be explicitly enabled AND
# the system must have passed the paper-trading gate (a human decision). Default
# OFF. This flag alone does not enable live money — that is a further gate.
PAPER_TRADING_ENABLED_DEFAULT: Final[bool] = False

# Real-money execution is never enabled by this phase. Hard default OFF.
LIVE_TRADING_ENABLED_DEFAULT: Final[bool] = False

# Self-modification locks. During NOOP the brain may LEARN (observe/calibrate)
# but may NOT change trading rules, risk, or strategy automatically.
ALLOW_AUTO_RULE_CHANGES_DEFAULT: Final[bool] = False
ALLOW_AUTO_RISK_CHANGES_DEFAULT: Final[bool] = False
ALLOW_WALK_FORWARD_AUTORUN_DEFAULT: Final[bool] = False
ALLOW_PRIORS_DECAY_DEFAULT: Final[bool] = False


class NoopSafetyViolation(RuntimeError):
    """Raised when code attempts an action forbidden in the NOOP phase."""


# ---------------------------------------------------------------------------
# Public predicates
# ---------------------------------------------------------------------------

def noop_mode_active() -> bool:
    """True when the agent is in NOOP learning mode (default True)."""
    return _env_true("NOOP_MODE", NOOP_MODE_DEFAULT)


def paper_trading_enabled() -> bool:
    """
    True only if paper trading has been explicitly enabled. Even then, NOOP mode
    being active takes precedence: NOOP forbids paper orders. Both must agree to
    permit paper execution.
    """
    if noop_mode_active():
        return False
    return _env_true("PAPER_TRADING_ENABLED", PAPER_TRADING_ENABLED_DEFAULT)


def live_trading_enabled() -> bool:
    """True only if real-money trading has been explicitly enabled (never in NOOP)."""
    if noop_mode_active():
        return False
    return _env_true("LIVE_TRADING_ENABLED", LIVE_TRADING_ENABLED_DEFAULT)


def any_execution_allowed() -> bool:
    """True if ANY non-noop order placement (paper or live) is permitted."""
    return paper_trading_enabled() or live_trading_enabled()


def auto_rule_changes_allowed() -> bool:
    """May the system change entry/exit/strategy rules automatically? Default No."""
    if noop_mode_active():
        return False
    return _env_true("ALLOW_AUTO_RULE_CHANGES", ALLOW_AUTO_RULE_CHANGES_DEFAULT)


def auto_risk_changes_allowed() -> bool:
    """May the system change risk/sizing rules automatically? Default No."""
    if noop_mode_active():
        return False
    return _env_true("ALLOW_AUTO_RISK_CHANGES", ALLOW_AUTO_RISK_CHANGES_DEFAULT)


def walk_forward_autorun_allowed() -> bool:
    """May walk-forward optimization run automatically (it rewrites params)? Default No."""
    if noop_mode_active():
        return False
    return _env_true("ALLOW_WALK_FORWARD_AUTORUN", ALLOW_WALK_FORWARD_AUTORUN_DEFAULT)


def priors_decay_allowed() -> bool:
    """May nightly priors decay run automatically? Default No (already off in code)."""
    if noop_mode_active():
        return False
    return _env_true("ALLOW_PRIORS_DECAY", ALLOW_PRIORS_DECAY_DEFAULT)


# ---------------------------------------------------------------------------
# Hard assertions — call these right before a forbidden action would happen.
# ---------------------------------------------------------------------------

def assert_no_live_execution(context: str = "") -> None:
    """
    Guard the broker order path. Raises NoopSafetyViolation if any execution
    (paper or live) is attempted while NOOP forbids it.

    Call this immediately before any place_order / mirror_*_to_broker path that
    is NOT the NoopAdapter.
    """
    if not any_execution_allowed():
        where = f" [{context}]" if context else ""
        raise NoopSafetyViolation(
            f"Execution blocked in NOOP phase{where}: no real or paper orders "
            f"are permitted. (NOOP_MODE active or execution flags off.)"
        )


def assert_no_auto_rule_change(context: str = "") -> None:
    """Guard any code path that would mutate trading rules automatically."""
    if not auto_rule_changes_allowed():
        where = f" [{context}]" if context else ""
        raise NoopSafetyViolation(
            f"Automatic rule change blocked in NOOP phase{where}: rule changes "
            f"require explicit human approval."
        )


# ---------------------------------------------------------------------------
# Status snapshot (for dashboards / logs)
# ---------------------------------------------------------------------------

def safety_status() -> dict:
    """Human-readable snapshot of the current safety posture."""
    return {
        "noop_mode_active": noop_mode_active(),
        "paper_trading_enabled": paper_trading_enabled(),
        "live_trading_enabled": live_trading_enabled(),
        "any_execution_allowed": any_execution_allowed(),
        "auto_rule_changes_allowed": auto_rule_changes_allowed(),
        "auto_risk_changes_allowed": auto_risk_changes_allowed(),
        "walk_forward_autorun_allowed": walk_forward_autorun_allowed(),
        "priors_decay_allowed": priors_decay_allowed(),
    }
