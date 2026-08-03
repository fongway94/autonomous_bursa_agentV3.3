#!/usr/bin/env python3
"""
Quick trigger-frequency diagnostic.

Run locally (with working data_provider) to see how many triggers
would fire under strict vs loose filters over your last N days.

Usage:
    python trigger_diagnostic.py --days 30 --ticker SPY
    python trigger_diagnostic.py --days 60 --tickers TQQQ,QQQ,SPY
"""
from __future__ import annotations
import argparse
import sys
from collections import Counter

# Try to import; if missing, print clear error
try:
    from data_provider import get_history, health, ensure_probed
    from screener import screen_all_stocks, load_parameters
    from trigger_filter import strict_trigger_check
except Exception as e:
    print("Import error:", e)
    print("Fix: ensure dependencies installed (pandas, yfinance) and data_provider reachable.")
    sys.exit(1)


def main():
    p = argparse.ArgumentParser(description="Trigger frequency diagnostic")
    p.add_argument("--days", type=int, default=30, help="Lookback days")
    p.add_argument("--ticker", type=str, default="SPY", help="Ticker (or first of --tickers)")
    p.add_argument("--tickers", type=str, default=None, help="Comma-separated list")
    p.add_argument("--strict-confidence", type=float, default=80.0, help="Strict filter threshold")
    p.add_argument("--loose-confidence", type=float, default=70.0, help="Loose filter threshold")
    args = p.parse_args()

    tickers = args.tickers.split(",") if args.tickers else [args.ticker]
    print(f"Diagnostic for: {', '.join(tickers)}")
    print(f"Strict confidence threshold: {args.strict_confidence}")
    print(f"Loose confidence threshold:  {args.loose_confidence}")
    print()

    ensure_probed()
    h = health()
    print(f"Data source: {'Moomoo OpenD ✅' if h.get('moomoo_available') else 'yfinance fallback ⚠️'}")
    if not h.get("moomoo_available"):
        print("WARNING: No Moomoo OpenD. Results may differ from live environment.")
    print()

    params = load_parameters()
    loose_count = 0
    strict_count = 0
    blocked_reasons = Counter()

    for tk in tickers:
        try:
            # Pull enough data for the screener
            df = get_history(tk, period=f"{args.days}d", timeout=30)
            if df is None or df.empty:
                print(f"  {tk}: NO DATA — skip")
                continue
        except Exception as e:
            print(f"  {tk}: FETCH ERROR — {e}")
            continue

        # Run a simplified scan (use existing logic but just count)
        # For speed, we don't need full screen_all_stocks; we inspect the latest row
        try:
            from screener import compute_indicators, get_ticker_sector, get_ticker_name
            df_ind = compute_indicators(df, params)
            if df_ind.empty:
                continue
            last = df_ind.iloc[-1]
            # Build a synthetic setup similar to what analyze_stock_setup produces
            close = float(last["Close"])
            vol_ratio = float(last.get("Vol_Ratio", 0))
            rsi = float(last["RSI"])
            setup = {
                "signal": "GOLD BUY (BREAKOUT)",  # worst-case assumption for count
                "confidence": 75.0 + min((vol_ratio - 1.2) * 12, 15) + (10 if 50 < rsi < 65 else 0),
                "rsi": rsi,
                "vol_ratio": vol_ratio,
            }

            loose_ok, loose_r = strict_trigger_check(
                setup, brain_action="BUY", brain_buy_score=70, brain_avoid_score=20,
                regime="NEUTRAL", min_confidence=args.loose_confidence, max_rsi=70, min_vol_ratio=1.2
            )
            strict_ok, strict_r = strict_trigger_check(
                setup, brain_action="BUY", brain_buy_score=70, brain_avoid_score=20,
                regime="NEUTRAL", min_confidence=args.strict_confidence, max_rsi=70, min_vol_ratio=1.2
            )

            conf = setup["confidence"]
            print(f"  {tk}: conf={conf:.1f} RSI={rsi:.1f} vol={vol_ratio:.2f}x  loose={loose_r:>20} strict={strict_r:>25}  result={'TRIGGER' if strict_ok else ('TRIGGER(loose)' if loose_ok else 'NONE')}")

            if loose_ok:
                loose_count += 1
            if strict_ok:
                strict_count += 1
            else:
                blocked_reasons[strict_r] += 1

        except Exception as e:
            print(f"  {tk}: ANALYSIS ERROR — {e}")

    print()
    print(f"Summary over {len(tickers)} tickers (last {args.days} days):")
    print(f"  Loose filter  (conf >= {args.loose_confidence}): {loose_count} potential triggers")
    print(f"  Strict filter (conf >= {args.strict_confidence}): {strict_count} potential triggers")
    if blocked_reasons:
        print(f"  Blocked by strict filter: {dict(blocked_reasons)}")
    if strict_count == 0:
        print()
        print("WARNING: STRICT FILTER PRODUCES ZERO TRIGGERS.")
        print("Recommendation: Lower min_confidence to 75 (matches backtest base) or adjust RSI/volume thresholds.")
    elif strict_count < loose_count / 3:
        print()
        print("WARNING: STRICT FILTER SUPPRESSES >66% OF LOOSE TRIGGERS.")
        print("This may be too selective. Consider using 75 instead of 80, or relaxing RSI/volume if market is quiet.")


if __name__ == "__main__":
    main()
