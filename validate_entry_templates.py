#!/usr/bin/env python3
"""
validate_entry_templates.py -- v3.7

Brain health validation script. Checks:
  1. All closed trades have valid entry > SL
  2. All closed trades have TP1 > entry  
  3. All closed trades have actual_risk_pct in [0.5%, 30%]
  4. All closed trades have risk_per_share > $0.01
  5. State priors are within valid state_id range (0-26)

Usage:
  python validate_entry_templates.py

Exit codes:
  0 = all checks pass
  1 = one or more failures
"""
from __future__ import annotations
import sys
sys.path.insert(0, '/home/user/bursa_agent')

from db import connect


def check_trades() -> list[str]:
    errors = []
    with connect() as c:
        rows = c.execute(
            """SELECT id, ticker, entry_price, stop_loss, tp1,
                      actual_risk_pct, risk_per_share, outcome, exit_type
               FROM trades WHERE status = 'CLOSED'"""
        ).fetchall()
    
    if not rows:
        print("  No closed trades yet -- skipping trade checks")
        return errors

    print(f"  Checking {len(rows)} closed trades...")
    for row in rows:
        tid, ticker, entry_price, stop_loss, tp1, actual_risk_pct, risk_per_share, outcome, exit_type = row
        if entry_price is None or stop_loss is None:
            errors.append(f"Trade #{tid} ({ticker}): entry_price or stop_loss is None")
            continue
        if not (entry_price > stop_loss):
            errors.append(f"Trade #{tid} ({ticker}): entry ({entry_price}) must be > SL ({stop_loss})")
        if tp1 is not None and not (tp1 > entry_price):
            errors.append(f"Trade #{tid} ({ticker}): tp1 ({tp1}) must be > entry ({entry_price})")
        if actual_risk_pct is not None:
            if not (0.005 <= actual_risk_pct <= 0.30):
                errors.append(f"Trade #{tid} ({ticker}): actual_risk_pct {actual_risk_pct*100:.2f}% outside [0.5%, 30%]")
        if risk_per_share is not None and risk_per_share <= 0.01:
            errors.append(f"Trade #{tid} ({ticker}): risk_per_share {risk_per_share:.4f} <= $0.01")
        # v3.7: exit_type should be populated for new trades
        if exit_type is None and outcome in ("WIN", "LOSS"):
            errors.append(f"Trade #{tid} ({ticker}): exit_type is None (old trade, normal if pre-v3.7)")

    return errors


def check_state_priors() -> list[str]:
    errors = []
    with connect() as c:
        rows = c.execute("SELECT state_id, action, alpha, beta, n_trades FROM state_priors").fetchall()
    
    if not rows:
        print("  No state priors yet -- skipping")
        return errors

    print(f"  Checking {len(rows)} state priors...")
    for row in rows:
        state_id, action, alpha, beta, n_trades = row
        if state_id < 0 or state_id > 26:
            errors.append(f"state_priors: state_id {state_id} out of range [0-26]")
        if alpha < 0 or beta < 0:
            errors.append(f"state_priors (state={state_id}, action={action}): alpha or beta negative")
        if n_trades < 0:
            errors.append(f"state_priors (state={state_id}, action={action}): n_trades negative")

    return errors


def main() -> int:
    print("=" * 60)
    print("Bursa Agent -- Brain Health Validation (v3.7)")
    print("=" * 60)

    all_errors = []

    print("\n[Trade Data]")
    errs = check_trades()
    for e in errs:
        print(f"  FAIL: {e}")
    all_errors.extend(errs)

    print("\n[State Priors]")
    errs = check_state_priors()
    for e in errs:
        print(f"  FAIL: {e}")
    all_errors.extend(errs)

    print("\n" + "=" * 60)
    if all_errors:
        print(f"RESULT: {len(all_errors)} FAILURES")
        for e in all_errors:
            print(f"  - {e}")
        return 1
    else:
        print("RESULT: ALL checks PASSED")
        return 0


if __name__ == "__main__":
    sys.exit(main())
