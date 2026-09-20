"""Per-user recently used stops for /station.

Stored in SQLite (standard library, no extra install). On Render the container
disk is wiped on every deploy/restart, so to keep presets you need a persistent
disk: mount one (for example at /data) and set PRESETS_DB=/data/presets.db.
Without that it still works, but the list resets whenever the service restarts.

All functions are blocking: call them through asyncio.to_thread.
"""
import os
import sqlite3
import time
from contextlib import closing

DB_PATH = os.getenv("PRESETS_DB", "presets.db")
MAX_PER_USER = 10


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS recent_stops ("
        "user_id INTEGER NOT NULL, stop_id TEXT NOT NULL, stop_name TEXT, last_used REAL NOT NULL, "
        "PRIMARY KEY (user_id, stop_id))"
    )
    return conn


def save(user_id, stop_id, stop_name=None):
    """Remember a stop for this user (or bump it to the top), keeping the newest MAX_PER_USER."""
    try:
        with closing(_connect()) as conn, conn:
            conn.execute(
                "INSERT INTO recent_stops (user_id, stop_id, stop_name, last_used) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, stop_id) DO UPDATE SET "
                "stop_name = COALESCE(excluded.stop_name, stop_name), last_used = excluded.last_used",
                (user_id, stop_id, stop_name, time.time()),
            )
            conn.execute(
                "DELETE FROM recent_stops WHERE user_id = ? AND stop_id NOT IN "
                "(SELECT stop_id FROM recent_stops WHERE user_id = ? ORDER BY last_used DESC LIMIT ?)",
                (user_id, user_id, MAX_PER_USER),
            )
    except (sqlite3.Error, OSError) as exc:
        print(f"Could not save preset: {exc}")


def recent(user_id, query="", limit=25):
    """[(stop_id, stop_name)] newest first, optionally filtered by what the user has typed."""
    query = str(query or "").strip().lower()
    try:
        with closing(_connect()) as conn:
            rows = conn.execute(
                "SELECT stop_id, stop_name FROM recent_stops WHERE user_id = ? ORDER BY last_used DESC",
                (user_id,),
            ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        print(f"Could not read presets: {exc}")
        return []
    if query:
        rows = [r for r in rows if r[0].lower().startswith(query) or query in (r[1] or "").lower()]
    return rows[:limit]
