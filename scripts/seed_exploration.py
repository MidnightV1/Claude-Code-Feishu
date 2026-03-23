#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Seed exploration queue from external sources.

Sources:
  - Feishu tasks: unfinished tasks become exploration items
  - Error Tracker: new bugs from the bitable

Usage:
  python3 scripts/seed_exploration.py --source tasks
  python3 scripts/seed_exploration.py --source errors
  python3 scripts/seed_exploration.py --source all
  python3 scripts/seed_exploration.py --list          # show current queue
  python3 scripts/seed_exploration.py --add "title" --priority P1 --pillar collect
"""

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.infra.exploration import (
    ExplorationQueue, ExplorationTask, Priority, TaskSource,
)

# Tasks matching these patterns are personal reminders, not explorable work items.
# Each pattern is checked case-insensitively against the task title.
SKIP_PATTERNS = [
    "提醒", "牙医", "看病", "挂号", "体检", "预约",  # personal reminders
    "买", "取快递", "取件", "寄",                      # errands
    "生日", "纪念日", "约饭", "聚餐",                   # social
    "签证", "机票", "酒店", "行李",                     # travel logistics
    "缴费", "还款", "转账",                             # finance
]

# Tasks must contain at least one of these keywords to be considered technical/explorable.
TECH_INDICATORS = [
    "skill", "bug", "fix", "修复", "feature", "优化", "重构", "设计", "开发",
    "架构", "pipeline", "cron", "API", "模型", "数据", "同步", "抓取",
    "认证", "CLI", "插件", "hook", "部署", "迁移", "Phase", "P1", "P2",
    "briefing", "日报", "tracker", "explorer", "hub", "agent", "飞书",
    "memory", "知识库", "自主", "进化", "搜索", "健康数据", "源",
]

# Tasks with these keywords are "just do it" items, not worth exploring.
# They have known solutions — exploration would just produce a summary of what to do.
EXECUTE_NOT_EXPLORE = [
    "修复", "fix", "bug",               # known issue, just fix it
    "部署", "deploy", "迁移",            # execution, not research
    "配置", "config", "设置",            # configuration work
    "登录", "认证", "login",             # credential setup
    "安装", "install",                   # installation
    "清理", "cleanup", "删除",           # maintenance
    "更新", "update", "升级", "upgrade", # routine updates
]

# Tasks with these keywords are genuinely worth exploring (need research/analysis).
EXPLORE_INDICATORS = [
    "设计", "design", "架构", "architecture",   # design decisions
    "评估", "evaluate", "对比", "compare",      # comparative analysis
    "调研", "research", "分析", "analysis",     # research
    "方案", "proposal", "策略", "strategy",     # strategy
    "框架", "framework", "模型", "model",       # architectural choices
    "自主", "进化", "自动", "智能",              # evolution/autonomy
]


def _is_explorable(title: str) -> bool:
    """Return True if the task needs research/exploration, not just execution."""
    t = title.lower()
    # Hard skip patterns (personal reminders)
    for pat in SKIP_PATTERNS:
        if pat in t:
            return False
    # Skip execution tasks — these have known solutions, no exploration needed
    has_execute_kw = any(kw.lower() in t for kw in EXECUTE_NOT_EXPLORE)
    has_explore_kw = any(kw.lower() in t for kw in EXPLORE_INDICATORS)
    # If it has explore keywords, it's explorable even if it also has execute keywords
    if has_explore_kw:
        return True
    # If it only has execute keywords, skip — just do it, don't explore
    if has_execute_kw:
        return False
    # Must have at least one tech indicator
    for kw in TECH_INDICATORS:
        if kw.lower() in t:
            return True
    # Fallback: if no indicator matched, skip — better to miss than pollute
    return False


def run_cmd(cmd: list[str], timeout: int = 30) -> str:
    """Run a command and return stdout."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(PROJECT_ROOT),
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"Command failed: {e}", file=sys.stderr)
        return ""


async def seed_from_tasks(queue: ExplorationQueue) -> int:
    """Seed queue from unfinished feishu tasks."""
    output = run_cmd([
        sys.executable,
        str(PROJECT_ROOT / ".claude/skills/feishu-task/scripts/task_ctl.py"),
        "list",
    ])
    if not output:
        print("No tasks found or task_ctl failed")
        return 0

    added = 0
    existing_titles = {t.title for t in queue.list_all()}

    for line in output.strip().split("\n"):
        # Parse task_ctl output: [status] title (due: date)
        line = line.strip()
        if not line or "[done]" in line.lower():
            continue

        # Extract title (between ] and (due: or end of line)
        title = line
        if "]" in title:
            title = title.split("]", 1)[1].strip()
        if "(due:" in title:
            title = title.split("(due:")[0].strip()
        if "(" in title:
            title = title.split("(")[0].strip()

        if not title or title in existing_titles:
            continue

        # Filter: only technical/project tasks, skip personal reminders
        if not _is_explorable(title):
            print(f"  ~ skipped (personal): {title}")
            continue

        task = ExplorationTask(
            title=title,
            source=TaskSource.FEISHU_TASK,
            source_context=line,
            priority=Priority.P1_HIGH,
            autonomy_level=1,  # L1: execute then notify
            pillar="internalize",
            estimated_messages=15,
        )
        await queue.add(task)
        existing_titles.add(title)
        added += 1
        print(f"  + [P1] {title} (from feishu task)")

    return added


async def seed_from_errors(queue: ExplorationQueue) -> int:
    """Seed queue from Error Tracker bitable."""
    output = run_cmd([
        sys.executable,
        str(PROJECT_ROOT / ".claude/skills/feishu-bitable/scripts/bitable_ctl.py"),
        "records", "A4bLb6NXKaW5rds9J7aczRson9d",
        "--filter", "CurrentValue.[Status] != \"Fixed\"",
    ])
    if not output:
        print("No error records found or bitable_ctl failed")
        return 0

    added = 0
    existing_titles = {t.title for t in queue.list_all()}

    try:
        records = json.loads(output)
    except json.JSONDecodeError:
        # Try line-by-line parsing
        records = []
        for line in output.strip().split("\n"):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if isinstance(records, dict):
        records = records.get("records", records.get("items", [records]))

    for record in records:
        fields = record.get("fields", record)
        title = fields.get("Title", fields.get("title", fields.get("Bug", "")))
        if not title or f"[Bug] {title}" in existing_titles:
            continue

        task = ExplorationTask(
            title=f"[Bug] {title}",
            description=fields.get("Description", fields.get("description", "")),
            source=TaskSource.ERROR_TRACKER,
            source_context=json.dumps(fields, ensure_ascii=False)[:300],
            priority=Priority.P0_BLOCKING,
            autonomy_level=1,  # L1: fix then notify
            pillar="internalize",
            estimated_messages=20,
        )
        await queue.add(task)
        existing_titles.add(f"[Bug] {title}")
        added += 1
        print(f"  + [P0] [Bug] {title} (from error tracker)")

    return added


async def add_manual(queue: ExplorationQueue, title: str,
                     priority: str = "P2", pillar: str = "collect",
                     description: str = "", source: str = "manual",
                     autonomy: int = 1) -> None:
    """Add a task manually."""
    prio_map = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    task = ExplorationTask(
        title=title,
        description=description,
        source=source,
        priority=prio_map.get(priority.upper(), 2),
        autonomy_level=autonomy,
        pillar=pillar,
        estimated_messages=15,
    )
    await queue.add(task)
    print(f"Added: [P{task.priority}] {title} ({pillar}, L{autonomy})")


async def show_queue(queue: ExplorationQueue) -> None:
    """Display current queue."""
    tasks = queue.list_all()
    if not tasks:
        print("Queue is empty")
        return

    status_order = {"in_progress": 0, "pending": 1, "done": 2, "dropped": 3}
    tasks.sort(key=lambda t: (status_order.get(t.status, 9), t.priority, t.created_at))

    for t in tasks:
        flag = {"pending": "⏳", "in_progress": "🔄", "done": "✅", "dropped": "❌"}.get(t.status, "?")
        print(f"  {flag} [P{t.priority}] {t.title} ({t.pillar}) [{t.source}]")
        if t.result_summary:
            print(f"     → {t.result_summary[:100]}")

    counts = queue.count_by_status()
    print(f"\nTotal: {len(tasks)} | " + " ".join(f"{k}:{v}" for k, v in counts.items()))


async def main():
    parser = argparse.ArgumentParser(description="Seed exploration queue")
    parser.add_argument("--source", choices=["tasks", "errors", "all"],
                        help="Seed from external source")
    parser.add_argument("--list", action="store_true", help="Show current queue")
    parser.add_argument("--add", help="Add a task manually")
    parser.add_argument("--priority", default="P2", help="Priority (P0-P3)")
    parser.add_argument("--pillar", default="collect",
                        choices=["collect", "internalize", "feedback"],
                        help="Exploration pillar")
    parser.add_argument("--description", default="", help="Task description")
    parser.add_argument("--source-tag", default="manual",
                        help="Source tag (e.g. skill_review, manual)")
    parser.add_argument("--autonomy", type=int, default=1,
                        choices=[0, 1, 2], help="Autonomy level (0=silent, 1=notify, 2=confirm)")
    args = parser.parse_args()

    queue = ExplorationQueue()
    await queue.load()

    if args.list:
        await show_queue(queue)
        return

    if args.add:
        await add_manual(queue, args.add, args.priority, args.pillar,
                         args.description, args.source_tag, args.autonomy)
        return

    if args.source:
        total = 0
        if args.source in ("tasks", "all"):
            print("Seeding from feishu tasks...")
            total += await seed_from_tasks(queue)
        if args.source in ("errors", "all"):
            print("Seeding from error tracker...")
            total += await seed_from_errors(queue)
        print(f"\nTotal added: {total}")
        return

    parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
