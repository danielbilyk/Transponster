"""
Usage statistics tracking via SQLite.
"""

import logging
import os
import sqlite3
import threading
from datetime import datetime

DB_PATH = os.environ.get("STATS_DB", "/app/data/stats.db")

_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
    return _local.conn


def init_db():
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transcriptions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,
            user_id     TEXT    NOT NULL,
            username    TEXT    NOT NULL DEFAULT '',
            channel_id  TEXT    NOT NULL DEFAULT '',
            filename    TEXT    NOT NULL DEFAULT '',
            mode        TEXT    NOT NULL DEFAULT 'txt_only',
            file_size   INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    logging.info("[stats] Database initialized")


def record_transcription(
    *,
    user_id: str,
    username: str = "",
    channel_id: str = "",
    filename: str = "",
    mode: str = "txt_only",
    file_size: int = 0,
    timestamp: str | None = None,
):
    ts = timestamp or datetime.utcnow().isoformat()
    try:
        conn = _get_conn()
        conn.execute(
            """INSERT INTO transcriptions
               (timestamp, user_id, username, channel_id, filename, mode, file_size)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ts, user_id, username, channel_id, filename, mode, file_size),
        )
        conn.commit()
        logging.info(f"[stats] Recorded transcription: {filename} by {username or user_id}")
    except Exception as e:
        logging.error(f"[stats] Failed to record transcription: {e}")


def get_stats(year: int | None = None) -> dict:
    conn = _get_conn()
    where = ""
    params: list = []
    if year:
        where = "WHERE timestamp LIKE ?"
        params = [f"{year}-%"]

    total = conn.execute(
        f"SELECT COUNT(*) as c FROM transcriptions {where}", params
    ).fetchone()["c"]

    by_user = conn.execute(
        f"""SELECT username, user_id, COUNT(*) as count
            FROM transcriptions {where}
            GROUP BY COALESCE(NULLIF(username, ''), user_id) ORDER BY count DESC""",
        params,
    ).fetchall()

    by_month = conn.execute(
        f"""SELECT substr(timestamp, 1, 7) as month, COUNT(*) as count
            FROM transcriptions {where}
            GROUP BY month ORDER BY month""",
        params,
    ).fetchall()

    by_mode = conn.execute(
        f"""SELECT mode, COUNT(*) as count
            FROM transcriptions {where}
            GROUP BY mode ORDER BY count DESC""",
        params,
    ).fetchall()

    total_size = conn.execute(
        f"SELECT COALESCE(SUM(file_size), 0) as s FROM transcriptions {where}",
        params,
    ).fetchone()["s"]

    return {
        "total_transcriptions": total,
        "total_file_size_bytes": total_size,
        "by_user": [{"username": r["username"], "user_id": r["user_id"], "count": r["count"]} for r in by_user],
        "by_month": [{"month": r["month"], "count": r["count"]} for r in by_month],
        "by_mode": [{"mode": r["mode"], "count": r["count"]} for r in by_mode],
    }
