import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "users.db"


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def init_db(admin_id: int | None) -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS authorized_users (
                user_id INTEGER PRIMARY KEY,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    if admin_id is not None:
        add_user(admin_id)


def add_user(user_id: int) -> bool:
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO authorized_users (user_id) VALUES (?)", (user_id,)
        )
        conn.commit()
        return cursor.rowcount > 0


def remove_user(user_id: int) -> bool:
    with _connect() as conn:
        cursor = conn.execute("DELETE FROM authorized_users WHERE user_id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount > 0


def is_authorized(user_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM authorized_users WHERE user_id = ? LIMIT 1", (user_id,)
        ).fetchone()
    return row is not None


def count_users() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM authorized_users").fetchone()
    return int(row[0]) if row else 0
