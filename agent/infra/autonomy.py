# -*- coding: utf-8 -*-
"""Superintendent autonomy — notification routing by level.

L0 (Silent):  log only, no user-visible output
L1 (Notify):  execute then send feishu notification
L2 (Approve): create feishu doc, wait for user confirmation
L3 (Discuss): real-time conversation (handled in chat flow, not here)
"""

import logging
import time
from dataclasses import dataclass, field

from agent.infra.models import AutonomyLevel
from agent.infra.store import load_json, save_json

log = logging.getLogger("hub.autonomy")

# Persistent log of autonomous actions
ACTION_LOG_PATH = "data/autonomy_log.jsonl"


@dataclass
class AutonomousAction:
    """Record of an autonomous action taken by the Superintendent."""
    level: int                          # AutonomyLevel value
    category: str                       # e.g. "bug_fix", "config_change", "exploration"
    summary: str                        # human-readable one-liner
    detail: str = ""                    # optional longer description
    commit_sha: str = ""                # if code was changed
    rollback_cmd: str = ""              # how to undo (for L1)
    timestamp: float = field(default_factory=time.time)


async def log_action(action: AutonomousAction) -> None:
    """Append an action to the autonomy log (JSONL)."""
    import json
    import asyncio

    entry = {
        "ts": action.timestamp,
        "level": action.level,
        "category": action.category,
        "summary": action.summary,
        "detail": action.detail,
        "commit_sha": action.commit_sha,
        "rollback_cmd": action.rollback_cmd,
    }
    line = json.dumps(entry, ensure_ascii=False)

    def _write():
        import os
        os.makedirs("data", exist_ok=True)
        with open(ACTION_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    await asyncio.to_thread(_write)


async def notify(
    dispatcher,
    action: AutonomousAction,
    *,
    open_id: str = "",
    card_json: str = "",
) -> str | None:
    """Route notification based on autonomy level.

    Args:
        dispatcher: Feishu Dispatcher instance.
        action: The autonomous action to notify about.
        open_id: User open_id for DM delivery (L1).
        card_json: Optional interactive card JSON to send instead of text.

    Returns:
        message_id if a notification was sent, None for L0.
    """
    # Always log regardless of level
    await log_action(action)

    level = AutonomyLevel(action.level)

    if level == AutonomyLevel.L0_SILENT:
        log.info("[L0] %s: %s", action.category, action.summary)
        return None

    if level == AutonomyLevel.L1_NOTIFY:
        log.info("[L1] notifying: %s", action.summary)
        # Use interactive card if provided
        if card_json:
            if open_id:
                return await dispatcher.send_card_raw_to_user(open_id, card_json)
            return await dispatcher.send_card_raw_to_delivery(card_json)
        text = _format_l1_notification(action)
        if open_id:
            return await dispatcher.send_to_user(open_id, text)
        return await dispatcher.send_to_delivery_target(text)

    if level == AutonomyLevel.L2_APPROVE:
        # L2 creates a feishu doc for approval — handled by caller
        # Here we just send a pointer notification
        text = _format_l2_notification(action)
        log.info("[L2] approval needed: %s", action.summary)
        if open_id:
            return await dispatcher.send_to_user(open_id, text)
        return await dispatcher.send_to_delivery_target(text)

    if level == AutonomyLevel.L3_DISCUSS:
        # L3 is real-time discussion — just log, actual discussion happens in chat
        log.info("[L3] needs discussion: %s", action.summary)
        return None

    return None


def _format_l1_notification(action: AutonomousAction) -> str:
    """Format L1 (execute-then-notify) message."""
    parts = [f"{{{{card:header=[L1] {action.category},color=turquoise}}}}"]
    parts.append(f"**{action.summary}**")

    if action.detail:
        parts.append(f"\n{action.detail}")

    if action.commit_sha:
        parts.append(f"\n`commit: {action.commit_sha[:8]}`")

    if action.rollback_cmd:
        parts.append(f"\n回滚: `{action.rollback_cmd}`")

    return "\n".join(parts)


def _format_l2_notification(action: AutonomousAction) -> str:
    """Format L2 (approval-needed) pointer message."""
    parts = [f"{{{{card:header=[L2] 需要确认: {action.category},color=orange}}}}"]
    parts.append(f"**{action.summary}**")

    if action.detail:
        parts.append(f"\n{action.detail}")

    parts.append("\n请在文档中评论确认后我开始执行。")
    return "\n".join(parts)


async def get_recent_actions(
    hours: float = 24,
    level: AutonomyLevel | None = None,
) -> list[dict]:
    """Read recent actions from the log.

    Args:
        hours: Look back this many hours.
        level: Filter by level (None = all).

    Returns:
        List of action dicts, newest first.
    """
    import json

    cutoff = time.time() - hours * 3600
    actions = []

    try:
        with open(ACTION_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry["ts"] >= cutoff:
                        if level is None or entry["level"] == level.value:
                            actions.append(entry)
                except (json.JSONDecodeError, KeyError):
                    continue
    except FileNotFoundError:
        pass

    actions.sort(key=lambda x: x["ts"], reverse=True)
    return actions
