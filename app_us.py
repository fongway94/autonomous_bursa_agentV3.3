# app_us.py
# Entry point for the US market Streamlit Cloud deployment.
# Forces MARKET_MODE=US before importing app.py so the scheduler
# always runs US SWING regardless of the sidebar selection.

import os
os.environ["MARKET_MODE"] = "US"
os.environ.setdefault("TRADING_MODE", "SWING")

# Import everything from the main app — identical UI, just US-locked.
from app import *  # noqa: F401, F403
