"""Tests for _notify() — delivery chat only (notifier bot), no DM."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def _dispatcher():
    d = MagicMock()
    d.send_to_delivery_target = AsyncMock()
    d.send_to_user = AsyncMock()
    return d


async def _call_notify(dispatcher, color, message, open_id=""):
    from agent.jobs import maqs
    await maqs._notify(dispatcher, color, message, open_id)


def test_delivery_chat_always_called():
    """_notify_mads (delivery chat) must always be called."""
    from agent.jobs import maqs

    notify_mads_mock = AsyncMock()
    dispatcher = _dispatcher()

    with patch.object(maqs, "_notify_mads", notify_mads_mock):
        asyncio.run(_call_notify(dispatcher, "red", "error msg", open_id="ou_xyz"))

    notify_mads_mock.assert_awaited_once()
    args = notify_mads_mock.call_args[0]
    assert args[2] == "error msg"


def test_no_dm_even_when_open_id_provided():
    """DM removed — send_to_user must NOT be called even with open_id."""
    from agent.jobs import maqs

    dispatcher = _dispatcher()
    with patch.object(maqs, "_notify_mads", new=AsyncMock()):
        asyncio.run(_call_notify(dispatcher, "yellow", "fix done", open_id="ou_abc123"))

    dispatcher.send_to_user.assert_not_awaited()


def test_no_dm_when_open_id_empty():
    """send_to_user must not be called when open_id is empty."""
    from agent.jobs import maqs

    dispatcher = _dispatcher()
    with patch.object(maqs, "_notify_mads", new=AsyncMock()):
        asyncio.run(_call_notify(dispatcher, "green", "all good", open_id=""))

    dispatcher.send_to_user.assert_not_awaited()
