# -*- coding: utf-8 -*-
"""Card action callback router — handles button clicks from interactive cards.

Responsibilities:
- Route card action events to registered handlers
- Persist action results in SQLite for context continuity
- Inject button selections into conversation history
"""

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Callable, Awaitable

log = logging.getLogger("hub.card_actions")


class CardActionStore:
    """SQLite-backed card action persistence.

    Stores button click results so they can be:
    1. Picked up by pending async waiters (via asyncio.Event)
    2. Included in conversation history for context continuity
    """

    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        db_path = os.path.join(data_dir, "card_actions.db")
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.Lock()
        self._init_schema()
        log.info("CardActionStore opened: %s", db_path)

    def _init_schema(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS card_actions (
                action_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                chat_id TEXT,
                sender_id TEXT,
                action_type TEXT NOT NULL,
                payload TEXT,
                choice TEXT,
                choice_label TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL,
                resolved_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_ca_status
                ON card_actions(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_ca_message
                ON card_actions(message_id);
        """)
        self._conn.commit()

    def create_pending(self, action_id: str, message_id: str,
                       action_type: str, chat_id: str = "",
                       sender_id: str = "",
                       payload: dict | None = None) -> None:
        """Record a new pending card action (button sent, awaiting click)."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO card_actions "
                "(action_id, message_id, chat_id, sender_id, action_type, "
                " payload, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (action_id, message_id, chat_id, sender_id, action_type,
                 json.dumps(payload or {}), "pending", now),
            )
            self._conn.commit()

    def resolve(self, action_id: str, choice: str,
                choice_label: str = "") -> bool:
        """Mark action as resolved with the user's choice. Returns success."""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE card_actions SET status='resolved', choice=?, "
                "choice_label=?, resolved_at=? "
                "WHERE action_id=? AND status='pending'",
                (choice, choice_label, now, action_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get(self, action_id: str) -> dict | None:
        """Get action record by ID."""
        with self._lock:
            row = self._conn.execute(
                "SELECT action_id, message_id, chat_id, sender_id, "
                "action_type, payload, choice, choice_label, status, "
                "created_at, resolved_at "
                "FROM card_actions WHERE action_id=?",
                (action_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "action_id": row[0], "message_id": row[1],
            "chat_id": row[2], "sender_id": row[3],
            "action_type": row[4], "payload": json.loads(row[5] or "{}"),
            "choice": row[6], "choice_label": row[7],
            "status": row[8], "created_at": row[9], "resolved_at": row[10],
        }

    def find_by_message(self, message_id: str) -> dict | None:
        """Find action by the card message_id (from button value context)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT action_id FROM card_actions WHERE message_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (message_id,),
            ).fetchone()
        if not row:
            return None
        return self.get(row[0])

    def expire_old(self, max_age_seconds: int = 3600) -> int:
        """Expire pending actions older than max_age. Returns count."""
        cutoff = time.time() - max_age_seconds
        with self._lock:
            cur = self._conn.execute(
                "UPDATE card_actions SET status='expired' "
                "WHERE status='pending' AND created_at < ?",
                (cutoff,),
            )
            self._conn.commit()
            return cur.rowcount

    def cleanup(self, retention_days: int = 7) -> int:
        """Remove records older than retention period."""
        cutoff = time.time() - retention_days * 86400
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM card_actions WHERE created_at < ?", (cutoff,),
            )
            self._conn.commit()
            return cur.rowcount

    def close(self):
        self._conn.close()


# ── Action handler type ──
# handler(action_type, value, operator_id, context) -> (choice, label, response_card)
ActionHandler = Callable[
    [str, dict, str, dict],
    Awaitable[tuple[str, str, dict | None]]
]


class CardActionRouter:
    """Routes card action callbacks to registered handlers.

    Each handler returns (choice, choice_label, optional_response_card).
    """

    def __init__(self, store: CardActionStore):
        self._store = store
        self._handlers: dict[str, ActionHandler] = {}
        # In-memory waiters: action_id → asyncio.Event
        self._waiters: dict[str, Any] = {}

    def register(self, action_type: str, handler: ActionHandler) -> None:
        """Register a handler for an action type."""
        self._handlers[action_type] = handler

    def create_waiter(self, action_id: str) -> Any:
        """Create an asyncio.Event for waiting on an action result."""
        import asyncio
        event = asyncio.Event()
        self._waiters[action_id] = event
        return event

    def remove_waiter(self, action_id: str) -> None:
        """Clean up a waiter."""
        self._waiters.pop(action_id, None)

    async def handle(self, action_type: str, value: dict,
                     operator_id: str, context: dict) -> dict | None:
        """Process a card action callback.

        Args:
            action_type: The action identifier from button value.
            value: Full button value dict.
            operator_id: Who clicked (open_id).
            context: Callback context (message_id, chat_id).

        Returns:
            Response dict for P2CardActionTriggerResponse, or None.
        """
        handler = self._handlers.get(action_type)
        if not handler:
            log.warning("No handler for action_type=%s", action_type)
            return None

        try:
            choice, label, response_card = await handler(
                action_type, value, operator_id, context
            )
        except Exception as e:
            log.error("Card action handler error: %s (type=%s)", e, action_type)
            return {"toast": {"type": "error", "content": f"处理失败: {e}"}}

        # Resolve in store
        action_id = value.get("action_id", "")
        if action_id:
            self._store.resolve(action_id, choice, label)
            # Wake up any waiter
            waiter = self._waiters.pop(action_id, None)
            if waiter:
                waiter.set()

        # Build response
        resp: dict = {}
        if label:
            resp["toast"] = {"type": "success", "content": label}
        if response_card:
            resp["card"] = {
                "type": "raw",
                "data": response_card,
            }
        return resp if resp else None
