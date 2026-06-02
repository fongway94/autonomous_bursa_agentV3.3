# app_my.py — MY-locked Streamlit Cloud deployment
#
# Deploy as the PRIMARY app on Streamlit Cloud:
#   Repo: autonomous_bursa_agentV3.3  |  Branch: main  |  Main file: app_my.py
#
# Add to this app's Secrets on Streamlit Cloud:
#   MARKET_MODE = "MY"
#   GITHUB_TOKEN = "..."
#   TELEGRAM_BOT_TOKEN = "..."
#   TELEGRAM_CHAT_ID = "..."
#   GIST_ID = "..."
#
# HOW THE LOCK WORKS:
#   Same pattern as app_us.py — patches set_active_market so the
#   sidebar switcher cannot write a marker file that overrides MY lock.

import os

# ── 1. Lock market BEFORE any other import ──────────────────────────────
os.environ["MARKET_MODE"] = "MY"
os.environ.setdefault("TRADING_MODE", "SWING")

# ── 2. Patch market_profiles to prevent marker file writes ──────────────
import market_profiles as _mp

_orig_set_market = _mp.set_active_market
_orig_set_mode   = _mp.set_trading_mode

def _my_set_market(market_code: str, persist: bool = True):
    """Sidebar can call set_active_market but NEVER writes the marker file.
    The env var MARKET_MODE=MY always wins on the next rerun."""
    return _orig_set_market("MY", persist=False)   # force MY, no file write

def _my_set_mode(mode: str, persist: bool = True):
    """Mode switches are allowed but not persisted to file."""
    return _orig_set_mode(mode, persist=False)

_mp.set_active_market = _my_set_market
_mp.set_trading_mode  = _my_set_mode
_mp.reset_cache()

# ── 3. Run the full app ──────────────────────────────────────────────────
import runpy
runpy.run_path("app.py", run_name="__main__")
