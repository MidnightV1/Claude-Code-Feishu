# -*- coding: utf-8 -*-
"""Exploration queue and log — the Superintendent's autonomous work pipeline.

Exploration queue: persistent task list for autonomous investigation.
Exploration log: JSONL record of completed explorations.

Sources: user fragments, work discoveries, external signals,
periodic scans, cognition evolution, feishu tasks, error tracker.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from enum import IntEnum
from typing import Optional

from agent.infra.store import load_json, save_json

log = logging.getLogger("hub.exploration")

QUEUE_PATH = "data/exploration_queue.json"
LOG_PATH = "data/exploration_log.jsonl"
QUEUE_MAX_SIZE = 30


class Priority(IntEnum):
    """Exploration task priority."""
    P0_BLOCKING = 0    # Blocking issue, handle immediately
    P1_HIGH = 1        # High value, daily exploration priority
    P2_NORMAL = 2      # Valuable but not urgent
    P3_WATCHING = 3    # Observing, waiting for more signals


class TaskSource(str):
    """Where the exploration task came from."""
    USER_FRAGMENT = "user_fragment"       # User mentioned but didn't expand
    WORK_DISCOVERY = "work_discovery"     # Found during implementation
    EXTERNAL_SIGNAL = "external_signal"   # API change, dependency update, etc.
    PERIODIC_SCAN = "periodic_scan"       # Skill health, code quality, etc.
    COGNITION = "cognition"              # COGNITION.md / soul file calibration
    FEISHU_TASK = "feishu_task"          # Unfinished feishu task
    ERROR_TRACKER = "error_tracker"      # Bug from Error Tracker bitable


@dataclass
class ExplorationTask:
    """A task in the exploration queue."""
    id: str = ""
    title: str = ""
    description: str = ""
    source: str = ""                     # TaskSource value
    source_context: str = ""             # Original context (e.g. user quote)
    priority: int = Priority.P2_NORMAL
    autonomy_level: int = 0              # AutonomyLevel value (0=L0, 1=L1, 2=L2)
    pillar: str = ""                     # "collect" | "internalize" | "feedback"
    estimated_messages: int = 10         # Estimated Sonnet messages to complete
    status: str = "pending"              # "pending" | "in_progress" | "done" | "dropped"
    result_summary: str = ""             # Filled when done
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float = 0.0

    def __post_init__(self):
        if not self.id:
            import uuid
            self.id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = time.time()
            self.updated_at = self.created_at


@dataclass
class ExplorationLog:
    """Record of a completed exploration."""
    task_id: str = ""
    title: str = ""
    pillar: str = ""
    source: str = ""
    priority: int = 0
    messages_used: int = 0
    summary: str = ""
    action_taken: str = ""               # What was done as a result
    autonomy_level: int = 0
    user_rating: str = ""                # "up" | "down" | "" (unrated)
    timestamp: float = field(default_factory=time.time)


class ExplorationQueue:
    """Manages the exploration queue with persistence."""

    def __init__(self, queue_path: str = QUEUE_PATH):
        self.queue_path = queue_path
        self._tasks: list[ExplorationTask] = []

    async def load(self):
        """Load queue from disk."""
        data = await load_json(self.queue_path, {"version": 1, "tasks": []})
        self._tasks = [
            ExplorationTask(**{
                k: v for k, v in t.items()
                if k in ExplorationTask.__dataclass_fields__
            })
            for t in data.get("tasks", [])
        ]
        log.info("Exploration queue loaded: %d tasks", len(self._tasks))

    async def save(self):
        """Save queue to disk."""
        data = {
            "version": 1,
            "tasks": [asdict(t) for t in self._tasks],
        }
        await save_json(self.queue_path, data)

    # ── CRUD ──

    async def add(self, task: ExplorationTask) -> ExplorationTask:
        """Add a task to the queue. Auto-prunes if over limit."""
        self._tasks.append(task)
        await self._prune()
        await self.save()
        log.info("Exploration task added: [P%d] %s", task.priority, task.title)
        return task

    async def update(self, task_id: str, **patch) -> ExplorationTask | None:
        """Update a task by ID prefix."""
        task = self._find(task_id)
        if not task:
            return None
        for k, v in patch.items():
            if hasattr(task, k):
                setattr(task, k, v)
        task.updated_at = time.time()
        await self.save()
        return task

    async def complete(self, task_id: str, summary: str = "") -> ExplorationTask | None:
        """Mark a task as done."""
        task = self._find(task_id)
        if not task:
            return None
        task.status = "done"
        task.result_summary = summary
        task.completed_at = time.time()
        task.updated_at = time.time()
        await self.save()
        log.info("Exploration task completed: %s", task.title)
        return task

    async def drop(self, task_id: str, reason: str = "") -> ExplorationTask | None:
        """Drop a task (low priority pruned or no longer relevant)."""
        task = self._find(task_id)
        if not task:
            return None
        task.status = "dropped"
        task.result_summary = reason or "Dropped"
        task.updated_at = time.time()
        await self.save()
        return task

    async def remove(self, task_id: str) -> bool:
        """Remove a task entirely (completed/dropped cleanup)."""
        before = len(self._tasks)
        self._tasks = [t for t in self._tasks if not t.id.startswith(task_id)]
        if len(self._tasks) < before:
            await self.save()
            return True
        return False

    # ── Queries ──

    def list_pending(self, priority: Priority | None = None) -> list[ExplorationTask]:
        """List pending tasks, sorted by priority then creation time."""
        tasks = [t for t in self._tasks if t.status == "pending"]
        if priority is not None:
            tasks = [t for t in tasks if t.priority <= priority]
        tasks.sort(key=lambda t: (t.priority, t.created_at))
        return tasks

    def list_all(self) -> list[ExplorationTask]:
        """List all tasks regardless of status."""
        return list(self._tasks)

    def count_by_status(self) -> dict[str, int]:
        """Count tasks by status."""
        counts: dict[str, int] = {}
        for t in self._tasks:
            counts[t.status] = counts.get(t.status, 0) + 1
        return counts

    def estimate_budget(self, max_messages: int = 50) -> list[ExplorationTask]:
        """Select tasks that fit within a message budget, prioritized."""
        pending = self.list_pending()
        selected = []
        remaining = max_messages
        for task in pending:
            if task.estimated_messages <= remaining:
                selected.append(task)
                remaining -= task.estimated_messages
            if remaining <= 0:
                break
        return selected

    def _find(self, task_id: str) -> ExplorationTask | None:
        for t in self._tasks:
            if t.id.startswith(task_id):
                return t
        return None

    async def _prune(self):
        """Remove lowest-priority tasks if queue exceeds max size."""
        active = [t for t in self._tasks if t.status in ("pending", "in_progress")]
        if len(active) <= QUEUE_MAX_SIZE:
            return

        # Sort: lowest priority (highest number) + oldest first → candidates for removal
        active.sort(key=lambda t: (-t.priority, t.created_at))
        to_drop = active[:len(active) - QUEUE_MAX_SIZE]
        drop_ids = {t.id for t in to_drop}

        for t in self._tasks:
            if t.id in drop_ids:
                t.status = "dropped"
                t.result_summary = "Auto-pruned: queue overflow"
                t.updated_at = time.time()
                log.info("Auto-pruned: [P%d] %s", t.priority, t.title)


# ── Log functions ──

async def append_log(entry: ExplorationLog) -> None:
    """Append a completed exploration to the log."""
    import asyncio

    line = json.dumps(asdict(entry), ensure_ascii=False)

    def _write():
        os.makedirs("data", exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    await asyncio.to_thread(_write)


def read_log_sync(hours: float = 24) -> list[dict]:
    """Read recent exploration log entries."""
    cutoff = time.time() - hours * 3600
    entries = []
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("timestamp", 0) >= cutoff:
                        entries.append(entry)
                except (json.JSONDecodeError, KeyError):
                    continue
    except FileNotFoundError:
        pass
    entries.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    return entries


async def read_log(hours: float = 24) -> list[dict]:
    """Async wrapper for read_log_sync."""
    import asyncio
    return await asyncio.to_thread(read_log_sync, hours)


async def rate_log_entry(task_id: str, rating: str) -> bool:
    """Record user rating for an exploration log entry.

    Args:
        task_id: The task_id to match in the log.
        rating: "up" or "down".

    Returns:
        True if the entry was found and updated.
    """
    import asyncio

    def _update():
        if not os.path.exists(LOG_PATH):
            return False
        lines = []
        found = False
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    lines.append(line)
                    continue
                try:
                    entry = json.loads(stripped)
                    if entry.get("task_id") == task_id and not entry.get("user_rating"):
                        entry["user_rating"] = rating
                        found = True
                    lines.append(json.dumps(entry, ensure_ascii=False) + "\n")
                except (json.JSONDecodeError, KeyError):
                    lines.append(line)
        if found:
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(lines)
        return found

    return await asyncio.to_thread(_update)
