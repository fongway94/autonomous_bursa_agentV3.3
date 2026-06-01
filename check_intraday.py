"""Quick diagnostic — check EMA-200 trend filter for intraday curated-6."""
from datetime import date
import data_provider as dp
from intraday_backtest_v2 import compute_daily_ema_trend

tickers = ["TNA", "GOOGL", "TQQQ", "MSTR", "SOXL", "PLTR"]

print("=" * 45)
print("Intraday EMA-200 Trend Filter Check")
print("=" * 45)

blocked = []
for tk in tickers:
    dp.reset()
    df = dp.get_history(tk, period="1y")
    if df is None or df.empty:
        print(f"{tk:6s}  NO DATA")
        continue
    trend = compute_daily_ema_trend(df, ema_len=200)
    today = date.today()
    is_up = bool(trend.get(today, False))
    status = "UP  ✅  (longs allowed)" if is_up else "DOWN ❌  (longs BLOCKED)"
    print(f"  {tk:6s}  EMA-200 trend: {status}")
    if not is_up:
        blocked.append(tk)

print()
if blocked:
    print(f"BLOCKED by EMA-200: {', '.join(blocked)}")
    print("→ These tickers will NOT fire intraday signals today.")
else:
    print("All 6 tickers trend UP — EMA-200 filter is NOT the blocker.")
    print("→ Check VWAP / rel-vol / OR breakout conditions.")
print("=" * 45)
