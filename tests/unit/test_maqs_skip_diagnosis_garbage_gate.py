"""Test that skip_diagnosis=True path applies garbage gate on existing diagnosis.

Cases:
  1. skip_diagnosis=True + garbage diagnosis → status=stalled, needs_human=True, fix not called
  2. skip_diagnosis=True + valid diagnosis   → fix_ticket is called (garbage gate passes)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


VALID_DIAGNOSIS = (
    "## 影响范围\n"
    "<affected-files>\n- agent/jobs/maqs.py\n</affected-files>\n"
    "<diagnosis_meta><complexity>L1</complexity>"
    "<user_impact>low</user_impact></diagnosis_meta>"
)

GARBAGE_DIAGNOSIS = "you've hit your limit"


def _make_ticket(diagnosis: str, status: str = "fixing") -> dict:
    return {
        "title": "TEST-skip-garbage",
        "phenomenon": "skip diagnosis garbage gate test",
        "severity": "P1",
        "diagnosis": diagnosis,
        "reject_count": 0,
        "status": status,
        "golden_data": "",
    }


def test_garbage_diagnosis_stalls_when_skip_diagnosis():
    """skip_diagnosis=True + garbage diagnosis → stalled+needs_human, fix_ticket not called."""
    from agent.jobs import maqs

    bitable_calls = []
    fix_mock = AsyncMock(return_value="should not be called")

    async def capture_bitable(app_token, table_id, record_id, fields):
        bitable_calls.append(fields)

    async def run():
        router = MagicMock()
        dispatcher = MagicMock()
        ticket = _make_ticket(GARBAGE_DIAGNOSIS)

        with (
            patch.object(maqs, "_bitable_update", new=AsyncMock(side_effect=capture_bitable)),
            patch.object(maqs, "_send_status_card", new=AsyncMock(return_value="card_mid")),
            patch.object(maqs, "_update_status_card", new=AsyncMock()),
            patch.object(maqs, "_notify", new=AsyncMock()),
            patch.object(maqs, "fix_ticket", fix_mock),
            patch.object(maqs, "write_artifact", MagicMock()),
        ):
            await maqs.process_ticket(
                router, dispatcher,
                app_token="app1", table_id="tbl1", record_id="recskip001",
                ticket=ticket, notify_open_id="open_123",
                skip_diagnosis=True,
            )

    asyncio.run(run())

    stall_update = next(
        (c for c in bitable_calls if c.get("status") == "stalled"), None
    )
    assert stall_update is not None, "Expected a stalled bitable update"
    assert stall_update.get("needs_human") is True, (
        f"Expected needs_human=True in stall update, got: {stall_update}"
    )
    fix_mock.assert_not_awaited()


def test_valid_diagnosis_proceeds_to_fix_when_skip_diagnosis():
    """skip_diagnosis=True + valid diagnosis → fix_ticket is called, no garbage-gate stall."""
    from agent.jobs import maqs

    bitable_calls = []
    fix_mock = AsyncMock(return_value="[ERROR] test stop")

    async def capture_bitable(app_token, table_id, record_id, fields):
        bitable_calls.append(fields)

    async def run():
        router = MagicMock()
        dispatcher = MagicMock()
        ticket = _make_ticket(VALID_DIAGNOSIS)

        with (
            patch.object(maqs, "_worktree_create", new=AsyncMock(return_value="/tmp/wt_test")),
            patch.object(maqs, "_worktree_remove", new=AsyncMock()),
            patch.object(maqs, "_bitable_update", new=AsyncMock(side_effect=capture_bitable)),
            patch.object(maqs, "_send_status_card", new=AsyncMock(return_value="card_mid")),
            patch.object(maqs, "_update_status_card", new=AsyncMock()),
            patch.object(maqs, "_notify", new=AsyncMock()),
            patch.object(maqs, "fix_ticket", fix_mock),
            patch.object(maqs, "write_artifact", MagicMock()),
        ):
            await maqs.process_ticket(
                router, dispatcher,
                app_token="app1", table_id="tbl1", record_id="recskip002",
                ticket=ticket, notify_open_id="open_123",
                skip_diagnosis=True,
            )

    asyncio.run(run())

    fix_mock.assert_awaited_once()

    garbage_gate_stall = next(
        (c for c in bitable_calls
         if c.get("status") == "stalled" and c.get("needs_human") is True and "diagnosis" in c),
        None,
    )
    assert garbage_gate_stall is None, (
        f"Valid diagnosis must not trigger garbage-gate stall, got: {garbage_gate_stall}"
    )
