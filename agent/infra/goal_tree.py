# -*- coding: utf-8 -*-
"""Goal Tree — OKR-driven strategic layer for autonomous exploration.

Loads/saves the goal tree from YAML. Provides query interface for
the explorer, daily planner, and next-explore evaluation.

The goal tree answers: "What should the system be investigating?"
Each goal has sub_questions that decompose into explorable units.
"""

import logging
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("hub.goal_tree")

GOAL_TREE_PATH = "data/goal_tree.yaml"


def load(path: str = GOAL_TREE_PATH) -> dict:
    """Load goal tree from YAML."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        log.debug("Goal tree loaded: %d goals", len(data.get("goals", [])))
        return data
    except FileNotFoundError:
        log.warning("Goal tree not found: %s", path)
        return {"version": 1, "mission": "", "goals": []}
    except Exception as e:
        log.error("Goal tree load error: %s", e)
        return {"version": 1, "mission": "", "goals": []}


def save(data: dict, path: str = GOAL_TREE_PATH):
    """Save goal tree to YAML."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False,
                  sort_keys=False, width=120)
    log.info("Goal tree saved")


def get_active_goals(data: dict) -> list[dict]:
    """Return goals that are actively being explored (not achieved/paused)."""
    return [
        g for g in data.get("goals", [])
        if g.get("status") in ("exploring", "blocked")
    ]


def get_open_questions(data: dict, priority: Optional[str] = None) -> list[dict]:
    """Return open sub-questions across all active goals.

    Each item: {"goal_id", "goal_title", "priority", "question", "findings"}
    """
    questions = []
    for goal in get_active_goals(data):
        if priority and goal.get("priority") != priority:
            continue
        for sq in goal.get("sub_questions", []):
            if sq.get("status") == "open":
                questions.append({
                    "goal_id": goal["id"],
                    "goal_title": goal["title"],
                    "priority": goal.get("priority", "P2"),
                    "question": sq["q"],
                    "findings": sq.get("findings", ""),
                })
    return questions


def format_for_prompt(data: dict, max_goals: int = 10) -> str:
    """Format goal tree as context for LLM prompts.

    Concise representation: mission + active goals + open questions.
    """
    parts = []
    mission = data.get("mission", "")
    if mission:
        parts.append(f"## 系统使命\n{mission}\n")

    goals = get_active_goals(data)[:max_goals]
    if not goals:
        return "\n".join(parts) + "\n(无活跃目标)"

    parts.append("## 活跃目标 (OKR)")
    for g in goals:
        status_icon = {"exploring": "🔍", "blocked": "🚧"}.get(g["status"], "")
        parts.append(f"\n### [{g.get('priority', 'P2')}] {g['title']} {status_icon}")
        parts.append(f"*Why*: {g.get('why', '')}")

        open_qs = [sq for sq in g.get("sub_questions", []) if sq.get("status") == "open"]
        if open_qs:
            parts.append("**待回答的问题**:")
            for sq in open_qs:
                findings = sq.get("findings", "")
                suffix = f" — 已知: {findings}" if findings else ""
                parts.append(f"- {sq['q']}{suffix}")

        progress = g.get("progress", "")
        if progress:
            parts.append(f"*进展*: {progress}")

        krs = g.get("key_results", [])
        if krs:
            parts.append("*关键结果*: " + " | ".join(krs))

    return "\n".join(parts)


def update_question(data: dict, goal_id: str, question: str,
                    status: str = None, findings: str = None) -> bool:
    """Update a specific sub-question's status and findings."""
    for goal in data.get("goals", []):
        if goal.get("id") != goal_id:
            continue
        for sq in goal.get("sub_questions", []):
            if sq["q"] == question:
                if status:
                    sq["status"] = status
                if findings:
                    sq["findings"] = findings
                return True
    return False


def update_progress(data: dict, goal_id: str, progress: str) -> bool:
    """Update a goal's progress field."""
    for goal in data.get("goals", []):
        if goal.get("id") == goal_id:
            goal["progress"] = progress
            return True
    return False
