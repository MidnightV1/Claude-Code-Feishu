# -*- coding: utf-8 -*-
"""Data models for orchestrated task execution."""

import time
from dataclasses import dataclass, field


@dataclass
class SubTask:
    id: str
    title: str
    prompt: str
    status: str = "pending"   # pending | running | done | failed
    result: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0


@dataclass
class TaskPlan:
    plan_id: str
    description: str
    original_prompt: str
    subtasks: list[SubTask] = field(default_factory=list)
    status: str = "planning"  # planning | awaiting_confirm | running | validating | done | cancelled
    chat_id: str = ""
    session_key: str = ""
    reply_to: str = ""        # message_id to reply to
    created_at: float = field(default_factory=time.time)

    _STATUS_ICONS = {"pending": "⬜", "running": "🔄", "done": "✅", "failed": "❌"}

    def is_all_done(self) -> bool:
        return all(s.status in ("done", "failed") for s in self.subtasks)

    def done_count(self) -> int:
        return sum(1 for s in self.subtasks if s.status == "done")

    def failed_count(self) -> int:
        return sum(1 for s in self.subtasks if s.status == "failed")

    def render_plan(self) -> str:
        lines = [f"📋 **并行任务计划**\n", f"> {self.description}\n"]
        for s in self.subtasks:
            lines.append(f"⬜ **{s.id}.** {s.title}")
        lines.append(f"\n回复 **确认** 开始并行执行，或发送其他消息继续对话。")
        return "\n".join(lines)

    def render_progress(self) -> str:
        total = len(self.subtasks)
        done = self.done_count()
        failed = self.failed_count()

        if self.status == "validating":
            header = "🔍 **验收中…**"
        elif self.is_all_done():
            header = "✅ **子任务全部完成**"
        else:
            header = "🚀 **并行执行中**"

        lines = [header]
        for s in self.subtasks:
            icon = self._STATUS_ICONS.get(s.status, "⬜")
            dur = ""
            if s.status in ("done", "failed") and s.started_at and s.finished_at:
                secs = int(s.finished_at - s.started_at)
                dur = f" ({secs}s)"
            lines.append(f"{icon} **{s.id}.** {s.title}{dur}")

        status_parts = [f"{done}/{total} 完成"]
        if failed:
            status_parts.append(f"{failed} 失败")
        lines.append(f"\n{' | '.join(status_parts)}")
        return "\n".join(lines)
