"""Tests for credit_keeper.db."""

from __future__ import annotations

from pathlib import Path

from credit_keeper.db import CredentialDB


def test_tables_created_on_init(tmp_path: Path) -> None:
    db = CredentialDB(tmp_path / "test.db")
    with db._lock:
        tables = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    names = [t[0] for t in tables]
    assert "credentials" in names
    assert "usage_snapshots" in names
    assert "request_log" in names


def test_upsert_credential_insert_and_update(tmp_path: Path) -> None:
    db = CredentialDB(tmp_path / "test.db")
    auth = "Bearer abc123"
    h1 = db.upsert_credential("user1", auth, "Pro Plan")
    assert isinstance(h1, str) and len(h1) == 64  # SHA-256 hex

    # Second upsert updates last_seen_at
    h2 = db.upsert_credential("user1", auth, "Pro Plan")
    assert h1 == h2

    with db._lock:
        row = db._conn.execute(
            "SELECT client_id, subscription_title FROM credentials WHERE auth_hash = ?",
            (h1,),
        ).fetchone()
    assert row == ("user1", "Pro Plan")


def test_insert_usage_snapshot(tmp_path: Path) -> None:
    db = CredentialDB(tmp_path / "test.db")
    db.insert_usage_snapshot(
        client_id="user1",
        current_usage=50,
        usage_limit=100,
        resource_type="api_calls",
        display_name="API Calls",
        unit="calls",
        days_until_reset=15,
        next_date_reset=1700000000.0,
        raw_json='{"test": true}',
    )
    with db._lock:
        row = db._conn.execute(
            "SELECT client_id, current_usage, usage_limit, resource_type FROM usage_snapshots"
        ).fetchone()
    assert row == ("user1", 50, 100, "api_calls")


def test_mark_exhausted_and_available(tmp_path: Path) -> None:
    db = CredentialDB(tmp_path / "test.db")
    auth_hash = db.upsert_credential("user1", "Bearer xyz", "Plan")

    # Initially not exhausted
    with db._lock:
        row = db._conn.execute(
            "SELECT is_exhausted FROM credentials WHERE auth_hash = ?",
            (auth_hash,),
        ).fetchone()
    assert row[0] == 0

    db.mark_exhausted(auth_hash)
    with db._lock:
        row = db._conn.execute(
            "SELECT is_exhausted FROM credentials WHERE auth_hash = ?",
            (auth_hash,),
        ).fetchone()
    assert row[0] == 1

    db.mark_available(auth_hash)
    with db._lock:
        row = db._conn.execute(
            "SELECT is_exhausted FROM credentials WHERE auth_hash = ?",
            (auth_hash,),
        ).fetchone()
    assert row[0] == 0


def test_get_available_credential_excludes_hash(tmp_path: Path) -> None:
    db = CredentialDB(tmp_path / "test.db")
    h1 = db.upsert_credential("user1", "Bearer aaa", "Plan A")
    h2 = db.upsert_credential("user2", "Bearer bbb", "Plan B")

    # Both available - excluding h1 should return h2
    result = db.get_available_credential(exclude_auth_hash=h1)
    assert result is not None
    assert result[1] == h2

    # Excluding h2 should return h1
    result = db.get_available_credential(exclude_auth_hash=h2)
    assert result is not None
    assert result[1] == h1


def test_get_available_credential_returns_none_when_all_exhausted(tmp_path: Path) -> None:
    db = CredentialDB(tmp_path / "test.db")
    h1 = db.upsert_credential("user1", "Bearer aaa", "Plan A")
    h2 = db.upsert_credential("user2", "Bearer bbb", "Plan B")

    db.mark_exhausted(h1)
    db.mark_exhausted(h2)

    result = db.get_available_credential()
    assert result is None


def test_get_available_credential_skips_exhausted(tmp_path: Path) -> None:
    db = CredentialDB(tmp_path / "test.db")
    h1 = db.upsert_credential("user1", "Bearer aaa", "Plan A")
    h2 = db.upsert_credential("user2", "Bearer bbb", "Plan B")

    db.mark_exhausted(h1)

    result = db.get_available_credential()
    assert result is not None
    assert result[1] == h2


def test_insert_request_log(tmp_path: Path) -> None:
    db = CredentialDB(tmp_path / "test.db")
    db.insert_request_log(
        method="GET",
        url="https://example.com/api",
        host="example.com",
        client_id="user1",
        auth_hash="abc123",
    )
    with db._lock:
        row = db._conn.execute(
            "SELECT method, url, host, client_id, auth_hash FROM request_log"
        ).fetchone()
    assert row == ("GET", "https://example.com/api", "example.com", "user1", "abc123")
