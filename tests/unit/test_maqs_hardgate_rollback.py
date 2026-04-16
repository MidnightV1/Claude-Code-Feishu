"""Test that Hardgate rejection rolls back to 'fixing' (not 'open').

Gold standard:
  fix_branch="fix/MAQS-abc12345", allowed_files=["agent/jobs/maqs.py"],
  but fix also touched agent/main.py
  → bitable status updated to "fixing", not "open"
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call


def _make_ticket(status="open"):
    return {
        "title": "TEST-hardgate",
        "phenomenon": "scope violation test",
        "severity": "P1",
        "diagnosis": "## 影响范围\n<affected-files>\n- agent/jobs/maqs.py\n</affected-files>\n<diagnosis_meta><complexity>L1</complexity><user_impact>low</user_impact></diagnosis_meta>",
        "reject_count": 0,
        "status": status,
        "golden_data": "",
    }


def _make_hardgate_result(passed=False):
    from agent.jobs.hardgate import HardgateResult
    return HardgateResult(
        passed=passed,
        details={
            "diff_scope": {
                "ok": False,
                "allowed": ["agent/jobs/maqs.py"],
                "actual": ["agent/jobs/maqs.py", "agent/main.py"],
                "out_of_scope": ["agent/main.py"],
            },
            "smoke": {"ok": True, "output": ""},
            "pytest": {"ok": True, "output": ""},
        },
    )


async def _run_process_ticket_hardgate_reject():
    from agent.jobs import maqs

    router = MagicMock()
    dispatcher = MagicMock()
    ticket = _make_ticket()
    bitable_calls = []

    async def capture_update(app_token, table_id, record_id, fields):
        bitable_calls.append(fields)

    with (
        patch.object(maqs, "_worktree_create", new=AsyncMock(return_value="/tmp/wt_test")),
        patch.object(maqs, "_worktree_remove", new=AsyncMock()),
        patch.object(maqs, "_git_in", new=AsyncMock(return_value=(0, "abc1234", ""))),
        patch.object(maqs, "_bitable_update", new=AsyncMock(side_effect=capture_update)),
        patch.object(maqs, "_send_status_card", new=AsyncMock(return_value="card_mid")),
        patch.object(maqs, "_update_status_card", new=AsyncMock()),
        patch.object(maqs, "_notify", new=AsyncMock()),
        patch.object(maqs, "diagnose_ticket", new=AsyncMock(
            return_value="## 影响范围\n<affected-files>\n- agent/jobs/maqs.py\n</affected-files>\n<diagnosis_meta><complexity>L1</complexity><user_impact>low</user_impact></diagnosis_meta>"
        )),
        patch.object(maqs, "fix_ticket", new=AsyncMock(return_value="fix report content")),
        patch.object(maqs, "write_artifact", MagicMock()),
        patch("agent.jobs.hardgate.Hardgate") as MockHardgate,
        patch("agent.jobs.hardgate.parse_affected_files", return_value=["agent/jobs/maqs.py"]),
    ):
        mock_hg = MagicMock()
        mock_hg.run = AsyncMock(return_value=_make_hardgate_result(passed=False))
        MockHardgate.return_value = mock_hg

        await maqs.process_ticket(
            router, dispatcher,
            app_token="app1", table_id="tbl1", record_id="recabc12345",
            ticket=ticket, notify_open_id="open_123",
        )

    return bitable_calls


def test_hardgate_reject_sets_status_open():
    """Hardgate rejection must set status='open' so both pipelines can re-pick."""
    bitable_calls = asyncio.run(_run_process_ticket_hardgate_reject())

    # Find the rejection rollback update
    rejection_update = next(
        (c for c in bitable_calls if c.get("qa_verdict") == "REJECT"),
        None,
    )
    assert rejection_update is not None, "Expected a REJECT bitable update"
    assert rejection_update["status"] == "open", (
        f"Expected status='open' on Hardgate reject, got {rejection_update['status']!r}"
    )


def test_run_pipeline_picks_up_fixing_tickets():
    """run_maqs_pipeline must query both status='open' AND status='fixing'."""
    from agent.jobs import maqs

    query_calls = []

    async def capture_query(app_token, table_id, filter_str="", limit=10):
        query_calls.append(filter_str)
        return []

    async def run():
        router = MagicMock()
        dispatcher = MagicMock()
        config = {"bitable_app_token": "app1", "bitable_table_id": "tbl1"}

        with (
            patch.object(maqs, "_reset_stale_intermediate_tickets", new=AsyncMock(return_value=0)),
            patch.object(maqs, "_bitable_query", new=AsyncMock(side_effect=capture_query)),
            patch.object(maqs, "_worktree_cleanup_stale", new=AsyncMock()),
        ):
            await maqs.run_maqs_pipeline(router, dispatcher, config)

    asyncio.run(run())

    assert query_calls, "Expected _bitable_query to be called"
    filter_used = query_calls[0]
    assert "fixing" in filter_used, (
        f"Pipeline filter must include 'fixing', got: {filter_used!r}"
    )
    assert "open" in filter_used, (
        f"Pipeline filter must still include 'open', got: {filter_used!r}"
    )


def test_pipeline_skips_diagnosis_for_fixing_tickets_with_diagnosis():
    """When ticket.status='fixing' and diagnosis exists, skip_diagnosis=True."""
    from agent.jobs import maqs

    process_calls = []

    async def capture_process_ticket(router, dispatcher, app_token, table_id,
                                     record_id, ticket, notify_open_id="",
                                     skip_diagnosis=False, merge_queue=None):
        process_calls.append({"skip_diagnosis": skip_diagnosis, "status": ticket.get("status")})

    fixing_ticket = {
        "record_id": "rec001",
        "fields": {
            "title": "test",
            "severity": "P1",
            "status": "fixing",
            "diagnosis": "existing diagnosis content",
            "reject_count": 0,
        },
    }

    async def run():
        router = MagicMock()
        dispatcher = MagicMock()
        config = {"bitable_app_token": "app1", "bitable_table_id": "tbl1"}

        with (
            patch.object(maqs, "_reset_stale_intermediate_tickets", new=AsyncMock(return_value=0)),
            patch.object(maqs, "_bitable_query", new=AsyncMock(return_value=[fixing_ticket])),
            patch.object(maqs, "_worktree_cleanup_stale", new=AsyncMock()),
            patch.object(maqs, "process_ticket", new=AsyncMock(side_effect=capture_process_ticket)),
        ):
            await maqs.run_maqs_pipeline(router, dispatcher, config)

    asyncio.run(run())

    assert process_calls, "process_ticket must be called"
    assert process_calls[0]["skip_diagnosis"] is True, (
        "status='fixing' with existing diagnosis must pass skip_diagnosis=True"
    )
