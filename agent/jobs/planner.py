# -*- coding: utf-8 -*-
"""Daily Planner — strategic exploration task generation.

Replaces seed_exploration.py's role. Runs daily (Opus), generates
exploration tasks aligned with Goal Tree objectives.

Input signals:
- Goal Tree: open questions and progress gaps
- Recent exploration log: what's been done, avoid duplication
- Recent conversations: implicit priority signals from user
- External signals: arxiv papers, twitter trends (via existing pipelines)
- Error Tracker: unfixed bugs
- System health: recent errors, performance issues

Output:
- Prioritized exploration tasks added to the queue
- Goal Tree progress updates (if new info discovered)

Registered as cron handler 'planner'. Recommended schedule: daily 02:30.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from agent.infra.models import LLMConfig
from agent.infra.exploration import (
    ExplorationQueue, ExplorationTask, Priority, read_log,
)
from agent.infra import goal_tree as gt
from agent.llm.router import LLMRouter

log = logging.getLogger("hub.planner")

# Planner uses Opus for strategic thinking
PLANNER_CONFIG = LLMConfig(
    provider="claude-cli",
    model="opus",
)

PLANNER_SYSTEM = """\
你是探索规划专家。你的工作是每天为自主探索系统制定当日的探索计划。

## 系统使命
{mission}

## 目标树 (OKR)
{goal_tree}

## 近期已完成的探索（避免重复）
{recent_explorations}

## 当前探索队列（待执行）
{pending_tasks}

## 外部信号
{external_signals}

## 你的任务

分析以上所有输入，制定今天的探索计划。要求：

1. **目标对齐**：每个任务必须关联到一个具体的 Goal Tree 目标和子问题
2. **去重**：不要重复已完成的探索，在已有发现基础上深入
3. **优先级**：P0 目标的任务优先于 P1，P1 优先于 P2
4. **可探索性**：每个任务必须是可以通过代码分析/文档阅读/外部搜索回答的具体问题
5. **多样性**：不要所有任务都集中在同一个目标上
6. **深度 > 广度**：宁可深入调研 2-3 个问题，不要浮皮潦草覆盖 10 个
7. **新颖性**：优先那些能产出新洞察的方向，不要重复确认已知事实

输出 JSON 数组，每项：
```json
[{{
  "title": "具体的探索问题（问句形式）",
  "description": "为什么这个问题值得探索，预期能发现什么",
  "goal_id": "G1",
  "priority": 1,
  "pillar": "internalize",
  "source": "planner",
  "estimated_time_minutes": 30
}}]
```

pillar 选项：
- "collect": 信息采集和外部调研
- "internalize": 内部代码/系统分析
- "feedback": 用户反馈和体验分析

限制：最多 5 个任务。只输出 JSON，不要其他内容。
"""


class PlannerPlugin:
    """Daily exploration planner — strategic task generation."""

    def __init__(self, router: LLMRouter, queue: ExplorationQueue):
        self.router = router
        self.queue = queue

    def descriptor(self) -> dict:
        return {
            "handlers": [
                {"name": "planner", "fn": self.run},
            ],
        }

    async def run(self) -> str:
        """Generate daily exploration plan."""
        await self.queue.load()

        # Gather all input signals
        tree = gt.load()
        recent = await read_log(hours=72)
        pending = self.queue.list_pending()
        external = await self._gather_external_signals()

        # Build prompt
        prompt = self._build_prompt(tree, recent, pending, external)
        system = self._build_system(tree, recent, pending, external)

        log.info("Planner starting: %d goals, %d recent logs, %d pending, %d signals",
                 len(tree.get("goals", [])), len(recent), len(pending), len(external))

        # Run Opus
        config = LLMConfig(
            provider="claude-cli",
            model="opus",
            system_prompt=system,
        )
        result = await self.router.run(prompt=prompt, llm_config=config)

        if result.is_error:
            log.error("Planner failed: %s", result.text[:200])
            return f"Planner error: {result.text[:200]}"

        # Parse and create tasks
        tasks = self._parse_tasks(result.text)
        created = 0
        for task_data in tasks:
            task = ExplorationTask(
                title=task_data["title"],
                description=task_data.get("description", ""),
                source="planner",
                source_context=f"goal: {task_data.get('goal_id', '?')}",
                priority=task_data.get("priority", Priority.P1_HIGH),
                autonomy_level=1,  # L1: notify
                pillar=task_data.get("pillar", "internalize"),
                estimated_messages=max(5, task_data.get("estimated_time_minutes", 30) // 2),
            )
            await self.queue.add(task)
            created += 1
            log.info("Planner created: [P%d] %s", task.priority, task.title)

        summary = f"Planner: {created} tasks created from {len(tasks)} proposals"
        log.info(summary)
        return summary

    def _build_system(self, tree, recent, pending, external) -> str:
        tree_text = gt.format_for_prompt(tree)
        mission = tree.get("mission", "")

        recent_text = self._format_recent(recent)
        pending_text = self._format_pending(pending)
        external_text = self._format_external(external)

        return PLANNER_SYSTEM.format(
            mission=mission,
            goal_tree=tree_text,
            recent_explorations=recent_text,
            pending_tasks=pending_text,
            external_signals=external_text,
        )

    @staticmethod
    def _build_prompt(tree, recent, pending, external) -> str:
        return (
            "请分析所有输入信号，制定今天的探索计划。\n"
            "重点关注 P0 目标中尚未回答的问题。\n"
            "确保每个任务都是具体的、可调研的问题，而非模糊的改进方向。"
        )

    @staticmethod
    def _format_recent(logs: list) -> str:
        if not logs:
            return "(无近期探索记录)"
        lines = []
        for entry in logs[:15]:
            lines.append(
                f"- [{entry.get('priority', '?')}] {entry.get('title', '?')}: "
                f"{entry.get('summary', '')[:150]}"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_pending(tasks: list) -> str:
        if not tasks:
            return "(队列为空)"
        lines = []
        for t in tasks[:10]:
            lines.append(f"- [P{t.priority}] {t.title}")
        return "\n".join(lines)

    @staticmethod
    def _format_external(signals: list) -> str:
        if not signals:
            return "(无外部信号)"
        lines = []
        for s in signals[:10]:
            lines.append(f"- [{s.get('source', '?')}] {s.get('title', '?')}")
        return "\n".join(lines)

    async def _gather_external_signals(self) -> list:
        """Gather external signals from existing pipelines."""
        signals = []

        # 1. Error Tracker — unfixed bugs
        try:
            proj = str(Path(__file__).resolve().parent.parent.parent)
            bitable_script = f"{proj}/.claude/skills/feishu-bitable/scripts/bitable_ctl.py"
            proc = await asyncio.create_subprocess_exec(
                "python3", bitable_script, "records",
                "A4bLb6NXKaW5rds9J7aczRson9d",  # Error Tracker app
                "tblA4bLb6NXKaW5",               # table (placeholder, actual ID may differ)
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=proj,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                try:
                    records = json.loads(stdout.decode())
                    for r in records if isinstance(records, list) else []:
                        status = r.get("fields", {}).get("Status", "")
                        if status != "Fixed":
                            signals.append({
                                "source": "error_tracker",
                                "title": r.get("fields", {}).get("Title", "Unknown bug"),
                            })
                except (json.JSONDecodeError, KeyError):
                    pass
        except Exception as e:
            log.debug("Error tracker signal fetch failed: %s", e)

        # 2. Recent arxiv papers (from log)
        try:
            arxiv_log = Path(proj) / "data" / "arxiv_tracker.json"
            if arxiv_log.exists():
                with open(arxiv_log) as f:
                    arxiv_data = json.load(f)
                for paper in arxiv_data.get("recent_papers", [])[:3]:
                    signals.append({
                        "source": "arxiv",
                        "title": paper.get("title", ""),
                    })
        except Exception as e:
            log.debug("Arxiv signal fetch failed: %s", e)

        return signals

    @staticmethod
    def _parse_tasks(text: str) -> list:
        """Parse JSON task list from LLM output."""
        import re
        text = text.strip()

        # Extract JSON from code block if present
        if "```" in text:
            match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
            if match:
                text = match.group(1).strip()

        # Try to extract JSON array if text contains extra content
        if not text.startswith("["):
            match = re.search(r'\[.*\]', text, re.DOTALL)
            if match:
                text = match.group(0)

        try:
            tasks = json.loads(text)
            if isinstance(tasks, list):
                return tasks
        except json.JSONDecodeError:
            log.warning("Planner output not valid JSON: %s", text[:200])

        return []
