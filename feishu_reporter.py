# -*- coding: utf-8 -*-
"""Feishu notification adapter for long-task progress.

Implements ProgressReporter protocol. Updates a single Feishu card in-place
to show real-time task progress.
"""

import logging
from dispatcher import Dispatcher

log = logging.getLogger("hub.feishu_reporter")


class FeishuReporter:
    """Feishu card-based progress reporter for long tasks."""

    def __init__(self, dispatcher: Dispatcher):
        self.dispatcher = dispatcher
        # task_id → message_id (for card updates)
        self._card_ids: dict[str, str] = {}

    async def on_plan_ready(self, task) -> None:
        """Send plan card for user approval."""
        text = (
            f"**📋 任务计划** `{task.task_id}`\n\n"
            f"**目标：** {task.goal}\n\n"
            f"**步骤：**\n"
        )
        for i, step in enumerate(task.steps):
            text += f"{i+1}. **{step.name}** — {step.description}\n"
            text += f"   验收：{step.acceptance}\n"
        text += (
            f"\n共 {len(task.steps)} 步。"
            f"回复 **ok** 开始执行，或回复修改意见重新规划。"
        )

        msg_id = await self.dispatcher.send_card_return_id(task.chat_id, text)
        if msg_id:
            self._card_ids[task.task_id] = msg_id
            log.info("Plan card sent for task %s: %s", task.task_id, msg_id)
        else:
            # Fallback: send as normal message
            await self.dispatcher.send_text(task.chat_id, text)
            log.warning("Failed to get message_id for task %s plan card", task.task_id)

    async def on_step_start(self, task, step_index: int) -> None:
        """Update card to show current step running."""
        await self._update_progress_card(task)

    async def on_step_done(self, task, step_index: int) -> None:
        """Update card to show step completed."""
        await self._update_progress_card(task)

    async def on_completed(self, task) -> None:
        """Update card with final summary."""
        text = task.progress_text()
        text += "\n\n---\n"
        # Add last step results as summary
        results = []
        for i, step in enumerate(task.steps):
            if step.result:
                results.append(f"**{i+1}. {step.name}：** {step.result[:200]}")
        if results:
            text += "\n".join(results)

        msg_id = self._card_ids.get(task.task_id)
        if msg_id:
            await self.dispatcher.update_card(msg_id, text)
        else:
            await self.dispatcher.send_text(task.chat_id, text)
        log.info("Task %s completion card updated", task.task_id)

    async def on_failed(self, task, error: str) -> None:
        """Update card with failure info."""
        text = task.progress_text()
        text += f"\n\n---\n**错误：** {error[:500]}"

        msg_id = self._card_ids.get(task.task_id)
        if msg_id:
            await self.dispatcher.update_card(msg_id, text)
        else:
            await self.dispatcher.send_text(task.chat_id, text)
        log.warning("Task %s failure card updated: %s", task.task_id, error[:100])

    async def _update_progress_card(self, task) -> None:
        """Update the in-place progress card."""
        msg_id = self._card_ids.get(task.task_id)
        if not msg_id:
            return
        text = task.progress_text()
        ok = await self.dispatcher.update_card(msg_id, text)
        if not ok:
            log.debug("Progress card update failed for task %s", task.task_id)
