#!/usr/bin/env python3
"""
verify_moomoo.py — Standalone diagnostic for Moomoo OpenD setup.

Run this on your local PC AFTER:
  1. Moomoo OpenD is installed
  2. OpenD is launched and logged in
  3. You've run `pip install -r requirements.txt` (which now includes moomoo-api)

Usage:
    python verify_moomoo.py

It will:
  - Check if anything is listening on 127.0.0.1:11111 (port pre-check)
  - Verify the moomoo-api Python package is installed
  - Connect to OpenD and call get_global_state() to confirm liveness
  - Fetch 1y of daily bars for 0166.KL (Inari) via data_provider.get_history()
  - Compare against yfinance for the same ticker — should match (within rounding)
  - Print a clean PASS/FAIL summary you can paste back

If anything fails, the script prints exactly what's wrong and how to fix it.
"""

import sys
import socket
import time
from datetime import datetime


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
BOLD = "\033[1m"
RESET = "\033[0m"

# On Windows cmd, ANSI may not render — fall back to plain text.
if sys.platform == "win32":
    try:
        import colorama  # type: ignore
        colorama.init()
    except ImportError:
        GREEN = RED = YELLOW = BLUE = BOLD = RESET = ""


def banner(text: str) -> None:
    print()
    print(f"{BOLD}{BLUE}{'=' * 70}{RESET}")
    print(f"{BOLD}{BLUE}{text}{RESET}")
    print(f"{BOLD}{BLUE}{'=' * 70}{RESET}")


def ok(text: str) -> None:
    print(f"  {GREEN}✅ {text}{RESET}")


def fail(text: str) -> None:
    print(f"  {RED}❌ {text}{RESET}")


def warn(text: str) -> None:
    print(f"  {YELLOW}⚠️  {text}{RESET}")


def info(text: str) -> None:
    print(f"     {text}")


# ---------------------------------------------------------------------------
# Test 1: TCP port probe
# ---------------------------------------------------------------------------

def test_port_open(host: str = "127.0.0.1", port: int = 11111) -> bool:
    banner(f"TEST 1 — Is OpenD listening on {host}:{port}?")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect((host, port))
        ok(f"Port {port} is OPEN — something is listening")
        return True
    except ConnectionRefusedError:
        fail(f"Port {port} is CLOSED — nothing is listening")
        info("Fix: Launch Moomoo OpenD and make sure it's logged in.")
        info("     OpenD must be running for the agent to use real-time data.")
        return False
    except socket.timeout:
        fail(f"Port {port} timed out — firewall blocking?")
        info("Fix: Check Windows Firewall / Mac firewall allows OpenD on port 11111.")
        return False
    except Exception as e:
        fail(f"Unexpected error: {e}")
        return False
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Test 2: moomoo-api package
# ---------------------------------------------------------------------------

def test_package_installed() -> bool:
    banner("TEST 2 — Is the moomoo-api Python package installed?")
    try:
        import moomoo  # noqa: F401
        ok(f"moomoo-api is installed (version: {getattr(moomoo, '__version__', 'unknown')})")
        return True
    except ImportError:
        fail("moomoo-api is NOT installed")
        info("Fix: pip install moomoo-api>=8.0.0")
        info("     (Or: pip install -r requirements.txt from the repo root)")
        return False


# ---------------------------------------------------------------------------
# Test 3: OpenD liveness via SDK
# ---------------------------------------------------------------------------

def test_opend_liveness() -> bool:
    banner("TEST 3 — Can the moomoo SDK actually talk to OpenD?")
    try:
        from moomoo import OpenQuoteContext
    except ImportError:
        fail("Can't import OpenQuoteContext — package broken?")
        return False

    try:
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        ret, data = ctx.get_global_state()
        if ret == 0:  # RET_OK
            ok("OpenD responded to get_global_state()")
            info(f"     market_my: {data.get('market_my', 'unknown')}")
            info(f"     server_ver: {data.get('server_ver', 'unknown')}")
            info(f"     trd_logined: {data.get('trd_logined', 'unknown')}")
            ctx.close()
            return True
        else:
            fail(f"OpenD returned error: ret={ret}, data={data}")
            ctx.close()
            return False
    except Exception as e:
        fail(f"SDK call failed: {e}")
        info("Fix: OpenD may not be fully logged in yet. Check the OpenD window.")
        return False


# ---------------------------------------------------------------------------
# Test 4: data_provider end-to-end
# ---------------------------------------------------------------------------

def test_data_provider() -> bool:
    banner("TEST 4 — End-to-end via data_provider.get_history()")
    try:
        # Reset provider state so we get a fresh probe
        import data_provider as dp
        dp.reset()

        # Fetch Inari 1y
        ticker = "0166.KL"
        info(f"Fetching 1y of daily bars for {ticker}...")
        t0 = time.time()
        df = dp.get_history(ticker, period="1y", timeout=15)
        elapsed = time.time() - t0

        if df is None or df.empty:
            fail(f"data_provider returned empty for {ticker}")
            return False

        served_by = dp.provider_name()
        ok(f"Fetched {len(df)} rows in {elapsed:.2f}s — served by: {BOLD}{served_by}{RESET}")
        info(f"     Date range: {df.index.min().date()} → {df.index.max().date()}")
        info(f"     Last close: RM {df['Close'].iloc[-1]:.3f}")
        info(f"     Last volume: {int(df['Volume'].iloc[-1]):,}")

        if served_by == "moomoo":
            ok("🎉 Real-time Moomoo data is FLOWING")
        elif served_by == "yfinance":
            warn("Fell back to yfinance — Moomoo path failed somehow")
            info(f"     Check data_provider.health(): {dp.health()}")
            return False
        else:
            warn(f"Unexpected provider name: {served_by}")
        return True

    except ImportError as e:
        fail(f"Can't import data_provider: {e}")
        info("Fix: Run this script from the repo root directory.")
        return False
    except Exception as e:
        fail(f"data_provider call failed: {e}")
        import traceback
        traceback.print_exc()
        return False


# ---------------------------------------------------------------------------
# Test 5: Cross-check vs yfinance
# ---------------------------------------------------------------------------

def test_cross_check_yfinance() -> bool:
    banner("TEST 5 — Cross-check: Moomoo last close vs yfinance last close")
    try:
        import yfinance as yf
        import data_provider as dp

        ticker = "0166.KL"
        dp.reset()
        df_moomoo = dp.get_history(ticker, period="5d", timeout=15)
        df_yf = yf.Ticker(ticker).history(period="5d", timeout=15)

        if df_moomoo.empty or df_yf.empty:
            warn("One of the providers returned empty — can't cross-check")
            return False

        m_close = float(df_moomoo['Close'].iloc[-1])
        y_close = float(df_yf['Close'].iloc[-1])
        m_date = df_moomoo.index[-1].date()
        y_date = df_yf.index[-1].date()

        info(f"Moomoo  last bar: {m_date}  close=RM {m_close:.3f}")
        info(f"yfinance last bar: {y_date}  close=RM {y_close:.3f}")

        # Allow up to 2% deviation (Moomoo may be more current than yfinance)
        pct_diff = abs(m_close - y_close) / y_close * 100
        if pct_diff < 0.5:
            ok(f"Closes match within {pct_diff:.2f}% — providers agree ✅")
        elif pct_diff < 2.0:
            warn(f"Closes differ by {pct_diff:.2f}% — likely Moomoo is more recent")
            info("     (Moomoo intraday may show current price, yfinance shows yesterday)")
        else:
            warn(f"Closes differ by {pct_diff:.2f}% — investigate before relying on this")
            return False
        return True

    except Exception as e:
        warn(f"Cross-check failed (non-critical): {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print()
    print(f"{BOLD}Moomoo OpenD diagnostic for BursaAI v3.4{RESET}")
    print(f"Run at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Platform: {sys.platform}")

    results = []
    results.append(("Port 11111 open", test_port_open()))
    results.append(("moomoo-api installed", test_package_installed()))

    # Only proceed with SDK tests if the first two pass
    if all(r[1] for r in results):
        results.append(("OpenD liveness", test_opend_liveness()))
        results.append(("data_provider end-to-end", test_data_provider()))
        results.append(("yfinance cross-check", test_cross_check_yfinance()))

    # Summary
    banner("SUMMARY")
    for name, passed in results:
        marker = f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"
        print(f"  [{marker}]  {name}")

    all_critical_passed = all(r[1] for r in results[:4])  # cross-check is non-critical
    print()
    if all_critical_passed:
        print(f"{GREEN}{BOLD}🎉 All checks passed. Your agent will use Moomoo real-time data when run locally.{RESET}")
        print()
        print("Next steps:")
        print("  1. Keep OpenD running in the background")
        print("  2. Run the agent: streamlit run app.py")
        print("  3. Go to Settings tab → 📡 Data Source — should show 'Active: Moomoo'")
        return 0
    else:
        print(f"{RED}{BOLD}❌ Setup is incomplete. See messages above for the fix.{RESET}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
