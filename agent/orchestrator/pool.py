# -*- coding: utf-8 -*-
"""Sonnet worker pool — parallel subprocess execution with concurrency control."""

import asyncio
import time
import logging
from typing import Callable, Awaitable

from agent.llm.claude import ClaudeCli
from agent.orchestrator.models import SubTask
from agent.orchestrator.prompts import WORKER_SYSTEM

log = logging.getLogger("hub.worker_pool")


class WorkerPool:
    def __init__(self, claude_cli: ClaudeCli, max_concurrent: int = 3):
        self._claude = claude_cli
        self._sem = asyncio.Semaphore(max_concurrent)
        self.max_concurrent = max_concurrent

    async def execute_one(
        self,
        subtask: SubTask,
        system_prompt: str = "",
        on_update: Callable[[], Awaitable[None]] | None = None,
    ) -> SubTask:
        """Execute a single subtask under semaphore control."""
        async with self._sem:
            subtask.status = "running"
            subtask.started_at = time.time()
            log.info("Worker starting subtask %s: %s", subtask.id, subtask.title)
            if on_update:
                await on_update()

            result = await self._claude.run(
                subtask.prompt,
                model="sonnet",
                system_prompt=system_prompt or WORKER_SYSTEM,
                setting_sources="local",
            )

            subtask.finished_at = time.time()
            duration = int(subtask.finished_at - subtask.started_at)

            if result.is_error:
                subtask.status = "failed"
                subtask.error = result.text[:1000]
                log.warning("Worker subtask %s failed (%ds): %s",
                            subtask.id, duration, subtask.error[:200])
            else:
                subtask.status = "done"
                subtask.result = result.text
                log.info("Worker subtask %s done (%ds, %d chars)",
                         subtask.id, duration, len(result.text))

            if on_update:
                await on_update()
            return subtask

    async def execute_all(
        self,
        subtasks: list[SubTask],
        system_prompt: str = "",
        on_update: Callable[[], Awaitable[None]] | None = None,
    ):
        """Execute all subtasks in parallel (bounded by semaphore)."""
        tasks = [
            asyncio.create_task(
                self.execute_one(s, system_prompt, on_update)
            )
            for s in subtasks
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # Mark any subtask that raised an unexpected exception as failed
        for subtask, result in zip(subtasks, results):
            if isinstance(result, BaseException) and subtask.status == "running":
                subtask.status = "failed"
                subtask.error = f"{type(result).__name__}: {result}"
                subtask.finished_at = time.time()
                log.error("Worker subtask %s raised: %s", subtask.id, subtask.error)
