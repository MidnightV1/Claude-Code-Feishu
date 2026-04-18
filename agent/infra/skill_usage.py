# -*- coding: utf-8 -*-
"""Skill usage telemetry — append-only JSONL log.

Consumed by HealthPulse scanner to detect idle skills. Producer hooks
live in agent/llm/claude.py (tool_use stream) so chat / cron / MADS all
write through the same path.
"""

import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("hub.skill_usage")

_WORKSPACE = Path(__file__).resolve().parents[2]
_USAGE_LOG = _WORKSPACE / "data" / "skill_usage.jsonl"

# Match ".claude/skills/<name>/scripts/" — only script executions count,
# not path references (ls / cat / grep on skill dirs).
_SKILL_SCRIPT_RE = re.compile(r"\.claude/skills/([a-z0-9_-]+)/scripts/")


def extract_skill(name: str, inp: dict) -> str | None:
    """Derive skill name from a Claude CLI tool_use event, or None."""
    if name == "Skill":
        return (inp.get("skill") or "").strip() or None
    if name == "Bash":
        m = _SKILL_SCRIPT_RE.search(inp.get("command") or "")
        if m:
            return m.group(1)
    return None


def log_skill_usage(skill: str, source: str) -> None:
    """Append one JSONL entry. Silent on I/O errors — telemetry must not kill hot paths."""
    try:
        _USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {"timestamp": time.time(), "skill": skill, "source": source}
        with _USAGE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.debug("skill_usage append failed: %s", e)
