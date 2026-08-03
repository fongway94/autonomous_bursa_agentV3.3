#!/usr/bin/env python3
"""
Headless trading cycle — runs the brain WITHOUT Streamlit.

Why this exists
---------------
Until now the scheduler thread lived inside `app.py`, i.e. inside the Streamlit
web process. That coupled "does the agent think?" to "is the web UI awake?":

  * Streamlit Community Cloud hibernates any app with no traffic for 12 hours.
    A sleeping app runs NO Python, so no scan, no exit check, no learning.
  * Every redeploy/reboot recycles the container mid-cycle.
  * Cloud egress is a SHARED IP, which is a major contributor to the Yahoo
    throttling that silently truncates scans.
  * Free tier has no SLA. Trading decisions were riding on it.

This script decouples them. One invocation is a complete, self-contained cycle:

    restore brain from Gist  ->  run one cycle  ->  back up brain to Gist

Run it from GitHub Actions cron (see .github/workflows/trading-cycle.yml, which
ships as scripts/trading-cycle.workflow.yml), from a VPS crontab, or by hand.
Streamlit then becomes a pure READ-ONLY viewer of the same Gist-backed DB, and
it no longer matters whether it is awake.

The brain's state lives in the Gist, not in any container. That is what makes
this safe to run from ephemeral CI runners.

Usage
-----
    GITHUB_TOKEN=ghp_...  MARKET_MODE=MY  python run_cycle.py
    python run_cycle.py --market MY --mode SWING
    python run_cycle.py --dry-run          # restore + report, no trading
    python run_cycle.py --skip-restore     # local runs with an existing DB

Env / secrets
-------------
    GITHUB_TOKEN   (required for restore/backup — classic PAT, `gist` scope)
    GIST_ID        (optional; also discoverable from the DB meta table)
    MARKET_MODE    MY | US            (default MY)
    TRADING_MODE   SWING | INTRADAY   (default SWING)

Exit codes
----------
    0  cycle completed (possibly with a degraded-data warning)
    1  cycle failed — CI turns red and you get an email
    2  refused to run: market closed / holiday (not an error)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone, timedelta

MYT = timezone(timedelta(hours=8))


def _log(msg: str) -> None:
    ts = datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts} MYT] {msg}", flush=True)


def _section(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one headless trading cycle.")
    ap.add_argument("--market", choices=["MY", "US"],
                    help="Override MARKET_MODE.")
    ap.add_argument("--mode", choices=["SWING", "INTRADAY"],
                    help="Override TRADING_MODE.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Restore and report state, but run no cycle.")
    ap.add_argument("--skip-restore", action="store_true",
                    help="Use the local DB as-is (no Gist download).")
    ap.add_argument("--skip-backup", action="store_true",
                    help="Do not upload the DB afterwards.")
    ap.add_argument("--force", action="store_true",
                    help="Run even when the market is closed.")
    args = ap.parse_args()

    # Must be set BEFORE importing db/market_profiles — the DB path and the
    # active profile are both resolved from these at import time.
    if args.market:
        os.environ["MARKET_MODE"] = args.market
    if args.mode:
        os.environ["TRADING_MODE"] = args.mode

    market = os.environ.get("MARKET_MODE", "MY")
    mode = os.environ.get("TRADING_MODE", "SWING")

    _section(f"BursaAI headless cycle — {market} {mode}")
    _log(f"python {sys.version.split()[0]}")

    # ---------------------------------------------------------------- restore
    if not args.skip_restore:
        _section("1/4  Restore brain from Gist")
        try:
            import persistence
            if not persistence.is_configured():
                _log("ERROR: GITHUB_TOKEN not set — refusing to run.")
                _log("  Without it the brain cannot be restored, and this "
                     "runner would start from an EMPTY database, then back "
                     "that emptiness up over your real one.")
                return 1

            res = persistence.restore()
            if res.get("ok"):
                _log(f"OK restored {res.get('bytes_restored', 0):,} bytes "
                     f"(gist {res.get('gist_id')})")
            else:
                reason = str(res.get("reason", ""))
                low = reason.lower()
                # A genuine first run (no gist yet / gist has no file for this
                # market+mode) is legitimate. Anything else — HTTP 401, 404,
                # network — must NOT be swallowed: continuing would run on an
                # empty brain and then overwrite the good backup with it.
                first_run = ("first run" in low
                             or "no gist_id" in low
                             or "has no file" in low)
                if first_run:
                    _log(f"No existing backup ({reason}) — starting a fresh brain.")
                else:
                    _log(f"ERROR restore failed: {reason}")
                    _log("  Refusing to continue: running on an empty brain "
                         "would overwrite your real backup at the end of "
                         "this cycle.")
                    return 1
        except Exception as e:
            _log(f"ERROR restore raised: {type(e).__name__}: {e}")
            traceback.print_exc()
            return 1
    else:
        _section("1/4  Restore SKIPPED (--skip-restore)")

    # ------------------------------------------------------------ pre-flight
    _section("2/4  Pre-flight")
    try:
        from db import current_db_path
        from repository import active_trades, closed_trades

        _log(f"DB: {current_db_path()}")
        n_active = len(active_trades())
        n_closed = len(closed_trades())
        _log(f"positions: {n_active} active / {n_closed} closed")

        try:
            from db import connect
            with connect(readonly=True) as c:
                priors = c.execute("SELECT COUNT(*) FROM state_priors").fetchone()[0]
            _log(f"brain: {priors} state priors")
        except Exception:
            pass
    except Exception as e:
        _log(f"ERROR pre-flight failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1

    # Market-hours check. A closed market is not a failure, so exit 2 keeps CI
    # green while still being distinguishable in the logs.
    if not args.force and not args.dry_run:
        try:
            from market_calendar import is_trading_day

            # Use the EXCHANGE's local date, not MYT — for US, MYT is 12-13h
            # ahead, so a MYT date can be the wrong trading day entirely.
            try:
                from market_profiles import active_profile
                today = datetime.now(active_profile().timezone).date()
            except Exception:
                today = datetime.now(MYT).date()

            if not is_trading_day(today):
                _log(f"{today} is not a trading day for {market} — nothing to do.")
                return 2
        except Exception as e:
            _log(f"warn: trading-day check unavailable ({e}) — continuing.")

    if args.dry_run:
        _section("3/4  Cycle SKIPPED (--dry-run)")
        _log("Dry run complete; brain restored and inspected, nothing traded.")
        return 0

    # ------------------------------------------------------------------ cycle
    _section("3/4  Run cycle")
    summary: dict = {}
    cycle_failed = False
    try:
        import scheduler
        summary = scheduler.run_once() or {}

        _log("summary: " + json.dumps(summary, default=str)[:900])

        if summary.get("degraded"):
            _log(f"WARNING data degraded — coverage "
                 f"{summary.get('coverage', 0) * 100:.0f}%. Auto-entry was "
                 f"suppressed; exits still ran.")
        else:
            _log(f"scanned={summary.get('scan_count', 0)} "
                 f"settled={summary.get('settled', 0)} "
                 f"entries={summary.get('auto_entries', 0)}")
    except Exception as e:
        cycle_failed = True
        _log(f"ERROR cycle raised: {type(e).__name__}: {e}")
        traceback.print_exc()
        # Deliberately fall through to the backup step: a crash mid-cycle may
        # still have closed trades and updated priors, and losing that is worse
        # than persisting a partially-complete cycle.

    # ----------------------------------------------------------------- backup
    if not args.skip_backup:
        _section("4/4  Back up brain to Gist")
        try:
            import persistence
            res = persistence.backup(
                force=True,
                reason=f"headless cycle {market}/{mode}"
                       f"{' (after error)' if cycle_failed else ''}",
            )
            if res.get("ok"):
                _log(f"OK backed up {res.get('size_kb', 0)} KB "
                     f"(gist {res.get('gist_id')})")
            else:
                _log(f"ERROR backup failed: {res.get('reason')}")
                # A lost backup on an ephemeral runner = lost learning.
                return 1
        except Exception as e:
            _log(f"ERROR backup raised: {type(e).__name__}: {e}")
            traceback.print_exc()
            return 1
    else:
        _section("4/4  Backup SKIPPED (--skip-backup)")

    if cycle_failed:
        _log("FAILED — cycle errored (state was still persisted).")
        return 1

    _log("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
