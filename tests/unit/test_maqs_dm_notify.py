"""Tests for _notify() DM channel: open_id passed → send_to_user called."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch


def _dispatcher(send_to_delivery=None, send_to_user=None):
    d = MagicMock()
    d.send_to_delivery_target = send_to_delivery or AsyncMock()
    d.send_to_user = send_to_user or AsyncMock()
    return d


async def _call_notify(dispatcher, color, message, open_id=""):
    from agent.jobs import maqs
    await maqs._notify(dispatcher, color, message, open_id)


def test_dm_sent_when_open_id_provided():
    """When open_id is given, send_to_user must be awaited once."""
    from agent.jobs import maqs

    dispatcher = _dispatcher()
    with patch.object(maqs, "_notify_mads", new=AsyncMock()):
        asyncio.run(_call_notify(dispatcher, "yellow", "fix done", open_id="ou_abc123"))

    dispatcher.send_to_user.assert_awaited_once()
    args = dispatcher.send_to_user.call_args[0]
    assert args[0] == "ou_abc123"
    assert "MAQS" in args[1]
    assert "yellow" in args[1]
    assert "fix done" in args[1]


def test_no_dm_when_open_id_empty():
    """When open_id is empty, send_to_user must NOT be called."""
    from agent.jobs import maqs

    dispatcher = _dispatcher()
    with patch.object(maqs, "_notify_mads", new=AsyncMock()):
        asyncio.run(_call_notify(dispatcher, "green", "all good", open_id=""))

    dispatcher.send_to_user.assert_not_awaited()


def test_delivery_chat_always_called():
    """_notify_mads (delivery chat) must always be called regardless of open_id."""
    from agent.jobs import maqs

    notify_mads_mock = AsyncMock()
    dispatcher = _dispatcher()

    with patch.object(maqs, "_notify_mads", notify_mads_mock):
        asyncio.run(_call_notify(dispatcher, "red", "error msg", open_id="ou_xyz"))

    notify_mads_mock.assert_awaited_once()
    args = notify_mads_mock.call_args[0]
    assert args[2] == "error msg"


def test_dm_failure_does_not_raise():
    """If send_to_user raises, _notify must swallow the exception."""
    from agent.jobs import maqs

    dispatcher = _dispatcher(send_to_user=AsyncMock(side_effect=Exception("network error")))
    with patch.object(maqs, "_notify_mads", new=AsyncMock()):
        # Must not raise
        asyncio.run(_call_notify(dispatcher, "red", "msg", open_id="ou_fail"))


async def _call_notify_full(dispatcher, color, message, open_id="", dm_color="", dm_message=""):
    from agent.jobs import maqs
    await maqs._notify(dispatcher, color, message, open_id, dm_color=dm_color, dm_message=dm_message)


def test_stall_dm_uses_red_color():
    """Stall path: DM must receive red color, group receives orange."""
    from agent.jobs import maqs

    dispatcher = _dispatcher()
    notify_mads_mock = AsyncMock()
    with patch.object(maqs, "_notify_mads", notify_mads_mock):
        asyncio.run(_call_notify_full(
            dispatcher, "orange", "MAQS 连续 3 次 QA REJECT: MAQS-abc\n需要人工介入。",
            open_id="ou_user1",
            dm_color="red",
            dm_message="工单 MAQS-abc 需要人工介入\n原因: 连续 3 次 QA REJECT",
        ))

    # Group (delivery chat) gets orange
    group_args = notify_mads_mock.call_args[0]
    assert group_args[1] == "orange"

    # DM gets red with structured message
    dm_args = dispatcher.send_to_user.call_args[0]
    assert "color=red" in dm_args[1]
    assert "工单 MAQS-abc 需要人工介入" in dm_args[1]
    assert "原因: 连续 3 次 QA REJECT" in dm_args[1]


def test_dm_color_falls_back_to_group_color_when_not_specified():
    """When dm_color not given, DM inherits group color (backwards compat)."""
    from agent.jobs import maqs

    dispatcher = _dispatcher()
    with patch.object(maqs, "_notify_mads", new=AsyncMock()):
        asyncio.run(_call_notify_full(
            dispatcher, "yellow", "fix done", open_id="ou_abc",
        ))

    dm_args = dispatcher.send_to_user.call_args[0]
    assert "color=yellow" in dm_args[1]
    assert "fix done" in dm_args[1]
