# -*- coding: utf-8 -*-
"""Heartbeat monitor — two-layer Sonnet architecture.

Layer 1 (Triage): Sonnet reads task snapshot, judges OK vs anomaly.
Layer 2 (Action): Sonnet via CLI with full tool access, analyzes and acts.

Notifications delivered to user DM in natural conversational tone.
Input: Feishu task snapshot only.
"""

import asyncio
import hashlib
import os
import re
import time
import logging
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from agent.infra.models import LLMConfig, LLMResult
from agent.llm.router import LLMRouter
from agent.platforms.feishu.dispatcher import Dispatcher

log = logging.getLogger("hub.heartbeat")

HEARTBEAT_TOKEN = "HEARTBEAT_OK"
HEARTBEAT_TOKEN_PATTERN = re.compile(
    r'(\*{0,2})HEARTBEAT_OK(\*{0,2})',
    re.IGNORECASE,
)
ACK_MAX_CHARS = 300
SNAPSHOT_TIMEOUT = 30  # seconds for task_ctl.py snapshot subprocess

TRIAGE_PROMPT = (
    "当前时间：{current_time}\n\n"
    "以下是当前任务状态快照：\n\n"
    "{task_snapshot}\n\n"
    "判断规则：\n"
    "- 已逾期（截止时间 < 当前时间）→ 异常\n"
    "- 距离截止不足 30 分钟 → 异常\n"
    "- 所有任务截止时间充裕 → 正常\n\n"
    "如果正常，回复 HEARTBEAT_OK。\n"
    "如果异常，简要列出需要处理的事项（哪个任务、什么状态、距离截止多久）。"
)

ACTION_PROMPT = (
    "你是用户的 AI 助手，通过飞书 DM 给用户发提醒。\n\n"
    "需要关注的任务：\n{triage_findings}\n\n"
    "任务快照：\n{task_snapshot}\n\n"
    "可用工具（仅在需要修改任务时使用）：\n"
    "python3 .claude/skills/feishu-task/scripts/task_ctl.py <command>\n"
    "命令：update, complete, create\n\n"
    "**输出规则（严格遵守）：**\n"
    "- 只输出最终发给用户的消息文本\n"
    "- 禁止输出分析过程、内部推理、行动计划\n"
    "- 风格：像朋友提醒一样自然简短，一两句话\n"
    "- 禁止使用「心跳」「行动报告」「任务状态确认」等系统术语\n"
    "- 示例：「XX 快到期了，12:30 截止，要不要帮你标记完成？」\n"
    "- 如果执行了工具操作，顺带提一句做了什么"
)


class HeartbeatMonitor:
    def __init__(self, config: dict, router: LLMRouter, dispatcher: Dispatcher,
                 workspace_dir: str, notify_open_id: str = ""):
        self.enabled = config.get("enabled", True)
        self.interval = config.get("interval_seconds", 1800)
        self.workspace_dir = workspace_dir
        self.notify_open_id = notify_open_id

        # Triage LLM (Sonnet — accurate judgment, no tools needed)
        triage_cfg = config.get("triage", config.get("llm", {}))
        self.triage_llm = LLMConfig(
            provider=triage_cfg.get("provider", "claude-cli"),
            model=triage_cfg.get("model", "sonnet"),
            timeout_seconds=triage_cfg.get("timeout_seconds", 120),
        )

        # Action LLM (Sonnet — full tool access, triggered only on anomaly)
        action_cfg = config.get("action", {})
        self.action_llm = LLMConfig(
            provider=action_cfg.get("provider", "claude-cli"),
            model=action_cfg.get("model", "sonnet"),
            timeout_seconds=action_cfg.get("timeout_seconds", 300),
        )

        # Active hours
        ah = config.get("active_hours", {})
        self.active_start = ah.get("start", "00:00")
        self.active_end = ah.get("end", "23:59")
        self.tz_name = ah.get("timezone", "Asia/Shanghai")

        # Task snapshot config
        self._alert_window_hours = config.get("alert_window_hours",
            config.get("tasks", {}).get("alert_window_hours", 2))

        self.router = router
        self.dispatcher = dispatcher
        self._task: asyncio.Task | None = None
        self._last_sent_at: float | None = None
        # Pending notifications per session key — consumed by feishu_bot on next user message
        self._pending_notifications: dict[str, list[str]] = defaultdict(list)
        # Notification dedup: hash → timestamp, 30-min window
        self._sent_hashes: dict[str, float] = {}
        self._dedup_window = 1800  # seconds

    async def start(self):
        if not self.enabled:
            log.info("Heartbeat disabled")
            return
        self._task = asyncio.create_task(self._loop())
        log.info("Heartbeat started (interval=%ds, triage=%s/%s, action=%s/%s)",
                 self.interval,
                 self.triage_llm.provider, self.triage_llm.model,
                 self.action_llm.provider, self.action_llm.model)

    async def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Heartbeat stopped")

    async def run_once(self, reason: str = "manual") -> str:
        """Run a single heartbeat cycle. Returns: ran/skipped/suppressed/error."""
        if not self._is_within_active_hours():
            log.debug("Heartbeat skipped: outside active hours")
            return "skipped"

        # ── Collect task snapshot ──
        snapshot = await self._collect_task_snapshot()
        if not snapshot:
            log.debug("Heartbeat skipped: no task data")
            return "skipped"

        now = datetime.now(ZoneInfo(self.tz_name))
        current_time = now.strftime("%Y-%m-%d %H:%M (%A)")

        # ── Layer 1: Triage (Sonnet) ──
        triage_prompt = TRIAGE_PROMPT.format(
            current_time=current_time,
            task_snapshot=snapshot,
        )

        triage_result = await self.router.run(
            prompt=triage_prompt,
            llm_config=self.triage_llm,
        )

        if triage_result.is_error:
            log.warning("Heartbeat triage error: %s", triage_result.text[:200])
            return "error"

        should_skip, cleaned = self._strip_heartbeat_token(triage_result.text)
        if should_skip:
            log.info("Heartbeat OK (triage: all clear) [%s]", reason)
            return "suppressed"

        log.info("Heartbeat triage detected anomaly, triggering action [%s]", reason)

        # ── Layer 2: Action (Sonnet with tools) ──
        action_prompt = ACTION_PROMPT.format(
            triage_findings=cleaned,
            task_snapshot=snapshot,
        )

        action_result = await self.router.run(
            prompt=action_prompt,
            llm_config=self.action_llm,
        )

        if action_result.is_error:
            log.warning("Heartbeat action error: %s", action_result.text[:200])
            # Fallback: deliver triage findings directly
            await self._deliver(cleaned)
            return "error"

        # Deliver action report (natural tone, no system headers)
        ok = await self._deliver(action_result.text)
        if ok:
            self._last_sent_at = time.time()
            log.info("Heartbeat action report delivered (%d chars) [%s]",
                     len(action_result.text), reason)
            return "ran"
        else:
            log.warning("Heartbeat delivery failed")
            return "error"

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "interval_seconds": self.interval,
            "triage_model": f"{self.triage_llm.provider}/{self.triage_llm.model}",
            "action_model": f"{self.action_llm.provider}/{self.action_llm.model}",
            "active_hours": f"{self.active_start}-{self.active_end} ({self.tz_name})",
            "last_sent_at": self._last_sent_at,
        }

    # ═══ Internal ═══

    def _is_duplicate(self, text: str) -> bool:
        """Check if similar notification was sent within dedup window."""
        now = time.time()
        # Purge expired entries
        self._sent_hashes = {
            h: ts for h, ts in self._sent_hashes.items()
            if now - ts < self._dedup_window
        }
        h = hashlib.md5(text.encode()).hexdigest()
        if h in self._sent_hashes:
            return True
        self._sent_hashes[h] = now
        return False

    async def _deliver(self, text: str) -> str | None:
        """Deliver message and store for conversation context injection."""
        if self._is_duplicate(text):
            log.info("Heartbeat notification dedup: suppressed duplicate")
            return "dedup"
        if self.notify_open_id:
            result = await self.dispatcher.send_to_user(self.notify_open_id, text)
            # Store for context injection: session_key matches feishu_bot p2p pattern
            session_key = f"user:{self.notify_open_id}"
            now = datetime.now(ZoneInfo(self.tz_name)).strftime("%H:%M")
            self._pending_notifications[session_key].append(
                f"[系统通知 {now}] 已向用户发送任务提醒：{text}")
            return result
        return await self.dispatcher.send_to_delivery_target(text)

    def drain_notifications(self, session_key: str) -> list[str]:
        """Pop and return pending notifications for a session. Called by feishu_bot."""
        return self._pending_notifications.pop(session_key, [])

    async def _loop(self):
        try:
            while True:
                await asyncio.sleep(self.interval)
                try:
                    await self.run_once(reason="interval")
                except Exception as e:
                    log.error("Heartbeat cycle error: %s", e)
        except asyncio.CancelledError:
            pass

    def _is_within_active_hours(self) -> bool:
        now = datetime.now(ZoneInfo(self.tz_name))
        now_minutes = now.hour * 60 + now.minute

        start = self._parse_hhmm(self.active_start)
        end = self._parse_hhmm(self.active_end)

        if end > start:
            return start <= now_minutes < end
        else:  # crosses midnight
            return now_minutes >= start or now_minutes < end

    @staticmethod
    def _parse_hhmm(s: str) -> int:
        parts = s.split(":")
        return int(parts[0]) * 60 + int(parts[1])

    @staticmethod
    def _strip_heartbeat_token(text: str) -> tuple[bool, str]:
        """Strip HEARTBEAT_OK token. Returns (should_skip, cleaned_text).

        should_skip=True only when the token was present AND remaining text is short
        (i.e., the LLM said "all clear" with minimal commentary).
        """
        has_token = bool(HEARTBEAT_TOKEN_PATTERN.search(text))
        cleaned = HEARTBEAT_TOKEN_PATTERN.sub("", text).strip()
        if has_token and len(cleaned) <= ACK_MAX_CHARS:
            return True, cleaned
        return False, cleaned

    async def _collect_task_snapshot(self) -> str:
        """Collect task snapshot via task_ctl.py subprocess."""
        script = os.path.join(
            self.workspace_dir,
            ".claude/skills/feishu-task/scripts/task_ctl.py")
        if not os.path.exists(script):
            return ""
        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", script, "snapshot",
                "--window-hours", str(self._alert_window_hours),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace_dir,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=SNAPSHOT_TIMEOUT)
            return stdout.decode("utf-8").strip()
        except Exception as e:
            log.warning("Task snapshot failed: %s", e)
            return ""

