# -*- coding: utf-8 -*-
"""Long-task orchestrator. Platform-agnostic state machine + persistence.

Zero platform imports — progress notification goes through ProgressReporter protocol.
"""

import asyncio
import glob
import os
import time
import uuid
import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional, Protocol, runtime_checkable

from store import load_json, save_json

log = logging.getLogger("hub.task_runner")

TASKS_PATH = "data/tasks.json"
REQUESTS_DIR = "data/task_requests"


# ═══ Data Models ═══

@dataclass
class TaskStep:
    name: str = ""
    description: str = ""
    acceptance: str = ""
    status: str = "pending"       # pending → running → completed → failed
    result: Optional[str] = None

    @staticmethod
    def from_dict(d: dict) -> "TaskStep":
        return TaskStep(**{k: v for k, v in d.items() if k in TaskStep.__dataclass_fields__})


@dataclass
class TaskPlan:
    task_id: str = ""
    session_key: str = ""         # "user:ou_xxx" / "chat:oc_xxx"
    status: str = "planning"      # planning → awaiting_approval → executing → completed → failed
    goal: str = ""
    steps: list[TaskStep] = field(default_factory=list)
    current_step: int = 0         # 0-indexed
    cli_session_id: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    error: Optional[str] = None
    chat_id: str = ""             # for reply routing
    sender_id: str = ""

    def __post_init__(self):
        if not self.task_id:
            self.task_id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = time.time()
            self.updated_at = self.created_at

    @staticmethod
    def from_dict(d: dict) -> "TaskPlan":
        d = dict(d)
        d["steps"] = [TaskStep.from_dict(s) for s in d.get("steps", [])]
        return TaskPlan(**{k: v for k, v in d.items() if k in TaskPlan.__dataclass_fields__})

    def to_dict(self) -> dict:
        return asdict(self)

    def progress_text(self) -> str:
        """Render progress as markdown for card display."""
        lines = [f"**{self.goal}**\n"]
        for i, step in enumerate(self.steps):
            if step.status == "completed":
                icon = ":DONE:"
            elif step.status == "running":
                icon = "🔄"
            elif step.status == "failed":
                icon = "❌"
            else:
                icon = "⬜"
            marker = " ← 当前" if step.status == "running" else ""
            lines.append(f"{icon} {i + 1}. {step.name}{marker}")
        done = sum(1 for s in self.steps if s.status == "completed")
        total = len(self.steps)
        if self.status == "executing":
            lines.append(f"\n> Step {self.current_step + 1}/{total} 执行中...")
        elif self.status == "completed":
            lines.append(f"\n> 全部完成 ({total}/{total})")
        elif self.status == "failed":
            lines.append(f"\n> 失败于 Step {self.current_step + 1}: {self.error or 'unknown'}")
        return "\n".join(lines)


# ═══ ProgressReporter Protocol ═══

@runtime_checkable
class ProgressReporter(Protocol):
    async def on_plan_ready(self, task: TaskPlan) -> None: ...
    async def on_step_start(self, task: TaskPlan, step_index: int) -> None: ...
    async def on_step_done(self, task: TaskPlan, step_index: int) -> None: ...
    async def on_completed(self, task: TaskPlan) -> None: ...
    async def on_failed(self, task: TaskPlan, error: str) -> None: ...


class NullReporter:
    """No-op reporter for testing or headless mode."""
    async def on_plan_ready(self, task): pass
    async def on_step_start(self, task, step_index): pass
    async def on_step_done(self, task, step_index): pass
    async def on_completed(self, task): pass
    async def on_failed(self, task, error): pass


# ═══ TaskRunner ═══

class TaskRunner:
    """Orchestrator: manages task lifecycle, delegates execution to Claude CLI."""

    def __init__(self, router, reporter: ProgressReporter | None = None):
        self.router = router
        self.reporter = reporter or NullReporter()
        self._tasks: dict[str, TaskPlan] = {}   # task_id → TaskPlan
        self._lock = asyncio.Lock()

    async def start(self):
        """Load persisted tasks on startup."""
        data = await load_json(TASKS_PATH, default=[])
        for d in data:
            task = TaskPlan.from_dict(d)
            self._tasks[task.task_id] = task
        active = [t for t in self._tasks.values() if t.status in ("planning", "executing")]
        if active:
            log.info("Loaded %d tasks (%d active)", len(self._tasks), len(active))

    async def _save(self):
        """Persist all tasks."""
        data = [t.to_dict() for t in self._tasks.values()]
        await save_json(TASKS_PATH, data)

    # ── Public API ──

    async def create_task(self, goal: str, session_key: str,
                          chat_id: str, sender_id: str) -> TaskPlan:
        """Create a new task and start planning phase."""
        task = TaskPlan(
            session_key=session_key,
            goal=goal,
            chat_id=chat_id,
            sender_id=sender_id,
        )
        async with self._lock:
            self._tasks[task.task_id] = task
            await self._save()
        log.info("Task created: %s — %s", task.task_id, goal[:60])
        # Kick off planning in background
        asyncio.create_task(self._plan(task))
        return task

    async def approve_task(self, task_id: str) -> bool:
        """User approves the plan. Starts execution."""
        task = self._tasks.get(task_id)
        if not task or task.status != "awaiting_approval":
            return False
        task.status = "executing"
        task.updated_at = time.time()
        await self._save()
        log.info("Task approved: %s", task_id)
        asyncio.create_task(self._execute(task))
        return True

    async def reject_task(self, task_id: str, feedback: str = "") -> bool:
        """User rejects — replan with feedback."""
        task = self._tasks.get(task_id)
        if not task or task.status != "awaiting_approval":
            return False
        task.status = "planning"
        task.updated_at = time.time()
        new_goal = task.goal
        if feedback:
            new_goal += f"\n\n用户反馈：{feedback}"
        task.goal = new_goal
        await self._save()
        log.info("Task rejected, replanning: %s", task_id)
        asyncio.create_task(self._plan(task))
        return True

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a task."""
        task = self._tasks.get(task_id)
        if not task or task.status in ("completed", "failed"):
            return False
        task.status = "failed"
        task.error = "用户取消"
        task.updated_at = time.time()
        await self._save()
        log.info("Task cancelled: %s", task_id)
        return True

    def get_task(self, task_id: str) -> TaskPlan | None:
        return self._tasks.get(task_id)

    def get_awaiting_task(self, session_key: str) -> TaskPlan | None:
        """Find a task awaiting approval for this session."""
        for t in self._tasks.values():
            if t.session_key == session_key and t.status == "awaiting_approval":
                return t
        return None

    def list_active(self) -> list[TaskPlan]:
        """Tasks that are not terminal (for heartbeat snapshot)."""
        return [
            t for t in self._tasks.values()
            if t.status in ("planning", "awaiting_approval", "executing")
        ]

    def list_all(self) -> list[TaskPlan]:
        return list(self._tasks.values())

    async def check_pending_requests(self, session_key: str,
                                     chat_id: str, sender_id: str) -> int:
        """Check for task request files written by task_ctl.py create.

        Called after each Claude CLI response. Returns number of tasks created.
        """
        if not os.path.isdir(REQUESTS_DIR):
            return 0

        created = 0
        for path in glob.glob(os.path.join(REQUESTS_DIR, "*.json")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    req = json.load(f)
                os.remove(path)

                goal = req.get("goal", "")
                steps_data = req.get("steps", [])
                if not goal or not steps_data:
                    continue

                task = TaskPlan(
                    session_key=session_key,
                    goal=goal,
                    chat_id=chat_id,
                    sender_id=sender_id,
                )
                task.steps = [TaskStep.from_dict(s) for s in steps_data]
                task.status = "awaiting_approval"

                async with self._lock:
                    self._tasks[task.task_id] = task
                    await self._save()

                await self.reporter.on_plan_ready(task)
                log.info("Task from request: %s — %s (%d steps)",
                         task.task_id, goal[:60], len(task.steps))
                created += 1
            except Exception as e:
                log.warning("Failed to process task request %s: %s", path, e)
                try:
                    os.remove(path)
                except OSError:
                    pass
        return created

    # ── Planning Phase ──

    async def _plan(self, task: TaskPlan):
        """Call Claude CLI with the long-task skill to generate a plan."""
        from models import LLMConfig
        try:
            plan_prompt = (
                f"你是任务规划器。用户要求：\n\n{task.goal}\n\n"
                "请为这个任务生成执行计划。输出严格 JSON（不要 markdown 包裹）：\n"
                '{"steps": [{"name": "步骤简称", "description": "具体做什么", '
                '"acceptance": "怎么算完成"}]}\n\n'
                "约束：\n"
                "- 3-8 个步骤\n"
                "- 每步应产出可验证的结果\n"
                "- 步骤间依赖用执行顺序表达\n"
                '- 不要包含「确认需求」类步骤'
            )
            llm_config = LLMConfig(provider="claude-cli", model="opus", timeout_seconds=120)
            result = await self.router.run(
                prompt=plan_prompt,
                llm_config=llm_config,
                session_key=f"task:{task.task_id}",
            )
            if result.is_error:
                raise RuntimeError(f"LLM error: {result.text[:200]}")

            # Save CLI session for context continuity
            if result.session_id:
                task.cli_session_id = result.session_id

            # Parse JSON plan from response
            steps_data = self._parse_plan_json(result.text)
            task.steps = [TaskStep.from_dict(s) for s in steps_data]
            task.status = "awaiting_approval"
            task.updated_at = time.time()
            await self._save()

            await self.reporter.on_plan_ready(task)
            log.info("Plan ready for task %s: %d steps", task.task_id, len(task.steps))

        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            task.updated_at = time.time()
            await self._save()
            await self.reporter.on_failed(task, str(e))
            log.error("Planning failed for %s: %s", task.task_id, e)

    @staticmethod
    def _parse_plan_json(text: str) -> list[dict]:
        """Extract steps array from LLM response (handles markdown wrapping)."""
        # Try direct parse
        text = text.strip()
        # Strip markdown code fence if present
        if text.startswith("```"):
            lines = text.splitlines()
            # Remove first and last fence lines
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        try:
            data = json.loads(text)
            if isinstance(data, dict) and "steps" in data:
                return data["steps"]
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

        # Try to find JSON object in text
        import re
        match = re.search(r'\{[\s\S]*"steps"\s*:\s*\[[\s\S]*\]\s*\}', text)
        if match:
            try:
                data = json.loads(match.group())
                return data["steps"]
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Cannot parse plan JSON from LLM response: {text[:200]}")

    # ── Execution Phase ──

    async def _execute(self, task: TaskPlan):
        """Execute steps sequentially via Claude CLI."""
        from models import LLMConfig
        try:
            for i, step in enumerate(task.steps):
                task.current_step = i
                step.status = "running"
                task.updated_at = time.time()
                await self._save()
                await self.reporter.on_step_start(task, i)

                # Build execution prompt
                exec_prompt = (
                    f"你正在执行任务「{task.goal}」的第 {i+1}/{len(task.steps)} 步。\n\n"
                    f"**当前步骤：** {step.name}\n"
                    f"**具体内容：** {step.description}\n"
                    f"**验收标准：** {step.acceptance}\n\n"
                    "请执行这一步。完成后，简要说明做了什么以及验收结果。"
                )

                llm_config = LLMConfig(
                    provider="claude-cli",
                    model="sonnet",
                    timeout_seconds=600,
                )

                result = await self.router.run(
                    prompt=exec_prompt,
                    llm_config=llm_config,
                    session_key=f"task:{task.task_id}",
                )

                # Update CLI session for continuity
                if result.session_id:
                    task.cli_session_id = result.session_id

                if result.is_error:
                    step.status = "failed"
                    step.result = result.text[:500]
                    task.status = "failed"
                    task.error = f"Step {i+1} failed: {result.text[:200]}"
                    task.updated_at = time.time()
                    await self._save()
                    await self.reporter.on_failed(task, task.error)
                    return

                step.status = "completed"
                step.result = result.text[:1000]
                task.updated_at = time.time()
                await self._save()
                await self.reporter.on_step_done(task, i)
                log.info("Task %s step %d/%d done: %s",
                         task.task_id, i+1, len(task.steps), step.name)

            # All steps done
            task.status = "completed"
            task.updated_at = time.time()
            await self._save()
            await self.reporter.on_completed(task)
            log.info("Task completed: %s", task.task_id)

        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            task.updated_at = time.time()
            await self._save()
            await self.reporter.on_failed(task, str(e))
            log.error("Task execution error %s: %s", task.task_id, e)
