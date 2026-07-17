import sqlite3
import os
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger("db")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transfers.db")

_local = threading.local()


def get_connection():
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            remote_path TEXT UNIQUE NOT NULL,
            local_path TEXT,
            size INTEGER DEFAULT 0,
            checksum TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);

        CREATE TABLE IF NOT EXISTS progress (
            file_id INTEGER PRIMARY KEY,
            bytes_transferred INTEGER DEFAULT 0,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            level TEXT DEFAULT 'INFO',
            message TEXT
        );
    """)
    conn.commit()
    logger.info("Database initialized")


def add_file(remote_path, local_path, size, checksum=None):
    conn = get_connection()
    with conn:
        existing = conn.execute(
            "SELECT id, status FROM files WHERE remote_path = ?", (remote_path,)
        ).fetchone()
        if existing:
            if existing["status"] in ("pending", "transferring", "failed", "complete"):
                conn.execute(
                    "UPDATE files SET status = 'pending', local_path = ?, size = ?, updated_at = ? WHERE id = ?",
                    (local_path, size, _now(), existing["id"]),
                )
                conn.execute(
                    "DELETE FROM progress WHERE file_id = ?", (existing["id"],)
                )
            return existing["id"]
        cursor = conn.execute(
            "INSERT INTO files (remote_path, local_path, size, checksum, status) VALUES (?, ?, ?, ?, 'pending')",
            (remote_path, local_path, size, checksum),
        )
        return cursor.lastrowid


def update_file_status(file_id, status):
    conn = get_connection()
    with conn:
        conn.execute(
            "UPDATE files SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), file_id),
        )


def update_progress(file_id, bytes_transferred):
    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO progress (file_id, bytes_transferred, last_updated) VALUES (?, ?, ?)",
            (file_id, bytes_transferred, _now()),
        )


def get_progress(file_id):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM progress WHERE file_id = ?", (file_id,)
    ).fetchone()
    return dict(row) if row else {"bytes_transferred": 0}


def get_pending_files():
    conn = get_connection()
    rows = conn.execute(
        "SELECT f.*, COALESCE(p.bytes_transferred, 0) as bytes_transferred "
        "FROM files f LEFT JOIN progress p ON f.id = p.file_id "
        "WHERE f.status IN ('pending', 'transferring') ORDER BY f.id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_files():
    conn = get_connection()
    rows = conn.execute(
        "SELECT f.*, COALESCE(p.bytes_transferred, 0) as bytes_transferred "
        "FROM files f LEFT JOIN progress p ON f.id = p.file_id "
        "ORDER BY f.status, f.id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_file_by_remote(remote_path):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM files WHERE remote_path = ?", (remote_path,)
    ).fetchone()
    return dict(row) if row else None


def reset_stalled_transfers():
    conn = get_connection()
    with conn:
        file_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM files WHERE status = 'transferring'"
            ).fetchall()
        ]
        conn.execute(
            "UPDATE files SET status = 'pending', updated_at = ? WHERE status = 'transferring'",
            (_now(),),
        )
        if file_ids:
            placeholders = ",".join("?" * len(file_ids))
            conn.execute(
                f"DELETE FROM progress WHERE file_id IN ({placeholders})", file_ids
            )


def add_log(level, message):
    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT INTO logs (timestamp, level, message) VALUES (?, ?, ?)",
            (_now(), level, message),
        )


def get_logs(limit=100):
    limit = max(1, min(limit, 1000))
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def clear_completed():
    conn = get_connection()
    with conn:
        conn.execute(
            "DELETE FROM progress WHERE file_id IN (SELECT id FROM files WHERE status = 'complete')"
        )
        conn.execute("DELETE FROM files WHERE status = 'complete'")
