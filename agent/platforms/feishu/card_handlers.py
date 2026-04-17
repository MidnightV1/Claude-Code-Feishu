# -*- coding: utf-8 -*-
"""Built-in Feishu card action handlers."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from agent.platforms.feishu.card_actions import ActionHandler
from agent.platforms.feishu.dispatcher import Dispatcher

log = logging.getLogger("hub.feishu_bot")


@dataclass
class CardHandlerDeps:
    dispatcher: Any
    inject_message: Callable[[str, str, str, str], Awaitable[None] | None]
    cancel_task: Callable[[str], bool]


def menu_action_handler(deps: CardHandlerDeps) -> ActionHandler:
    async def _handle_menu(action_type, value, operator_id, context):
        cmd = value.get("command", "")
        if not cmd:
            return ("ignored", "", None)
        chat_id = context.get("chat_id", "")
        if chat_id:
            result = deps.inject_message(
                chat_id, operator_id, cmd, f"[快捷操作] {cmd}"
            )
            if asyncio.iscoroutine(result):
                await result
        return (cmd, f"已触发: {cmd}", None)

    return _handle_menu


def confirm_handler(deps: CardHandlerDeps) -> ActionHandler:
    async def _handle_confirm(action_type, value, operator_id, context):
        choice = value.get("choice", "cancel")
        label = value.get("label", "")
        msg_id = context.get("message_id", "")
        if choice == "confirm":
            display = f"✅ 已确认: {label}"
        else:
            display = f"❌ 已取消: {label}"
        if msg_id:
            card_json = Dispatcher.build_interactive_card(
                [{"tag": "markdown", "content": display}],
                header=label,
                color="green" if choice == "confirm" else "grey",
            )
            asyncio.ensure_future(deps.dispatcher.update_card_raw(msg_id, card_json))
        return (choice, display, None)

    return _handle_confirm


def select_handler(deps: CardHandlerDeps) -> ActionHandler:
    async def _handle_select(action_type, value, operator_id, context):
        choice = value.get("choice", "")
        label = value.get("label", "")
        group = value.get("group", "")
        msg_id = context.get("message_id", "")
        chat_id = context.get("chat_id", "")
        display = f"已选择: {label}"
        if msg_id:
            card_json = Dispatcher.build_interactive_card(
                [{"tag": "markdown", "content": f"✅ {display}"}],
                header=group,
                color="blue",
            )
            asyncio.ensure_future(deps.dispatcher.update_card_raw(msg_id, card_json))
        if chat_id:
            result = deps.inject_message(
                chat_id,
                operator_id,
                f"[选择] {group}: {label}",
                display,
            )
            if asyncio.iscoroutine(result):
                await result
        return (choice, display, None)

    return _handle_select


def explore_feedback_handler(deps: CardHandlerDeps) -> ActionHandler:
    async def _handle_explore_feedback(action_type, value, operator_id, context):
        rating = value.get("choice", "")
        task_id = value.get("task_id", "")
        title = value.get("title", "探索任务")
        msg_id = context.get("message_id", "")

        if rating not in ("up", "down") or not task_id:
            return ("ignored", "", None)

        from agent.infra.exploration import (
            ExplorationQueue,
            Priority,
            rate_log_entry,
            read_log,
        )

        await rate_log_entry(task_id, rating)

        try:
            recent = await read_log(hours=168)
            rated_entry = next(
                (entry for entry in recent if entry.get("task_id") == task_id),
                None,
            )
            if rated_entry and rated_entry.get("pillar"):
                pillar = rated_entry["pillar"]
                queue = ExplorationQueue()
                await queue.load()
                adjusted = 0
                for task in queue.list_pending():
                    if task.pillar == pillar:
                        if rating == "up" and task.priority > Priority.P1_HIGH:
                            await queue.update(task.id, priority=task.priority - 1)
                            adjusted += 1
                        elif rating == "down" and task.priority < Priority.P3_WATCHING:
                            await queue.update(task.id, priority=task.priority + 1)
                            adjusted += 1
                if adjusted:
                    log.info(
                        "Adjusted %d tasks in pillar=%s by %s",
                        adjusted,
                        pillar,
                        rating,
                    )
        except Exception as e:
            log.warning("Explore priority adjustment failed: %s", e)

        emoji = "👍" if rating == "up" else "👎"
        display = f"{emoji} 已评价: {title}"
        if msg_id:
            card_json = Dispatcher.build_interactive_card(
                [{"tag": "markdown", "content": display}],
                header=f"[探索] {title}",
                color="green" if rating == "up" else "grey",
            )
            asyncio.ensure_future(deps.dispatcher.update_card_raw(msg_id, card_json))

        log.info("Explore feedback: %s = %s (task_id=%s)", title, rating, task_id)
        return (rating, display, None)

    return _handle_explore_feedback


def abort_task_handler(deps: CardHandlerDeps) -> ActionHandler:
    async def _handle_abort(action_type, value, operator_id, context):
        task_key = value.get("key", "")
        msg_id = context.get("message_id", "")
        if not task_key:
            return ("ignored", "", None)

        cancelled = deps.cancel_task(task_key)
        if cancelled:
            if msg_id:
                card_json = Dispatcher.build_interactive_card(
                    [{"tag": "markdown", "content": "⏹ 已中止"}],
                    color="grey",
                )
                asyncio.ensure_future(
                    deps.dispatcher.update_card_raw(msg_id, card_json)
                )
            return ("aborted", "⏹ 已中止", None)

        if msg_id:
            card_json = Dispatcher.build_interactive_card(
                [{"tag": "markdown", "content": "⏹ 任务已结束"}],
                color="grey",
            )
            asyncio.ensure_future(deps.dispatcher.update_card_raw(msg_id, card_json))
        return ("no_task", "任务已结束", None)

    return _handle_abort


def register_builtin_handlers(action_router: Any, deps: CardHandlerDeps) -> None:
    action_router.register("menu_action", menu_action_handler(deps))
    action_router.register("confirm", confirm_handler(deps))
    action_router.register("select", select_handler(deps))
    action_router.register("explore_feedback", explore_feedback_handler(deps))
    action_router.register("abort_task", abort_task_handler(deps))
