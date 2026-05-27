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
    refresh_token TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT,
    is_exhausted INTEGER DEFAULT 0,
    is_dead INTEGER DEFAULT 0
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
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

        # Migrate existing DBs: add refresh_token column if missing
        try:
            self._conn.execute("ALTER TABLE credentials ADD COLUMN refresh_token TEXT")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

        # Migrate existing DBs: add is_dead column if missing
        try:
            self._conn.execute(
                "ALTER TABLE credentials ADD COLUMN is_dead INTEGER DEFAULT 0"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

    def _hash(self, authorization_header: str) -> str:
        return hashlib.sha256(authorization_header.encode("utf-8")).hexdigest()

    def upsert_credential(
        self,
        client_id: str,
        authorization_header: str,
        subscription_title: str | None = None,
        refresh_token: str | None = None,
    ) -> str:
        """Insert or update a credential. Returns the auth_hash."""
        auth_hash = self._hash(authorization_header)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO credentials (client_id, authorization_header, auth_hash,
                                         subscription_title, refresh_token,
                                         first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(auth_hash) DO UPDATE SET
                    client_id = excluded.client_id,
                    subscription_title = COALESCE(excluded.subscription_title, credentials.subscription_title),
                    refresh_token = COALESCE(excluded.refresh_token, credentials.refresh_token),
                    last_seen_at = datetime('now')
                """,
                (client_id, authorization_header, auth_hash, subscription_title, refresh_token),
            )
            self._conn.commit()
        return auth_hash

    def update_refresh_token(self, auth_hash: str, refresh_token: str) -> None:
        """Update the refresh token for a credential identified by auth_hash."""
        with self._lock:
            self._conn.execute(
                "UPDATE credentials SET refresh_token = ? WHERE auth_hash = ?",
                (refresh_token, auth_hash),
            )
            self._conn.commit()

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

    def mark_dead(self, auth_hash: str) -> None:
        """Mark a credential as dead (token rejected by backend, e.g. 401/403)."""
        with self._lock:
            self._conn.execute(
                "UPDATE credentials SET is_dead = 1 WHERE auth_hash = ?",
                (auth_hash,),
            )
            self._conn.commit()

    def mark_alive(self, auth_hash: str) -> None:
        """Mark a credential as alive (clear the dead flag)."""
        with self._lock:
            self._conn.execute(
                "UPDATE credentials SET is_dead = 0 WHERE auth_hash = ?",
                (auth_hash,),
            )
            self._conn.commit()

    def is_dead(self, auth_hash: str) -> bool:
        """Return True if the credential is marked dead."""
        with self._lock:
            row = self._conn.execute(
                "SELECT is_dead FROM credentials WHERE auth_hash = ?",
                (auth_hash,),
            ).fetchone()
        if row is None:
            return False
        return bool(row[0])

    def is_in_warning(self, client_id: str, threshold_pct: float) -> bool:
        """Return True when the latest usage snapshot for client_id shows
        remaining/limit*100 < threshold_pct. Returns False for empty
        client_id, non-positive threshold, missing snapshot, or non-positive
        usage_limit.
        """
        if not client_id or threshold_pct <= 0:
            return False
        with self._lock:
            row = self._conn.execute(
                """
                SELECT current_usage, usage_limit FROM usage_snapshots
                WHERE client_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (client_id,),
            ).fetchone()
        if row is None:
            return False
        current_usage, usage_limit = row
        if not usage_limit or usage_limit <= 0:
            return False
        remaining_pct = (usage_limit - current_usage) * 100.0 / usage_limit
        return remaining_pct < threshold_pct

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
                    WHERE is_exhausted = 0 AND is_dead = 0 AND auth_hash != ?
                    ORDER BY RANDOM() LIMIT 1
                    """,
                    (exclude_auth_hash,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT authorization_header, auth_hash, client_id
                    FROM credentials
                    WHERE is_exhausted = 0 AND is_dead = 0
                    ORDER BY RANDOM() LIMIT 1
                    """
                ).fetchone()
        if row is None:
            return None
        return (row[0], row[1], row[2])

    def get_best_available_credential(
        self, exclude_auth_hash: str | None = None
    ) -> tuple[str, str, str] | None:
        """Return (authorization_header, auth_hash, client_id) of the
        non-exhausted credential with the highest remaining credits
        (usage_limit - current_usage), excluding the specified hash.

        Falls back to get_available_credential if no usage data exists.
        """
        with self._lock:
            if exclude_auth_hash:
                row = self._conn.execute(
                    """
                    SELECT c.authorization_header, c.auth_hash, c.client_id
                    FROM credentials c
                    INNER JOIN usage_snapshots u ON c.client_id = u.client_id
                    WHERE c.is_exhausted = 0 AND c.is_dead = 0
                      AND c.auth_hash != ?
                      AND u.id = (
                          SELECT MAX(u2.id) FROM usage_snapshots u2
                          WHERE u2.client_id = c.client_id
                      )
                    ORDER BY (u.usage_limit - u.current_usage) DESC,
                             c.last_seen_at DESC
                    LIMIT 1
                    """,
                    (exclude_auth_hash,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT c.authorization_header, c.auth_hash, c.client_id
                    FROM credentials c
                    INNER JOIN usage_snapshots u ON c.client_id = u.client_id
                    WHERE c.is_exhausted = 0 AND c.is_dead = 0
                      AND u.id = (
                          SELECT MAX(u2.id) FROM usage_snapshots u2
                          WHERE u2.client_id = c.client_id
                      )
                    ORDER BY (u.usage_limit - u.current_usage) DESC,
                             c.last_seen_at DESC
                    LIMIT 1
                    """
                ).fetchone()
        if row is None:
            # Fall back to random selection if no usage data
            return self.get_available_credential(exclude_auth_hash=exclude_auth_hash)
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

    # --- Read-only query methods for the web UI ---

    def get_all_credentials(self) -> list[dict]:
        """Return all credentials as a list of dicts."""
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            rows = self._conn.execute(
                "SELECT * FROM credentials ORDER BY last_seen_at DESC"
            ).fetchall()
            self._conn.row_factory = None
        return [dict(r) for r in rows]

    def get_credential_count(self) -> int:
        """Return total number of credentials."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM credentials").fetchone()
        return row[0] if row else 0

    def get_exhausted_count(self) -> int:
        """Return number of exhausted credentials."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM credentials WHERE is_exhausted = 1"
            ).fetchone()
        return row[0] if row else 0

    def get_dead_count(self) -> int:
        """Return number of dead credentials (tokens rejected by backend)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM credentials WHERE is_dead = 1"
            ).fetchone()
        return row[0] if row else 0

    def get_warning_count(self, threshold_pct: float) -> int:
        """Number of distinct client_ids whose latest snapshot is in warning."""
        if threshold_pct <= 0:
            return 0
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT u.client_id, u.current_usage, u.usage_limit
                    FROM usage_snapshots u
                    INNER JOIN (
                        SELECT client_id, MAX(id) AS max_id
                        FROM usage_snapshots
                        GROUP BY client_id
                    ) latest ON u.id = latest.max_id
                    WHERE u.usage_limit > 0
                      AND ((u.usage_limit - u.current_usage) * 100.0 / u.usage_limit) < ?
                )
                """,
                (threshold_pct,),
            ).fetchone()
        return row[0] if row else 0

    def get_client_id_by_auth_hash(self, auth_hash: str) -> str | None:
        """Return the client_id for a credential identified by auth_hash, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT client_id FROM credentials WHERE auth_hash = ?",
                (auth_hash,),
            ).fetchone()
        if row is None:
            return None
        return row[0]

    def get_usage_history(self, client_id: str, limit: int = 100) -> list[dict]:
        """Return usage snapshots for a given client_id, newest first."""
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            rows = self._conn.execute(
                """
                SELECT * FROM usage_snapshots
                WHERE client_id = ?
                ORDER BY captured_at DESC
                LIMIT ?
                """,
                (client_id, limit),
            ).fetchall()
            self._conn.row_factory = None
        return [dict(r) for r in rows]

    def get_latest_usage_per_credential(self) -> dict[str, dict]:
        """Return a mapping of client_id -> latest usage snapshot dict."""
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            rows = self._conn.execute(
                """
                SELECT u.* FROM usage_snapshots u
                INNER JOIN (
                    SELECT client_id, MAX(id) as max_id
                    FROM usage_snapshots GROUP BY client_id
                ) latest ON u.id = latest.max_id
                """
            ).fetchall()
            self._conn.row_factory = None
        return {r["client_id"]: dict(r) for r in rows}

    def get_request_logs(self, page: int = 1, per_page: int = 50) -> list[dict]:
        """Return paginated request logs, newest first."""
        offset = (page - 1) * per_page
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            rows = self._conn.execute(
                """
                SELECT * FROM request_log
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (per_page, offset),
            ).fetchall()
            self._conn.row_factory = None
        return [dict(r) for r in rows]

    def get_request_log_count(self) -> int:
        """Return total number of request log entries."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM request_log").fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()
