"""Deep diagnostic — why no ORB signal fired today."""
from datetime import datetime, date, time as dtime, timedelta
from zoneinfo import ZoneInfo
import data_provider as dp
from intraday_backtest import (
    compute_session_vwap,
    compute_session_relative_volume,
    compute_opening_range,
    ORBConfig,
)
from intraday_backtest_v2 import compute_daily_ema_trend

US_ET = ZoneInfo("America/New_York")
cfg = ORBConfig(
    interval="5m",
    opening_range_minutes=15,
    target_r_multiple=2.0,
    rel_vol_threshold=1.2,
    require_vwap_support=True,
    session_open=dtime(9, 30),
    session_close=dtime(16, 0),
    flat_by=dtime(15, 55),
)

tickers = ["TNA", "GOOGL", "TQQQ", "SOXL", "PLTR"]  # MSTR excluded (DOWN trend)

print("=" * 60)
print("Intraday Deep Diagnostic — Why No Signal?")
print(f"Time now (ET): {datetime.now(US_ET).strftime('%H:%M')}")
print("=" * 60)

for tk in tickers:
    print(f"\n--- {tk} ---")
    dp.reset()
    try:
        df = dp.get_history(tk, interval="5m", period="5d", timeout=30)
    except Exception as e:
        print(f"  FETCH ERROR: {e}")
        continue

    if df is None or df.empty:
        print("  NO 5m DATA")
        continue

    # Filter to today's RTH only
    today = date.today()
    try:
        rth = df.between_time(dtime(9, 30), dtime(15, 55), inclusive="left")
        rth = rth[rth.index.normalize() == rth.index.normalize().max()]
    except Exception as e:
        print(f"  FILTER ERROR: {e}")
        continue

    if rth.empty:
        print("  No RTH bars for today yet")
        continue

    print(f"  Bars today: {len(rth)}")

    # Opening range
    or_high, or_low = compute_opening_range(rth, dtime(9, 30), 15)
    if or_high is None:
        print("  OR not established yet (need 3+ bars in 09:30-09:45)")
        continue
    or_range = or_high - or_low
    print(f"  OR high={or_high:.3f}  OR low={or_low:.3f}  range={or_range:.3f}")

    # Latest price
    last = rth.iloc[-1]
    last_close = float(last["Close"])
    last_time = rth.index[-1].strftime("%H:%M")
    print(f"  Latest bar: {last_time} ET  close={last_close:.3f}")

    # Breakout check
    above_or = last_close > or_high
    print(f"  Close > OR_high? {'✅ YES' if above_or else '❌ NO  ← not broken out yet'}")

    if not above_or:
        continue

    # VWAP
    vwap_s = compute_session_vwap(rth)
    vwap = float(vwap_s.iloc[-1])
    above_vwap = last_close > vwap
    print(f"  VWAP={vwap:.3f}  Close > VWAP? {'✅ YES' if above_vwap else '❌ NO  ← below VWAP'}")

    # Rel-vol
    rvol_s = compute_session_relative_volume(rth)
    rvol = float(rvol_s.iloc[-1])
    vol_ok = rvol >= cfg.rel_vol_threshold
    print(f"  Rel-vol={rvol:.2f}x  >= 1.2x? {'✅ YES' if vol_ok else f'❌ NO  ← only {rvol:.2f}x'}")

    if above_or and above_vwap and vol_ok:
        print(f"  🚀 SIGNAL SHOULD FIRE — all conditions met!")
    else:
        print(f"  No signal — conditions not fully met")

print("\n" + "=" * 60)
print("Summary: if no ticker shows '🚀 SIGNAL SHOULD FIRE'")
print("→ Market is moving sideways / no clean breakout today")
print("→ This is normal — ORB only fires on strong breakout days")
print("=" * 60)
