# -*- coding: utf-8 -*-
"""Unit tests for agent.platforms.feishu.command_router."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent.platforms.feishu.command_router import CommandRouter


@pytest.mark.asyncio
async def test_route_restart_denies_non_admin(router):
    user_store = MagicMock()
    user_store.get.return_value = None
    command_router = CommandRouter(
        router=router,
        scheduler=MagicMock(),
        dispatcher=MagicMock(),
        user_store=user_store,
        admin_open_ids={"admin1"},
    )

    result = await command_router.route("#restart", "c1", "p2p", "non_admin_user", "sk1")

    assert "权限" in result


@pytest.mark.asyncio
async def test_route_restart_allows_admin_via_admin_open_ids(router):
    user_store = MagicMock()
    user_store.get.return_value = None
    command_router = CommandRouter(
        router=router,
        scheduler=MagicMock(),
        dispatcher=MagicMock(),
        user_store=user_store,
        admin_open_ids={"admin1"},
    )

    result = await command_router.route("#restart", "c1", "p2p", "admin1", "sk1")

    assert result == "服务将在 3 秒后重启..."


@pytest.mark.asyncio
async def test_route_restart_allows_admin_via_user_store(router):
    admin_user = MagicMock()
    admin_user.is_admin.return_value = True
    user_store = MagicMock()
    user_store.get.return_value = admin_user
    command_router = CommandRouter(
        router=router,
        scheduler=MagicMock(),
        dispatcher=MagicMock(),
        user_store=user_store,
    )

    result = await command_router.route("#restart", "c1", "p2p", "user-1", "sk1")

    assert result == "服务将在 3 秒后重启..."


@pytest.mark.asyncio
async def test_route_help_returns_help_text_and_plugin_section(router):
    command_router = CommandRouter(router=router, scheduler=MagicMock(), dispatcher=MagicMock())
    command_router.send_menu_card = AsyncMock()
    command_router.register("#briefing", AsyncMock(), "| `#briefing run` | 触发日报 |")

    result = await command_router.route("#help", "c1", "p2p", "user-1", "sk1")

    assert "claude-code-feishu commands" in result
    assert "#briefing run" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("command, expected", [("#opus", "Opus"), ("#sonnet", "Sonnet"), ("#haiku", "Haiku")])
async def test_route_switch_model_updates_session_config(router, command, expected):
    router.save_session = AsyncMock()
    command_router = CommandRouter(router=router, scheduler=MagicMock(), dispatcher=MagicMock())

    result = await command_router.route(command, "c1", "p2p", "user-1", "sk1")

    assert result == f"已切换到 **{expected}**"
    assert router.get_session_llm("sk1") == {"provider": "claude-cli", "model": expected.lower()}


@pytest.mark.asyncio
async def test_route_think_toggles_effort(router):
    router.save_session = AsyncMock()
    command_router = CommandRouter(router=router, scheduler=MagicMock(), dispatcher=MagicMock())

    result_off = await command_router.route("#think", "c1", "p2p", "user-1", "sk1")
    assert result_off == "深度推理 **已关闭**（低消耗模式）"
    assert router.get_session_llm("sk1") == {"effort": "low"}

    result_on = await command_router.route("#think", "c1", "p2p", "user-1", "sk1")

    assert result_on == "深度推理 **已开启**（默认模式）"
    assert router.get_session_llm("sk1") == {}


@pytest.mark.asyncio
async def test_route_jobs_returns_scheduled_job_list(router):
    scheduler = MagicMock()
    scheduler.list_jobs.return_value = [
        SimpleNamespace(
            name="daily-briefing",
            enabled=True,
            schedule=SimpleNamespace(expr="0 9 * * *", every_seconds=None),
            state=SimpleNamespace(next_run_at=1713258000),
        )
    ]
    command_router = CommandRouter(router=router, scheduler=scheduler, dispatcher=MagicMock())

    result = await command_router.route("#jobs", "c1", "p2p", "user-1", "sk1")

    assert "daily-briefing" in result
    assert "0 9 * * *" in result


@pytest.mark.asyncio
async def test_route_reset_clears_session_and_sends_card(router):
    router.clear_session = MagicMock()
    router.save_session = AsyncMock()
    dispatcher = MagicMock()
    dispatcher._build_card_json.return_value = {"card": "reset"}
    dispatcher.send_card_raw = AsyncMock()
    command_router = CommandRouter(router=router, scheduler=MagicMock(), dispatcher=dispatcher)

    result = await command_router.route("#reset", "c1", "p2p", "user-1", "sk1")

    assert result is None
    router.clear_session.assert_called_once_with("sk1")
    dispatcher.send_card_raw.assert_awaited_once_with("c1", {"card": "reset"})


@pytest.mark.asyncio
async def test_route_dispatches_registered_plugin_command(router):
    command_router = CommandRouter(router=router, scheduler=MagicMock(), dispatcher=MagicMock())
    handler = AsyncMock(return_value="plugin ok")
    command_router.register("#briefing", handler, "| `#briefing run` | 触发日报 |")

    result = await command_router.route("#briefing run", "c1", "p2p", "user-1", "sk1")

    handler.assert_awaited_once_with("#briefing", "run")
    assert result == "plugin ok"


@pytest.mark.asyncio
async def test_route_returns_none_for_non_command_and_skill_route(router):
    command_router = CommandRouter(router=router, scheduler=MagicMock(), dispatcher=MagicMock())

    plain_result = await command_router.route("hello world", "c1", "p2p", "user-1", "sk1")
    skill_result = await command_router.route("#plan build roadmap", "c1", "p2p", "user-1", "sk1")

    assert plain_result is None
    assert skill_result is None
