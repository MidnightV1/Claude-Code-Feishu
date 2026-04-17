"""Unit tests for Hardgate module — deterministic pre-QA gate."""

import asyncio
from unittest.mock import AsyncMock, patch


# ── parse_affected_files ───────────────────────────────────────────────────

def test_parse_affected_files_basic():
    from agent.jobs.hardgate import parse_affected_files

    diagnosis = """
<affected-files>
- agent/jobs/maqs.py
- agent/jobs/mads/helpers.py
</affected-files>
"""
    result = parse_affected_files(diagnosis)
    assert result == ["agent/jobs/maqs.py", "agent/jobs/mads/helpers.py"]


def test_parse_affected_files_with_line_numbers():
    from agent.jobs.hardgate import parse_affected_files

    diagnosis = """
<affected-files>
- agent/jobs/maqs.py:563
- agent/jobs/mads/helpers.py:324
</affected-files>
"""
    result = parse_affected_files(diagnosis)
    assert result == ["agent/jobs/maqs.py", "agent/jobs/mads/helpers.py"]


def test_parse_affected_files_empty():
    from agent.jobs.hardgate import parse_affected_files

    assert parse_affected_files("no xml block here") == []


# ── Gold standard: diff scope violation ───────────────────────────────────

def test_diff_scope_violation():
    """Gold standard: fix_branch modified agent/main.py beyond allowed scope."""
    from agent.jobs.hardgate import Hardgate, HardgateResult

    # git diff returns two files; only one is allowed
    git_diff_output = "agent/jobs/maqs.py\nagent/main.py"

    async def run_test():
        with patch("agent.jobs.hardgate._git", new=AsyncMock(return_value=(0, git_diff_output, ""))):
            hg = Hardgate()
            # Bypass smoke and pytest — they make real subprocess calls
            hg._run_smoke_test = AsyncMock(return_value={"ok": True, "output": ""})
            hg._run_pytest = AsyncMock(return_value={"ok": True, "output": ""})
            return await hg.run(
                fix_branch="fix/MAQS-abc12345",
                allowed_files=["agent/jobs/maqs.py"],
            )

    result = asyncio.run(run_test())

    assert isinstance(result, HardgateResult)
    assert result.passed is False

    ds = result.details["diff_scope"]
    assert ds["ok"] is False
    assert ds["allowed"] == ["agent/jobs/maqs.py"]
    assert "agent/jobs/maqs.py" in ds["actual"]
    assert "agent/main.py" in ds["actual"]


# ── All checks pass ────────────────────────────────────────────────────────

def test_all_checks_pass():
    from agent.jobs.hardgate import Hardgate

    git_diff_output = "agent/jobs/maqs.py"

    async def run_test():
        with patch("agent.jobs.hardgate._git", new=AsyncMock(return_value=(0, git_diff_output, ""))):
            hg = Hardgate()
            hg._run_smoke_test = AsyncMock(return_value={"ok": True, "output": ""})
            hg._run_pytest = AsyncMock(return_value={"ok": True, "output": ""})
            return await hg.run(
                fix_branch="fix/MAQS-abc12345",
                allowed_files=["agent/jobs/maqs.py"],
            )

    result = asyncio.run(run_test())
    assert result.passed is True
    assert result.details["diff_scope"]["ok"] is True


# ── Smoke test failure blocks gate ─────────────────────────────────────────

def test_smoke_failure_blocks():
    from agent.jobs.hardgate import Hardgate

    async def run_test():
        with patch("agent.jobs.hardgate._git", new=AsyncMock(return_value=(0, "agent/jobs/maqs.py", ""))):
            hg = Hardgate()
            hg._run_smoke_test = AsyncMock(return_value={"ok": False, "output": "import error"})
            hg._run_pytest = AsyncMock(return_value={"ok": True, "output": ""})
            return await hg.run("fix/MAQS-abc12345", ["agent/jobs/maqs.py"])

    result = asyncio.run(run_test())
    assert result.passed is False
    assert result.details["smoke"]["ok"] is False


# ── Empty allowed_files scope check ──────────────────────────────────────

def test_empty_allowed_files_rejects_non_test_modifications():
    """When affected_files is empty, non-test file changes should be rejected."""
    from agent.jobs.hardgate import Hardgate

    async def run_test():
        with patch("agent.jobs.hardgate._git", new=AsyncMock(return_value=(0, "agent/jobs/maqs.py", ""))):
            hg = Hardgate()
            hg._run_smoke_test = AsyncMock(return_value={"ok": True, "output": ""})
            hg._run_pytest = AsyncMock(return_value={"ok": True, "output": ""})
            return await hg.run("fix/MAQS-abc12345", [])

    result = asyncio.run(run_test())
    assert result.details["diff_scope"]["ok"] is False


def test_empty_allowed_files_passes_test_only_modifications():
    """When affected_files is empty, test-only changes should still pass."""
    from agent.jobs.hardgate import Hardgate

    async def run_test():
        with patch("agent.jobs.hardgate._git", new=AsyncMock(return_value=(0, "tests/test_foo.py", ""))):
            hg = Hardgate()
            hg._run_smoke_test = AsyncMock(return_value={"ok": True, "output": ""})
            hg._run_pytest = AsyncMock(return_value={"ok": True, "output": ""})
            return await hg.run("fix/MAQS-abc12345", [])

    result = asyncio.run(run_test())
    assert result.details["diff_scope"]["ok"] is True


# ── _check_prompt_changes ──────────────────────────────────────────────────

def test_prompt_changes_detects_prompt_assignment():
    from agent.jobs.hardgate import Hardgate

    diff_with_prompt = (
        "diff --git a/agent/jobs/maqs.py b/agent/jobs/maqs.py\n"
        "+GATEKEEPER_PROMPT = \"\"\"\\\n"
        "+you are a QA agent...\n"
    )

    async def run_test():
        with patch("agent.jobs.hardgate._git", new=AsyncMock(return_value=(0, diff_with_prompt, ""))):
            hg = Hardgate()
            return await hg._check_prompt_changes("fix/MAQS-abc12345")

    result = asyncio.run(run_test())
    assert result["prompt_changes"] is True
    assert result["ok"] is False


def test_prompt_changes_detects_system_prompt_assignment():
    from agent.jobs.hardgate import Hardgate

    diff_with_system_prompt = (
        "diff --git a/agent/llm/router.py b/agent/llm/router.py\n"
        "+    system_prompt = build_prompt(ctx)\n"
    )

    async def run_test():
        with patch("agent.jobs.hardgate._git", new=AsyncMock(return_value=(0, diff_with_system_prompt, ""))):
            hg = Hardgate()
            return await hg._check_prompt_changes("fix/MAQS-abc12345")

    result = asyncio.run(run_test())
    assert result["prompt_changes"] is True
    assert result["ok"] is False


def test_prompt_changes_no_prompt_in_diff():
    from agent.jobs.hardgate import Hardgate

    diff_no_prompt = (
        "diff --git a/agent/jobs/hardgate.py b/agent/jobs/hardgate.py\n"
        "+    def _check_prompt_changes(self):\n"
        "+        return {}\n"
    )

    async def run_test():
        with patch("agent.jobs.hardgate._git", new=AsyncMock(return_value=(0, diff_no_prompt, ""))):
            hg = Hardgate()
            return await hg._check_prompt_changes("fix/MAQS-abc12345")

    result = asyncio.run(run_test())
    assert result["prompt_changes"] is False
    assert result["ok"] is True


def test_prompt_changes_git_error_is_non_blocking():
    from agent.jobs.hardgate import Hardgate

    async def run_test():
        with patch("agent.jobs.hardgate._git", new=AsyncMock(return_value=(1, "", "fatal: not a git repo"))):
            hg = Hardgate()
            return await hg._check_prompt_changes("fix/MAQS-abc12345")

    result = asyncio.run(run_test())
    assert result["ok"] is True
    assert result["prompt_changes"] is False


def test_prompt_changes_blocks_hardgate_run():
    """Gold standard: prompt change in diff causes Hardgate to REJECT."""
    from agent.jobs.hardgate import Hardgate

    diff_with_prompt = "+INVESTIGATOR_PROMPT = \"new prompt\"\n"

    async def run_test():
        with patch("agent.jobs.hardgate._git", new=AsyncMock(return_value=(0, diff_with_prompt, ""))):
            hg = Hardgate()
            hg._run_smoke_test = AsyncMock(return_value={"ok": True, "output": ""})
            hg._run_pytest = AsyncMock(return_value={"ok": True, "output": ""})
            return await hg.run("fix/MAQS-abc12345", ["agent/jobs/maqs.py"])

    result = asyncio.run(run_test())
    assert result.passed is False
    assert result.details["prompt_changes"]["ok"] is False
    assert result.details["prompt_changes"]["prompt_changes"] is True


# ── Regression: prompt_changes must not fire on line reshuffling ──────────

def test_prompt_changes_ignores_line_reshuffle():
    """Large refactors that move `system_prompt=system_prompt,` around without
    changing its value should NOT flag prompt_changes. Previously any `+`
    line matching the pattern fired the check regardless of matching `-`."""
    from agent.jobs.hardgate import Hardgate

    diff_reshuffle = (
        "--- a/agent/jobs/mads/helpers.py\n"
        "+++ b/agent/jobs/mads/helpers.py\n"
        "@@ -10,3 +10,3 @@\n"
        "-        system_prompt=system_prompt,\n"
        "+        system_prompt=system_prompt,\n"
        "@@ -50,3 +50,3 @@\n"
        "-        system_prompt=system_prompt,\n"
        "+        system_prompt=system_prompt,\n"
    )

    async def run_test():
        with patch("agent.jobs.hardgate._git",
                   new=AsyncMock(return_value=(0, diff_reshuffle, ""))):
            return await Hardgate()._check_prompt_changes("fix/MAQS-abc")

    result = asyncio.run(run_test())
    assert result["prompt_changes"] is False
    assert result["ok"] is True


def test_prompt_changes_detects_real_value_change():
    """An added prompt line without a matching removal IS a real change."""
    from agent.jobs.hardgate import Hardgate

    diff_real = (
        "--- a/agent/jobs/maqs.py\n"
        "+++ b/agent/jobs/maqs.py\n"
        "-INVESTIGATOR_PROMPT = \"old\"\n"
        "+INVESTIGATOR_PROMPT = \"new value\"\n"
    )

    async def run_test():
        with patch("agent.jobs.hardgate._git",
                   new=AsyncMock(return_value=(0, diff_real, ""))):
            return await Hardgate()._check_prompt_changes("fix/MAQS-abc")

    result = asyncio.run(run_test())
    assert result["prompt_changes"] is True
    assert result["ok"] is False


def test_prompt_changes_detects_pure_addition():
    """A new prompt assignment with no matching `-` line is a real change."""
    from agent.jobs.hardgate import Hardgate

    diff_new = (
        "--- a/agent/jobs/maqs.py\n"
        "+++ b/agent/jobs/maqs.py\n"
        "+NEW_PROMPT = \"added\"\n"
    )

    async def run_test():
        with patch("agent.jobs.hardgate._git",
                   new=AsyncMock(return_value=(0, diff_new, ""))):
            return await Hardgate()._check_prompt_changes("fix/MAQS-abc")

    result = asyncio.run(run_test())
    assert result["prompt_changes"] is True
    assert result["ok"] is False
