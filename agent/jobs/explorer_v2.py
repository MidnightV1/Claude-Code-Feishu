# -*- coding: utf-8 -*-
"""Heartbeat-driven exploration engine (Phase 2).

Fetches [explore] tasks from Feishu, evaluates complexity, picks model,
executes via CLI with timeout management.

Called by HeartbeatMonitor when system is idle (no user messages for 30min,
no active CLI processes, triage=all_clear).

Key design:
- Tasks sourced from Feishu tasklist ([explore] prefix), not internal queue
- Sonnet evaluates complexity → picks Sonnet or Opus
- Single-step timeout: Sonnet 120s / Opus 300s
- Session timeout: 60min hard cap with forced summary
- Results per autonomy matrix: L0 silent, L1 notify, L2 doc
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass

from agent.infra.autonomy import AutonomousAction
from agent.infra.autonomy import notify as autonomy_notify
from agent.infra.models import AutonomyLevel, LLMConfig
from agent.llm.router import LLMRouter
from agent.platforms.feishu.dispatcher import Dispatcher

log = logging.getLogger("hub.explorer_v2")

# Session timeout: 60min hard cap for entire exploration session
SESSION_TIMEOUT = 3600

# Model selection keywords
OPUS_KEYWORDS = re.compile(
    r"设计|架构|方案|评估|分析.*策略|对比.*方案|规划",
    re.IGNORECASE,
)

TASK_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".claude/skills/feishu-task/scripts/task_ctl.py",
)
DOC_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".claude/skills/feishu-doc/scripts/doc_ctl.py",
)
EXPLORE_FOLDER = "Wnwofg48rlhLAbdAxo3cV04xn4f"  # 自动探索 folder


@dataclass
class ExploreTask:
    """Parsed explore task from Feishu."""
    task_id: str
    title: str      # without [explore] prefix
    raw_title: str   # original


async def fetch_explore_tasks(workspace_dir: str) -> list[ExploreTask]:
    """Fetch pending [explore] tasks from Feishu tasklist."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", TASK_SCRIPT, "list",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_dir,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = stdout.decode("utf-8").strip()
    except Exception as e:
        log.warning("Failed to fetch explore tasks: %s", e)
        return []

    tasks = []
    for line in output.splitlines():
        # Format: ⬜ [explore] Title  (task-id)
        if "[explore]" not in line.lower():
            continue
        if line.strip().startswith("✅"):
            continue  # skip completed

        # Extract task ID from parentheses at end
        id_match = re.search(r'\(([0-9a-f-]+)\)\s*$', line)
        if not id_match:
            continue

        task_id = id_match.group(1)
        # Extract title: everything between [explore] and (id)
        title_match = re.search(r'\[explore\]\s*(.+?)\s*\(', line, re.IGNORECASE)
        raw_title = title_match.group(1).strip() if title_match else line
        tasks.append(ExploreTask(
            task_id=task_id,
            title=raw_title,
            raw_title=line.strip(),
        ))

    return tasks


def pick_model(title: str) -> str:
    """Pick model based on task complexity."""
    if OPUS_KEYWORDS.search(title):
        return "opus"
    return "sonnet"


def _load_explore_system() -> str:
    """Build system prompt with Goal Tree context."""
    from agent.infra import goal_tree as gt
    tree = gt.load()
    tree_text = gt.format_for_prompt(tree, max_goals=5)
    mission = tree.get("mission", "")
    return (
        f"你是深度探索 agent，在用户空闲时自主调研系统进化方向。\n\n"
        f"## 使命\n{mission}\n\n"
        f"## 目标树\n{tree_text}\n\n"
        "## 工作方法\n"
        "你是研究员，不是搜索引擎。每个任务要：\n"
        "1. 读代码/文档理解现状（不凭猜测）\n"
        "2. 识别关键矛盾\n"
        "3. 如适用，搜索外部最佳实践\n"
        "4. 给出具体、可操作的结论\n"
        "5. 评估 ROI\n\n"
        "## 输出格式\n"
        "```\n## 结论\n一句话核心发现。\n\n"
        "## 关键发现\n1. [发现] — 证据\n\n"
        "## 推荐行动\n- [行动] — 收益/成本\n\n"
        "## 后续方向\n- [方向]\n```\n\n"
        "## 约束\n"
        "- 你有充足时间，请充分调研\n"
        "- 宁可说\"价值不大，因为...\"也不要给没深度的结论\n"
        "- 发现小 bug（L1 级别）可直接修复，commit 以 [L1-auto] 开头\n"
    )


EXPLORE_SYSTEM = None  # lazy-loaded


async def execute_task(
    task: ExploreTask,
    router: LLMRouter,
    workspace_dir: str,
    session_start: float,
) -> tuple[str, str]:
    """Execute a single explore task.

    Returns (status, summary):
        status: "done" | "timeout" | "error"
        summary: result text or error description
    """
    # Check session timeout
    elapsed = time.time() - session_start
    remaining = SESSION_TIMEOUT - elapsed
    if remaining < 60:
        return "timeout", "Session timeout — less than 60s remaining"

    model = pick_model(task.title)

    log.info("Exploring [%s]: %s (model=%s)",
             task.task_id[:8], task.title, model)

    prompt = (
        f"# 探索任务: {task.title}\n\n"
        f"请深入调研这个主题。你有充足时间，请：\n"
        f"1. 先读代码和文档理解现状\n"
        f"2. 识别关键矛盾\n"
        f"3. 如需要，搜索外部最佳实践\n"
        f"4. 给出具体结论和推荐行动\n"
        f"5. 按系统提示中的结论格式输出"
    )

    # Lazy-load system prompt with Goal Tree context
    sys_prompt = _load_explore_system()

    # Don't set timeout_seconds — let CLI use idle-based timeout (default 900s idle,
    # 3600s hard cap). Explore tasks use tools (read files, search) which produce
    # no stream output during execution; explicit timeout_seconds would be treated
    # as idle timeout and kill the process prematurely.
    llm_config = LLMConfig(
        provider="claude-cli",
        model=model,
        system_prompt=sys_prompt,
    )

    try:
        result = await router.run(
            prompt=prompt,
            llm_config=llm_config,
            session_key=None,  # no session persistence for explore
        )
    except Exception as e:
        log.error("Explore execution error [%s]: %s", task.task_id[:8], e)
        return "error", str(e)[:300]

    if result.is_error:
        log.warning("Explore LLM error [%s]: %s", task.task_id[:8], result.text[:200])
        return "error", result.text[:300]

    summary = result.text[:8000] if result.text else "No output"
    log.info("Explore done [%s]: %d chars, %dms",
             task.task_id[:8], len(result.text), result.duration_ms)

    return "done", summary


async def complete_task(task: ExploreTask, summary: str, workspace_dir: str):
    """Mark Feishu task as complete with summary in comment."""
    try:
        # Update task with summary (truncated for API)
        proc = await asyncio.create_subprocess_exec(
            "python3", TASK_SCRIPT, "complete", task.task_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_dir,
        )
        await asyncio.wait_for(proc.communicate(), timeout=15)
        log.info("Explore task completed: %s", task.task_id[:8])
    except Exception as e:
        log.warning("Failed to complete explore task %s: %s", task.task_id[:8], e)


async def create_followup(title: str, workspace_dir: str):
    """Create a follow-up [explore] task for interrupted work."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", TASK_SCRIPT, "create",
            "--summary", f"[explore] {title}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_dir,
        )
        await asyncio.wait_for(proc.communicate(), timeout=15)
        log.info("Follow-up explore task created: %s", title[:80])
    except Exception as e:
        log.warning("Failed to create follow-up task: %s", e)


async def _create_explore_doc(title: str, content: str, workspace_dir: str) -> str:
    """Create a Feishu doc with exploration results. Returns doc URL or empty string."""
    from datetime import date
    import traceback
    doc_title = f"[探索] {title} — {date.today().isoformat()}"

    for attempt in range(3):
        try:
            # Create doc
            proc = await asyncio.create_subprocess_exec(
                "python3", DOC_SCRIPT, "create", doc_title,
                "--folder", EXPLORE_FOLDER,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace_dir,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = stdout.decode("utf-8").strip()

            # Extract doc_id from output like "Created: <doc_id>"
            doc_id = ""
            doc_url = ""
            for line in output.splitlines():
                if line.strip().startswith("Created:"):
                    doc_id = line.split(":", 1)[1].strip()
                if "URL:" in line:
                    doc_url = line.split("URL:", 1)[1].strip()

            if not doc_id:
                err_detail = stderr.decode("utf-8").strip()
                log.warning("Explore doc create: no doc_id in output (attempt %d/3): %s | stderr: %s",
                            attempt + 1, output[:200], err_detail[:200])
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return ""

            # Append content
            proc = await asyncio.create_subprocess_exec(
                "python3", DOC_SCRIPT, "append", doc_id, content,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace_dir,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
            log.info("Explore doc created: %s → %s", title[:50], doc_id)
            return doc_url or f"https://feishu.cn/docx/{doc_id}"

        except Exception as e:
            log.warning("Explore doc creation failed (attempt %d/3): %s\n%s",
                        attempt + 1, e, traceback.format_exc())
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)

    return ""


def _classify_finding(summary: str, action_taken: str,
                      scores: dict | None = None) -> str:
    """Classify exploration finding type for routing.

    Unified with ExplorerPlugin._classify_finding (explorer.py) —
    finding_type describes WHAT HAPPENED. Notification level is
    computed separately (see notify_level computation in run_exploration).

    Returns: "l1_fix" | "l2_proposal" | "monitoring" | "diagnostic"
    """
    if action_taken:
        return "l1_fix"

    s = summary[:3000].lower()

    # L2 proposal: scoring-based (preferred) or content-based
    from agent.jobs.explorer import _has_l2_content
    if scores and scores.get("actionability", 0) >= 4.0 and _has_l2_content(scores, summary):
        return "l2_proposal"

    # Content-only fallback (when scores unavailable)
    if not scores:
        has_proposals = ("## 推荐行动" in summary or "推荐行动" in summary[:2000]
                         or "## recommended" in s)
        design_signals = any(kw in summary[:2000] for kw in [
            "方案", "设计", "架构", "迁移", "实施路径",
        ])
        if has_proposals and design_signals:
            return "l2_proposal"

    # Monitoring: confirms existing design or measures baseline
    monitoring_signals = ["验证通过", "确认", "baseline", "基线",
                          "working as", "正常运行", "符合预期"]
    if sum(1 for kw in monitoring_signals if kw in s) >= 2:
        return "monitoring"

    return "diagnostic"


async def _detect_commits(workspace_dir: str, since_ref: str) -> str:
    """Detect [L1-auto] commits made since a git ref during exploration.

    Returns space-separated 'hash message' strings, or empty string.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "log", f"{since_ref}..HEAD", "--oneline",
            "--grep=[L1-auto]", "--format=%h %s",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_dir,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        commits = stdout.decode("utf-8").strip()
        return commits
    except Exception as e:
        log.debug("Commit detection failed: %s", e)
        return ""


async def _get_head_ref(workspace_dir: str) -> str:
    """Get current HEAD short hash."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--short", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_dir,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        return stdout.decode("utf-8").strip()
    except Exception:
        return ""


async def run_exploration(
    router: LLMRouter,
    dispatcher: Dispatcher,
    workspace_dir: str,
    notify_open_id: str = "",
) -> str:
    """Main exploration entry point — called by heartbeat when idle.

    Fetches [explore] tasks, executes them, handles results.
    Returns summary string for logging.
    """
    tasks = await fetch_explore_tasks(workspace_dir)
    if not tasks:
        log.debug("No [explore] tasks pending")
        return "no_tasks"

    log.info("Explore session starting: %d tasks available", len(tasks))
    session_start = time.time()
    completed = 0
    errors = 0

    for task in tasks:
        # Check session timeout before each task
        if time.time() - session_start > SESSION_TIMEOUT - 60:
            log.info("Explore session timeout, stopping after %d tasks", completed)
            # Create follow-up for remaining tasks is unnecessary —
            # they're still in Feishu tasklist and will be picked up next cycle
            break

        # Snapshot HEAD before execution to detect commits made by explorer
        pre_ref = await _get_head_ref(workspace_dir)

        status, summary = await execute_task(task, router, workspace_dir, session_start)

        if status == "done":
            await complete_task(task, summary, workspace_dir)
            completed += 1

            # Detect L1-auto commits made during this exploration
            action_taken = ""
            reentry_escalated = False
            if pre_ref:
                action_taken = await _detect_commits(workspace_dir, pre_ref)

            # 72h reentry gate: if committed files were recently L1-auto-modified, escalate to L2
            if action_taken:
                try:
                    from agent.infra.autonomy import check_file_reentry, get_committed_files
                    committed_files = await get_committed_files(action_taken, workspace_dir)
                    reentry_hits = await check_file_reentry(committed_files, hours=72, cwd=workspace_dir)
                    if reentry_hits:
                        reentry_escalated = True
                        log.warning("72h reentry gate: %d file(s) recently auto-modified → L2: %s",
                                    len(reentry_hits),
                                    ", ".join(h["file"] for h in reentry_hits[:3]))
                except Exception as e:
                    log.debug("Reentry check failed: %s", e)

            # Score and log to exploration_log.jsonl (parity with explorer.py)
            # Score BEFORE classify so scores inform L2 classification
            task_duration = int(time.time() - session_start)  # approx
            auto_scores = {}
            try:
                from agent.infra.exploration_scoring import score_exploration
                from agent.infra.exploration import ExplorationLog, append_log
                auto_scores = await score_exploration(
                    title=task.title,
                    summary=summary,
                    duration_seconds=task_duration,
                    messages_used=max(1, len(summary) // 4000),
                    router=router,
                    use_llm=True,
                )
            except Exception as e:
                log.warning("Explore scoring failed [%s]: %s",
                            task.task_id[:8], e)

            # Classify with scores (unified with explorer.py logic)
            finding_type = _classify_finding(summary, action_taken, auto_scores)

            try:
                from agent.infra.exploration import ExplorationLog, append_log
                await append_log(ExplorationLog(
                    task_id=task.task_id,
                    title=task.title,
                    pillar="explore",
                    source="feishu_task",
                    priority="P1",
                    messages_used=max(1, len(summary) // 4000),
                    summary=summary,
                    action_taken=action_taken,
                    finding_type=finding_type,
                    autonomy_level=(2 if finding_type == "l2_proposal" else 1),
                    duration_seconds=task_duration,
                    auto_scores=auto_scores,
                ))
                log.info("Explore scored [%s]: tier=%s w=%.2f type=%s",
                         task.task_id[:8],
                         auto_scores.get("tier", "?"),
                         auto_scores.get("weighted", 0),
                         finding_type)
            except Exception as e:
                log.warning("Explore scoring/logging failed [%s]: %s",
                            task.task_id[:8], e)

            # Notify level — DECOUPLED from finding_type.
            # An l1_fix can also contain L2-worthy proposals (dual-label).
            from agent.jobs.explorer import _has_l2_content
            if reentry_escalated:
                notify_level = AutonomyLevel.L2_APPROVE
                log.info("72h reentry gate escalated %s to L2", finding_type)
            elif finding_type == "l2_proposal":
                notify_level = AutonomyLevel.L2_APPROVE
            elif _has_l2_content(auto_scores, summary):
                notify_level = AutonomyLevel.L2_APPROVE
                log.info("L2 content detected in %s finding — elevating notify level",
                         finding_type)
            else:
                notify_level = AutonomyLevel.L1_NOTIFY
            if notify_open_id:
                # Write full content to Feishu doc, send concise card
                doc_url = await _create_explore_doc(
                    task.title, summary, workspace_dir
                )
                # Card: key conclusions (first 1500 chars) + doc link
                card_summary = summary[:1500]
                if len(summary) > 1500:
                    card_summary += "\n\n..."
                if doc_url:
                    card_summary += f"\n\n[完整报告]({doc_url})"
                try:
                    from agent.jobs.explorer import ExplorerPlugin
                    card_json = ExplorerPlugin._build_explore_card(
                        task.title, card_summary, task.task_id
                    )
                    # Extract first commit sha from action_taken (format: "hash msg\nhash msg")
                    _first_sha = action_taken.split()[0] if action_taken else ""
                    action = AutonomousAction(
                        level=notify_level,
                        category="exploration:explore",
                        summary=task.title,
                        detail=card_summary,
                        commit_sha=_first_sha,
                        rollback_cmd=f"git revert {_first_sha}" if _first_sha else "",
                        source="explorer_v2",
                    )
                    await autonomy_notify(
                        dispatcher, action,
                        open_id=notify_open_id, card_json=card_json,
                    )
                except Exception as e:
                    log.warning("Explore notification failed: %s", e)

        elif status == "timeout":
            log.info("Explore task timeout: %s", task.title)
            # Session timeout — remaining tasks stay in Feishu for next cycle
            break
        else:
            errors += 1

    elapsed = int(time.time() - session_start)
    result = f"Explore session: {completed} done, {errors} errors, {elapsed}s elapsed"
    log.info(result)
    return result
