"""Regression tests for Hardgate REJECT notification.

Prior bug: notification only showed `diff_scope` regardless of which check
actually failed, so when smoke or pytest failed (with diff_scope ok), the
user saw a misleading `diff_scope={'ok': True, ...}` body.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def _run_with_hardgate_details(details):
    from agent.jobs import maqs
    from agent.jobs.hardgate import HardgateResult

    captured = []

    async def fake_notify(dispatcher, color, msg, open_id):
        captured.append(msg)

    ticket = {
        "title": "t", "phenomenon": "p", "severity": "P1",
        "reject_count": 2, "status": "open", "golden_data": "",
        "diagnosis": (
            "<affected-files>\n- agent/jobs/maqs.py\n</affected-files>\n"
            "<diagnosis_meta><complexity>L1</complexity></diagnosis_meta>"
        ),
    }

    async def run():
        with (
            patch.object(maqs, "_worktree_create", new=AsyncMock(return_value="/tmp/wt")),
            patch.object(maqs, "_worktree_remove", new=AsyncMock()),
            patch.object(maqs, "_git_in", new=AsyncMock(return_value=(0, "abc", ""))),
            patch.object(maqs, "_bitable_update", new=AsyncMock()),
            patch.object(maqs, "_send_status_card", new=AsyncMock(return_value="mid")),
            patch.object(maqs, "_update_status_card", new=AsyncMock()),
            patch.object(maqs, "_notify", side_effect=fake_notify),
            patch.object(maqs, "diagnose_ticket", new=AsyncMock(return_value=ticket["diagnosis"])),
            patch.object(maqs, "fix_ticket", new=AsyncMock(return_value="fix")),
            patch.object(maqs, "write_artifact", MagicMock()),
            patch("agent.jobs.hardgate.Hardgate") as MockHG,
            patch("agent.jobs.hardgate.parse_affected_files",
                  return_value=["agent/jobs/maqs.py"]),
        ):
            mock_hg = MagicMock()
            mock_hg.run = AsyncMock(return_value=HardgateResult(passed=False, details=details))
            MockHG.return_value = mock_hg
            await maqs.process_ticket(
                MagicMock(), MagicMock(),
                app_token="app", table_id="tbl", record_id="rec123",
                ticket=ticket, notify_open_id="",
            )

    asyncio.run(run())
    return captured[0] if captured else ""


def test_notify_shows_failing_check_not_diff_scope_when_smoke_fails():
    msg = _run_with_hardgate_details({
        "smoke": {"ok": False, "output": "ModuleNotFoundError: No module named foo"},
        "pytest": {"ok": True, "output": ""},
        "diff_scope": {"ok": True,
                       "allowed": ["agent/jobs/maqs.py"],
                       "actual": ["agent/jobs/maqs.py"]},
        "prompt_changes": {"ok": True, "output": ""},
        "typecheck": {"ok": True, "output": "skip"},
    })
    assert "smoke:" in msg
    assert "ModuleNotFoundError" in msg
    assert "diff_scope" not in msg


def test_notify_shows_multiple_failing_checks():
    msg = _run_with_hardgate_details({
        "smoke": {"ok": False, "output": "AssertionError: config missing"},
        "pytest": {"ok": False, "output": "1 failed: test_foo FAILED"},
        "diff_scope": {"ok": True, "allowed": ["a.py"], "actual": ["a.py"]},
        "prompt_changes": {"ok": True, "output": ""},
        "typecheck": {"ok": True, "output": "skip"},
    })
    assert "smoke:" in msg
    assert "pytest:" in msg
    assert "AssertionError" in msg
    assert "test_foo FAILED" in msg


def test_notify_diff_scope_shows_sorted_lists_and_out_of_scope():
    msg = _run_with_hardgate_details({
        "smoke": {"ok": True, "output": ""},
        "pytest": {"ok": True, "output": ""},
        "diff_scope": {"ok": False,
                       "allowed": ["agent/jobs/maqs.py"],
                       "actual": ["agent/main.py", "agent/jobs/maqs.py"],
                       "out_of_scope": ["agent/main.py"]},
        "prompt_changes": {"ok": True, "output": ""},
        "typecheck": {"ok": True, "output": "skip"},
    })
    assert "diff_scope:" in msg
    assert "out_of_scope=['agent/main.py']" in msg
    assert "actual=['agent/jobs/maqs.py', 'agent/main.py']" in msg
