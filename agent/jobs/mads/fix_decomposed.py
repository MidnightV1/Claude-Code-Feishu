# -*- coding: utf-8 -*-
"""MADS decomposed fix — per-file sequential execution for L3/L4 tickets.

Each sub-task modifies a single target file. Changes accumulate in the same
worktree so sub-task N sees all prior changes. Per-sub-task commits for
rollback granularity.
"""

import logging
import re

from agent.jobs.hardgate import parse_affected_files
from agent.jobs.mads.helpers import git_in, run_agent, write_artifact

log = logging.getLogger("hub.mads.fix_decomposed")

SUB_TASK_TIMEOUT = 600


def _extract_modified_files(patch: str) -> list[str]:
    """Extract file paths from unified diff output."""
    return re.findall(r"^diff --git a/(.*?) b/", patch, re.MULTILINE)


def _build_subtask_prompt(
    diagnosis: str,
    target_file: str,
    subtask_num: int,
    total: int,
    completed_files: list[str],
    remaining_files: list[str],
    affected_files_block: str,
    reject_feedback: str = "",
) -> tuple[str, str]:
    """Build system + user prompts for a single sub-task."""
    context_parts = []

    if completed_files:
        context_parts.append(
            "**Already completed sub-tasks** (do NOT re-modify these files):\n"
            + "\n".join(f"- {f} (done)" for f in completed_files)
        )

    context_parts.append(
        f"**Current sub-task ({subtask_num}/{total})**: "
        f"Modify `{target_file}` as described in the diagnosis."
    )

    if remaining_files:
        context_parts.append(
            "**Remaining sub-tasks** (will be handled separately):\n"
            + "\n".join(f"- {f}" for f in remaining_files)
        )

    subtask_context = "\n\n".join(context_parts)

    system_prompt = (
        f"{affected_files_block}\n\n"
        f"## Sub-task context\n\n{subtask_context}\n\n"
        f"## Diagnosis\n\n{diagnosis}\n\n"
        "You are an implementer fixing a bug. Focus ONLY on the current "
        "sub-task file. Do NOT modify any other files."
    )
    if reject_feedback:
        system_prompt += f"\n\n## Previous rejection feedback\n{reject_feedback}"

    user_prompt = (
        f"Implement the fix for sub-task {subtask_num}/{total}: "
        f"modify `{target_file}` based on the diagnosis."
    )

    return system_prompt, user_prompt


async def fix_decomposed(
    router,
    diagnosis: str,
    ticket_id: str,
    workdir: str,
    branch: str,
    *,
    golden_data: str = "",
    reject_feedback: str = "",
    provider: str = "sonnet",
) -> tuple[str, bool]:
    """Execute per-file decomposed fix in a worktree.

    Returns (combined_report, success). On sub-task failure, stops early
    and returns partial results — caller should escalate to human.
    """
    affected = parse_affected_files(diagnosis)
    if len(affected) < 2:
        log.info("[%s] Only %d affected file(s), delegating to monolithic fix",
                 ticket_id, len(affected))
        return "", False  # signal caller to use monolithic

    affected_block = (
        "<affected_files>\n"
        + "\n".join(f"- {f}" for f in affected)
        + "\n</affected_files>"
    )

    log.info("[%s] Decomposed fix — %d sub-tasks: %s",
             ticket_id, len(affected), affected)

    reports = []
    completed_files: list[str] = []

    for i, target_file in enumerate(affected):
        subtask_num = i + 1
        remaining = affected[i + 1:]

        system_prompt, user_prompt = _build_subtask_prompt(
            diagnosis=diagnosis,
            target_file=target_file,
            subtask_num=subtask_num,
            total=len(affected),
            completed_files=completed_files,
            remaining_files=remaining,
            affected_files_block=affected_block,
            reject_feedback=reject_feedback if i == 0 else "",
        )

        log.info("[%s] Sub-task %d/%d: %s", ticket_id, subtask_num, len(affected), target_file)

        output = await run_agent(
            router=router,
            role=f"fixer-sub{subtask_num}",
            model=provider,
            prompt=user_prompt,
            system_prompt=system_prompt,
            workdir=workdir,
        )

        if output.startswith("[ERROR"):
            log.warning("[%s] Sub-task %d failed: %s", ticket_id, subtask_num, output[:200])
            reports.append(f"## Sub-task {subtask_num}/{len(affected)}: {target_file} — FAILED\n{output}")
            break

        # Verify scope: only target + completed files should be modified
        rc, patch, _ = await git_in(workdir, "diff", f"dev...{branch}", "--name-only")
        if rc == 0 and patch:
            modified = set(patch.splitlines())
            expected = set(completed_files + [target_file])
            dir_prefixes = [f for f in expected if f.endswith("/")]
            unexpected = set()
            for m in modified:
                if m in expected:
                    continue
                if any(m.startswith(d) for d in dir_prefixes):
                    continue
                unexpected.add(m)
            if unexpected:
                log.warning("[%s] Sub-task %d scope violation: %s",
                            ticket_id, subtask_num, sorted(unexpected))
                reports.append(
                    f"## Sub-task {subtask_num}/{len(affected)}: {target_file} — SCOPE VIOLATION\n"
                    f"Unexpected files: {sorted(unexpected)}"
                )
                break

        # Per-sub-task commit
        await git_in(workdir, "add", "-A")
        commit_msg = f"fix(MAQS-{ticket_id}): sub-task {subtask_num}/{len(affected)} — {target_file}"
        rc, _, _ = await git_in(workdir, "commit", "-m", commit_msg, "--allow-empty")
        if rc != 0:
            log.warning("[%s] Sub-task %d commit failed (no changes?)", ticket_id, subtask_num)

        completed_files.append(target_file)
        reports.append(f"## Sub-task {subtask_num}/{len(affected)}: {target_file} — OK\n{output[:500]}")
        log.info("[%s] Sub-task %d/%d done — %s",
                 ticket_id, subtask_num, len(affected), target_file)

    success = len(completed_files) == len(affected)
    combined = "\n\n".join(reports)
    write_artifact(ticket_id, "fix_decomposed.md", combined)

    if success:
        log.info("[%s] All %d sub-tasks completed", ticket_id, len(affected))
    else:
        log.warning("[%s] Decomposed fix incomplete: %d/%d sub-tasks done",
                    ticket_id, len(completed_files), len(affected))

    return combined, success
