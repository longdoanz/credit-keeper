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


def test_get_best_available_credential_picks_highest_remaining(tmp_path: Path) -> None:
    """get_best_available_credential should return the credential with the most remaining credits."""
    db = CredentialDB(tmp_path / "test.db")
    db.upsert_credential("user1", "Bearer aaa", "Plan A")
    db.upsert_credential("user2", "Bearer bbb", "Plan B")
    db.upsert_credential("user3", "Bearer ccc", "Plan C")

    # user1: 90/100 used (10 remaining)
    db.insert_usage_snapshot(
        client_id="user1", current_usage=90, usage_limit=100,
        resource_type="api_calls", display_name="API", unit="calls",
        days_until_reset=15, next_date_reset=None, raw_json="{}",
    )
    # user2: 20/100 used (80 remaining) - best choice
    db.insert_usage_snapshot(
        client_id="user2", current_usage=20, usage_limit=100,
        resource_type="api_calls", display_name="API", unit="calls",
        days_until_reset=15, next_date_reset=None, raw_json="{}",
    )
    # user3: 60/100 used (40 remaining)
    db.insert_usage_snapshot(
        client_id="user3", current_usage=60, usage_limit=100,
        resource_type="api_calls", display_name="API", unit="calls",
        days_until_reset=15, next_date_reset=None, raw_json="{}",
    )

    result = db.get_best_available_credential()
    assert result is not None
    assert result[0] == "Bearer bbb"  # user2 has the most remaining
    assert result[2] == "user2"


def test_get_best_available_credential_excludes_hash(tmp_path: Path) -> None:
    """get_best_available_credential should respect exclude_auth_hash."""
    db = CredentialDB(tmp_path / "test.db")
    h1 = db.upsert_credential("user1", "Bearer aaa", "Plan A")
    db.upsert_credential("user2", "Bearer bbb", "Plan B")

    db.insert_usage_snapshot(
        client_id="user1", current_usage=10, usage_limit=100,
        resource_type="api_calls", display_name="API", unit="calls",
        days_until_reset=15, next_date_reset=None, raw_json="{}",
    )
    db.insert_usage_snapshot(
        client_id="user2", current_usage=50, usage_limit=100,
        resource_type="api_calls", display_name="API", unit="calls",
        days_until_reset=15, next_date_reset=None, raw_json="{}",
    )

    # user1 has more remaining, but is excluded
    result = db.get_best_available_credential(exclude_auth_hash=h1)
    assert result is not None
    assert result[2] == "user2"


def test_get_best_available_credential_falls_back_when_no_usage(tmp_path: Path) -> None:
    """get_best_available_credential should fall back to get_available_credential when no usage data."""
    db = CredentialDB(tmp_path / "test.db")
    db.upsert_credential("user1", "Bearer aaa", "Plan A")

    # No usage snapshots - should still return something
    result = db.get_best_available_credential()
    assert result is not None
    assert result[0] == "Bearer aaa"


def test_refresh_token_storage_and_retrieval(tmp_path: Path) -> None:
    """upsert_credential should store refresh_token when provided."""
    db = CredentialDB(tmp_path / "test.db")
    auth_hash = db.upsert_credential("user1", "Bearer abc", "Plan", refresh_token="rt-123")

    with db._lock:
        row = db._conn.execute(
            "SELECT refresh_token FROM credentials WHERE auth_hash = ?",
            (auth_hash,),
        ).fetchone()
    assert row[0] == "rt-123"


def test_update_refresh_token(tmp_path: Path) -> None:
    """update_refresh_token should update the token for an existing credential."""
    db = CredentialDB(tmp_path / "test.db")
    auth_hash = db.upsert_credential("user1", "Bearer abc", "Plan")

    # Initially no refresh token
    with db._lock:
        row = db._conn.execute(
            "SELECT refresh_token FROM credentials WHERE auth_hash = ?",
            (auth_hash,),
        ).fetchone()
    assert row[0] is None

    # Update refresh token
    db.update_refresh_token(auth_hash, "new-refresh-token")

    with db._lock:
        row = db._conn.execute(
            "SELECT refresh_token FROM credentials WHERE auth_hash = ?",
            (auth_hash,),
        ).fetchone()
    assert row[0] == "new-refresh-token"


# --- Tests for is_in_warning, get_warning_count, get_client_id_by_auth_hash ---

def _insert_snapshot(db: CredentialDB, client_id: str, current_usage: int, usage_limit: int) -> None:
    db.insert_usage_snapshot(
        client_id=client_id,
        current_usage=current_usage,
        usage_limit=usage_limit,
        resource_type="api_calls",
        display_name="API Calls",
        unit="calls",
        days_until_reset=15,
        next_date_reset=None,
        raw_json="{}",
    )


def test_is_in_warning_below_threshold(tmp_path: Path) -> None:
    """Snapshot with current_usage=1850, usage_limit=2000 → 7.5% remaining < 10% → True."""
    db = CredentialDB(tmp_path / "test.db")
    _insert_snapshot(db, "c1", current_usage=1850, usage_limit=2000)
    assert db.is_in_warning("c1", 10.0) is True


def test_is_in_warning_above_threshold(tmp_path: Path) -> None:
    """Snapshot with current_usage=500, usage_limit=2000 → 75% remaining > 10% → False."""
    db = CredentialDB(tmp_path / "test.db")
    _insert_snapshot(db, "c1", current_usage=500, usage_limit=2000)
    assert db.is_in_warning("c1", 10.0) is False


def test_is_in_warning_empty_client_id(tmp_path: Path) -> None:
    """Empty client_id always returns False."""
    db = CredentialDB(tmp_path / "test.db")
    assert db.is_in_warning("", 10.0) is False


def test_is_in_warning_zero_threshold(tmp_path: Path) -> None:
    """threshold_pct=0 means feature is off → always False."""
    db = CredentialDB(tmp_path / "test.db")
    _insert_snapshot(db, "c1", current_usage=1999, usage_limit=2000)
    assert db.is_in_warning("c1", 0) is False


def test_is_in_warning_no_snapshot(tmp_path: Path) -> None:
    """No snapshot for c2 → False."""
    db = CredentialDB(tmp_path / "test.db")
    assert db.is_in_warning("c2", 10.0) is False


def test_is_in_warning_zero_limit(tmp_path: Path) -> None:
    """Snapshot with usage_limit=0 → False (avoid divide-by-zero)."""
    db = CredentialDB(tmp_path / "test.db")
    _insert_snapshot(db, "c1", current_usage=0, usage_limit=0)
    assert db.is_in_warning("c1", 10.0) is False


def test_is_in_warning_uses_latest_snapshot(tmp_path: Path) -> None:
    """When two snapshots exist, only the latest (higher id) is used."""
    db = CredentialDB(tmp_path / "test.db")
    # Older snapshot: near depletion (would be in warning)
    _insert_snapshot(db, "c1", current_usage=1950, usage_limit=2000)
    # Newer snapshot: plenty remaining (not in warning)
    _insert_snapshot(db, "c1", current_usage=500, usage_limit=2000)
    assert db.is_in_warning("c1", 10.0) is False


def test_get_warning_count_zero_threshold(tmp_path: Path) -> None:
    """threshold_pct <= 0 → returns 0 (feature off)."""
    db = CredentialDB(tmp_path / "test.db")
    _insert_snapshot(db, "c1", current_usage=1990, usage_limit=2000)
    assert db.get_warning_count(0) == 0
    assert db.get_warning_count(-5.0) == 0


def test_get_warning_count_counts_distinct_clients(tmp_path: Path) -> None:
    """Three clients: two in warning, one not → get_warning_count(10.0) == 2."""
    db = CredentialDB(tmp_path / "test.db")
    # c1: 1850/2000 used → 7.5% remaining → in warning
    _insert_snapshot(db, "c1", current_usage=1850, usage_limit=2000)
    # c2: 1900/2000 used → 5% remaining → in warning
    _insert_snapshot(db, "c2", current_usage=1900, usage_limit=2000)
    # c3: 500/2000 used → 75% remaining → NOT in warning
    _insert_snapshot(db, "c3", current_usage=500, usage_limit=2000)
    assert db.get_warning_count(10.0) == 2


def test_get_client_id_by_auth_hash_found(tmp_path: Path) -> None:
    """Upsert a credential and look it up by auth_hash."""
    db = CredentialDB(tmp_path / "test.db")
    auth_hash = db.upsert_credential("user42", "Bearer token42", "Plan")
    result = db.get_client_id_by_auth_hash(auth_hash)
    assert result == "user42"


def test_get_client_id_by_auth_hash_missing(tmp_path: Path) -> None:
    """Unknown auth_hash returns None."""
    db = CredentialDB(tmp_path / "test.db")
    result = db.get_client_id_by_auth_hash("nonexistent-hash-value")
    assert result is None
