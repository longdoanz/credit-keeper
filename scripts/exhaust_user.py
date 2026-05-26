"""Mark all credentials of a given user as exhausted (or reset).

Usage:
    uv run python scripts/exhaust_user.py exhaust <client_id_prefix>
    uv run python scripts/exhaust_user.py reset
    uv run python scripts/exhaust_user.py show
"""

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "credit-keeper.db"


def show(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id, client_id, substr(auth_hash,1,12), is_exhausted "
        "FROM credentials ORDER BY id"
    ).fetchall()
    print(f"{'id':<4}{'client_id':<58}{'hash':<14}exhausted")
    print("-" * 90)
    for r in rows:
        print(f"{r[0]:<4}{r[1] or '-':<58}{r[2]:<14}{r[3]}")


def exhaust(conn: sqlite3.Connection, prefix: str) -> None:
    cur = conn.execute(
        "UPDATE credentials SET is_exhausted = 1 WHERE client_id LIKE ?",
        (f"{prefix}%",),
    )
    conn.commit()
    print(f"Marked {cur.rowcount} credentials as exhausted")


def reset(conn: sqlite3.Connection) -> None:
    cur = conn.execute("UPDATE credentials SET is_exhausted = 0")
    conn.commit()
    print(f"Reset {cur.rowcount} credentials to available")


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
            reset(conn)
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
