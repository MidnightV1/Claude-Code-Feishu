#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task management CLI — create, query, and manage long-running tasks.

Usage:
    python task_ctl.py create --goal "目标" --plan '{"steps":[...]}'
    python task_ctl.py list              # List all tasks
    python task_ctl.py active            # List active tasks only
    python task_ctl.py status <task_id>  # Show task details
    python task_ctl.py cancel <task_id>  # Cancel a task

The `create` command writes a request file to data/task_requests/.
The hub picks it up after the current CLI session ends and creates
a TaskPlan with the correct session context.
"""

import sys
import os
import json
import time
import uuid
from datetime import datetime

# Project root (4 levels up from scripts/)
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
sys.path.insert(0, PROJECT_ROOT)
from store import load_json_sync, save_json_sync

TASKS_PATH = os.path.join(PROJECT_ROOT, "data", "tasks.json")
REQUESTS_DIR = os.path.join(PROJECT_ROOT, "data", "task_requests")


def _fmt_time(ts: float) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def _fmt_age(ts: float) -> str:
    if not ts:
        return "—"
    delta = int(time.time() - ts)
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h{(delta % 3600) // 60}m"
    return f"{delta // 86400}d"


def cmd_create(goal: str, plan_json: str):
    """Write a task request file for the hub to pick up."""
    try:
        plan = json.loads(plan_json)
        steps = plan.get("steps", plan) if isinstance(plan, dict) else plan
        if not isinstance(steps, list) or len(steps) < 2:
            print("Error: plan must contain at least 2 steps.")
            sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON — {e}")
        sys.exit(1)

    os.makedirs(REQUESTS_DIR, exist_ok=True)
    request_id = uuid.uuid4().hex[:12]
    request = {
        "request_id": request_id,
        "goal": goal,
        "steps": steps,
        "created_at": time.time(),
    }
    path = os.path.join(REQUESTS_DIR, f"{request_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(request, f, ensure_ascii=False, indent=2)

    print(f"Task request created: {request_id}")
    print(f"Goal: {goal}")
    print(f"Steps: {len(steps)}")
    print("Hub will pick this up and create a TaskPlan for user approval.")


def cmd_list(active_only=False):
    tasks = load_json_sync(TASKS_PATH, default=[])
    if active_only:
        tasks = [t for t in tasks if t.get("status") in ("planning", "awaiting_approval", "executing")]
    if not tasks:
        print("No tasks." if not active_only else "No active tasks.")
        return
    tasks.sort(key=lambda t: t.get("created_at", 0), reverse=True)
    print(f"{'ID':<14} {'Status':<20} {'Steps':>7} {'Age':>6}  Goal")
    print("-" * 80)
    for t in tasks:
        steps = t.get("steps", [])
        done = sum(1 for s in steps if s.get("status") == "completed")
        total = len(steps)
        progress = f"{done}/{total}" if total > 0 else "—"
        print(
            f"{t['task_id']:<14} {t['status']:<20} {progress:>7} "
            f"{_fmt_age(t.get('created_at', 0)):>6}  {t.get('goal', '')[:50]}"
        )


def cmd_status(task_id: str):
    tasks = load_json_sync(TASKS_PATH, default=[])
    task = None
    for t in tasks:
        if t.get("task_id", "").startswith(task_id):
            task = t
            break
    if not task:
        print(f"Task {task_id} not found.")
        sys.exit(1)

    print(f"Task:    {task['task_id']}")
    print(f"Status:  {task['status']}")
    print(f"Goal:    {task['goal']}")
    print(f"Created: {_fmt_time(task.get('created_at', 0))}")
    print(f"Updated: {_fmt_time(task.get('updated_at', 0))}")
    if task.get("error"):
        print(f"Error:   {task['error']}")
    print()
    steps = task.get("steps", [])
    if steps:
        print("Steps:")
        for i, s in enumerate(steps):
            icon = {"completed": "✓", "running": "→", "failed": "✗"}.get(s.get("status", ""), " ")
            print(f"  {icon} {i+1}. {s['name']} [{s.get('status', 'pending')}]")
            if s.get("result"):
                for line in s["result"][:200].splitlines()[:3]:
                    print(f"       {line}")


def cmd_cancel(task_id: str):
    tasks = load_json_sync(TASKS_PATH, default=[])
    found = False
    for t in tasks:
        if t.get("task_id", "").startswith(task_id):
            if t["status"] in ("completed", "failed"):
                print(f"Task {t['task_id']} already {t['status']}.")
                return
            t["status"] = "failed"
            t["error"] = "Cancelled via CLI"
            t["updated_at"] = time.time()
            found = True
            print(f"Task {t['task_id']} cancelled.")
            break
    if not found:
        print(f"Task {task_id} not found.")
        sys.exit(1)
    save_json_sync(TASKS_PATH, tasks)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd == "create":
        goal = ""
        plan_json = ""
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--goal" and i + 1 < len(sys.argv):
                goal = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--plan" and i + 1 < len(sys.argv):
                plan_json = sys.argv[i + 1]
                i += 2
            else:
                i += 1
        if not goal or not plan_json:
            print('Usage: task_ctl.py create --goal "目标" --plan \'{"steps":[...]}\'')
            sys.exit(1)
        cmd_create(goal, plan_json)
    elif cmd == "list":
        cmd_list()
    elif cmd == "active":
        cmd_list(active_only=True)
    elif cmd == "status":
        if len(sys.argv) < 3:
            print("Usage: task_ctl.py status <task_id>")
            sys.exit(1)
        cmd_status(sys.argv[2])
    elif cmd == "cancel":
        if len(sys.argv) < 3:
            print("Usage: task_ctl.py cancel <task_id>")
            sys.exit(1)
        cmd_cancel(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
