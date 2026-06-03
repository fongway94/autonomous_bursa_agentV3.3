# persistence.py
"""
Persistent backup of the agent's SQLite database to a private GitHub Gist.

Solves the v3.1 problem where Streamlit Cloud container resets (caused by
GitHub pushes, manual reboots, 7-day sleep, or platform maintenance) wiped
the agent's learning data — including state_priors (Bayesian brain),
trade history, account balance, biases, parameters, etc.

Design
------
* A single private Gist holds the latest copy of `bursa_agent.db`.
* On boot, the agent downloads the latest backup BEFORE the scheduler
  starts (so the brain is restored before any cycle runs).
* Backups fire on:
    1. Every closed trade (so brain learning is preserved instantly)
    2. Every hourly scheduler heartbeat (safety net)
    3. Daily maintenance (consolidation)
* Old gist revisions are kept by GitHub forever (free), so you have
  rollback history without doing anything.

Credentials
-----------
Requires ONE secret in Streamlit Cloud → Manage app → Secrets:

  GITHUB_TOKEN = "ghp_..."
  # Personal Access Token (CLASSIC, not fine-grained) with scope: gist.
  # Generate at https://github.com/settings/tokens (NOT ?type=beta)

Optionally, also set GIST_ID if you want to survive container resets
without relying on the local marker file:

  GIST_ID = "your-gist-id-here"

The first backup will create the gist; subsequent backups update the
same gist (we remember its ID in a tiny marker file inside the data dir,
which itself is also backed up).  v3.1.9 ALSO stores the gist_id in the
SQLite `meta` table so it survives container resets via the Gist backup.

Safety guarantees
-----------------
* All backup/restore code is wrapped in try/except — never crashes the agent.
* If GITHUB_TOKEN is missing, the module degrades silently (status shown
  in UI as "❌ not configured" but app still works).
* DB file is compressed with gzip before upload (typically 4-10x smaller).
* gzip + base64-encoded for Gist storage (Gists store text).
* v3.1.6: ML classifier .pkl is also backed up alongside the DB.
* v3.1.9: gist_id is stored in SQLite `meta` table (survives restore)
  and GIST_ID env var is used as fallback when local marker is lost.
"""

from __future__ import annotations
import os
import gzip
import base64
import json
import threading
from datetime import datetime, timezone, timedelta

import requests

from db import DATA_DIR, current_db_path, get_meta, set_meta
from logger import get_logger

log = get_logger("persistence")

MYT = timezone(timedelta(hours=8))
GIST_API = "https://api.github.com/gists"

# v3.6 multi-market: each market gets its OWN filename inside the gist
# (one gist per market is also possible; we use one-gist-many-files to
# keep the user's "rotate the PAT" workflow unchanged).
#
# DB_PATH is now COMPUTED via current_db_path() to track the active market.

def _active_market_code() -> str:
    try:
        from market_profiles import active_market_code
        return active_market_code()
    except Exception:
        return "MY"


def _active_trading_mode() -> str:
    try:
        from market_profiles import active_trading_mode
        return active_trading_mode()
    except Exception:
        return "SWING"


def _gist_filename() -> str:
    """Unique Gist filename per (market, mode).

    v3.7 fix: include trading mode in filename so SWING and INTRADAY
    backups are stored as separate Gist files and never overwrite each other.

    Examples:
      bursa_agent_MY_SWING_db.b64.gz     ← MY app on Streamlit Cloud
      bursa_agent_US_SWING_db.b64.gz     ← US app on Streamlit Cloud
      bursa_agent_US_INTRADAY_db.b64.gz  ← local PC only

    Because each deployment (MY cloud, US cloud, local PC INTRADAY) uses
    a different filename, there is zero Gist conflict between them.
    No IS_STREAMLIT_CLOUD guard needed — filename isolation is sufficient.
    """
    code = _active_market_code()
    mode = _active_trading_mode()
    return f"bursa_agent_{code}_{mode}_db.b64.gz"


def _ml_gist_filename() -> str:
    code = _active_market_code()
    mode = _active_trading_mode()
    return f"setup_classifier_{code}_{mode}.pkl.b64.gz"


# Backwards-compat module aliases (legacy v3.3 names). Many call sites use
# these as constants; we re-resolve them at call time below.
GIST_FILENAME = _gist_filename()
ML_GIST_FILENAME = _ml_gist_filename()
MARKER_FILE = os.path.join(DATA_DIR, ".gist_marker.json")
ML_MODEL_PATH = os.path.join(DATA_DIR, "setup_classifier.pkl")


def _db_path() -> str:
    """Always returns the ACTIVE market's DB path.

    v3.6 back-compat: if a caller has monkey-patched the module-level
    `DB_PATH` constant (legacy v3.3 tests do this), honour that override
    so the test suite stays green.
    """
    # Look up the module attribute dynamically so monkey-patching works.
    overridden = globals().get("DB_PATH")
    real = current_db_path()
    if overridden and overridden != real:
        return overridden
    return real


# Maintain backward compatibility for any code that imports DB_PATH from us.
DB_PATH = current_db_path()

# Avoid overlapping backups
_BACKUP_LOCK = threading.RLock()

# Avoid uploading more often than this many seconds even if called rapidly
MIN_BACKUP_INTERVAL_SEC = 30


# ---------------------------------------------------------------------------
# Credentials + marker
# ---------------------------------------------------------------------------

def _get_secret(key: str) -> str | None:
    # 1. Try os.environ (Streamlit Cloud uses this)
    val = _os_val = os.environ.get(key)
    if val:
        return val

    # 2. Try streamlit.secrets if running inside Streamlit
    try:
        import streamlit as st
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass

    # 3. Try manual TOML parsing (for tests, background threads on local PC)
    try:
        secrets_path = os.path.join(".streamlit", "secrets.toml")
        if os.path.exists(secrets_path):
            import toml
            with open(secrets_path) as f:
                data = toml.load(f)
                if key in data:
                    return str(data[key])
    except Exception:
        pass

    return None


def _token() -> str | None:
    return _get_secret("GITHUB_TOKEN")


def is_configured() -> bool:
    return bool(_token())


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _marker_file_path() -> str:
    code = _active_market_code()
    mode = _active_trading_mode()
    return os.path.join(DATA_DIR, f".gist_marker_{code}_{mode}.json")


# v3.1.9: read marker from DB meta first (survives container reset + restore),
# then fall back to local JSON file for backwards compatibility.
def _read_marker() -> dict:
    # Primary: DB meta
    try:
        raw = get_meta("gist_marker")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    # Fallback: local file
    marker_path = _marker_file_path()
    if not os.path.exists(marker_path):
        # Fall back to legacy shared marker file
        if os.path.exists(MARKER_FILE):
            try:
                with open(MARKER_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}
    try:
        with open(marker_path) as f:
            return json.load(f)
    except Exception:
        return {}


# v3.1.9: write to DB meta (backed up in Gist) AND local file.
def _write_marker(data: dict) -> None:
    try:
        set_meta("gist_marker", json.dumps(data))
    except Exception as e:
        log.warning(f"DB meta marker write failed: {e}")
    try:
        marker_path = _marker_file_path()
        with open(marker_path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning(f"file marker write failed: {e}")


def _resolve_gist_id() -> str | None:
    """Resolve the Gist ID to use for backup or restore.

    Priority order:
    1. Market-specific GIST_ID env var / secrets (e.g. GIST_ID_US or GIST_ID_MY)
    2. Global GIST_ID env var / secrets
    3. Cached gist_id in DB meta or local marker files
    """
    code = _active_market_code()
    gist_id = _get_secret(f"GIST_ID_{code}")
    if not gist_id:
        gist_id = _get_secret("GIST_ID")
    if gist_id:
        return gist_id

    marker = _read_marker()
    return marker.get("gist_id")


# ---------------------------------------------------------------------------
# Encode / decode
# ---------------------------------------------------------------------------

def _encode_db_for_gist() -> str:
    """Read the SQLite DB, gzip + base64-encode for storage in a text Gist."""
    if not os.path.exists(_db_path()):
        raise FileNotFoundError(f"DB not found at {_db_path()}")
    with open(_db_path(), "rb") as f:
        raw = f.read()
    compressed = gzip.compress(raw, compresslevel=6)
    encoded = base64.b64encode(compressed).decode("ascii")
    return encoded


def _decode_gist_to_db(encoded: str, target_path: str) -> int:
    """Reverse of _encode_db_for_gist. Returns bytes written."""
    compressed = base64.b64decode(encoded.encode("ascii"))
    raw = gzip.decompress(compressed)
    # Write atomically — temp file then rename, so a half-written DB never
    # appears.
    tmp_path = target_path + ".restoring"
    with open(tmp_path, "wb") as f:
        f.write(raw)
    os.replace(tmp_path, target_path)
    return len(raw)


def _encode_ml_for_gist() -> str | None:
    """
    Encode the ML classifier .pkl for gist storage.
    Returns None if no .pkl exists (no model to back up yet).
    """
    if not os.path.exists(ML_MODEL_PATH):
        return None
    with open(ML_MODEL_PATH, "rb") as f:
        raw = f.read()
    compressed = gzip.compress(raw, compresslevel=6)
    return base64.b64encode(compressed).decode("ascii")


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

_last_backup_ts: datetime | None = None


def backup(force: bool = False, reason: str = "") -> dict:
    """
    Backup the DB to the configured Gist.

    Returns a status dict — never raises. Safe to call from anywhere.

    v3.7: Each (market, mode) pair uses a unique Gist filename so there
    is zero conflict between Streamlit Cloud SWING backups and local PC
    INTRADAY backups. No IS_STREAMLIT_CLOUD guard needed.
    """
    global _last_backup_ts
    result = {"ok": False, "reason": "", "size_kb": 0,
              "gist_id": None, "skipped": False}

    if not is_configured():
        result["reason"] = "GITHUB_TOKEN not set"
        return result

    # Rate limit
    now = datetime.now(MYT)
    if not force and _last_backup_ts:
        elapsed = (now - _last_backup_ts).total_seconds()
        if elapsed < MIN_BACKUP_INTERVAL_SEC:
            result["skipped"] = True
            result["reason"] = f"rate-limited ({elapsed:.0f}s < "\
                               f"{MIN_BACKUP_INTERVAL_SEC}s)"
            return result

    with _BACKUP_LOCK:
        try:
            encoded = _encode_db_for_gist()
            size_kb = len(encoded) / 1024
            result["size_kb"] = round(size_kb, 1)

            gist_id = _resolve_gist_id()

            files = {_gist_filename(): {"content": encoded}}

            # v3.1.6: also include the ML classifier .pkl if it exists
            ml_encoded = _encode_ml_for_gist()
            ml_size_kb = 0.0
            if ml_encoded:
                files[_ml_gist_filename()] = {"content": ml_encoded}
                ml_size_kb = len(ml_encoded) / 1024

            payload = {
                "description": (
                    f"BursaAI agent DB backup — "
                    f"{now.strftime('%Y-%m-%d %H:%M:%S')} MYT. "
                    f"Reason: {reason or 'periodic'}. "
                    f"DB: {size_kb:.1f} KB | ML: {ml_size_kb:.1f} KB "
                    f"(both compressed)."
                ),
                "files": files,
            }

            if gist_id:
                # Update existing gist
                r = requests.patch(f"{GIST_API}/{gist_id}",
                                   json=payload,
                                   headers=_headers(),
                                   timeout=30)
            else:
                # First backup — create new private gist
                payload["public"] = False
                r = requests.post(GIST_API, json=payload,
                                  headers=_headers(), timeout=30)

            if r.status_code in (200, 201):
                gist_id = r.json().get("id")
                _write_marker({
                    "gist_id": gist_id,
                    "last_backup_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "last_backup_size_kb": round(size_kb, 1),
                    "last_reason": reason,
                })
                _last_backup_ts = now
                result.update({"ok": True, "gist_id": gist_id,
                                "reason": reason or "ok"})
                log.info(f"backup OK ({size_kb:.1f} KB) → gist {gist_id}")
            else:
                result["reason"] = f"HTTP {r.status_code}: {r.text[:200]}"
                log.error(f"backup failed: {result['reason']}")
        except Exception as e:
            result["reason"] = f"exception: {e}"
            log.error(f"backup exception: {e}")

    return result


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def restore(gist_id: str | None = None) -> dict:
    """
    Restore the DB from the configured Gist. Called once on boot,
    BEFORE the scheduler starts.

    If `gist_id` not given, reads from DB meta (survives container resets)
    then local marker file, then GIST_ID env var.
    """
    result = {"ok": False, "reason": "", "bytes_restored": 0,
              "gist_id": None}

    if not is_configured():
        result["reason"] = "GITHUB_TOKEN not set"
        return result

    if gist_id is None:
        gist_id = _resolve_gist_id()

    if not gist_id:
        result["reason"] = "no gist_id in meta, marker, or GIST_ID env (first run — nothing to restore)"
        return result

    try:
        r = requests.get(f"{GIST_API}/{gist_id}",
                         headers=_headers(), timeout=30)
        if r.status_code != 200:
            result["reason"] = f"HTTP {r.status_code}: {r.text[:200]}"
            return result

        gist = r.json()
        files = gist.get("files", {})

        # v3.7 migration: look for new filename first, then fall back to
        # old v3.6 filename (bursa_agent_<CODE>_db.b64.gz without mode),
        # and finally fall back to ancient v3.3 filename (bursa_agent_db.b64.gz) for MY.
        # This allows recovery after the filename scheme changed mid-session.
        target_file = _gist_filename()
        if target_file not in files:
            # Try legacy filename (v3.6 format without trading mode)
            code = _active_market_code()
            legacy_filename = f"bursa_agent_{code}_db.b64.gz"
            ancient_filename = "bursa_agent_db.b64.gz"
            if legacy_filename in files:
                log.warning(
                    f"New filename '{target_file}' not in Gist — "
                    f"falling back to legacy '{legacy_filename}' for restore"
                )
                target_file = legacy_filename
            elif code == "MY" and ancient_filename in files:
                log.warning(
                    f"New filename '{target_file}' not in Gist — "
                    f"falling back to ancient '{ancient_filename}' for restore"
                )
                target_file = ancient_filename
            else:
                result["reason"] = (
                    f"gist {gist_id} has no file '{_gist_filename()}', "
                    f"legacy '{legacy_filename}', or ancient '{ancient_filename}'"
                )
                return result

        def _fetch_file_content(file_meta):
            """Get file content, handling truncated gists via raw_url."""
            if file_meta.get("truncated") or not file_meta.get("content"):
                raw_url = file_meta.get("raw_url")
                if not raw_url:
                    return None
                r2 = requests.get(raw_url, headers=_headers(), timeout=60)
                return r2.text
            return file_meta["content"]

        encoded = _fetch_file_content(files[target_file])
        if encoded is None:
            result["reason"] = "DB file truncated with no raw_url"
            return result

        # SAFETY: backup the existing DB before overwriting (just in case)
        if os.path.exists(_db_path()):
            backup_path = _db_path() + ".pre_restore"
            try:
                import shutil
                shutil.copy2(_db_path(), backup_path)
            except Exception:
                pass

        bytes_restored = _decode_gist_to_db(encoded.strip(), _db_path())

        # v3.1.13: re-apply schema migrations after restore.
        # Critical for forward-compat: the Gist backup may have been made
        # before a column was added (e.g. cycle_started_at in v3.1.10).
        # Without this, the restored DB has stale schema and the next
        # write to a new column crashes with sqlite3.OperationalError.
        # init_db() is idempotent — safe to call.
        try:
            from db import init_db as _init_db_after_restore
            _init_db_after_restore()
            log.info("post-restore init_db() ran — pending migrations applied")
        except Exception as _mig_err:
            # Don't fail the whole restore over a migration glitch —
            # the restore itself succeeded. Log and continue.
            log.error(f"post-restore init_db failed: {_mig_err}")

        # v3.1.6: also restore the ML classifier .pkl if present (with fallbacks)
        ml_bytes = 0
        ml_target_file = _ml_gist_filename()
        if ml_target_file not in files:
            # Try legacy (v3.6)
            code = _active_market_code()
            legacy_ml = f"setup_classifier_{code}.pkl.b64.gz"
            ancient_ml = "setup_classifier.pkl.b64.gz"
            if legacy_ml in files:
                ml_target_file = legacy_ml
            elif code == "MY" and ancient_ml in files:
                ml_target_file = ancient_ml
            else:
                ml_target_file = None

        if ml_target_file and ml_target_file in files:
            try:
                ml_encoded = _fetch_file_content(files[ml_target_file])
                if ml_encoded:
                    ml_bytes = _decode_gist_to_db(ml_encoded.strip(),
                                                    ML_MODEL_PATH)
                    log.info(f"ML classifier restored ({ml_bytes} bytes) from {ml_target_file}")
            except Exception as e:
                log.warning(f"ML classifier restore failed (non-fatal): {e}")

        # v3.1.9: store the gist_id in DB meta so next container reset
        # can find it even if the marker file is wiped.
        try:
            _write_marker({
                "gist_id": gist_id,
                "restored_at": datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception:
            pass

        result.update({"ok": True, "bytes_restored": bytes_restored,
                        "ml_bytes_restored": ml_bytes,
                        "gist_id": gist_id,
                        "source_file": target_file,
                        "reason": (f"restored DB + ML from {target_file}"
                                   if ml_bytes else f"restored DB from {target_file}")})
        log.info(f"restore OK (DB={bytes_restored}, ML={ml_bytes}) "
                  f"← gist {gist_id}")
    except Exception as e:
        result["reason"] = f"exception: {e}"
        log.error(f"restore exception: {e}")

    return result


# ---------------------------------------------------------------------------
# Status (for dashboard)
# ---------------------------------------------------------------------------

def get_status() -> dict:
    """Returns the current backup status for the Settings tab UI."""
    marker = _read_marker()
    return {
        "configured": is_configured(),
        "gist_id": marker.get("gist_id") or os.environ.get("GIST_ID"),
        "last_backup_at": marker.get("last_backup_at"),
        "last_backup_size_kb": marker.get("last_backup_size_kb"),
        "last_reason": marker.get("last_reason"),
        "db_size_kb": (round(os.path.getsize(_db_path()) / 1024, 1)
                       if os.path.exists(_db_path()) else 0),
    }


# ---------------------------------------------------------------------------
# Boot-time restore (called from app.py BEFORE scheduler.ensure_started)
# ---------------------------------------------------------------------------

_BOOT_RESTORE_ATTEMPTED = False


def boot_restore_once() -> dict:
    """
    Idempotent boot-time restore. Called from app.py top-of-script.

    Only runs once per Python process. If the DB already exists and
    looks healthy (has the `account` row), skips restore — assumes
    the DB persisted from a previous boot in the same container.
    """
    global _BOOT_RESTORE_ATTEMPTED
    if _BOOT_RESTORE_ATTEMPTED:
        return {"skipped": True, "reason": "already attempted this process"}
    _BOOT_RESTORE_ATTEMPTED = True

    if not is_configured():
        return {"skipped": True, "reason": "GITHUB_TOKEN not set"}

    # Check if local DB already has data — if yes, don't overwrite
    try:
        from db import connect
        with connect(readonly=True) as c:
            row = c.execute(
                "SELECT cash_balance FROM account WHERE id=1"
            ).fetchone()
        if row and row["cash_balance"] is not None:
            # Local DB is populated — only restore if it's empty/fresh.
            # Check: is there at least 1 trade or 1 state_prior row?
            with connect(readonly=True) as c:
                t = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
                p = c.execute("SELECT COUNT(*) FROM state_priors").fetchone()[0]
            if t > 0 or p > 0:
                log.info(
                    f"boot-restore skipped: local DB has data "
                    f"({t} trades, {p} state priors)"
                )
                return {"skipped": True,
                        "reason": f"local DB has data ({t} trades, {p} priors)"}
    except Exception as e:
        log.warning(f"boot-restore precheck failed (will attempt restore): {e}")

    return restore()
