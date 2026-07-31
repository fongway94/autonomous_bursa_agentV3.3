"""
Tests for v3.8 corrupt-DB auto-recovery.

Context: Streamlit Cloud containers can be killed mid-write, leaving the
SQLite database (or its -wal/-shm sidecars) in a state where every read
raises ``sqlite3.DatabaseError: database disk image is malformed`` — the
app then boots with "Scheduler did not start: database disk image is
malformed" and the scheduler never starts.

The fix under test:
  * db.db_health()           — fast read-only corruption detector
  * persistence.recover_corrupt_db() — repair ladder: stale-WAL sidecar
    removal → Gist restore → iterdump salvage → fresh rebuild. The corrupt
    file is always preserved as <path>.corrupt-<timestamp>.
"""

import os
import struct
import sqlite3


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_valid_db(path: str, n_rows: int = 50) -> None:
    """Create a small valid WAL-mode SQLite DB with a populated table."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        for i in range(n_rows):
            conn.execute("INSERT INTO t VALUES (?, ?)", (i, f"value-{i}"))
        conn.commit()
    finally:
        conn.close()


def _corrupt_page2(path: str) -> None:
    """Flip the first byte of page 2 (a b-tree page header).

    Reproduces the exact failure the app sees in production: opening or
    running PRAGMA quick_check raises
    ``sqlite3.DatabaseError: database disk image is malformed``.
    """
    data = bytearray(open(path, "rb").read())
    page_size = struct.unpack(">H", data[16:18])[0]
    data[page_size] ^= 0xFF
    open(path, "wb").write(bytes(data))


def _quarantine_files(path: str) -> list:
    return sorted(f for f in os.listdir(os.path.dirname(path))
                  if os.path.basename(f).startswith(
                      os.path.basename(path) + ".corrupt-"))


# ---------------------------------------------------------------------------
# db.db_health
# ---------------------------------------------------------------------------

def test_db_health_missing_file_is_healthy(tmp_path):
    from db import db_health
    missing = tmp_path / "does_not_exist.db"
    h = db_health(str(missing))
    assert h["healthy"] is True
    assert h["missing"] is True


def test_db_health_healthy_db(tmp_path):
    from db import db_health
    p = tmp_path / "ok.db"
    _make_valid_db(str(p))
    h = db_health(str(p))
    assert h["healthy"] is True
    assert h["missing"] is False


def test_db_health_detects_malformed_db(tmp_path):
    """A structurally corrupted DB must be reported unhealthy — this is the
    detector behind the 'database disk image is malformed' boot failure."""
    from db import db_health
    p = tmp_path / "corrupt.db"
    _make_valid_db(str(p))
    _corrupt_page2(str(p))

    h = db_health(str(p))
    assert h["healthy"] is False
    assert "malformed" in (h.get("error") or "").lower()


# ---------------------------------------------------------------------------
# persistence.recover_corrupt_db
# ---------------------------------------------------------------------------

def test_recover_missing_db_is_noop(tmp_path):
    import persistence
    missing = tmp_path / "nope.db"
    res = persistence.recover_corrupt_db(str(missing))
    assert res["healthy"] is True
    assert res["recovered"] is False
    assert res["action"] == "none"


def test_recover_healthy_db_is_noop(tmp_path):
    import persistence
    p = tmp_path / "healthy.db"
    _make_valid_db(str(p))
    before = open(p, "rb").read()

    res = persistence.recover_corrupt_db(str(p))
    assert res["healthy"] is True
    assert res["recovered"] is False
    assert res["action"] == "none"
    # untouched: same bytes, no quarantine files, no sidecars moved
    assert open(p, "rb").read() == before
    assert _quarantine_files(str(p)) == []


def test_recover_corrupt_db_rebuilds_fresh(monkeypatch, tmp_path):
    """Hard corruption + no Gist backup → quarantine + fresh rebuild.

    The rebuilt DB must be fully usable by the repository layer (schema +
    seeds present), and the corrupt original must be preserved."""
    import persistence
    # Never even attempt a network restore (no GITHUB_TOKEN).
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    p = tmp_path / "brain.db"
    _make_valid_db(str(p), n_rows=10)
    _corrupt_page2(str(p))

    res = persistence.recover_corrupt_db(str(p))
    assert res["recovered"] is True
    assert res["healthy"] is True
    # page-2 corruption breaks sqlite_master → iterdump cannot run →
    # recovery deterministically falls through to a fresh rebuild.
    assert res["action"] == "rebuilt", res

    # corrupt file preserved for forensics
    assert _quarantine_files(str(p)), "corrupt file must be quarantined"

    # new DB is structurally sound
    from db import db_health
    assert db_health(str(p))["healthy"] is True

    # and app-usable: singleton rows the UI reads on every render exist
    conn = sqlite3.connect(str(p))
    try:
        assert conn.execute(
            "SELECT cash_balance FROM account WHERE id=1").fetchone() is not None
        assert conn.execute(
            "SELECT id FROM scheduler_state WHERE id=1").fetchone() is not None
        assert conn.execute(
            "SELECT id FROM parameters WHERE id=1").fetchone() is not None
    finally:
        conn.close()


def test_recover_prefers_gist_restore(monkeypatch, tmp_path):
    """If a Gist backup is configured and reachable, it wins over a rebuild."""
    import persistence

    p = tmp_path / "brain.db"
    _make_valid_db(str(p))
    _corrupt_page2(str(p))

    monkeypatch.setattr(persistence, "is_configured", lambda: True)
    monkeypatch.setattr(
        persistence, "restore",
        lambda *a, **k: {"ok": True, "bytes_restored": 12345,
                          "gist_id": "gist-abc", "source_file": "x.b64.gz",
                          "ml_bytes_restored": 0},
    )

    res = persistence.recover_corrupt_db(str(p))
    assert res["recovered"] is True
    assert res["healthy"] is True
    assert res["action"] == "gist_restore", res


def test_recover_sidecar_removed_stage(monkeypatch, tmp_path):
    """Stage 1: if a stale -wal/-shm sidecar is the culprit, removing it
    fixes the DB with zero data loss (no quarantine of the main file)."""
    import persistence
    import db as db_module

    p = tmp_path / "sidecar.db"
    _make_valid_db(str(p))
    # create a sidecar that (in the simulated world) is the corrupt part
    sidecar = str(p) + "-wal"
    with open(sidecar, "wb") as f:
        f.write(os.urandom(256))

    # Simulate: health check fails while the sidecar is present, passes
    # once it has been moved aside (i.e. main file is intact).
    calls = {"n": 0}

    def _fake_health(path=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"healthy": False, "error": "database disk image is "
                                               "malformed", "path": path}
        return {"healthy": True, "error": None, "path": path}

    monkeypatch.setattr(db_module, "db_health", _fake_health)

    res = persistence.recover_corrupt_db(str(p))
    assert res["recovered"] is True
    assert res["healthy"] is True
    assert res["action"] == "sidecar_removed", res
    # sidecar moved aside, main file untouched
    assert not os.path.exists(sidecar)
    assert os.path.exists(str(p))
    assert any("sidecar.db-wal.corrupt-" in f
               for f in os.listdir(tmp_path))


# ---------------------------------------------------------------------------
# persistence._salvage_db
# ---------------------------------------------------------------------------

def test_salvage_db_copies_rows_from_readable_db(tmp_path):
    """iterdump salvage must copy every readable row into a new DB."""
    import persistence

    src = tmp_path / "readable.db"
    _make_valid_db(str(src), n_rows=100)

    dst = tmp_path / "salvaged.db"
    ok = persistence._salvage_db(str(src), str(dst))
    assert ok is True

    conn = sqlite3.connect(str(dst))
    try:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 100
    finally:
        conn.close()


def test_salvage_db_returns_false_on_hard_corruption(tmp_path):
    """A structurally broken DB must not produce a partial target file."""
    import persistence

    src = tmp_path / "broken.db"
    _make_valid_db(str(src))
    _corrupt_page2(str(src))  # breaks sqlite_master → iterdump raises

    dst = tmp_path / "partial.db"
    ok = persistence._salvage_db(str(src), str(dst))
    assert ok is False
    # partial target cleaned up so the rebuild fallback can take its place
    assert not os.path.exists(str(dst))
