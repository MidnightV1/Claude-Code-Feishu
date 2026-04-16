"""Test that MAQS except block sends a red Feishu notification on pipeline crash."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def _make_ticket(title="TEST-001"):
    return {
        "title": title,
        "phenomenon": "test phenomenon",
        "severity": "P2",
        "diagnosis": "",
        "reject_count": 0,
    }


async def _run_process_ticket_with_crash(dispatcher, notify_open_id="open_123"):
    """Run process_ticket with a mocked crash at diagnosis phase."""
    from agent.jobs import maqs

    router = MagicMock()
    ticket = _make_ticket()

    with (
        patch.object(maqs, "diagnose_ticket", new=AsyncMock(side_effect=RuntimeError("mock crash"))),
        patch.object(maqs, "_worktree_remove", new=AsyncMock()),
        patch.object(maqs, "_bitable_update", new=AsyncMock()),
    ):
        await maqs.process_ticket(
            router, dispatcher,
            app_token="app1", table_id="tbl1", record_id="rec1",
            ticket=ticket, notify_open_id=notify_open_id,
        )


def test_crash_sends_red_notify():
    """Pipeline crash must trigger _notify with color='red'."""
    from agent.jobs import maqs

    notify_mock = AsyncMock()
    dispatcher = MagicMock()

    with patch.object(maqs, "_notify", notify_mock):
        asyncio.run(_run_process_ticket_with_crash(dispatcher, notify_open_id="open_123"))

    notify_mock.assert_awaited_once()
    args = notify_mock.call_args
    assert args[0][1] == "red", f"Expected color='red', got {args[0][1]!r}"
    assert "TEST-001" in args[0][2], "Notification must include ticket_id"
    assert "mock crash" in args[0][2], "Notification must include error message"


def test_crash_notify_failure_does_not_suppress_bitable_update():
    """If _notify itself fails, bitable stalled status must still be written."""
    from agent.jobs import maqs

    bitable_mock = AsyncMock()
    dispatcher = MagicMock()

    with (
        patch.object(maqs, "_notify", side_effect=Exception("feishu unavailable")),
        patch.object(maqs, "diagnose_ticket", new=AsyncMock(side_effect=RuntimeError("mock crash"))),
        patch.object(maqs, "_worktree_remove", new=AsyncMock()),
        patch.object(maqs, "_bitable_update", bitable_mock),
    ):
        # Should not raise even if _notify fails
        asyncio.run(maqs.process_ticket(
            MagicMock(), dispatcher,
            app_token="app1", table_id="tbl1", record_id="rec1",
            ticket=_make_ticket(), notify_open_id="open_123",
        ))

    assert bitable_mock.await_count >= 1
    stalled_calls = [
        c for c in bitable_mock.call_args_list
        if c[0][-1].get("status") == "stalled"
    ]
    assert len(stalled_calls) == 1, "Expected exactly one stalled bitable update"
