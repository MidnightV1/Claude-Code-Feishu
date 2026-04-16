# -*- coding: utf-8 -*-
"""Priority merge queue with file persistence for MAQS/MADS."""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict

from agent.infra.store import load_json, save_json

log = logging.getLogger("hub.merge_queue")

QUEUE_PATH = "data/merge_queue.json"


@dataclass
class MergeRequest:
    id: str
    branch: str
    wt_path: str
    priority: int       # 0=P0, 1=P1, 2=P2
    ticket_id: str
    enqueued_at: float = field(default_factory=time.time)

    @staticmethod
    def make(branch: str, wt_path: str, priority: int, ticket_id: str) -> "MergeRequest":
        return MergeRequest(
            id=str(uuid.uuid4()),
            branch=branch,
            wt_path=wt_path,
            priority=priority,
            ticket_id=ticket_id,
        )


class MergeQueue:
    """Priority merge queue with asyncio-safe persistence.

    Requests are sorted by (priority, enqueued_at): P0 always before P1/P2,
    ties resolved FIFO. asyncio.Lock serialises concurrent enqueue/dequeue.
    """

    def __init__(self, queue_path: str = QUEUE_PATH):
        self.queue_path = queue_path
        self._requests: list[MergeRequest] = []
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def load(self):
        """Load queue from disk."""
        data = await load_json(self.queue_path, {"requests": []})
        self._requests = [
            MergeRequest(**{k: v for k, v in r.items()
                            if k in MergeRequest.__dataclass_fields__})
            for r in data.get("requests", [])
        ]
        if self._requests:
            log.info("MergeQueue loaded: %d pending request(s)", len(self._requests))

    async def _save(self):
        await save_json(self.queue_path, {
            "requests": [asdict(r) for r in self._requests],
        })

    async def enqueue(self, request: MergeRequest):
        """Add a merge request and persist."""
        async with self._get_lock():
            self._requests.append(request)
            await self._save()
            log.debug("MergeQueue enqueued [P%d] %s", request.priority, request.ticket_id)

    async def process_next(self) -> MergeRequest | None:
        """Pop and return the highest-priority request (min priority, FIFO on ties)."""
        async with self._get_lock():
            if not self._requests:
                return None
            self._requests.sort(key=lambda r: (r.priority, r.enqueued_at))
            req = self._requests.pop(0)
            await self._save()
            log.debug("MergeQueue dequeued [P%d] %s", req.priority, req.ticket_id)
            return req

    def __len__(self) -> int:
        return len(self._requests)
