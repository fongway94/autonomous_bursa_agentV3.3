#!/usr/bin/env python3
"""
Professional Swing Trader — Reset US SWING DB for fresh brain training
Use this when you want to start paper trading US from scratch.

What it does:
- Activates US market + SWING mode
- Backs up current US_SWING DB to timestamped file (safety)
- Deletes all trades, logs, state_priors, bias, etc. but keeps schema
- Resets account to $5k default
- Resets scheduler_state
- Keeps Gist backup intact (old data still in Gist history)

Usage:
    python reset_us_swing.py              # interactive confirm
    python reset_us_swing.py --yes        # auto confirm
    python reset_us_swing.py --backup-only  # just backup, don't reset

After reset:
- Start Robo-Trader in UI
- It will be in EXPLORE mode 0/50 trades
- Every closed trade teaches the brain
"""

import os, sys, shutil, argparse
from pathlib import Path
from datetime import datetime

# Force US SWING
os.environ["MARKET_MODE"] = "US"
os.environ["TRADING_MODE"] = "SWING"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="skip confirmation")
    ap.add_argument("--backup-only", action="store_true", help="only backup, don't reset")
    args = ap.parse_args()

    # Import after env set
    import market_profiles
    market_profiles.reset_cache()
    market_profiles.reset_trading_mode_cache()
    from db import DATA_DIR, init_db, current_db_path, connect
    from repository import save_account, save_bias_state, save_parameters
    from market_profiles import active_profile

    init_db()
    db_path = current_db_path()
    print(f"Active DB: {db_path}")
    print(f"Profile: {active_profile().display_name} {active_profile().code} SWING")
    print(f"Default capital: {active_profile().default_capital} {active_profile().currency_symbol}")

    if not Path(db_path).exists():
        print("DB does not exist yet — will be created on next app run. Nothing to reset.")
        return

    # Backup
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path(DATA_DIR) / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"bursa_agent_US_SWING_backup_{ts}.db"
    try:
        shutil.copy2(db_path, backup_path)
        # Also copy wal/shm if exist
        for suffix in ("-wal","-shm"):
            src = Path(str(db_path)+suffix)
            if src.exists():
                shutil.copy2(src, backup_dir / f"{backup_path.name}{suffix}")
        print(f"✅ Backup saved to {backup_path}")
    except Exception as e:
        print(f"⚠️ Backup failed: {e}")

    if args.backup_only:
        print("Backup-only mode — not resetting.")
        return

    if not args.yes:
        print("\nThis will DELETE all US SWING trades, brain priors, logs, and reset account to $5k.")
        print("MY market and US INTRADAY are NOT touched (separate DB files).")
        resp = input("Type YES to confirm: ").strip()
        if resp != "YES":
            print("Aborted.")
            return

    # Reset
    with connect() as c:
        for tbl in ("trades","partial_exits","trade_log","scheduler_log","learning_events",
                    "parameter_history","bias_history","state_priors","data_quality_log",
                    "scan_cache","alert_log","maintenance_state","regime_history","meta",
                    "custom_watchlist","corporate_actions_processed","decision_journal"):
            try:
                c.execute(f"DELETE FROM {tbl}")
            except Exception as e:
                print(f"skip {tbl}: {e}")
        # Reset scheduler_state
        c.execute(
            "UPDATE scheduler_state SET running=0, interval_sec=3600, last_run_at=NULL, "
            "next_run_at=NULL, last_heartbeat=NULL, consecutive_failures=0, last_error=NULL, "
            "autotrade_enabled=1, autoexit_enabled=1, kill_switch=0, exploration_mode=1, "
            "exploration_trades_target=50, owner_pid=0, cycle_started_at=NULL, "
            "corp_action_autoadjust=1, last_corp_action_scan_at=NULL, broker_mode='NOOP' "
            "WHERE id=1"
        )
        c.execute(
            "UPDATE live_trigger_config SET enabled=0, min_confidence=60.0, exploit_mode_only=0, "
            "alert_on_entry=1, alert_on_full_exit=1, alert_on_stop_loss=1, alert_on_trailing_stop=1, "
            "alert_on_partial_exit=0, alert_on_risk_rejected=0, telegram_enabled=1, email_enabled=0, "
            "email_recipients='', actor_filter='AGENT' WHERE id=1"
        )

    save_account(initial_capital=5000.0, cash_balance=5000.0, total_equity=5000.0)
    save_bias_state({"breakout_bias":1.0,"pullback_bias":1.0,"sector_biases":{},"strategy_stats":{},"sector_stats":{},"total_closed_trades":0,"system_win_rate":0.5})
    save_parameters({
        "ema_trend":200,"ema_fast":10,"ema_slow":20,
        "rsi_oversold_pullback":40.0,"rsi_overbought":70.0,
        "volume_surge_ratio":1.5,"breakout_period":20,
        "atr_period":14,"atr_multiplier_stop":1.5,
        "min_price":5.0,"max_price":500.0,
    }, source="RESET_US", reason="fresh US SWING start")

    print("✅ US SWING DB reset complete — fresh $5k paper account, EXPLORE 0/50.")
    print("Next: Start Streamlit app_us.py and Robo-Trader will begin training.")

if __name__ == "__main__":
    main()
