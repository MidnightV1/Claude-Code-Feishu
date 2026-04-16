# -*- coding: utf-8 -*-
"""In-process cron scheduler. Pattern from OpenClaw src/cron/service/timer.ts."""

import asyncio
import os
import re
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Callable, Awaitable
from croniter import croniter

import yaml

from agent.infra.models import (
    CronJob, CronSchedule, CronJobState, LLMConfig, LLMResult,
    cron_job_from_dict, to_dict,
)
from agent.infra.store import load_json, save_json, save_json_sync
from agent.llm.router import LLMRouter
from agent.platforms.feishu.dispatcher import Dispatcher

log = logging.getLogger("hub.scheduler")

MAX_TIMER_DELAY = 60.0       # seconds, prevents drift
ERROR_BACKOFF = [30, 60, 300, 900, 3600]  # exponential backoff schedule


class CronScheduler:
    def __init__(self, config: dict, router: LLMRouter, dispatcher: Dispatcher,
                 bot_dispatchers: dict[str, tuple[Dispatcher, str]] | None = None):
        self.enabled = config.get("enabled", True)
        self.state_path = config.get("store_path", "data/jobs.json")  # runtime state only
        # Declarative definitions: config/jobs.yaml (git-tracked, read-only)
        self.seed_path = config.get("seed_path", "config/jobs.yaml")
        self.router = router
        self.dispatcher = dispatcher
        self._bot_dispatchers = bot_dispatchers or {}  # {name: (Dispatcher, open_id)}
        self._jobs: list[CronJob] = []
        self._timer: asyncio.TimerHandle | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._missed_jobs_task: asyncio.Task | None = None
        self._save_lock = asyncio.Lock()
        self._handler_tasks: dict[str, asyncio.Task] = {}
        self._handlers: dict[str, Callable[..., Awaitable[str]]] = {}
        self._stopped = False

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
        self._missed_jobs_task = asyncio.create_task(self._run_missed_jobs_bg())
        self._recompute_all()
        await self._save_store()
        self._arm_timer()
        log.info("Scheduler started, %d jobs loaded", len(self._jobs))

    async def reload(self):
        """Hot-reload jobs from disk (triggered by SIGUSR1)."""
        old_count = len(self._jobs)
        await self._load_store()
        if self._missed_jobs_task and not self._missed_jobs_task.done():
            self._missed_jobs_task.cancel()
        self._missed_jobs_task = asyncio.create_task(self._run_missed_jobs_bg())
        self._recompute_all()
        self._arm_timer()
        log.info("Scheduler reloaded: %d → %d jobs", old_count, len(self._jobs))

    async def stop(self):
        self._stopped = True
        if self._timer:
            self._timer.cancel()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._missed_jobs_task and not self._missed_jobs_task.done():
            self._missed_jobs_task.cancel()
            try:
                await self._missed_jobs_task
            except asyncio.CancelledError:
                pass
        for task in list(self._handler_tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(self._handler_tasks.values()):
            if not task.done():
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        # Synchronous save during shutdown — immune to os._exit() race.
        if self._jobs:
            try:
                self._save_state_sync()
            except Exception as e:
                log.error("Failed to save job state on shutdown: %s", e)
        log.info("Scheduler stopped (%d jobs, state persisted)", len(self._jobs))

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
        if self._stopped:
            return
        if self._running:
            # Already executing — _arm_timer() in finally block will re-arm
            return
        # Guard against task still pending (not yet started)
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._on_timer())

    async def _on_timer(self):
        if self._stopped:
            return
        self._running = True
        try:
            now = time.time()
            due = [
                j for j in self._jobs
                if j.enabled and j.state.next_run_at and j.state.next_run_at <= now
            ]

            for job in due:
                if job.handler:
                    task = self._handler_tasks.get(job.handler)
                    if task and not task.done():
                        log.warning("Handler %s still running, skip job %s", job.handler, job.id)
                        continue
                    task = asyncio.create_task(self._execute_job(job))
                    self._handler_tasks[job.handler] = task
                    task.add_done_callback(
                        lambda done, handler=job.handler: (
                            self._handler_tasks.pop(handler, None)
                            if self._handler_tasks.get(handler) is done else None
                        )
                    )
                    continue
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
                if text is None:
                    text = ""
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

                # Silent token suppression (like heartbeat's HEARTBEAT_OK)
                if job.silent_token and job.deliver_to_feishu:
                    pattern = re.compile(
                        r'(\*{0,2})' + re.escape(job.silent_token) + r'(\*{0,2})',
                        re.IGNORECASE,
                    )
                    if pattern.search(result.text):
                        cleaned = pattern.sub("", result.text).strip()
                        if len(cleaned) <= 300:
                            log.info("Job %s silent (token found, %d residual chars)",
                                     job.id, len(cleaned))
                            await self._save_store()
                            return result.text

                # Deliver to Feishu
                if job.deliver_to_feishu and result.text.strip():
                    text = result.text
                    # Only add card header if job result doesn't have its own
                    if not text.lstrip().startswith("{{card:"):
                        text = f"{{{{card:header={job.name},color=blue}}}}\n" + text
                    await self._deliver(job, text)

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

    async def _deliver(self, job: CronJob, text: str):
        """Route notification to the correct bot dispatcher."""
        if job.notify_bot and job.notify_bot in self._bot_dispatchers:
            dispatcher, open_id = self._bot_dispatchers[job.notify_bot]
            if open_id:
                await dispatcher.send_to_user(open_id, text)
            elif dispatcher.delivery_chat_id:
                await dispatcher.send_to_delivery_target(text)
            else:
                log.warning("Bot dispatcher '%s' has no open_id or delivery_chat_id, "
                            "falling back to default", job.notify_bot)
                await self.dispatcher.send_to_delivery_target(text)
        else:
            await self.dispatcher.send_to_delivery_target(text)

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

    async def _run_missed_jobs_bg(self):
        """Run missed jobs in background so they don't block startup."""
        try:
            now = time.time()
            missed = [
                j for j in self._jobs
                if j.enabled and j.state.next_run_at and j.state.next_run_at < now
                and not (j.one_shot and j.state.last_status == "ok")
                # Guard: if last_run_at >= next_run_at, the job already ran for this slot
                and not (j.state.last_run_at and j.state.last_run_at >= j.state.next_run_at)
            ]
            if missed:
                log.info("Running %d missed jobs (background)", len(missed))
                for job in missed:
                    if job.handler:
                        task = self._handler_tasks.get(job.handler)
                        if task and not task.done():
                            log.warning("Handler %s still running, skip missed job %s", job.handler, job.id)
                            continue
                        task = asyncio.create_task(self._execute_job(job))
                        self._handler_tasks[job.handler] = task
                        task.add_done_callback(
                            lambda done, handler=job.handler: (
                                self._handler_tasks.pop(handler, None)
                                if self._handler_tasks.get(handler) is done else None
                            )
                        )
                        continue
                    await self._execute_job(job)
        except Exception as e:
            log.error("Missed jobs background task failed: %s", e)

    # ═══ Persistence ═══

    # ═══ Two-phase persistence ═══
    # Definitions: config/jobs.yaml (read-only, git-tracked)
    # Runtime state: data/jobs.json (read-write, scheduler-owned)

    def _load_seed(self) -> list[dict]:
        """Load declarative job definitions from config/jobs.yaml."""
        try:
            with open(self.seed_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            jobs = data.get("jobs", [])
            if jobs:
                log.info("Loaded %d job definitions from %s", len(jobs), self.seed_path)
            return jobs
        except FileNotFoundError:
            log.warning("Seed file %s not found, falling back to state file", self.seed_path)
            return []
        except Exception as e:
            log.error("Failed to load seed %s: %s", self.seed_path, e)
            return []

    def _load_state_map(self) -> dict[str, dict]:
        """Load runtime state from data/jobs.json. Returns {job_id: state_dict}."""
        try:
            from agent.infra.store import load_json_sync
            data = load_json_sync(self.state_path, {"version": 1, "states": {}})
            # Support both old format (jobs array) and new format (states map)
            if "states" in data:
                return data["states"]
            # Migration: old format had full job dicts in a list
            if "jobs" in data and isinstance(data["jobs"], list):
                return {j["id"]: j.get("state", {}) for j in data["jobs"] if "id" in j}
            return {}
        except Exception as e:
            log.warning("Failed to load state from %s: %s", self.state_path, e)
            return {}

    async def _load_store(self):
        """Two-phase load: seed definitions + runtime state overlay."""
        seed_jobs = self._load_seed()
        state_map = self._load_state_map()

        if seed_jobs:
            # Build jobs from seed, overlay runtime state
            jobs = []
            for jd in seed_jobs:
                # Normalize seed dict to match cron_job_from_dict expectations
                jd = dict(jd)
                # Parse schedule string → schedule dict
                if isinstance(jd.get("schedule"), str):
                    sched = self._parse_schedule(jd["schedule"])
                    jd["schedule"] = to_dict(sched)
                # Parse model shorthand → llm dict
                if "model" in jd and not isinstance(jd.get("llm"), dict):
                    parts = jd.pop("model").split("/", 1)
                    jd["llm"] = {"provider": parts[0], "model": parts[1] if len(parts) > 1 else ""}
                # Ensure enabled defaults to True
                jd.setdefault("enabled", True)
                # Overlay runtime state
                job_id = jd.get("id", "")
                if job_id in state_map:
                    jd["state"] = state_map[job_id]
                job = cron_job_from_dict(jd)
                jobs.append(job)
            self._jobs = jobs
        else:
            # Fallback: no seed file, load from state file (legacy/migration)
            data = await load_json(self.state_path, {"version": 1, "jobs": []})
            legacy_jobs = data.get("jobs", [])
            if legacy_jobs:
                log.warning("No seed file, loaded %d jobs from legacy state file", len(legacy_jobs))
                self._jobs = [cron_job_from_dict(j) for j in legacy_jobs]
            else:
                log.warning("No jobs found in seed or state files")
                self._jobs = []

    async def _save_store(self):
        """Save runtime state only — never touches seed definitions."""
        states = {}
        for j in self._jobs:
            states[j.id] = to_dict(j.state)
        data = {"version": 2, "states": states}
        async with self._save_lock:
            task = asyncio.create_task(save_json(self.state_path, data))
            try:
                await task
            except asyncio.CancelledError:
                await task
                raise

    def _save_state_sync(self):
        """Synchronous state save for shutdown path."""
        states = {}
        for j in self._jobs:
            states[j.id] = to_dict(j.state)
        data = {"version": 2, "states": states}
        save_json_sync(self.state_path, data)

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
