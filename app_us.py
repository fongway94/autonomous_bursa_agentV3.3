# app_us.py — US-locked Streamlit Cloud deployment
#
# Deploy as a SEPARATE app on Streamlit Cloud:
#   Repo: autonomous_bursa_agentV3.3  |  Branch: main  |  Main file: app_us.py
#
# Add to this app's Secrets on Streamlit Cloud:
#   MARKET_MODE = "US"
#   GITHUB_TOKEN = "..."        (same as main app)
#   TELEGRAM_BOT_TOKEN = "..."  (same)
#   TELEGRAM_CHAT_ID = "..."    (same)
#   GIST_ID = "..."             (same)
#
# HOW THE LOCK WORKS:
#   1. MARKET_MODE=US is set here (and in secrets) before anything else.
#   2. market_profiles reads env var FIRST → always resolves to US.
#   3. set_active_market is patched to NEVER write the marker file,
#      so sidebar switches only affect the current page view but cannot
#      persist across reruns (which would override the env var lock).

import os

# ── 1. Lock market BEFORE any other import ──────────────────────────────
os.environ["MARKET_MODE"] = "US"
os.environ.setdefault("TRADING_MODE", "SWING")

# ── 2. Patch market_profiles to prevent marker file writes ──────────────
import market_profiles as _mp

_orig_set_market = _mp.set_active_market
_orig_set_mode   = _mp.set_trading_mode

def _us_set_market(market_code: str, persist: bool = True):
    """Sidebar can call set_active_market but NEVER writes the marker file.
    The env var MARKET_MODE=US always wins on the next rerun."""
    return _orig_set_market("US", persist=False)   # force US, no file write

def _us_set_mode(mode: str, persist: bool = True):
    """Mode switches (SWING/INTRADAY) are allowed but not persisted to file,
    because the env var + secrets are the source of truth here."""
    return _orig_set_mode(mode, persist=False)

_mp.set_active_market = _us_set_market
_mp.set_trading_mode  = _us_set_mode
_mp.reset_cache()

# ── 3. Run the full app ──────────────────────────────────────────────────
# Streamlit re-executes this entire file on every user interaction,
# so the patches above are always re-applied before app.py logic runs.
# Using runpy keeps __file__ and relative imports working correctly.
import runpy
runpy.run_path("app.py", run_name="__main__")
