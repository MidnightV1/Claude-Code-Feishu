"""Tests for _send_long_as_doc — doc share-to-user logic.

Verifies that share_to is read from self.heartbeat.notify_open_id,
not self._config (which was never set and always returned {}).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def _make_bot(notify_open_id="ou_test123"):
    """Build a minimal fake bot that satisfies SessionMixin._send_long_as_doc."""
    from agent.platforms.feishu.session import SessionMixin

    class FakeBot(SessionMixin):
        pass

    bot = FakeBot()

    # heartbeat with notify_open_id (the field under test)
    hb = MagicMock()
    hb.notify_open_id = notify_open_id
    bot.heartbeat = hb

    # api that records post calls; doc creation returns success
    api = MagicMock()
    api.post.return_value = {
        "code": 0,
        "data": {"document": {"document_id": "doc_abc"}},
    }
    bot._feishu_api = api

    # dispatcher
    bot.dispatcher = MagicMock()
    bot.dispatcher.send_text = AsyncMock(return_value="msg_001")

    return bot, api


def _run_send_long(bot, text="x" * 4001):
    return asyncio.run(
        bot._send_long_as_doc(chat_id="chat_1", text=text, reply_to=None)
    )


def test_members_api_called_when_notify_open_id_set():
    """When heartbeat.notify_open_id is non-empty, permissions/members must be POSTed."""
    bot, api = _make_bot(notify_open_id="ou_test123")

    with patch("agent.platforms.feishu.utils.append_markdown_to_doc"):
        _run_send_long(bot)

    calls = [str(c.args[0]) for c in api.post.call_args_list]
    members_calls = [c for c in calls if "permissions/doc_abc/members" in c]
    assert len(members_calls) == 1, f"Expected 1 members call, got: {calls}"

    members_body = api.post.call_args_list[-1].kwargs.get("body") or api.post.call_args_list[-1].args[1]
    assert members_body["member_id"] == "ou_test123"
    assert members_body["perm"] == "full_access"


def test_members_api_not_called_when_notify_open_id_empty():
    """When heartbeat.notify_open_id is empty, permissions/members must NOT be called."""
    bot, api = _make_bot(notify_open_id="")

    with patch("agent.platforms.feishu.utils.append_markdown_to_doc"):
        _run_send_long(bot)

    calls = [str(c.args[0]) for c in api.post.call_args_list]
    members_calls = [c for c in calls if "/members" in c]
    assert len(members_calls) == 0, f"Expected no members call, got: {calls}"
