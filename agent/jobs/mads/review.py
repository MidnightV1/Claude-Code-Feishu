# -*- coding: utf-8 -*-
"""MADS Design Review flow.

Creates a Feishu task for review tracking and polls the design document
for user comments to determine approval status.
"""

import json
import time
from datetime import datetime, timedelta

from agent.jobs.mads.helpers import doc_ctl, task_ctl, notify, log


async def create_review_task(doc_url: str, ticket_title: str) -> str:
    """Create a Feishu task to track design doc review (48h deadline).

    Returns task_guid on success, or "" on failure.
    """
    summary = f"[MADS] Review: {ticket_title}"
    due_dt = datetime.now() + timedelta(hours=48)
    due_str = due_dt.strftime("%Y-%m-%d %H:%M")

    try:
        rc, stdout, stderr = await task_ctl(
            "create", summary,
            "--due", due_str,
            "--desc", f"Design doc for review: {doc_url}",
        )
        if rc != 0:
            log.warning("Review task creation failed (rc=%d): %s", rc, stderr[:200])
            return ""

        # stdout format: "Created: <guid>\n  Title: ..."
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("Created: "):
                guid = line.split("Created: ", 1)[1].strip()
                log.info("Review task created: %s", guid)
                return guid

        log.warning("Review task: could not parse guid from output: %s", stdout[:200])
        return ""
    except Exception as e:
        log.warning("Review task creation error: %s", e)
        return ""


async def check_review_status(doc_id: str, review_started_at: float) -> tuple[str, str]:
    """Check the design doc for user comments to determine review status.

    Returns:
        ("feedback", json_str)  — user left comments (any comment = feedback)
        ("approved", json_str)  — user left approval comment (含 "可以"/"通过"/"LGTM")
        ("pending", "")         — no comments yet, regardless of time elapsed

    Note: 48h reminder logic is in the pipeline, not here. This function
    never auto-approves — explicit user feedback is always required.
    """
    try:
        rc, stdout, stderr = await doc_ctl("analyze", doc_id)
        if rc != 0:
            log.warning("doc_ctl analyze failed (rc=%d): %s", rc, stderr[:200])
            return ("pending", "")

        data = json.loads(stdout)
        annotations = data.get("annotations", [])

        if not annotations:
            return ("pending", "")

        # Check if latest comment is an approval signal
        latest_text = ""
        for ann in annotations:
            thread = ann.get("thread", [])
            if thread:
                latest_text = thread[-1].get("text", "")

        approval_signals = ["可以", "通过", "没问题", "同意", "ok", "lgtm", "approved"]
        if latest_text and any(s in latest_text.lower() for s in approval_signals):
            return ("approved", json.dumps(annotations, ensure_ascii=False))

        return ("feedback", json.dumps(annotations, ensure_ascii=False))

    except json.JSONDecodeError as e:
        log.warning("check_review_status: failed to parse doc analyze output: %s", e)
        return ("pending", "")
    except Exception as e:
        log.warning("check_review_status error: %s", e)
        return ("pending", "")


async def send_review_reminder(dispatcher, ticket_title: str, doc_url: str):
    """Send a reminder notification that the design doc is awaiting review."""
    message = (
        f"**[MADS] 设计文档待审阅**\n\n"
        f"「{ticket_title}」的设计文档已等待 48 小时，尚未收到评论。\n\n"
        f"请在文档中评论\"可以\"确认通过，或留下反馈意见：\n"
        f"[查看设计文档]({doc_url})"
    )
    await notify(dispatcher, "yellow", message, header="MADS Review")
