"""Unit tests for worktree_merge_to_dev rebase-on-conflict recovery."""

import asyncio
from unittest.mock import AsyncMock, patch, call


def _run(coro):
    return asyncio.run(coro)


# ── Direct merge succeeds (no conflict) ────────────────────────────────────

def test_direct_merge_success():
    """When merge succeeds on first try, no rebase is attempted."""
    from agent.jobs.mads.helpers import worktree_merge_to_dev

    mock_git = AsyncMock(return_value=(0, "", ""))
    mock_remove = AsyncMock()

    async def run():
        with patch("agent.jobs.mads.helpers.git", mock_git), \
             patch("agent.jobs.mads.helpers.worktree_remove", mock_remove):
            return await worktree_merge_to_dev("/tmp/wt", "fix/MAQS-abc")

    result = _run(run())
    assert result is True
    # Should have: checkout dev, merge, then cleanup
    calls = [c.args for c in mock_git.call_args_list]
    assert ("checkout", "dev") == calls[0]
    assert calls[1][0] == "merge"
    mock_remove.assert_called_once()


# ── Direct merge fails, rebase succeeds, retry merge succeeds ──────────────

def test_rebase_recovery_success():
    """When direct merge fails but rebase succeeds, retry merge should work."""
    from agent.jobs.mads.helpers import worktree_merge_to_dev

    call_count = {"merge": 0, "checkout": 0}

    async def mock_git(*args, timeout=30):
        if args[0] == "checkout" and args[1] == "dev":
            call_count["checkout"] += 1
            return (0, "", "")
        if args[0] == "merge" and args[1] == "--no-ff":
            call_count["merge"] += 1
            if call_count["merge"] == 1:
                return (1, "", "CONFLICT (content): Merge conflict in foo.py")
            return (0, "", "")  # retry succeeds
        if args[0] == "merge" and args[1] == "--abort":
            return (0, "", "")
        return (0, "", "")

    async def mock_git_in(cwd, *args, timeout=30):
        # rebase succeeds
        return (0, "", "")

    mock_remove = AsyncMock()

    async def run():
        with patch("agent.jobs.mads.helpers.git", mock_git), \
             patch("agent.jobs.mads.helpers.git_in", mock_git_in), \
             patch("agent.jobs.mads.helpers.worktree_remove", mock_remove):
            return await worktree_merge_to_dev("/tmp/wt", "fix/MAQS-abc")

    result = _run(run())
    assert result is True
    assert call_count["merge"] == 2  # first attempt failed, retry succeeded
    mock_remove.assert_called_once()


# ── Direct merge fails, rebase fails (real conflict) ───────────────────────

def test_rebase_fails_real_conflict():
    """When both merge and rebase fail, returns False (real conflict)."""
    from agent.jobs.mads.helpers import worktree_merge_to_dev

    async def mock_git(*args, timeout=30):
        if args[0] == "checkout":
            return (0, "", "")
        if args[0] == "merge" and args[1] == "--no-ff":
            return (1, "", "CONFLICT")
        if args[0] == "merge" and args[1] == "--abort":
            return (0, "", "")
        return (0, "", "")

    rebase_calls = []

    async def mock_git_in(cwd, *args, timeout=30):
        rebase_calls.append(args)
        if args[0] == "rebase" and args[1] == "dev":
            return (1, "", "CONFLICT (content): Merge conflict in bar.py")
        if args[0] == "diff":
            return (0, "bar.py\n", "")
        if args[0] == "rebase" and args[1] == "--abort":
            return (0, "", "")
        return (0, "", "")

    async def run():
        with patch("agent.jobs.mads.helpers.git", mock_git), \
             patch("agent.jobs.mads.helpers.git_in", mock_git_in), \
             patch("agent.jobs.mads.helpers.worktree_remove", AsyncMock()):
            return await worktree_merge_to_dev("/tmp/wt", "fix/MAQS-abc")

    result = _run(run())
    assert result is False
    # Should have attempted rebase, then abort
    rebase_cmds = [c[0] for c in rebase_calls]
    assert "rebase" in rebase_cmds


# ── Checkout dev fails ─────────────────────────────────────────────────────

def test_checkout_dev_fails():
    """If we can't checkout dev, return False immediately."""
    from agent.jobs.mads.helpers import worktree_merge_to_dev

    async def mock_git(*args, timeout=30):
        if args[0] == "checkout":
            return (1, "", "error: cannot checkout dev")
        return (0, "", "")

    async def run():
        with patch("agent.jobs.mads.helpers.git", mock_git):
            return await worktree_merge_to_dev("/tmp/wt", "fix/MAQS-abc")

    result = _run(run())
    assert result is False


# ── parse_affected_files strips annotations ────────────────────────────────

def test_parse_affected_files_strips_annotations():
    """Parenthetical annotations like (新建) must be stripped from paths."""
    from agent.jobs.hardgate import parse_affected_files

    diagnosis = """
<affected_files>
- .claude/skills/invest-radar/scripts/daily_review.py (新建)
- .claude/skills/invest-radar/scripts/invest_ctl.py (修改)
- data/jobs.json (修改)
</affected_files>
"""
    result = parse_affected_files(diagnosis)
    assert result == [
        ".claude/skills/invest-radar/scripts/daily_review.py",
        ".claude/skills/invest-radar/scripts/invest_ctl.py",
        "data/jobs.json",
    ]


def test_parse_affected_files_no_annotation_unchanged():
    """Paths without annotations should parse normally."""
    from agent.jobs.hardgate import parse_affected_files

    diagnosis = """
<affected_files>
- agent/jobs/maqs.py
- agent/jobs/mads/helpers.py:324
</affected_files>
"""
    result = parse_affected_files(diagnosis)
    assert result == ["agent/jobs/maqs.py", "agent/jobs/mads/helpers.py"]


def test_parse_affected_files_mixed_annotations():
    """Mix of annotated and non-annotated paths."""
    from agent.jobs.hardgate import parse_affected_files

    diagnosis = """
<affected_files>
- src/new_file.py (新建)
- src/existing.py
- tests/test_foo.py (新增)
</affected_files>
"""
    result = parse_affected_files(diagnosis)
    assert result == ["src/new_file.py", "src/existing.py", "tests/test_foo.py"]
