# -*- coding: utf-8 -*-
"""Orchestrator engine — plan → confirm → dispatch → validate.

Two trigger paths:
1. Natural: Opus main session decides to parallelize → embeds <task_plan> in response
   → Python detects it → user confirms → dispatch Sonnet workers
2. Explicit: #parallel command → Opus planning call → user confirms → dispatch

Both paths converge at confirm() → execute() → validate().
"""

import json
import logging
import re
import time
import uuid
from typing import Callable, Awaitable

from agent.llm.claude import ClaudeCli
from agent.orchestrator.models import TaskPlan, SubTask
from agent.orchestrator.pool import WorkerPool
from agent.orchestrator.prompts import (
    PLAN_SYSTEM, PLAN_PROMPT, WORKER_SYSTEM,
    VALIDATE_SYSTEM, VALIDATE_PROMPT,
)

log = logging.getLogger("hub.orchestrator")

# Confirmation keywords (case-insensitive first word)
_CONFIRM_WORDS = {"确认", "ok", "go", "确认执行", "开始", "yes"}
_CANCEL_WORDS = {"取消", "cancel", "算了", "不要"}

# Tag pattern for extracting plan from Opus response
_PLAN_TAG_RE = re.compile(
    r"<task_plan>\s*(.*?)\s*</task_plan>",
    re.DOTALL,
)


_PENDING_TTL = 600  # seconds — auto-expire unconfirmed plans after 10 min


class Orchestrator:
    def __init__(self, claude_cli: ClaudeCli, pool: WorkerPool):
        self._claude = claude_cli
        self._pool = pool
        self._pending: dict[str, TaskPlan] = {}  # session_key → plan awaiting confirmation
        self._pending_ts: dict[str, float] = {}  # session_key → set_pending timestamp

    # ═══ Plan detection from Opus response ═══

    @staticmethod
    def extract_plan_from_response(text: str) -> tuple[str, TaskPlan | None]:
        """Parse <task_plan> tag from Opus response.

        Returns (clean_text, plan_or_none).
        clean_text has the tag stripped so user only sees the natural description.
        """
        match = _PLAN_TAG_RE.search(text)
        if not match:
            return text, None

        raw_json = match.group(1).strip()
        # Strip markdown code block wrapper if present
        if raw_json.startswith("```"):
            raw_json = raw_json.split("\n", 1)[1] if "\n" in raw_json else raw_json[3:]
            raw_json = raw_json.rsplit("```", 1)[0]

        try:
            data = json.loads(raw_json)
        except (json.JSONDecodeError, ValueError) as e:
            log.warning("Plan tag found but JSON invalid: %s — raw: %s", e, raw_json[:200])
            return text, None

        subtasks = []
        for i, s in enumerate(data.get("subtasks", []), 1):
            prompt = s.get("prompt", "")
            if not prompt:
                continue
            subtasks.append(SubTask(
                id=str(i),
                title=s.get("title", f"子任务 {i}"),
                prompt=prompt,
            ))

        if len(subtasks) < 2:
            log.info("Plan tag found but <2 valid subtasks, ignoring")
            return text, None

        plan = TaskPlan(
            plan_id=uuid.uuid4().hex[:8],
            description=data.get("description", "并行任务"),
            original_prompt=data.get("original_task", ""),
            subtasks=subtasks,
        )

        # Strip the tag from displayed text
        clean = text[:match.start()] + text[match.end():]
        clean = clean.rstrip()

        log.info("Plan extracted from response: %s — %d subtasks", plan.plan_id, len(subtasks))
        return clean, plan

    # ═══ Explicit planning (fallback for #parallel) ═══

    async def create_plan(self, task: str) -> TaskPlan | None:
        """Ask Opus to decompose the task. Returns plan or None if not parallelizable."""
        result = await self._claude.run(
            PLAN_PROMPT.format(task=task),
            model="opus",
            system_prompt=PLAN_SYSTEM,
            timeout_seconds=120,
            setting_sources="local",
        )
        if result.is_error:
            log.warning("Planning failed: %s", result.text[:200])
            return None

        try:
            text = result.text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                text = text.rsplit("```", 1)[0]
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError) as e:
            log.warning("Plan JSON parse failed: %s — raw: %s", e, result.text[:300])
            return None

        if not data.get("parallel", True):
            log.info("Task not parallelizable: %s", data.get("reason", ""))
            return None

        subtasks = []
        for i, s in enumerate(data.get("subtasks", []), 1):
            subtasks.append(SubTask(
                id=str(i),
                title=s.get("title", f"子任务 {i}"),
                prompt=s.get("prompt", ""),
            ))

        if len(subtasks) < 2:
            return None

        plan = TaskPlan(
            plan_id=uuid.uuid4().hex[:8],
            description=data.get("description", task[:60]),
            original_prompt=task,
            subtasks=subtasks,
        )
        log.info("Plan created (explicit): %s — %d subtasks", plan.plan_id, len(subtasks))
        return plan

    # ═══ Plan state management ═══

    def set_pending(self, session_key: str, plan: TaskPlan):
        plan.status = "awaiting_confirm"
        plan.session_key = session_key
        self._pending[session_key] = plan
        self._pending_ts[session_key] = time.time()
        # Sweep expired plans
        self._sweep_expired()

    def _sweep_expired(self):
        now = time.time()
        for key in list(self._pending_ts):
            if now - self._pending_ts[key] > _PENDING_TTL:
                self._pending.pop(key, None)
                self._pending_ts.pop(key, None)
                log.info("Expired unconfirmed plan for %s", key)

    def has_pending(self, session_key: str) -> bool:
        # Check TTL inline
        ts = self._pending_ts.get(session_key)
        if ts and time.time() - ts > _PENDING_TTL:
            self._pending.pop(session_key, None)
            self._pending_ts.pop(session_key, None)
            return False
        return session_key in self._pending

    def get_pending(self, session_key: str) -> TaskPlan | None:
        return self._pending.get(session_key)

    def cancel(self, session_key: str):
        plan = self._pending.pop(session_key, None)
        self._pending_ts.pop(session_key, None)
        if plan:
            plan.status = "cancelled"
            log.info("Plan %s cancelled", plan.plan_id)

    def confirm(self, session_key: str) -> TaskPlan | None:
        plan = self._pending.pop(session_key, None)
        self._pending_ts.pop(session_key, None)
        if plan:
            plan.status = "running"
            log.info("Plan %s confirmed, dispatching", plan.plan_id)
        return plan

    @staticmethod
    def is_confirmation(text: str) -> bool:
        first = text.strip().split(None, 1)[0].lower() if text.strip() else ""
        return first in _CONFIRM_WORDS

    @staticmethod
    def is_cancellation(text: str) -> bool:
        first = text.strip().split(None, 1)[0].lower() if text.strip() else ""
        return first in _CANCEL_WORDS

    # ═══ Execution ═══

    async def execute(
        self,
        plan: TaskPlan,
        on_progress: Callable[[], Awaitable[None]] | None = None,
    ):
        """Dispatch all subtasks to worker pool."""
        log.info("Executing plan %s: %d subtasks", plan.plan_id, len(plan.subtasks))
        await self._pool.execute_all(
            plan.subtasks,
            system_prompt=WORKER_SYSTEM,
            on_update=on_progress,
        )
        plan.status = "validating"
        if on_progress:
            await on_progress()

    async def validate(self, plan: TaskPlan) -> str:
        """Opus validates collected results. Returns final response text."""
        results_parts = []
        for s in plan.subtasks:
            if s.status == "done":
                result_text = s.result if len(s.result) < 3000 else s.result[:3000] + "\n...(truncated)"
                results_parts.append(f"### 子任务 {s.id}: {s.title}\n{result_text}")
            else:
                results_parts.append(f"### 子任务 {s.id}: {s.title}\n❌ 失败: {s.error}")

        prompt = VALIDATE_PROMPT.format(
            task=plan.original_prompt,
            results="\n\n".join(results_parts),
        )

        log.info("Validating plan %s with Opus", plan.plan_id)
        result = await self._claude.run(
            prompt,
            model="opus",
            system_prompt=VALIDATE_SYSTEM,
            timeout_seconds=180,
        )

        plan.status = "done"

        if result.is_error:
            log.warning("Validation failed: %s", result.text[:200])
            return (
                f"⚠️ **验收出错**：{result.text[:200]}\n\n"
                f"以下是子任务原始结果：\n\n"
                + "\n\n".join(results_parts)
            )

        return result.text
