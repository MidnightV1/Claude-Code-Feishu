# -*- coding: utf-8 -*-
"""Persistent message state machine — SQLite primary + JSONL audit log.

Provides three-layer dedup:
1. message_id exact match
2. content_hash + time window (per msg_type)
3. State guard (completed messages never re-processed)
"""

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("hub.message_store")

# Dedup windows per message type (seconds)
DEDUP_WINDOWS = {
    "command": 60,     # 1 minute — block re-delivery but allow legitimate re-execution
    "chat": 300,       # 5 minutes
    "image": 1800,     # 30 minutes
    "file": 1800,      # 30 minutes
}

DEFAULT_RETENTION_DAYS = 7
JSONL_MAX_DAYS = 7


class MessageStore:
    """SQLite-backed message state with JSONL audit trail."""

    def __init__(self, data_dir: str):
        self._data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

        self._db_path = os.path.join(data_dir, "messages.db")
        self._jsonl_path = os.path.join(data_dir, "messages.jsonl")
        self._lock = threading.Lock()

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()
        self._startup_recovery()

        log.info("MessageStore opened: %s", self._db_path)

    def _init_schema(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                msg_type TEXT NOT NULL DEFAULT 'chat',
                state TEXT NOT NULL DEFAULT 'received',
                batch_key TEXT,
                response_id TEXT,
                sender_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_content_hash
                ON messages(content_hash, created_at);
            CREATE INDEX IF NOT EXISTS idx_state
                ON messages(state);
        """)
        self._conn.commit()

    def _startup_recovery(self):
        """Reset processing → failed on startup (unfinished work from crash)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE messages SET state='failed', updated_at=? "
                "WHERE state='processing'",
                (time.time(),),
            )
            if cur.rowcount:
                self._conn.commit()
                log.info("Startup recovery: %d processing → failed", cur.rowcount)

    # ── Public API ──

    def check_dup(self, message_id: str, content_hash: str,
                  msg_type: str = "chat") -> bool:
        """Check if message is a duplicate. Returns True to drop."""
        with self._lock:
            # Layer 1: exact message_id
            row = self._conn.execute(
                "SELECT state FROM messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if row:
                log.debug("Dedup: exact message_id match %s (state=%s)",
                          message_id, row[0])
                return True

            # Layer 2: content_hash + window
            window = DEDUP_WINDOWS.get(msg_type, 300)
            if window is None:
                # command: no window, always match
                existing = self._conn.execute(
                    "SELECT message_id FROM messages WHERE content_hash=? "
                    "LIMIT 1",
                    (content_hash,),
                ).fetchone()
            else:
                cutoff = time.time() - window
                existing = self._conn.execute(
                    "SELECT message_id FROM messages "
                    "WHERE content_hash=? AND created_at>? "
                    "LIMIT 1",
                    (content_hash, cutoff),
                ).fetchone()

            if existing:
                log.info("Content dedup: %s matches existing %s (type=%s)",
                         message_id, existing[0], msg_type)
                return True

            return False

    def record(self, message_id: str, content_hash: str,
               msg_type: str = "chat", sender_id: str = "",
               batch_key: str = ""):
        """Record a new message as received."""
        now = time.time()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO messages "
                    "(message_id, content_hash, msg_type, state, "
                    " batch_key, sender_id, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (message_id, content_hash, msg_type, "received",
                     batch_key, sender_id, now, now),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                pass  # race: already recorded
        self._jsonl_append(message_id, "record",
                           {"content_hash": content_hash, "msg_type": msg_type,
                            "sender_id": sender_id})

    def update_state(self, message_ids: list[str] | str, state: str,
                     response_id: str | None = None):
        """Transition message(s) to a new state."""
        if isinstance(message_ids, str):
            message_ids = [message_ids]
        if not message_ids:
            return
        now = time.time()
        with self._lock:
            for mid in message_ids:
                # Don't allow completed → processing regression
                cur = self._conn.execute(
                    "SELECT state FROM messages WHERE message_id=?",
                    (mid,),
                )
                row = cur.fetchone()
                if row and row[0] == "completed" and state != "completed":
                    continue

                params = [state, now]
                set_clause = "state=?, updated_at=?"
                if response_id:
                    set_clause += ", response_id=?"
                    params.append(response_id)
                params.append(mid)

                self._conn.execute(
                    f"UPDATE messages SET {set_clause} WHERE message_id=?",
                    params,
                )
            self._conn.commit()
        for mid in message_ids:
            self._jsonl_append(mid, "state_change",
                               {"state": state, "response_id": response_id})

    def get_state(self, message_id: str) -> str | None:
        """Get current state of a message."""
        row = self._conn.execute(
            "SELECT state FROM messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
        return row[0] if row else None

    def get_completed_chat_history(self, sender_id: str,
                                   limit: int = 30) -> list[dict]:
        """Get completed chat messages for recovery context."""
        rows = self._conn.execute(
            "SELECT message_id, content_hash, response_id, created_at "
            "FROM messages "
            "WHERE sender_id=? AND msg_type='chat' AND state='completed' "
            "  AND response_id IS NOT NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (sender_id, limit),
        ).fetchall()
        return [
            {"message_id": r[0], "content_hash": r[1],
             "response_id": r[2], "created_at": r[3]}
            for r in reversed(rows)
        ]

    def cleanup(self, retention_days: int = DEFAULT_RETENTION_DAYS):
        """Remove records older than retention period."""
        cutoff = time.time() - retention_days * 86400
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM messages WHERE created_at < ?", (cutoff,)
            )
            if cur.rowcount:
                self._conn.commit()
                log.info("Cleanup: removed %d messages older than %d days",
                         cur.rowcount, retention_days)

        # Also trim JSONL
        self._trim_jsonl(retention_days)

    def close(self):
        """Close database connection."""
        self._conn.close()

    # ── Helpers ──

    def _jsonl_append(self, message_id: str, action: str,
                      detail: dict | None = None):
        """Append one line to the audit log."""
        entry = {
            "ts": time.time(),
            "message_id": message_id,
            "action": action,
        }
        if detail:
            entry.update(detail)
        try:
            with open(self._jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass  # audit log failure is non-fatal

    def _trim_jsonl(self, retention_days: int):
        """Remove JSONL lines older than retention period."""
        if not os.path.exists(self._jsonl_path):
            return
        cutoff = time.time() - retention_days * 86400
        try:
            kept = []
            with open(self._jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("ts", 0) >= cutoff:
                            kept.append(line)
                    except json.JSONDecodeError:
                        continue
            tmp = self._jsonl_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for line in kept:
                    f.write(line + "\n")
            os.replace(tmp, self._jsonl_path)
        except Exception as e:
            log.warning("JSONL trim failed: %s", e)

    # ── Stats ──

    def stats(self) -> dict:
        """Return message counts by state."""
        rows = self._conn.execute(
            "SELECT state, COUNT(*) FROM messages GROUP BY state"
        ).fetchall()
        return dict(rows)


# ── Content hash helpers ──

def content_hash(sender_id: str, text: str) -> str:
    """Hash for chat/command dedup: sender + normalized text."""
    normalized = text.strip().lower()
    return hashlib.sha256(f"{sender_id}:{normalized}".encode()).hexdigest()[:32]


def media_hash(sender_id: str, media_key: str) -> str:
    """Hash for image/file dedup: sender + media key."""
    return hashlib.sha256(f"{sender_id}:{media_key}".encode()).hexdigest()[:32]
