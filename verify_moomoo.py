#!/usr/bin/env python3
"""
verify_moomoo.py — Standalone diagnostic for Moomoo OpenD setup.

Run this on your local PC AFTER:
  1. Moomoo OpenD is installed and launched
  2. You are logged in to OpenD (status shows "Connected")
  3. You've run `pip install -r requirements.txt` (includes moomoo-api)

Usage:
    python verify_moomoo.py

Three possible end states:
  1. ALL GREEN          → Moomoo path serves real-time Bursa data ✅
  2. FALLBACK WORKING   → Moomoo connects but can't serve Bursa data;
                          yfinance fallback handles everything correctly.
                          (Common: account lacks OpenAPI Bursa quote permission.)
  3. BROKEN             → Even yfinance fallback fails; agent will have
                          no data source.

States 1 and 2 are both "production usable". State 3 needs fixing.
"""

import sys
import socket
import time
import math
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

if sys.platform == "win32":
    try:
        import colorama  # type: ignore
        colorama.init()
    except ImportError:
        GREEN = RED = YELLOW = BLUE = BOLD = RESET = ""


def banner(text: str) -> None:
    print()
    print(f"{BOLD}{BLUE}{'=' * 72}{RESET}")
    print(f"{BOLD}{BLUE}{text}{RESET}")
    print(f"{BOLD}{BLUE}{'=' * 72}{RESET}")


def ok(text: str) -> None:
    print(f"  {GREEN}✅ {text}{RESET}")


def fail(text: str) -> None:
    print(f"  {RED}❌ {text}{RESET}")


def warn(text: str) -> None:
    print(f"  {YELLOW}⚠️  {text}{RESET}")


def info(text: str) -> None:
    print(f"     {text}")


def fmt_price(v) -> str:
    """NaN-safe price formatter — pandas returns NaN for in-progress bars."""
    try:
        if v is None:
            return "—"
        f = float(v)
        if math.isnan(f):
            return "— (in-progress bar)"
        return f"RM {f:.3f}"
    except (TypeError, ValueError):
        return f"{v!r}"


def fmt_volume(v) -> str:
    """NaN-safe volume formatter."""
    try:
        if v is None:
            return "—"
        f = float(v)
        if math.isnan(f):
            return "—"
        return f"{int(f):,}"
    except (TypeError, ValueError):
        return f"{v!r}"


# ---------------------------------------------------------------------------
# Tests 1-3 unchanged from v3.4
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
        return False
    except socket.timeout:
        fail(f"Port {port} timed out — firewall blocking?")
        return False
    except Exception as e:
        fail(f"Unexpected error: {e}")
        return False
    finally:
        sock.close()


def test_package_installed() -> bool:
    banner("TEST 2 — Is the moomoo-api Python package installed?")
    try:
        import moomoo  # noqa: F401
        ok(f"moomoo-api is installed (version: {getattr(moomoo, '__version__', 'unknown')})")
        return True
    except ImportError:
        fail("moomoo-api is NOT installed")
        info("Fix: pip install moomoo-api>=8.0.0")
        return False


def test_opend_liveness() -> bool:
    banner("TEST 3 — Can the moomoo SDK talk to OpenD?")
    try:
        from moomoo import OpenQuoteContext
    except ImportError:
        fail("Can't import OpenQuoteContext — package broken?")
        return False

    try:
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        ret, data = ctx.get_global_state()
        if ret == 0:
            ok("OpenD responded to get_global_state()")
            info(f"market_my:   {data.get('market_my', 'unknown')}")
            info(f"server_ver:  {data.get('server_ver', 'unknown')}")
            info(f"trd_logined: {data.get('trd_logined', 'unknown')}")
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
# Test 4 — REWRITTEN: distinguishes Moomoo-serves vs yfinance-fallback
# ---------------------------------------------------------------------------

def test_data_provider() -> str:
    """
    Returns one of:
      'moomoo'    — Moomoo served the data (best case)
      'fallback'  — Moomoo connected but failed; yfinance fallback worked
      'broken'    — Both providers failed; agent will have no data
    """
    banner("TEST 4 — End-to-end via data_provider.get_history()")
    try:
        import data_provider as dp
        dp.reset()

        ticker = "0166.KL"
        info(f"Fetching 1y of daily bars for {ticker}...")
        t0 = time.time()
        df = dp.get_history(ticker, period="1y", timeout=15)
        elapsed = time.time() - t0

        if df is None or df.empty:
            fail(f"BOTH providers failed for {ticker}")
            info(f"data_provider.health(): {dp.health()}")
            return "broken"

        served_by = dp.provider_name()
        h = dp.health()

        ok(f"Fetched {len(df)} rows in {elapsed:.2f}s — served by: {BOLD}{served_by}{RESET}")
        info(f"Date range:  {df.index.min().date()} → {df.index.max().date()}")
        info(f"Last close:  {fmt_price(df['Close'].iloc[-1])}")
        info(f"Last volume: {fmt_volume(df['Volume'].iloc[-1])}")

        if served_by == "moomoo":
            ok("🎉 Real-time Moomoo data is FLOWING")
            return "moomoo"

        if served_by == "yfinance":
            # We need to distinguish two sub-cases:
            #   (a) Moomoo never connected (port closed / not installed / etc.)
            #       → init_error explains why
            #   (b) Moomoo connected but per-call failed (e.g. "Unsupported quote market")
            #       → last_moomoo_error explains why; this is the most common case
            #         for retail MY accounts without OpenAPI quote permission.
            last_err = h.get("last_moomoo_error")
            init_err = h.get("init_error")
            moomoo_up = h.get("moomoo_available")

            if moomoo_up and last_err:
                warn("Moomoo OpenD connected but cannot serve Bursa data")
                info(f"Moomoo error:  {last_err}")
                if "Unsupported quote market" in last_err:
                    info("Diagnosis:     Your Moomoo account does not have")
                    info("               OpenAPI quote permission for Bursa MY.")
                    info("               The in-app data tier and OpenAPI tier are gated")
                    info("               independently. To enable real-time Bursa via API,")
                    info("               you may need to:")
                    info("                 1. Check Moomoo app Settings → API permissions")
                    info("                 2. Contact support@my.moomoo.com to ask")
                    info("                 3. Or accept that yfinance fallback is fine for")
                    info("                    daily-bar swing trading (recommended)")
                info("yfinance fallback worked — the agent is fully functional.")
                return "fallback"
            elif init_err:
                warn(f"Moomoo unavailable: {init_err}")
                info("yfinance fallback worked — the agent is fully functional.")
                return "fallback"
            else:
                warn("yfinance served the data (Moomoo state unclear)")
                info(f"health: {h}")
                return "fallback"

        warn(f"Unexpected provider name: {served_by}")
        info(f"health: {h}")
        return "fallback"

    except ImportError as e:
        fail(f"Can't import data_provider: {e}")
        info("Fix: Run this script from the repo root directory.")
        return "broken"
    except Exception as e:
        fail(f"data_provider call failed: {e}")
        import traceback
        traceback.print_exc()
        return "broken"


# ---------------------------------------------------------------------------
# Test 5 — graceful degradation when only yfinance available
# ---------------------------------------------------------------------------

def test_cross_check_yfinance(moomoo_serving: bool) -> bool:
    banner("TEST 5 — Cross-check: provider data consistency")

    if not moomoo_serving:
        info("Skipping cross-check (Moomoo isn't serving data, nothing to compare)")
        return True  # not a failure, just N/A

    try:
        import yfinance as yf
        import data_provider as dp

        ticker = "0166.KL"
        dp.reset()
        df_moomoo = dp.get_history(ticker, period="5d", timeout=15)
        df_yf = yf.Ticker(ticker).history(period="5d", timeout=15)

        if df_moomoo.empty or df_yf.empty:
            warn("One provider returned empty — can't cross-check")
            return True

        # NaN-safe close
        m_close_raw = df_moomoo["Close"].iloc[-1]
        y_close_raw = df_yf["Close"].iloc[-1]
        m_close = float(m_close_raw) if not math.isnan(float(m_close_raw)) else None
        y_close = float(y_close_raw) if not math.isnan(float(y_close_raw)) else None

        # If either is NaN (intraday), fall back to second-to-last bar
        if m_close is None and len(df_moomoo) >= 2:
            m_close = float(df_moomoo["Close"].iloc[-2])
        if y_close is None and len(df_yf) >= 2:
            y_close = float(df_yf["Close"].iloc[-2])

        m_date = df_moomoo.index[-1].date()
        y_date = df_yf.index[-1].date()

        info(f"Moomoo  last bar: {m_date}  close={fmt_price(m_close)}")
        info(f"yfinance last bar: {y_date}  close={fmt_price(y_close)}")

        if m_close is None or y_close is None or y_close == 0:
            warn("Could not compute deviation (insufficient closing prices)")
            return True

        pct_diff = abs(m_close - y_close) / y_close * 100
        if pct_diff < 0.5:
            ok(f"Closes match within {pct_diff:.2f}% — providers agree ✅")
        elif pct_diff < 2.0:
            warn(f"Closes differ by {pct_diff:.2f}% — likely Moomoo is more recent")
        else:
            warn(f"Closes differ by {pct_diff:.2f}% — investigate")
            return False
        return True

    except Exception as e:
        warn(f"Cross-check failed (non-critical): {e}")
        return True  # non-critical


# ---------------------------------------------------------------------------
# Main — three-state summary
# ---------------------------------------------------------------------------

def main() -> int:
    print()
    print(f"{BOLD}Moomoo OpenD diagnostic for BursaAI v3.6{RESET}")
    print(f"Run at:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Python:   {sys.version.split()[0]}")
    print(f"Platform: {sys.platform}")

    # Stage A: connectivity (must all pass before stage B is meaningful)
    stage_a = [
        ("Port 11111 open",     test_port_open()),
        ("moomoo-api installed", test_package_installed()),
    ]
    if all(r[1] for r in stage_a):
        stage_a.append(("OpenD liveness", test_opend_liveness()))

    stage_a_ok = all(r[1] for r in stage_a)

    # Stage B: end-to-end data flow
    dp_state = "broken"
    if stage_a_ok:
        dp_state = test_data_provider()
        crosscheck_ok = test_cross_check_yfinance(moomoo_serving=(dp_state == "moomoo"))
    else:
        warn("Skipping data-flow tests (connectivity failed)")
        crosscheck_ok = False

    # Summary
    banner("SUMMARY")
    for name, passed in stage_a:
        marker = f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"
        print(f"  [{marker}]  {name}")

    if stage_a_ok:
        state_marker = {
            "moomoo":   f"{GREEN}PASS{RESET}",
            "fallback": f"{YELLOW}PARTIAL{RESET}",
            "broken":   f"{RED}FAIL{RESET}",
        }[dp_state]
        print(f"  [{state_marker}]  data_provider end-to-end ({dp_state})")
        if dp_state == "moomoo":
            cc_marker = f"{GREEN}PASS{RESET}" if crosscheck_ok else f"{YELLOW}WARN{RESET}"
            print(f"  [{cc_marker}]  yfinance cross-check")

    print()
    if dp_state == "moomoo":
        print(f"{GREEN}{BOLD}🎉 ALL GREEN — Moomoo serves real-time Bursa data.{RESET}")
        print()
        print("Next steps:")
        print("  1. Keep OpenD running in the background")
        print("  2. Run the agent: streamlit run app.py")
        print("  3. Go to ⚙️ Settings tab → 📡 Data Source — should show 'Active: Moomoo'")
        return 0
    elif dp_state == "fallback":
        print(f"{YELLOW}{BOLD}⚠️  PARTIAL — Moomoo doesn't serve data, but yfinance fallback works.{RESET}")
        print()
        print("Your agent IS functional. The auto-fallback in data_provider.py")
        print("is doing exactly what it was designed for. For daily-bar swing")
        print("trading this is fully production-usable.")
        print()
        print("If you want to enable real-time Moomoo data, see the diagnosis")
        print("printed above (most commonly: account needs OpenAPI quote permission).")
        return 0
    else:
        print(f"{RED}{BOLD}❌ BROKEN — Neither Moomoo nor yfinance returned data.{RESET}")
        print()
        print("This is a real problem. Check:")
        print("  1. Internet connectivity")
        print("  2. yfinance rate limits (try again in 5 min)")
        print("  3. Run from the repo root directory")
        return 1


if __name__ == "__main__":
    sys.exit(main())
