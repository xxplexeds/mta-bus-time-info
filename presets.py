"""Per-user stops for /station: automatic recents plus starred ("saved") stops.

Stored in SQLite (standard library, no extra install). On Render the container
disk is wiped on every deploy/restart, so to keep this data you need a persistent
disk: mount one (for example at /data) and set PRESETS_DB=/data/presets.db.
Without that it still works, but everything resets whenever the service restarts.

All functions are blocking: call them through asyncio.to_thread.
"""
import os
import sqlite3
import time
from contextlib import closing

DB_PATH = os.getenv("PRESETS_DB", "presets.db")
MAX_PER_USER = 10   # recents kept per user
MAX_PINNED = 25     # saved stops per user (also Discord's dropdown limit)


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS recent_stops ("
        "user_id INTEGER NOT NULL, stop_id TEXT NOT NULL, stop_name TEXT, last_used REAL NOT NULL, "
        "PRIMARY KEY (user_id, stop_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS saved_stops ("
        "user_id INTEGER NOT NULL, stop_id TEXT NOT NULL, stop_name TEXT, saved_at REAL NOT NULL, "
        "PRIMARY KEY (user_id, stop_id))"
    )
    return conn


def _filter(rows, query):
    query = str(query or "").strip().lower()
    if not query:
        return rows
    return [r for r in rows if r[0].lower().startswith(query) or query in (r[1] or "").lower()]


# ---- recents (saved automatically after each /station) ----

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
        print(f"Could not save recent stop: {exc}")


def recent(user_id, query="", limit=25):
    """[(stop_id, stop_name)] newest first, optionally filtered by what the user has typed."""
    try:
        with closing(_connect()) as conn:
            rows = conn.execute(
                "SELECT stop_id, stop_name FROM recent_stops WHERE user_id = ? ORDER BY last_used DESC",
                (user_id,),
            ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        print(f"Could not read recent stops: {exc}")
        return []
    return _filter(rows, query)[:limit]


# ---- saved / starred stops (only changed by the user) ----

def pin(user_id, stop_id, stop_name=None):
    """Save a stop. Returns 'saved', 'exists', 'full' or 'error'."""
    try:
        with closing(_connect()) as conn, conn:
            exists = conn.execute(
                "SELECT 1 FROM saved_stops WHERE user_id = ? AND stop_id = ?", (user_id, stop_id)
            ).fetchone()
            if exists:
                conn.execute(
                    "UPDATE saved_stops SET stop_name = COALESCE(?, stop_name) WHERE user_id = ? AND stop_id = ?",
                    (stop_name, user_id, stop_id),
                )
                return "exists"
            count = conn.execute("SELECT COUNT(*) FROM saved_stops WHERE user_id = ?", (user_id,)).fetchone()[0]
            if count >= MAX_PINNED:
                return "full"
            conn.execute(
                "INSERT INTO saved_stops (user_id, stop_id, stop_name, saved_at) VALUES (?, ?, ?, ?)",
                (user_id, stop_id, stop_name, time.time()),
            )
            return "saved"
    except (sqlite3.Error, OSError) as exc:
        print(f"Could not save stop: {exc}")
        return "error"


def unpin(user_id, stop_id):
    """Remove a saved stop. Returns True if it worked."""
    try:
        with closing(_connect()) as conn, conn:
            conn.execute("DELETE FROM saved_stops WHERE user_id = ? AND stop_id = ?", (user_id, stop_id))
        return True
    except (sqlite3.Error, OSError) as exc:
        print(f"Could not remove saved stop: {exc}")
        return False


def pinned(user_id, query=""):
    """[(stop_id, stop_name)] in the order the user saved them."""
    try:
        with closing(_connect()) as conn:
            rows = conn.execute(
                "SELECT stop_id, stop_name FROM saved_stops WHERE user_id = ? ORDER BY saved_at",
                (user_id,),
            ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        print(f"Could not read saved stops: {exc}")
        return []
    return _filter(rows, query)
