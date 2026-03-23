# -*- coding: utf-8 -*-
"""Autonomous explorer v3 — deep, time-boxed, goal-driven exploration.

Replaces the original single-turn explorer. Key differences:
- Multi-step investigation (CC CLI multi-turn within one session)
- Time-boxed (default 1h per task), not message-limited
- Goal Tree driven: tasks aligned with OKR, findings flow back
- Exploration map: tracks what's been explored, prevents duplication
- Cross-model: Opus for thinking, Gemini for external search
- Structured conclusions: even "inconclusive" is a valid result

Flow:
1. Load Goal Tree + exploration map
2. Select highest-priority pending tasks
3. For each task: deep investigation with tool access
4. Log structured results, update Goal Tree progress
5. Notify per autonomy level

Registered as cron handler 'explorer'. Runs at 03:00 CST.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from agent.infra.models import LLMConfig, AutonomyLevel
from agent.infra.exploration import (
    ExplorationQueue, ExplorationTask, ExplorationLog,
    Priority, append_log, read_log,
)
from agent.infra import goal_tree as gt
from agent.infra.autonomy import AutonomousAction, notify as autonomy_notify
from agent.llm.router import LLMRouter
from agent.platforms.feishu.dispatcher import Dispatcher

log = logging.getLogger("hub.explorer")

# ── Configuration ──

# Time budget per task (seconds). User decision: "一小时之内尽可能出结论"
TASK_TIME_BUDGET = 3600

# Session time budget (all tasks combined)
SESSION_TIME_BUDGET = 3 * 3600  # 3 hours max session

# Max tasks per session
MAX_TASKS_PER_SESSION = 5

# Model selection
DEEP_MODELS = {
    "think": "opus",       # analysis, architecture, design
    "search": "sonnet",    # code search, file reads, fact gathering
    "default": "opus",     # default for exploration
}

# ── System Prompt ──

EXPLORE_SYSTEM = """\
你是一个深度探索 agent，在用户休息时自主调研系统进化方向。

## 你的使命
{mission}

## 当前目标树 (OKR)
{goal_tree}

## 已有探索记录（避免重复）
{exploration_map}

## 工作方法

你不是搜索引擎——你是研究员。每个探索任务都要经过：

1. **现状摸底**（必须）：读代码、读文档、读 git log，理解真实状态。不凭猜测。
2. **矛盾识别**：找到关键矛盾——解决它能带动全局的那个问题。
3. **外部参考**（如适用）：用工具搜索最佳实践、类似项目的做法。
4. **方案设计**：给出具体、可操作的结论。不要模糊的"建议优化"。
5. **ROI 评估**：实施成本 vs 预期收益，值不值得做。

## 输出要求

探索结束时，输出以下结构化结论：

```
## 结论
一句话核心发现。

## 关键发现
1. [发现1] — 证据/数据支撑
2. [发现2] — ...

## 推荐行动
- [行动1] — 预期收益 / 实施成本
- [行动2] — ...

## 目标树更新
- goal_id: [对应的目标ID]
- question_answered: [回答了哪个子问题]
- new_findings: [新发现的摘要]
- new_questions: [探索中发现的新问题]

## 后续方向
- [值得继续探索的方向1]
- [值得继续探索的方向2]

## Memory建议
如果探索发现了值得记住的内容（新的外部引用、工具URL、技术事实、用户行为模式等），
在这个章节列出。系统会自动提取并写入Memory。
- 事实类（API端点、版本号、URL）会即时写入
- 模式类（用户偏好、系统行为模式）会标记为待确认，积累多个信号后再正式写入
```

## 约束
- 你有充足的时间（最多1小时），请充分调研，不要急于下结论
- 宁可说"调研后发现这个方向价值不大，原因是..."也不要给出没有深度的结论
- 如果发现了小 bug 或明显可改进的代码（L1 级别），可以直接修复并 commit
- 修复时 commit message 必须以 [L1-auto] 开头
"""


class ExplorerPlugin:
    """Deep exploration plugin — cron handler + goal-driven investigation."""

    def __init__(self, router: LLMRouter, dispatcher: Dispatcher,
                 *, budget: int = 50, open_id: str = "",
                 card_dispatcher: Dispatcher | None = None):
        self.router = router
        self.dispatcher = dispatcher
        # Interactive cards (with buttons) must go through the bot that has
        # WebSocket card_action callbacks registered. The notifier bot
        # doesn't listen for card callbacks, causing Feishu error 200340.
        self.card_dispatcher = card_dispatcher or dispatcher
        self.budget = budget  # kept for backward compat, no longer primary constraint
        self.open_id = open_id
        self.queue = ExplorationQueue()
        self.workspace = str(Path(__file__).resolve().parents[2])

    def descriptor(self) -> dict:
        """Plugin descriptor for register_plugin()."""
        return {
            "handlers": [
                {"name": "explorer", "fn": self.run},
            ],
        }

    async def run(self) -> str:
        """Main exploration loop — called by cron scheduler."""
        await self.queue.load()

        pending = self.queue.list_pending()
        if not pending:
            log.info("Exploration queue empty, nothing to do")
            return "Queue empty"

        # Load Goal Tree for context
        tree = gt.load()

        # Build exploration map from recent logs (avoid duplication)
        recent_logs = await read_log(hours=72)  # last 3 days
        exploration_map = self._build_exploration_map(recent_logs)

        # Select tasks (priority-ordered, capped)
        selected = pending[:MAX_TASKS_PER_SESSION]
        log.info("Explorer starting: %d tasks selected (session budget=%ds)",
                 len(selected), SESSION_TIME_BUDGET)

        session_start = time.time()
        results = []

        for task in selected:
            # Check session time budget
            elapsed = time.time() - session_start
            if elapsed > SESSION_TIME_BUDGET - 300:  # 5min safety margin
                log.info("Session time budget reached after %d tasks", len(results))
                break

            result = await self._execute_task(task, tree, exploration_map)
            results.append(result)

            if result:
                # Update exploration map for subsequent tasks
                exploration_map.append({
                    "title": task.title,
                    "summary": result[:200],
                    "timestamp": time.time(),
                })

        # Summary
        done = sum(1 for r in results if r)
        elapsed = int(time.time() - session_start)
        summary = f"Explorer completed: {done}/{len(selected)} tasks in {elapsed}s"
        log.info(summary)
        return summary

    async def _execute_task(self, task: ExplorationTask,
                            tree: dict, exploration_map: list) -> str | None:
        """Execute a single deep exploration task."""
        log.info("Exploring: [P%d] %s", task.priority, task.title)

        # Mark in_progress
        await self.queue.update(task.id, status="in_progress")
        task_start = time.time()

        try:
            # Build rich prompt with Goal Tree context
            prompt = self._build_prompt(task, tree, exploration_map)

            # Pick model based on task nature
            model = self._pick_model(task)

            # No explicit timeout_seconds — let CC CLI use idle-based timeout.
            # CC will use tools (file reads, grep, web search) which produce
            # no stream output during execution. Explicit timeout would kill
            # the process prematurely during tool calls.
            llm_config = LLMConfig(
                provider="claude-cli",
                model=model,
                system_prompt=self._build_system_prompt(tree, exploration_map),
            )

            result = await self.router.run(
                prompt=prompt,
                llm_config=llm_config,
                session_key=None,
            )

            duration = int(time.time() - task_start)

            if result.is_error:
                log.warning("Exploration failed (%ds): %s — %s",
                            duration, task.title, result.text[:200])
                await self.queue.update(
                    task.id, status="pending",
                    result_summary=f"Error after {duration}s: {result.text[:200]}"
                )
                return None

            # Extract structured conclusion if present
            summary = result.text[:8000] if result.text else "No output"

            # Try to update Goal Tree from structured output
            self._update_goal_tree(tree, result.text)

            # Mark complete
            await self.queue.complete(task.id, summary)

            # Log to exploration log
            await append_log(ExplorationLog(
                task_id=task.id,
                title=task.title,
                pillar=task.pillar,
                source=task.source,
                priority=task.priority,
                messages_used=max(1, result.input_tokens // 4000),
                summary=summary,
                action_taken="",
                autonomy_level=task.autonomy_level,
            ))

            log.info("Exploration done: %s (%ds, %d tokens)",
                     task.title, duration, result.input_tokens + result.output_tokens)

            # P1-3: Try to extract memory-worthy facts from exploration output
            await self._maybe_update_memory(task, summary)

            # Notify based on autonomy level — write full content to doc
            if task.autonomy_level >= AutonomyLevel.L1_NOTIFY:
                from agent.jobs.explorer_v2 import _create_explore_doc
                doc_url = await _create_explore_doc(
                    task.title, summary, self.workspace
                )
                detail = summary[:1500]
                if doc_url:
                    detail += f"\n\n[完整报告]({doc_url})"
                action = AutonomousAction(
                    level=task.autonomy_level,
                    category=f"exploration:{task.pillar}",
                    summary=task.title,
                    detail=detail,
                )
                # Build interactive card with feedback buttons
                # Use card_dispatcher (main bot) for interactive cards —
                # only it has WebSocket card action callback registered
                card_json = self._build_explore_card(
                    task.title, detail, task.id
                )
                await autonomy_notify(
                    self.card_dispatcher, action,
                    open_id=self.open_id, card_json=card_json,
                )

            return summary

        except Exception as e:
            log.error("Exploration exception: %s — %s", task.title, e)
            await self.queue.update(
                task.id, status="pending",
                result_summary=f"Exception: {e}"
            )
            return None

    @staticmethod
    def _build_explore_card(title: str, detail: str, task_id: str) -> str:
        """Build an interactive card with feedback buttons for exploration results."""
        from agent.platforms.feishu.dispatcher import Dispatcher

        elements = [
            {"tag": "markdown", "content": detail},
            {"tag": "hr"},
        ]

        # Feedback buttons
        btn_group = Dispatcher.build_button_group([
            {
                "text": "👍 有价值",
                "type": "primary",
                "value": {
                    "action": "explore_feedback",
                    "action_id": f"explore_{task_id}_up",
                    "choice": "up",
                    "task_id": task_id,
                    "title": title,
                },
            },
            {
                "text": "👎 没帮助",
                "type": "default",
                "value": {
                    "action": "explore_feedback",
                    "action_id": f"explore_{task_id}_down",
                    "choice": "down",
                    "task_id": task_id,
                    "title": title,
                },
            },
        ])

        if isinstance(btn_group, list):
            elements.extend(btn_group)
        else:
            elements.append(btn_group)

        return Dispatcher.build_interactive_card(
            elements,
            header=f"[探索] {title}",
            color="turquoise",
        )

    async def _maybe_update_memory(self, task, summary: str) -> None:
        """Extract memory-worthy content from exploration output.

        Three-layer rhythm:
        - Facts (references, URLs, data points) → write immediately
        - Patterns (user preferences, recurring themes) → mark as pending
        """
        import re
        import os

        # Auto-discover the memory directory from .claude/projects/
        projects_dir = os.path.expanduser("~/.claude/projects")
        memory_dir = None
        if os.path.isdir(projects_dir):
            for d in os.listdir(projects_dir):
                candidate = os.path.join(projects_dir, d, "memory")
                if os.path.isdir(candidate):
                    memory_dir = candidate
                    break
        if not memory_dir:
            return

        # Look for structured memory suggestions in the output
        # The exploration prompt can include instructions to output these
        memory_match = re.search(
            r'## (?:Memory建议|Memory Update|记忆更新)\s*\n(.*?)(?=\n## |\Z)',
            summary, re.DOTALL
        )
        if not memory_match:
            return

        content = memory_match.group(1).strip()
        if not content or len(content) < 30:
            return

        # Classify: facts (references, data) vs patterns (insights, preferences)
        is_reference = any(kw in content.lower() for kw in [
            "url", "http", "api", "版本", "发布", "更新日期",
        ])

        if is_reference:
            # Fact layer: write immediately
            slug = re.sub(r'[^\w]', '_', task.title[:30]).strip('_').lower()
            filename = f"reference_explore_{slug}.md"
            filepath = os.path.join(memory_dir, filename)
            mem_content = (
                f"---\n"
                f"name: {task.title}\n"
                f"description: 探索发现 — {task.title[:60]}\n"
                f"type: reference\n"
                f"---\n\n"
                f"{content}\n"
            )
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(mem_content)
            log.info("Memory written (fact): %s", filename)
            self._update_memory_index(
                memory_dir, filename,
                f"探索发现 — {task.title[:60]}",
                "References",
            )
        else:
            # Pattern layer: write with [PENDING] tag, needs accumulation
            slug = re.sub(r'[^\w]', '_', task.title[:30]).strip('_').lower()
            filename = f"pending_explore_{slug}.md"
            filepath = os.path.join(memory_dir, filename)
            mem_content = (
                f"---\n"
                f"name: '[PENDING] {task.title}'\n"
                f"description: 待确认模式 — 需积累更多信号\n"
                f"type: project\n"
                f"---\n\n"
                f"**状态**: 待确认（单次探索发现，需要更多信号验证）\n\n"
                f"{content}\n"
            )
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(mem_content)
            log.info("Memory written (pending pattern): %s", filename)
            self._update_memory_index(
                memory_dir, filename,
                f"[PENDING] {task.title[:60]}",
                "Project",
            )

    @staticmethod
    def _update_memory_index(
        memory_dir: str, filename: str, description: str, section: str
    ) -> None:
        """Append a new memory file entry to MEMORY.md index.

        Prevents the 'dark knowledge' problem where memory files exist
        but aren't indexed, making them invisible to future conversations.
        """
        import os

        index_path = os.path.join(memory_dir, "MEMORY.md")
        if not os.path.isfile(index_path):
            return

        with open(index_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Skip if already indexed
        if filename in content:
            return

        entry = f"- [{description}]({filename})"

        # Find the section header and append after it
        section_header = f"## {section}"
        if section_header in content:
            # Insert after the last entry in that section (before next ## or EOF)
            import re
            pattern = rf'(## {re.escape(section)}\n(?:.*\n)*?)((?=\n## )|\Z)'
            match = re.search(pattern, content)
            if match:
                insert_pos = match.end(1)
                content = content[:insert_pos] + entry + "\n" + content[insert_pos:]
        else:
            # Section doesn't exist — append at end
            content = content.rstrip() + f"\n\n## {section}\n\n{entry}\n"

        with open(index_path, "w", encoding="utf-8") as f:
            f.write(content)
        log.info("MEMORY.md index updated: %s → %s", filename, section)

    def _build_system_prompt(self, tree: dict, exploration_map: list) -> str:
        """Build the system prompt with Goal Tree and exploration map."""
        tree_text = gt.format_for_prompt(tree)
        mission = tree.get("mission", "")

        # Format exploration map as concise summary
        map_lines = []
        for entry in exploration_map[-20:]:  # last 20 entries
            map_lines.append(f"- {entry.get('title', '?')}: {entry.get('summary', '')[:100]}")
        map_text = "\n".join(map_lines) if map_lines else "(无近期探索记录)"

        return EXPLORE_SYSTEM.format(
            mission=mission,
            goal_tree=tree_text,
            exploration_map=map_text,
        )

    def _build_prompt(self, task: ExplorationTask,
                      tree: dict, exploration_map: list) -> str:
        """Build the exploration prompt for a task."""
        parts = [f"# 探索任务: {task.title}"]

        if task.description:
            parts.append(f"\n{task.description}")

        if task.source_context:
            parts.append(f"\n## 背景\n{task.source_context}")

        # Find related goal and open questions
        related_goal = None
        for goal in tree.get("goals", []):
            for sq in goal.get("sub_questions", []):
                if sq.get("status") == "open" and any(
                    kw in task.title for kw in sq["q"].split("？")[0].split("、")[-3:]
                ):
                    related_goal = goal
                    break

        if related_goal:
            parts.append(f"\n## 关联目标: [{related_goal.get('priority', 'P2')}] {related_goal['title']}")
            parts.append(f"*Why*: {related_goal.get('why', '')}")
            open_qs = [sq for sq in related_goal.get("sub_questions", []) if sq["status"] == "open"]
            if open_qs:
                parts.append("**相关开放问题**:")
                for sq in open_qs[:3]:
                    parts.append(f"- {sq['q']}")

        parts.append(
            "\n## 要求\n"
            "深入调研这个主题。你有充足时间，请：\n"
            "1. 先读代码和文档理解现状\n"
            "2. 识别关键矛盾\n"
            "3. 如需要，搜索外部最佳实践\n"
            "4. 给出具体结论和推荐行动\n"
            "5. 按系统提示中的结论格式输出"
        )

        return "\n".join(parts)

    @staticmethod
    def _pick_model(task: ExplorationTask) -> str:
        """Pick model based on task priority and nature."""
        # P0/P1 tasks always get Opus
        if task.priority <= Priority.P1_HIGH:
            return "opus"
        # Architecture/design keywords get Opus
        import re
        if re.search(r"设计|架构|方案|评估|分析|策略|规划|进化", task.title):
            return "opus"
        return "sonnet"

    @staticmethod
    def _build_exploration_map(logs: list[dict]) -> list[dict]:
        """Build exploration map from recent logs."""
        return [
            {
                "title": entry.get("title", ""),
                "summary": entry.get("summary", "")[:200],
                "timestamp": entry.get("timestamp", 0),
            }
            for entry in logs
        ]

    @staticmethod
    def _update_goal_tree(tree: dict, result_text: str):
        """Try to extract goal tree updates from structured exploration output."""
        if not result_text or "目标树更新" not in result_text:
            return

        try:
            # Extract goal_id and findings from output
            import re
            goal_match = re.search(r'goal_id:\s*(G\d+)', result_text)
            findings_match = re.search(
                r'new_findings:\s*(.+?)(?:\n##|\n-\s*new_questions|\Z)',
                result_text, re.DOTALL
            )
            question_match = re.search(
                r'question_answered:\s*(.+?)(?:\n|\Z)', result_text
            )

            if goal_match and findings_match:
                goal_id = goal_match.group(1)
                findings = findings_match.group(1).strip()

                # Update progress
                gt.update_progress(tree, goal_id, findings[:200])

                # If a specific question was answered, update it
                if question_match:
                    question = question_match.group(1).strip()
                    # Try to find and update the matching question
                    for goal in tree.get("goals", []):
                        if goal.get("id") != goal_id:
                            continue
                        for sq in goal.get("sub_questions", []):
                            if question in sq["q"] or sq["q"] in question:
                                sq["findings"] = findings[:300]
                                break

                gt.save(tree)
                log.info("Goal tree updated for %s", goal_id)

        except Exception as e:
            log.warning("Failed to update goal tree from exploration: %s", e)
