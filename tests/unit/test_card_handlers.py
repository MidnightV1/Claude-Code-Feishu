# -*- coding: utf-8 -*-
"""Unit tests for agent.platforms.feishu.card_handlers."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent.platforms.feishu.card_handlers import (
    CardHandlerDeps,
    abort_task_handler,
    confirm_handler,
    menu_action_handler,
    register_builtin_handlers,
    select_handler,
)


@pytest.mark.asyncio
async def test_register_builtin_handlers_registers_expected_actions():
    router = MagicMock()
    deps = CardHandlerDeps(
        dispatcher=MagicMock(),
        inject_message=AsyncMock(),
        cancel_task=MagicMock(return_value=False),
    )

    register_builtin_handlers(router, deps)

    assert router.register.call_count >= 3
    action_types = [call.args[0] for call in router.register.call_args_list]
    assert "menu_action" in action_types
    assert "confirm" in action_types
    assert "select" in action_types


@pytest.mark.asyncio
async def test_menu_action_handler_injects_message():
    deps = CardHandlerDeps(
        dispatcher=MagicMock(),
        inject_message=AsyncMock(),
        cancel_task=MagicMock(return_value=False),
    )

    result = await menu_action_handler(deps)(
        "menu_action",
        {"command": "/status"},
        "ou_123",
        {"chat_id": "oc_456"},
    )

    deps.inject_message.assert_awaited_once_with(
        "oc_456", "ou_123", "/status", "[快捷操作] /status"
    )
    assert result == ("/status", "已触发: /status", None)


@pytest.mark.asyncio
async def test_confirm_handler_updates_card():
    dispatcher = MagicMock()
    dispatcher.update_card_raw = AsyncMock()
    deps = CardHandlerDeps(
        dispatcher=dispatcher,
        inject_message=AsyncMock(),
        cancel_task=MagicMock(return_value=False),
    )

    result = await confirm_handler(deps)(
        "confirm",
        {"choice": "confirm", "label": "删除任务"},
        "ou_123",
        {"message_id": "om_456"},
    )
    await asyncio.sleep(0)

    dispatcher.update_card_raw.assert_awaited_once()
    assert dispatcher.update_card_raw.await_args.args[0] == "om_456"
    assert "删除任务" in dispatcher.update_card_raw.await_args.args[1]
    assert result == ("confirm", "✅ 已确认: 删除任务", None)


@pytest.mark.asyncio
async def test_select_handler_updates_card_and_injects_message():
    dispatcher = MagicMock()
    dispatcher.update_card_raw = AsyncMock()
    deps = CardHandlerDeps(
        dispatcher=dispatcher,
        inject_message=AsyncMock(),
        cancel_task=MagicMock(return_value=False),
    )

    result = await select_handler(deps)(
        "select",
        {"choice": "deep", "label": "深度分析", "group": "分析模式"},
        "ou_123",
        {"message_id": "om_456", "chat_id": "oc_789"},
    )
    await asyncio.sleep(0)

    dispatcher.update_card_raw.assert_awaited_once()
    deps.inject_message.assert_awaited_once_with(
        "oc_789", "ou_123", "[选择] 分析模式: 深度分析", "已选择: 深度分析"
    )
    assert result == ("deep", "已选择: 深度分析", None)


@pytest.mark.asyncio
async def test_abort_task_handler_returns_aborted_when_cancelled():
    dispatcher = MagicMock()
    dispatcher.update_card_raw = AsyncMock()
    deps = CardHandlerDeps(
        dispatcher=dispatcher,
        inject_message=AsyncMock(),
        cancel_task=MagicMock(return_value=True),
    )

    result = await abort_task_handler(deps)(
        "abort_task",
        {"key": "task_1"},
        "ou_123",
        {"message_id": "om_456"},
    )
    await asyncio.sleep(0)

    deps.cancel_task.assert_called_once_with("task_1")
    dispatcher.update_card_raw.assert_awaited_once()
    assert result == ("aborted", "⏹ 已中止", None)
