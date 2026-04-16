# -*- coding: utf-8 -*-
"""Shared infrastructure for MADS/MAQS pipelines.

Extracted from maqs.py to enable reuse across all pipeline stages.
Covers: Bitable CRUD, Git operations, LLM agent runner, file-based
artifact I/O, notification, and JSON parsing.
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("hub.mads")

# ══════════════════════════════════════════════════════════════════════
#  Constants
# ══════════════════════════════════════════════════════════════════════

PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

_BITABLE_SCRIPT = os.path.join(
    PROJECT_ROOT, ".claude", "skills", "feishu-bitable", "scripts", "bitable_ctl.py"
)
_DOC_CTL_SCRIPT = os.path.join(
    PROJECT_ROOT, ".claude", "skills", "feishu-doc", "scripts", "doc_ctl.py"
)
_TASK_CTL_SCRIPT = os.path.join(
    PROJECT_ROOT, ".claude", "skills", "feishu-task", "scripts", "task_ctl.py"
)

# Artifact directory for file-based agent communication
ARTIFACTS_DIR = os.path.join(PROJECT_ROOT, "data", "mads")


# ══════════════════════════════════════════════════════════════════════
#  Bitable helpers
# ══════════════════════════════════════════════════════════════════════

async def bitable_add(app_token: str, table_id: str, fields: dict) -> str | None:
    """Create a bitable record, return record_id or None."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, _BITABLE_SCRIPT, "record", "add", app_token, table_id,
            "--fields", json.dumps(fields, ensure_ascii=False),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=PROJECT_ROOT,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0:
            out = stdout.decode().strip()
            if out.startswith("Created: "):
                return out.split("Created: ", 1)[1].strip()
            return "ok"
        else:
            log.warning("Bitable add failed: %s", stderr.decode()[:200])
    except asyncio.TimeoutError:
        log.warning("Bitable add timed out")
    except Exception as e:
        log.warning("Bitable add error: %s", e)
    return None


async def bitable_update(app_token: str, table_id: str,
                          record_id: str, fields: dict):
    """Update an existing bitable record."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, _BITABLE_SCRIPT, "record", "update",
            app_token, table_id, record_id,
            "--fields", json.dumps(fields, ensure_ascii=False),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=PROJECT_ROOT,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
    except Exception as e:
        log.warning("Bitable update error: %s", e)


async def bitable_query(app_token: str, table_id: str,
                         filter_str: str = "", limit: int = 50) -> list[dict]:
    """Query bitable records, return list of {record_id, fields}."""
    try:
        cmd = [sys.executable, _BITABLE_SCRIPT, "record", "list",
               app_token, table_id, "--limit", str(limit), "--json"]
        if filter_str:
            cmd.extend(["--filter", filter_str])
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=PROJECT_ROOT,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0:
            data = json.loads(stdout.decode())
            return data if isinstance(data, list) else []
    except Exception as e:
        log.warning("Bitable query error: %s", e)
    return []


async def bitable_get_status(app_token: str, table_id: str,
                              record_id: str) -> str | None:
    """Get the status field of a single record by record_id.

    Returns the status string, or None if the record cannot be retrieved.
    """
    try:
        cmd = [sys.executable, _BITABLE_SCRIPT, "record", "get",
               app_token, table_id, record_id]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=PROJECT_ROOT,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return None
        # Parse "  status: "value"" from text output
        for line in stdout.decode().splitlines():
            stripped = line.strip()
            if stripped.startswith("status:"):
                # status: "open" → extract "open"
                val = stripped.split(":", 1)[1].strip().strip('"')
                return val
    except Exception as e:
        log.warning("Bitable get_status error for %s: %s", record_id, e)
    return None


# ══════════════════════════════════════════════════════════════════════
#  Git helpers
# ══════════════════════════════════════════════════════════════════════

async def git(*args, timeout: int = 30) -> tuple[int, str, str]:
    """Run a git command, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=PROJECT_ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


async def git_create_branch(branch: str) -> bool:
    """Create and checkout a new branch from dev."""
    rc, _, _ = await git("checkout", branch)
    if rc == 0:
        return True
    rc, _, err = await git("checkout", "-b", branch, "dev")
    if rc != 0:
        log.error("Failed to create branch %s: %s", branch, err)
        return False
    return True


async def git_merge_to_dev(branch: str) -> bool:
    """Merge fix branch back to dev and delete the branch."""
    rc, _, err = await git("checkout", "dev")
    if rc != 0:
        log.error("Failed to checkout dev: %s", err)
        return False
    rc, _, err = await git("merge", "--no-ff", branch, "-m",
                            f"merge: {branch} into dev")
    if rc != 0:
        log.error("Failed to merge %s: %s", branch, err)
        return False
    await git("branch", "-d", branch)
    return True


async def git_revert_last() -> bool:
    """Revert the last commit (used on QA REJECT)."""
    rc, _, err = await git("revert", "--no-edit", "HEAD")
    if rc != 0:
        log.error("Failed to revert: %s", err)
        return False
    return True


async def git_current_branch() -> str:
    """Get current branch name."""
    rc, out, _ = await git("rev-parse", "--abbrev-ref", "HEAD")
    return out if rc == 0 else "unknown"


async def git_restore_branch(original: str):
    """Restore to original branch (cleanup on failure)."""
    await git("checkout", original)


# ── Worktree isolation ──

WORKTREE_BASE = os.path.join(PROJECT_ROOT, ".worktrees")


async def git_in(cwd: str, *args, timeout: int = 30) -> tuple[int, str, str]:
    """Run a git command in a specific directory."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


async def worktree_create(branch: str) -> str | None:
    """Create a git worktree for a fix branch. Returns worktree path or None.

    Creates branch from dev if it doesn't exist. The worktree lives in
    .worktrees/<branch-name>/ alongside the main repo.
    """
    os.makedirs(WORKTREE_BASE, exist_ok=True)
    safe_name = branch.replace("/", "_")
    wt_path = os.path.join(WORKTREE_BASE, safe_name)

    # Clean up stale worktree if directory exists
    if os.path.exists(wt_path):
        await git("worktree", "remove", "--force", wt_path)
        if os.path.exists(wt_path):
            shutil.rmtree(wt_path, ignore_errors=True)

    # Check if branch already exists
    rc, _, _ = await git("rev-parse", "--verify", branch)
    if rc == 0:
        # Branch exists — create worktree on it
        rc, _, err = await git("worktree", "add", wt_path, branch)
    else:
        # Create new branch from dev
        rc, _, err = await git("worktree", "add", "-b", branch, wt_path, "dev")

    if rc != 0:
        log.error("Failed to create worktree for %s: %s", branch, err)
        return None

    log.info("Worktree created: %s → %s", branch, wt_path)
    return wt_path


async def worktree_remove(wt_path: str, branch: str | None = None):
    """Remove a worktree and optionally delete the branch."""
    if not wt_path or not os.path.exists(wt_path):
        return
    rc, _, err = await git("worktree", "remove", "--force", wt_path)
    if rc != 0:
        log.warning("worktree remove failed: %s — forcing cleanup", err[:200])
        shutil.rmtree(wt_path, ignore_errors=True)
        await git("worktree", "prune")
    if branch:
        await git("branch", "-D", branch)


async def worktree_merge_to_dev(wt_path: str, branch: str) -> bool:
    """Merge worktree branch to dev in the main repo, then clean up.

    On merge conflict, attempts rebase onto latest dev first (handles the
    common case of stale branches). If rebase succeeds, retries merge.
    """
    rc, _, err = await git("checkout", "dev")
    if rc != 0:
        log.error("Failed to checkout dev for merge: %s", err)
        return False

    rc, _, err = await git("merge", "--no-ff", branch, "-m",
                            f"merge: {branch} into dev")
    if rc == 0:
        # Clean up worktree and branch
        await worktree_remove(wt_path, branch)
        return True

    # Merge failed — try rebase-then-merge recovery
    log.warning("Direct merge failed for %s, attempting rebase recovery", branch)
    await git("merge", "--abort")

    # Rebase the fix branch onto latest dev inside the worktree.
    # Worktrees share refs with the main repo, so 'dev' is already up-to-date
    # after our checkout above.
    rc_rb, _, err_rb = await git_in(wt_path, "rebase", "dev", timeout=60)

    if rc_rb != 0:
        # Rebase failed — real conflict
        # Extract conflicting files for diagnostics
        _, conflict_out, _ = await git_in(wt_path, "diff", "--name-only",
                                           "--diff-filter=U")
        conflict_files = [f.strip() for f in conflict_out.splitlines() if f.strip()]
        await git_in(wt_path, "rebase", "--abort")
        log.error("Rebase failed for %s — real conflict on: %s",
                  branch, conflict_files or err_rb[:200])
        return False

    # Rebase succeeded — retry merge
    rc, _, err = await git("checkout", "dev")
    if rc != 0:
        log.error("Failed to checkout dev for retry merge: %s", err)
        return False

    rc, _, err = await git("merge", "--no-ff", branch, "-m",
                            f"merge: {branch} into dev")
    if rc != 0:
        log.error("Merge still failed after rebase for %s: %s", branch, err)
        await git("merge", "--abort")
        return False

    # Clean up worktree and branch
    await worktree_remove(wt_path, branch)
    return True


async def worktree_cleanup_stale():
    """Remove any stale worktrees (orphaned from crashed pipelines)."""
    if not os.path.exists(WORKTREE_BASE):
        return
    await git("worktree", "prune")
    # Remove dirs that are no longer tracked by git worktree
    rc, out, _ = await git("worktree", "list", "--porcelain")
    tracked = set()
    if rc == 0:
        for line in out.split("\n"):
            if line.startswith("worktree "):
                tracked.add(line.split(" ", 1)[1])
    for entry in os.listdir(WORKTREE_BASE):
        entry_path = os.path.join(WORKTREE_BASE, entry)
        if entry_path not in tracked and os.path.isdir(entry_path):
            log.info("Cleaning stale worktree dir: %s", entry_path)
            shutil.rmtree(entry_path, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════
#  LLM helpers
# ══════════════════════════════════════════════════════════════════════

def parse_json_response(text: str) -> dict | list | None:
    """Parse JSON from LLM response, handling markdown fences and preamble text.

    Handles: bare JSON, ```json fenced blocks, and text-before-JSON patterns.
    """
    import re

    text = text.strip()

    # Try 1: Extract from markdown code fence (```json ... ``` or ``` ... ```)
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fence_match:
        candidate = fence_match.group(1).strip()
        try:
            obj, _ = json.JSONDecoder().raw_decode(candidate)
            return obj
        except (json.JSONDecodeError, ValueError):
            pass

    # Try 2: Find first [ or { and parse from there
    for i, ch in enumerate(text):
        if ch in ("[", "{"):
            try:
                obj, _ = json.JSONDecoder().raw_decode(text[i:])
                return obj
            except (json.JSONDecodeError, ValueError):
                continue

    return None


async def run_agent(router, role: str, model: str, prompt: str,
                     system_prompt: str, workdir: str | None = None) -> str:
    """Run an isolated agent call, return response text.

    Each call is a fresh `claude -p` invocation — no session carryover.
    Uses idle-based timeout (900s idle, 3600s hard cap) so tool-using agents
    are not killed while actively working without stream output.
    """
    from agent.infra.models import LLMConfig

    llm_config = LLMConfig(
        provider="claude-cli",
        model=model,
        system_prompt=system_prompt,
        workspace_dir=workdir,
    )
    log.info("MADS agent [%s] starting (model=%s, idle-based timeout)", role, model)
    result = await router.run(prompt=prompt, llm_config=llm_config)

    if result.is_error:
        log.warning("MADS agent [%s] failed: %s", role, result.text[:200])
        return f"[ERROR] {result.text[:500]}"

    log.info("MADS agent [%s] completed (tokens: %d in / %d out, cost: $%.3f)",
             role, result.input_tokens, result.output_tokens, result.cost_usd)
    return result.text


async def run_codex(prompt: str, workdir: str | None = None,
                     timeout: int = 300) -> str:
    """Run Codex CLI non-interactively, return response text.

    Uses `codex exec --full-auto` with workspace-write sandbox.
    Shares the same prompt/output format as Claude Implementer.
    """
    import asyncio as _aio
    import shutil

    codex_bin = shutil.which("codex")
    if not codex_bin:
        return "[ERROR] codex CLI not found"

    cmd = [
        codex_bin, "exec", prompt,
        # No sandbox — worktree isolation is our sandbox.
        # workspace-write uses overlay FS that discards changes on exit,
        # and blocks .git/ writes needed for git add/commit.
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    cwd = workdir or os.path.join(os.path.dirname(__file__), "..", "..", "..")

    log.info("Codex agent starting (timeout=%ds, cwd=%s)", timeout, cwd)
    try:
        proc = await _aio.create_subprocess_exec(
            *cmd, cwd=cwd,
            stdout=_aio.subprocess.PIPE,
            stderr=_aio.subprocess.PIPE,
        )
        stdout, stderr = await _aio.wait_for(
            proc.communicate(), timeout=timeout)
        text = stdout.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            # Check for rate limit / quota exhaustion
            if "rate" in err.lower() or "limit" in err.lower() or "quota" in err.lower():
                log.warning("Codex quota/rate limit hit: %s", err[:200])
                return "[ERROR:QUOTA] " + err[:500]
            log.warning("Codex failed (rc=%d): %s", proc.returncode, err[:200])
            return f"[ERROR] Codex exit {proc.returncode}: {err[:500]}"

        log.info("Codex agent completed (%d chars output)", len(text))
        return text

    except _aio.TimeoutError:
        log.warning("Codex timed out after %ds", timeout)
        try:
            proc.kill()
        except Exception:
            pass
        return f"[ERROR] Codex timed out after {timeout}s"
    except Exception as e:
        log.error("Codex unexpected error: %s", e)
        return f"[ERROR] {e}"


def parse_qa_verdict(qa_report: str) -> str:
    """Parse <qa_verdict> XML control block from QA report.

    Returns verdict string: "PASS" or "REJECT".
    """
    import re
    m = re.search(
        r"<qa_verdict>\s*<result>\s*(PASS|REJECT)\s*</result>",
        qa_report, re.IGNORECASE,
    )
    if m:
        return m.group(1).upper()

    log.warning("QA report missing <qa_verdict> control block, defaulting to REJECT")
    return "REJECT"


# ══════════════════════════════════════════════════════════════════════
#  File-based artifact I/O
# ══════════════════════════════════════════════════════════════════════

def _ticket_dir(ticket_id: str) -> Path:
    """Get or create artifact directory for a ticket."""
    d = Path(ARTIFACTS_DIR) / ticket_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_artifact(ticket_id: str, name: str, content: str):
    """Write an artifact file (diagnosis.md, contract.md, etc.)."""
    path = _ticket_dir(ticket_id) / name
    path.write_text(content, encoding="utf-8")
    log.debug("Artifact written: %s/%s (%d chars)", ticket_id, name, len(content))


def read_artifact(ticket_id: str, name: str) -> str | None:
    """Read an artifact file, return None if not found."""
    path = _ticket_dir(ticket_id) / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def append_artifact(ticket_id: str, name: str, content: str):
    """Append to an artifact file (for bidirectional contract negotiation)."""
    path = _ticket_dir(ticket_id) / name
    with open(path, "a", encoding="utf-8") as f:
        f.write(content)


def list_artifacts(ticket_id: str) -> list[str]:
    """List artifact files for a ticket."""
    d = _ticket_dir(ticket_id)
    return [f.name for f in sorted(d.iterdir()) if f.is_file()]


# ══════════════════════════════════════════════════════════════════════
#  Conflict prediction
# ══════════════════════════════════════════════════════════════════════

def predict_conflicts(modified_files: list[str], active_files: set[str]) -> list[str]:
    """Return files that appear in both modified_files and active_files.

    Used to predict merge conflicts when multiple worktrees are active.
    Preserves the order of modified_files in the result.
    """
    return [f for f in modified_files if f in active_files]


# ══════════════════════════════════════════════════════════════════════
#  Subprocess helpers (Feishu doc/task)
# ══════════════════════════════════════════════════════════════════════

async def run_script(script_path: str, *args, timeout: int = 60) -> tuple[int, str, str]:
    """Run a Python script as subprocess, return (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, script_path, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=PROJECT_ROOT,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


async def doc_ctl(*args, timeout: int = 60) -> tuple[int, str, str]:
    """Run doc_ctl.py command."""
    return await run_script(_DOC_CTL_SCRIPT, *args, timeout=timeout)


async def task_ctl(*args, timeout: int = 60) -> tuple[int, str, str]:
    """Run task_ctl.py command."""
    return await run_script(_TASK_CTL_SCRIPT, *args, timeout=timeout)


# ══════════════════════════════════════════════════════════════════════
#  Notification
# ══════════════════════════════════════════════════════════════════════

async def notify(dispatcher, color: str, message: str,
                  header: str = "MADS"):
    """Send notification via dispatcher (notifier app)."""
    try:
        text = f"{{{{card:header={header},color={color}}}}}\n{message}"
        await dispatcher.send_to_delivery_target(text)
    except Exception as e:
        log.warning("MADS notification failed: %s", e)


# ── Status card ──

_PHASE_LABELS = ["diagnosing", "contracting", "fixing", "hardgate", "reviewing", "merging"]
_SEVERITY_COLORS = {"P0": "red", "P1": "orange", "P2": "wathet", "P3": "grey"}
_STATUS_ICONS = {"running": "●", "done": "✅", "pending": "○", "failed": "❌"}


_STEP_ICONS = {0: "⬜", 1: "🔄", 2: "✅", 3: "❌", 4: "🔒"}


def _render_workflow_section(workflow_data) -> str:
    """Render workflow steps as card markdown section.

    Accepts TicketWorkflow object or serialized dict.
    """
    if workflow_data is None:
        return ""

    if isinstance(workflow_data, dict):
        steps = workflow_data.get("steps", [])
    elif hasattr(workflow_data, "steps"):
        steps = workflow_data.steps
    else:
        return ""

    if not steps:
        return ""

    lines = ["---", "**执行步骤**"]
    done_count = 0
    for i, step in enumerate(steps, 1):
        if isinstance(step, dict):
            status = step.get("status", 0)
            content = step.get("content", "")
            qa_reason = step.get("qa_reason", "")
        else:
            status = step.status if hasattr(step, "status") else 0
            status = int(status)
            content = getattr(step, "content", "")
            qa_reason = getattr(step, "qa_reason", "")

        icon = _STEP_ICONS.get(int(status), "⬜")
        suffix = ""
        if int(status) == 4:
            suffix = " ✓"
            done_count += 1
        elif int(status) == 3 and qa_reason:
            suffix = f" — {qa_reason[:40]}"
        elif int(status) == 2:
            done_count += 1

        lines.append(f"{icon} {i}. {content[:60]}{suffix}")

    lines.append(f"\n进度 {done_count}/{len(steps)}")
    return "\n".join(lines)


def build_status_card_json(
    ticket_id: str, title: str, phase: str,
    severity: str, ticket_type: str, phases_status: dict,
    workflow=None,
) -> str:
    """Build Feishu card JSON 2.0 for MAQS pipeline status."""
    color = _SEVERITY_COLORS.get(severity, "blue")
    parts = [
        f"{_STATUS_ICONS.get(phases_status.get(p, 'pending'), '○')} {p}"
        for p in _PHASE_LABELS
    ]
    if phase == "stalled":
        color = "orange"
        parts.append("❌ stalled")
    content = f"**{ticket_id}** · {ticket_type} · {severity}\n\n{'　'.join(parts)}"

    wf_section = _render_workflow_section(workflow)
    if wf_section:
        content += f"\n\n{wf_section}"

    card = {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": f"MAQS: {title}"},
            "template": color,
        },
        "body": {"elements": [{"tag": "markdown", "content": content}]},
    }
    return json.dumps(card, ensure_ascii=False)


async def send_status_card(
    dispatcher, ticket_id: str, title: str, phase: str,
    severity: str, ticket_type: str, phases_status: dict,
    workflow=None,
) -> str | None:
    """Send MAQS status card to delivery chat, return message_id."""
    try:
        card_json = build_status_card_json(
            ticket_id, title, phase, severity, ticket_type, phases_status,
            workflow=workflow,
        )
        return await dispatcher.send_card_raw_to_delivery(card_json)
    except Exception as e:
        log.warning("send_status_card failed: %s", e)
        return None


async def update_status_card(
    dispatcher, message_id: str, ticket_id: str, title: str,
    phase: str, severity: str, ticket_type: str, phases_status: dict,
    workflow=None,
) -> bool:
    """Update an existing MAQS status card in place."""
    try:
        card_json = build_status_card_json(
            ticket_id, title, phase, severity, ticket_type, phases_status,
            workflow=workflow,
        )
        return await dispatcher.update_card_raw(message_id, card_json)
    except Exception as e:
        log.warning("update_status_card failed: %s", e)
        return False
