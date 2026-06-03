# Current Session Handoff
# Date: 2026-06-03 (Wednesday) — Gist Separation & UI Restore Fixes
# Purpose: Pick up exactly where we left off if chat is disrupted
# GitHub: https://github.com/fongway94/autonomous_bursa_agentV3.3 (main branch)

---

## WHERE WE ARE RIGHT NOW

### Today's Milestones (Gist Isolation & UI Hardening)
- **Resolved Gist File Collisions:** Successfully isolated all database and ML model backups inside your private Gists using market-and-mode-specific filenames (e.g. `bursa_agent_US_SWING_db.b64.gz` and `bursa_agent_US_INTRADAY_db.b64.gz` side-by-side in your US Gist).
- **Fixed Streamlit Nested-Button Restore Bug:** The manual restore confirmation button in `app.py` previously suffered from a nested-button bug which caused the confirmation block to be skipped (preventing the restore from executing). Refactored to use a persistent `st.session_state["restore_confirm_active"]` toggle with clear confirmation/cancellation buttons.
- **Rerun-Proof Toast Notification System:** Standard Streamlit `st.success` banners disappear instantly upon a `st.rerun()`. We implemented a session-state-backed `pending_toast` dispatcher at the top of `app.py` that successfully preserves and fires the toast *after* the page reload completes.
- **Multi-Generational File Fallback:** If the latest v3.7 filename is not found inside the Gist, the app now automatically attempts a 3-tier cascade fallback: v3.7 name (`bursa_agent_MY_SWING_db.b64.gz`) -> v3.6 name (`bursa_agent_MY_db.b64.gz`) -> ancient v3.3 name (`bursa_agent_db.b64.gz` for MY).
- **Market-Specific Gist ID Fallback:** Created support for market-specific Gist ID secrets (`GIST_ID_MY` and `GIST_ID_US`) in the resolver, allowing complete, pristine separation of your Malaysia and US environments into two separate Gists.
- **Full Test Suite Green (621 passed, 0 failed):** Updated the regression tests in `test_persistence.py` and `test_ml_persistence.py` to support physical file cleanups and mock patch calls, resulting in a completely green build.

### Current Deployment Setup

```
Streamlit Cloud (24/7, always on):
  MY SWING  → yfinance → paper → Gist 99795fd2... (GIST_ID_MY) ✅

Local PC (user's Windows machine, must keep running for US):
  US SWING    → SIMULATE → Moomoo Book Trader → Gist 96d74d6d... (GIST_ID_US) ✅
  US INTRADAY → paper only → Gist 96d74d6d... (GIST_ID_US) ✅
  OpenD running on 127.0.0.1:11111 ✅
  NASDAQ Basic quote subscription active ✅
  Moomoo Desktop logged in ✅
  Moomoo Book Trader (paper account) active ✅
```

---

## ALL FILE MODIFICATIONS IN THIS SESSION (PUSHED TO GITHUB) ✅

### File 1: `persistence.py`
- Added `_marker_file_path()` to isolate local marker caches per (market, mode) (e.g. `.gist_marker_US_SWING.json`).
- Updated `_read_marker()` and `_write_marker()` to read/write specific files, falling back to legacy global `.gist_marker.json`.
- Centralized Gist ID resolution in `_resolve_gist_id()`, making explicit Streamlit secrets (`GIST_ID_<CODE>` -> `GIST_ID`) override stale file caches on disk.
- Enhanced `restore()` to search for files using multi-generational fallbacks (v3.7 -> v3.6 -> v3.3) for both databases and ML model classifiers.

### File 2: `app.py`
- Added a `pending_toast` check at the top of the script (after light theme injection) to pop and display any rerun-safe toasts.
- Updated the boot-time restore block to fire informative toasts for success (listing file and gist ID), skips (explaining why, e.g. local DB has data), and failures.
- Updated the settings backup panel warning banner to check for both `GIST_ID` and market-specific `GIST_ID_{code}`.
- Refactored the manual restore confirmation UI using `st.session_state["restore_confirm_active"]` to resolve the nested button skipping issue.

### File 3: `tests/test_ml_persistence.py`
- Patched both `requests.post` and `requests.patch` to prevent un-mocked requests from leaking to the live GitHub API during local test runs.

### File 4: `tests/test_persistence.py`
- Added disk-based marker file cleanup blocks at the start of `test_marker_read_write_round_trip` and `test_boot_restore_falls_back_to_gist_id_env_var` to wipe `.gist_marker_MY_SWING.json` and prevent test pollution.
- Updated the active DB wiping logic to remove `persistence._db_path()`.

### File 5: Handbooks & Revision History
- Updated `HandBook/PROJECT_HANDBOOK.md` (§14 and §15 detailing file isolation, active mode resolution, and fallback cascades).
- Updated `HandBook/SETUP_GUIDE.md` (Updated Gist backup file structures).
- Updated `HandBook/REVISION_HISTORY.md` (Added changelog entry under **v3.7 Hotfixes & Stability Tuning**).

---

## HOW TO CONFIGURE YOUR SYSTEM & CLEAN UP (RUNBOOK)

### Step 1: Clean Up Duplicate Gists on GitHub
1.  Open your [GitHub Gists Profile](https://gist.github.com/fongway94).
2.  Delete any Gists that are **NOT** these two:
    *   **Malaysia Gist:** `99795fd25789807a0e9f8c7196582a37` (Holds `bursa_agent_MY_SWING_db.b64.gz` & `setup_classifier_MY_SWING.pkl.b64.gz`)
    *   **US Gist:** `96d74d6dd4fdb6ad8fa4c99f93bf8ed0` (Holds both Swing & Intraday US backups side-by-side)

### Step 2: Configure Streamlit Secrets

#### A. On Streamlit Cloud (Malaysia 24/7 Swing):
Go to **Streamlit Cloud Dashboard → Manage App → Secrets** and set:
```toml
GITHUB_TOKEN = "ghp_your_classic_token_here"
GIST_ID_MY = "99795fd25789807a0e9f8c7196582a37"
```

#### B. On Your Local PC (US Swing & Intraday):
Open your local `C:\Users\USER\Project\autonomous_bursa_agentV3.3\.streamlit\secrets.toml` and set:
```toml
GITHUB_TOKEN = "ghp_your_classic_token_here"
GIST_ID_US = "96d74d6dd4fdb6ad8fa4c99f93bf8ed0"
```

### Step 3: Local PC Clean Startup (To Protect Your Intraday Trades)
1.  **Manual Backup:** Copy `C:\Users\USER\.bursa_agent_data` to your Desktop as a temporary safety backup before running any code.
2.  **Pull Latest Code:** In your PC command line, run `git pull origin main` to fetch the Gist updates and test files.
3.  **Wipe Old Cache Markers:** Delete any `.json` files inside your local `C:\Users\USER\.bursa_agent_data` directory.
4.  **Boot Up App:** Run `streamlit run app.py`. The app will detect your local DB, safely skip boot-time restore to protect your trades, and notify you:
    `ℹ️ Boot restore skipped: local DB has data ({X} trades, {Y} priors)`
5.  **Force Manual Backup:** Switch sidebar to **US** and **INTRADAY**, go to **Settings**, and click **💾 Backup now** to write `bursa_agent_US_INTRADAY_db.b64.gz` directly into your clean US Gist. Repeat this step for **SWING** mode.

---

## 24/7 INTRADAY SERVER PATH RECOMMENDATIONS

Since Moomoo OpenD cannot run on Streamlit Cloud, you have two options to run your 5-minute US Intraday strategy 24/7:

### Path A: Always-On Home PC (Free)
*   Disable Windows sleep mode ("Sleep" set to **Never**).
*   Enable auto-login on startup for both **Moomoo Desktop** and **OpenD**.
*   Create a `run_agent.bat` file in your startup directory to boot Streamlit automatically if Windows updates/reboots:
    ```bat
    @echo off
    cd C:\Users\USER\Project\autonomous_bursa_agentV3.3
    python -m streamlit run app.py --server.headless true
    ```
*   *(Optional)* Use **Cloudflare Tunnels** or **Ngrok** to securely access your local dashboard on your phone while away.

### Path B: Windows Cloud VPS (Recommended for Professional Trading)
*   **Recommendation:** **Contabo (Cloud VPS S with Windows OS)**
*   **Cost:** **~$13.26 / month** (Including Windows Server License).
*   **Specs:** 4 vCPUs, **8 GB RAM** (Crucial for Moomoo Desktop's memory footprint), NVMe SSD, Unlimited Bandwidth.
*   **Location:** Select **US East / New York / New Jersey** to minimize execution and market data feed latency.

---

## NEXT DEVELOPMENT SESSION AGENDA

### Step 1: Review Live Results
- Measure expectancy and win rate on the current live paper baseline.
- Monitor underperforming tickers (SOXL and PLTR) to evaluate potential removal.

### Step 2: Build Block 8 (INTRADAY Broker Mirroring)
- Wire `mirror_entry_to_broker()` into `intraday_engine.py` entries.
- Wire `mirror_exit_to_broker()` into settles and the 15:55 ET force-flat routine.

### Step 3: Build Block 9 (Enhanced ORB Strategy)
- Build a **historical same-time volume baseline** (re-evaluating relative volume based on average historical 11:30 volume instead of today's morning rush).
- Implement QQQ/NQ VWAP confirmation filters, midline stop-loss levels, and volume reclaim second-chance entries.

---
*Handoff written on: 2026-06-03 — Workspace is 100% green and in sync with GitHub main.*
