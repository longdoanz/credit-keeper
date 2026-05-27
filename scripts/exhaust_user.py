"""Manage credentials state in credit-keeper.db.

Usage:
    uv run python scripts/exhaust_user.py show
    uv run python scripts/exhaust_user.py exhaust <client_id_prefix>
    uv run python scripts/exhaust_user.py reset            # clear is_exhausted only
    uv run python scripts/exhaust_user.py revive           # clear is_dead only
    uv run python scripts/exhaust_user.py reset-all        # clear both flags
"""

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "credit-keeper.db"


def show(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id, client_id, substr(auth_hash,1,12), is_exhausted, is_dead "
        "FROM credentials ORDER BY id"
    ).fetchall()
    print(f"{'id':<4}{'client_id':<58}{'hash':<14}{'exhausted':<11}dead")
    print("-" * 95)
    for r in rows:
        print(f"{r[0]:<4}{r[1] or '-':<58}{r[2]:<14}{r[3]:<11}{r[4]}")


def exhaust(conn: sqlite3.Connection, prefix: str) -> None:
    cur = conn.execute(
        "UPDATE credentials SET is_exhausted = 1 WHERE client_id LIKE ?",
        (f"{prefix}%",),
    )
    conn.commit()
    print(f"Marked {cur.rowcount} credentials as exhausted")


def reset_exhausted(conn: sqlite3.Connection) -> None:
    cur = conn.execute("UPDATE credentials SET is_exhausted = 0")
    conn.commit()
    print(f"Cleared is_exhausted on {cur.rowcount} credentials")


def revive(conn: sqlite3.Connection) -> None:
    cur = conn.execute("UPDATE credentials SET is_dead = 0")
    conn.commit()
    print(f"Cleared is_dead on {cur.rowcount} credentials")


def reset_all(conn: sqlite3.Connection) -> None:
    cur = conn.execute("UPDATE credentials SET is_exhausted = 0, is_dead = 0")
    conn.commit()
    print(f"Cleared both flags on {cur.rowcount} credentials")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    cmd = sys.argv[1]
    conn = sqlite3.connect(DB)
    try:
        if cmd == "show":
            show(conn)
        elif cmd == "exhaust":
            if len(sys.argv) < 3:
                print("Error: 'exhaust' requires a client_id prefix")
                return 1
            exhaust(conn, sys.argv[2])
            print("\nState after:")
            show(conn)
        elif cmd == "reset":
            reset_exhausted(conn)
            print("\nState after:")
            show(conn)
        elif cmd == "revive":
            revive(conn)
            print("\nState after:")
            show(conn)
        elif cmd == "reset-all":
            reset_all(conn)
            print("\nState after:")
            show(conn)
        else:
            print(f"Unknown command: {cmd}")
            print(__doc__)
            return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
