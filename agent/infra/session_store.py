# -*- coding: utf-8 -*-
"""SQLite-backed session store — replaces sessions.json for persistence.

Schema: one row per session_key, JSON columns for structured data.
WAL mode for concurrent read/write performance.
"""

import json
import logging
import os
import sqlite3
import threading

log = logging.getLogger("hub.session_store")


class SessionStore:
    """SQLite session persistence. Thread-safe via threading.Lock."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()
        log.info("SessionStore opened: %s", db_path)

    def _init_schema(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_key TEXT PRIMARY KEY,
                session_id TEXT,
                llm_config TEXT,
                history TEXT,
                updated_at REAL
            );
        """)
        self._migrate_add_last_summary()
        self._conn.commit()

    def _migrate_add_last_summary(self):
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(sessions)")}
        if "last_summary" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN last_summary TEXT")
            log.info("Migrated sessions table: added last_summary column")
        if "last_summarized_ts" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN last_summarized_ts TEXT")
            log.info("Migrated sessions table: added last_summarized_ts column")

    def load_all(self) -> dict:
        """Load all sessions into memory dict. Called once at startup."""
        sessions = {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_key, session_id, llm_config, history, updated_at, last_summary, last_summarized_ts "
                "FROM sessions"
            ).fetchall()
        for key, sid, llm_json, hist_json, updated, last_summary, last_summarized_ts in rows:
            entry = {}
            if sid:
                entry["session_id"] = sid
            if llm_json:
                try:
                    entry["llm_config"] = json.loads(llm_json)
                except (json.JSONDecodeError, TypeError):
                    pass
            if hist_json:
                try:
                    entry["history"] = json.loads(hist_json)
                except (json.JSONDecodeError, TypeError):
                    pass
            if updated:
                entry["updated_at"] = updated
            if last_summary:
                entry["last_summary"] = last_summary
            if last_summarized_ts:
                entry["last_summarized_ts"] = last_summarized_ts
            if entry:
                sessions[key] = entry
        log.info("Loaded %d sessions from SQLite", len(sessions))
        return sessions

    def save(self, session_key: str, entry: dict):
        """Upsert a single session entry."""
        sid = entry.get("session_id")
        llm = json.dumps(entry["llm_config"], ensure_ascii=False) if entry.get("llm_config") else None
        hist = json.dumps(entry["history"], ensure_ascii=False) if entry.get("history") else None
        updated = entry.get("updated_at")
        last_summary = entry.get("last_summary")
        last_summarized_ts = entry.get("last_summarized_ts")
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (session_key, session_id, llm_config, history, updated_at, last_summary, last_summarized_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_key) DO UPDATE SET "
                "  session_id=excluded.session_id, "
                "  llm_config=excluded.llm_config, "
                "  history=excluded.history, "
                "  updated_at=excluded.updated_at, "
                "  last_summary=excluded.last_summary, "
                "  last_summarized_ts=excluded.last_summarized_ts",
                (session_key, sid, llm, hist, updated, last_summary, last_summarized_ts),
            )
            self._conn.commit()

    def delete(self, session_key: str):
        """Remove a session."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM sessions WHERE session_key=?", (session_key,)
            )
            self._conn.commit()

    def save_all(self, sessions: dict):
        """Bulk save all sessions. Used for migration."""
        with self._lock:
            for key, entry in sessions.items():
                sid = entry.get("session_id")
                llm = json.dumps(entry["llm_config"], ensure_ascii=False) if entry.get("llm_config") else None
                hist = json.dumps(entry["history"], ensure_ascii=False) if entry.get("history") else None
                updated = entry.get("updated_at")
                last_summary = entry.get("last_summary")
                last_summarized_ts = entry.get("last_summarized_ts")
                self._conn.execute(
                    "INSERT OR REPLACE INTO sessions "
                    "(session_key, session_id, llm_config, history, updated_at, last_summary, last_summarized_ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (key, sid, llm, hist, updated, last_summary, last_summarized_ts),
                )
            self._conn.commit()
        log.info("Bulk saved %d sessions to SQLite", len(sessions))

    def close(self):
        self._conn.close()
