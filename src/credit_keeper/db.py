"""SQLite storage layer for credential pool tracking."""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS credentials (
    id INTEGER PRIMARY KEY,
    client_id TEXT,
    authorization_header TEXT,
    auth_hash TEXT UNIQUE,
    subscription_title TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT,
    is_exhausted INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS usage_snapshots (
    id INTEGER PRIMARY KEY,
    client_id TEXT,
    current_usage INTEGER,
    usage_limit INTEGER,
    resource_type TEXT,
    display_name TEXT,
    unit TEXT,
    days_until_reset INTEGER,
    next_date_reset REAL,
    captured_at TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS request_log (
    id INTEGER PRIMARY KEY,
    ts TEXT,
    method TEXT,
    url TEXT,
    host TEXT,
    client_id TEXT,
    auth_hash TEXT
);
"""


class CredentialDB:
    """Thread-safe SQLite storage for credentials and usage data."""

    def __init__(self, db_path: str | Path = "./credit-keeper.db") -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()

        # Restrict file permissions on new DB files
        p = Path(self._db_path)
        if not p.exists():
            fd = os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)

    def _hash(self, authorization_header: str) -> str:
        return hashlib.sha256(authorization_header.encode("utf-8")).hexdigest()

    def upsert_credential(
        self,
        client_id: str,
        authorization_header: str,
        subscription_title: str | None = None,
    ) -> str:
        """Insert or update a credential. Returns the auth_hash."""
        auth_hash = self._hash(authorization_header)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO credentials (client_id, authorization_header, auth_hash,
                                         subscription_title, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(auth_hash) DO UPDATE SET
                    client_id = excluded.client_id,
                    subscription_title = COALESCE(excluded.subscription_title, credentials.subscription_title),
                    last_seen_at = datetime('now')
                """,
                (client_id, authorization_header, auth_hash, subscription_title),
            )
            self._conn.commit()
        return auth_hash

    def insert_usage_snapshot(
        self,
        client_id: str,
        current_usage: int,
        usage_limit: int,
        resource_type: str,
        display_name: str,
        unit: str,
        days_until_reset: int,
        next_date_reset: float | None,
        raw_json: str,
    ) -> None:
        """Insert a usage snapshot row."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO usage_snapshots
                    (client_id, current_usage, usage_limit, resource_type,
                     display_name, unit, days_until_reset, next_date_reset,
                     captured_at, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
                """,
                (
                    client_id,
                    current_usage,
                    usage_limit,
                    resource_type,
                    display_name,
                    unit,
                    days_until_reset,
                    next_date_reset,
                    raw_json,
                ),
            )
            self._conn.commit()

    def mark_exhausted(self, auth_hash: str) -> None:
        """Mark a credential as exhausted."""
        with self._lock:
            self._conn.execute(
                "UPDATE credentials SET is_exhausted = 1 WHERE auth_hash = ?",
                (auth_hash,),
            )
            self._conn.commit()

    def mark_available(self, auth_hash: str) -> None:
        """Mark a credential as available (not exhausted)."""
        with self._lock:
            self._conn.execute(
                "UPDATE credentials SET is_exhausted = 0 WHERE auth_hash = ?",
                (auth_hash,),
            )
            self._conn.commit()

    def get_available_credential(
        self, exclude_auth_hash: str | None = None
    ) -> tuple[str, str, str] | None:
        """Return (authorization_header, auth_hash, client_id) of a random
        non-exhausted credential, excluding the specified hash.
        Returns None if no credential is available.
        """
        with self._lock:
            if exclude_auth_hash:
                row = self._conn.execute(
                    """
                    SELECT authorization_header, auth_hash, client_id
                    FROM credentials
                    WHERE is_exhausted = 0 AND auth_hash != ?
                    ORDER BY RANDOM() LIMIT 1
                    """,
                    (exclude_auth_hash,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT authorization_header, auth_hash, client_id
                    FROM credentials
                    WHERE is_exhausted = 0
                    ORDER BY RANDOM() LIMIT 1
                    """
                ).fetchone()
        if row is None:
            return None
        return (row[0], row[1], row[2])

    def insert_request_log(
        self,
        method: str,
        url: str,
        host: str,
        client_id: str | None,
        auth_hash: str | None,
    ) -> None:
        """Log a request."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO request_log (ts, method, url, host, client_id, auth_hash)
                VALUES (datetime('now'), ?, ?, ?, ?, ?)
                """,
                (method, url, host, client_id, auth_hash),
            )
            self._conn.commit()

    def is_exhausted(self, auth_hash: str) -> bool:
        """Return True if the credential identified by auth_hash is marked exhausted."""
        with self._lock:
            row = self._conn.execute(
                "SELECT is_exhausted FROM credentials WHERE auth_hash = ?",
                (auth_hash,),
            ).fetchone()
        if row is None:
            return False
        return bool(row[0])

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()
