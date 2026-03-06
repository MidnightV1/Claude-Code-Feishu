# -*- coding: utf-8 -*-
"""In-process cron scheduler. Pattern from OpenClaw src/cron/service/timer.ts."""

import asyncio
import time
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Callable, Awaitable
from croniter import croniter

from agent.infra.models import (
    CronJob, CronSchedule, CronJobState, LLMConfig, LLMResult,
    cron_job_from_dict, to_dict,
)
from agent.infra.store import load_json, save_json
from agent.llm.router import LLMRouter
from agent.platforms.feishu.dispatcher import Dispatcher

log = logging.getLogger("hub.scheduler")

MAX_TIMER_DELAY = 60.0       # seconds, prevents drift
ERROR_BACKOFF = [30, 60, 300, 900, 3600]  # exponential backoff schedule


class CronScheduler:
    def __init__(self, config: dict, router: LLMRouter, dispatcher: Dispatcher):
        self.enabled = config.get("enabled", True)
        self.store_path = config.get("store_path", "data/jobs.json")
        self.router = router
        self.dispatcher = dispatcher
        self._jobs: list[CronJob] = []
        self._timer: asyncio.TimerHandle | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._handlers: dict[str, Callable[..., Awaitable[str]]] = {}

    def register_handler(self, name: str, fn: Callable[..., Awaitable[str]]):
        """Register a named handler callable. Jobs with matching handler field bypass LLM router."""
        self._handlers[name] = fn
        log.info("Handler registered: %s", name)

    async def start(self):
        if not self.enabled:
            log.info("Scheduler disabled")
            return
        await self._load_store()
        self._clear_stale_running()
        await self._run_missed_jobs()
        self._recompute_all()
        await self._save_store()
        self._arm_timer()
        log.info("Scheduler started, %d jobs loaded", len(self._jobs))

    async def reload(self):
        """Hot-reload jobs from disk (triggered by SIGUSR1)."""
        old_count = len(self._jobs)
        await self._load_store()
        self._recompute_all()
        self._arm_timer()
        log.info("Scheduler reloaded: %d → %d jobs", old_count, len(self._jobs))

    async def stop(self):
        if self._timer:
            self._timer.cancel()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._save_store()
        log.info("Scheduler stopped")

    # ═══ CRUD ═══

    async def add_job(
        self, name: str, schedule_expr: str, prompt: str = "",
        llm: LLMConfig | None = None, one_shot: bool = False,
        handler: str = "",
    ) -> CronJob:
        if handler and handler not in self._handlers:
            raise ValueError(f"Unknown handler: {handler}")
        schedule = self._parse_schedule(schedule_expr)
        job = CronJob(
            name=name,
            schedule=schedule,
            prompt=prompt,
            handler=handler,
            llm=llm or LLMConfig(),
            deliver_to_feishu=not handler,  # handler jobs manage own notifications
            one_shot=one_shot,
        )
        job.state.next_run_at = self._compute_next_run(job)
        self._jobs.append(job)
        await self._save_store()
        self._arm_timer()
        log.info("Job added: %s (%s) next=%s", job.id, name, job.state.next_run_at)
        return job

    async def remove_job(self, job_id: str) -> bool:
        before = len(self._jobs)
        self._jobs = [j for j in self._jobs if not j.id.startswith(job_id)]
        if len(self._jobs) < before:
            await self._save_store()
            self._arm_timer()
            return True
        return False

    async def update_job(self, job_id: str, **patch) -> CronJob | None:
        job = self._find_job(job_id)
        if not job:
            return None
        for k, v in patch.items():
            if hasattr(job, k):
                setattr(job, k, v)
        job.updated_at = time.time()
        if "schedule" in patch or "enabled" in patch:
            job.state.next_run_at = self._compute_next_run(job) if job.enabled else None
        await self._save_store()
        self._arm_timer()
        return job

    def list_jobs(self, include_disabled=False) -> list[CronJob]:
        if include_disabled:
            return list(self._jobs)
        return [j for j in self._jobs if j.enabled]

    async def run_job(self, job_id: str) -> str:
        """Trigger a job immediately. Returns result text."""
        job = self._find_job(job_id)
        if not job:
            return f"Job {job_id} not found"
        result = await self._execute_job(job)
        return result

    def _find_job(self, job_id: str) -> CronJob | None:
        for j in self._jobs:
            if j.id.startswith(job_id):
                return j
        return None

    # ═══ Timer ═══

    def _arm_timer(self):
        if self._timer:
            self._timer.cancel()

        enabled = [j for j in self._jobs if j.enabled and j.state.next_run_at]
        if not enabled:
            return

        nearest = min(j.state.next_run_at for j in enabled)
        delay = max(0, min(nearest - time.time(), MAX_TIMER_DELAY))

        loop = asyncio.get_running_loop()
        self._timer = loop.call_later(delay, self._on_timer_fire)

    def _on_timer_fire(self):
        if self._running:
            # Re-check later (watchdog)
            loop = asyncio.get_running_loop()
            self._timer = loop.call_later(MAX_TIMER_DELAY, self._on_timer_fire)
            return
        self._task = asyncio.create_task(self._on_timer())

    async def _on_timer(self):
        self._running = True
        try:
            now = time.time()
            due = [
                j for j in self._jobs
                if j.enabled and j.state.next_run_at and j.state.next_run_at <= now
            ]

            for job in due:
                await self._execute_job(job)

            # Recompute after execution (jobs may have become due during run)
            self._recompute_all()
            await self._save_store()
        except Exception as e:
            log.error("Scheduler tick error: %s", e)
        finally:
            self._running = False
            self._arm_timer()

    # ═══ Execution ═══

    async def _execute_job(self, job: CronJob) -> str:
        log.info("Executing job %s (%s)", job.id, job.name)
        job.state.last_run_at = time.time()

        # Advance next_run_at BEFORE execution to prevent re-execution on crash
        if not job.one_shot:
            job.state.next_run_at = self._compute_next_run(job)
        await self._save_store()

        try:
            # Handler jobs bypass the LLM router
            if job.handler:
                fn = self._handlers.get(job.handler)
                if not fn:
                    raise ValueError(f"Unknown handler: {job.handler}")
                text = await fn()
                result = LLMResult(text=text)
            else:
                result = await self.router.run(
                    prompt=job.prompt,
                    llm_config=job.llm,
                    session_key=None,  # cron jobs don't resume sessions
                )

            if result.is_error:
                job.state.last_status = "error"
                job.state.last_error = result.text[:500]
                job.state.consecutive_errors += 1
                self._apply_backoff(job)
                log.warning("Job %s error: %s", job.id, result.text[:200])
            else:
                job.state.last_status = "ok"
                job.state.last_error = None
                job.state.consecutive_errors = 0
                log.info("Job %s ok (%dms, $%.4f)", job.id, result.duration_ms, result.cost_usd)

                # Deliver to Feishu
                if job.deliver_to_feishu and result.text.strip():
                    header = f"**[{job.name}]** ({job.llm.provider}/{job.llm.model})\n\n"
                    await self.dispatcher.send_to_delivery_target(header + result.text)

            # One-shot: disable after run
            if job.one_shot:
                job.enabled = False
                log.info("One-shot job %s disabled", job.id)

            await self._save_store()
            return result.text

        except Exception as e:
            job.state.last_status = "error"
            job.state.last_error = str(e)
            job.state.consecutive_errors += 1
            self._apply_backoff(job)
            await self._save_store()
            log.error("Job %s exception: %s", job.id, e)
            return f"[Exception: {e}]"

    # ═══ Schedule Computation ═══

    def _compute_next_run(self, job: CronJob) -> float | None:
        s = job.schedule
        now = time.time()

        if s.kind == "cron" and s.expr:
            tz = ZoneInfo(s.tz)
            dt_now = datetime.now(tz)
            cron = croniter(s.expr, dt_now)
            return cron.get_next(float)

        elif s.kind == "every" and s.every_seconds:
            base = job.state.last_run_at or now
            return base + s.every_seconds

        elif s.kind == "at" and s.at_time:
            try:
                target = datetime.fromisoformat(s.at_time).timestamp()
                return target if target > now else None
            except ValueError:
                return None

        return None

    def _apply_backoff(self, job: CronJob):
        """Apply exponential backoff on consecutive errors."""
        idx = min(job.state.consecutive_errors - 1, len(ERROR_BACKOFF) - 1)
        backoff = ERROR_BACKOFF[max(0, idx)]
        normal_next = self._compute_next_run(job) or (time.time() + 3600)
        job.state.next_run_at = max(normal_next, time.time() + backoff)

    def _recompute_all(self):
        for job in self._jobs:
            if job.enabled and not job.one_shot:
                if job.state.next_run_at is None or job.state.next_run_at <= time.time():
                    job.state.next_run_at = self._compute_next_run(job)

    def _clear_stale_running(self):
        """Clear stale running markers from unclean shutdown."""
        for job in self._jobs:
            if job.state.last_status == "running":
                job.state.last_status = "error"
                job.state.last_error = "Interrupted by shutdown"

    async def _run_missed_jobs(self):
        now = time.time()
        missed = [
            j for j in self._jobs
            if j.enabled and j.state.next_run_at and j.state.next_run_at < now
            and not (j.one_shot and j.state.last_status == "ok")
            # Guard: if last_run_at >= next_run_at, the job already ran for this slot
            and not (j.state.last_run_at and j.state.last_run_at >= j.state.next_run_at)
        ]
        if missed:
            log.info("Running %d missed jobs", len(missed))
            for job in missed:
                await self._execute_job(job)

    # ═══ Persistence ═══

    async def _load_store(self):
        data = await load_json(self.store_path, {"version": 1, "jobs": []})
        self._jobs = [cron_job_from_dict(j) for j in data.get("jobs", [])]

    async def _save_store(self):
        data = {
            "version": 1,
            "jobs": [to_dict(j) for j in self._jobs],
        }
        await save_json(self.store_path, data)

    # ═══ Schedule Parsing ═══

    @staticmethod
    def _parse_schedule(expr: str) -> CronSchedule:
        """Parse schedule expression.

        Formats:
          "*/5 * * * *"  -> cron
          "30m" / "2h"   -> every
          "2026-03-01T09:00" -> at
        """
        expr = expr.strip()

        # Interval format: 30s, 5m, 2h
        if expr[-1] in "smh" and expr[:-1].isdigit():
            multipliers = {"s": 1, "m": 60, "h": 3600}
            seconds = int(expr[:-1]) * multipliers[expr[-1]]
            return CronSchedule(kind="every", every_seconds=seconds)

        # ISO datetime
        if "T" in expr or expr.count("-") >= 2:
            return CronSchedule(kind="at", at_time=expr)

        # Default: cron expression
        return CronSchedule(kind="cron", expr=expr)
