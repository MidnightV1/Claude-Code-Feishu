# -*- coding: utf-8 -*-
"""Heartbeat monitor — two-layer architecture.

Layer 1 (Triage): Haiku reads task snapshot, judges OK vs anomaly.
Layer 2 (Action): Sonnet via CLI with full tool access, analyzes and acts.

Input: Feishu task snapshot only (no HEARTBEAT.md).
"""

import asyncio
import os
import re
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from models import LLMConfig, LLMResult
from llm_router import LLMRouter
from dispatcher import Dispatcher

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
    "如果所有任务状态正常（无逾期、无紧急事项需要立即处理），回复 HEARTBEAT_OK。\n"
    "如果发现需要关注的异常（逾期任务、即将到期的任务无人负责等），"
    "简要列出需要处理的事项。"
)

ACTION_PROMPT = (
    "你是 nas-claude-hub 心跳监控的执行模块，运行在项目目录下，有完整工具权限。\n\n"
    "传感器检测到以下异常需要处理：\n{triage_findings}\n\n"
    "任务快照：\n{task_snapshot}\n\n"
    "请自主分析当前局面并采取需要的行动：\n"
    "- 需要更新的任务（状态、截止日期）→ 用 task_ctl.py update/complete\n"
    "- 需要通知的负责人 → 在报告中 @具体人名\n"
    "- 需要创建的跟进任务 → 用 task_ctl.py create\n"
    "- 需要同步给用户的信息\n\n"
    "工具：python3 .claude/skills/feishu-task/scripts/task_ctl.py <command>\n"
    "命令：create, list, get, update, complete, assign, unassign, delete\n\n"
    "执行完毕后，输出一份简要行动报告。此报告将通过飞书发送给用户。"
)


class HeartbeatMonitor:
    def __init__(self, config: dict, router: LLMRouter, dispatcher: Dispatcher,
                 workspace_dir: str):
        self.enabled = config.get("enabled", True)
        self.interval = config.get("interval_seconds", 1800)
        self.workspace_dir = workspace_dir

        # Triage LLM (Haiku — cheap, fast, no tools needed)
        triage_cfg = config.get("triage", config.get("llm", {}))
        self.triage_llm = LLMConfig(
            provider=triage_cfg.get("provider", "claude-cli"),
            model=triage_cfg.get("model", "haiku"),
            timeout_seconds=triage_cfg.get("timeout_seconds", 120),
            effort="low",
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

        # ── Layer 1: Triage (Haiku) ──
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
            header = "**[Heartbeat]** 传感器报告\n\n"
            await self.dispatcher.send_to_delivery_target(header + cleaned)
            return "error"

        # Deliver action report
        header = "**[Heartbeat]** 行动报告\n\n"
        ok = await self.dispatcher.send_to_delivery_target(
            header + action_result.text)
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

    @staticmethod
    def _format_duration(seconds: float) -> str:
        s = int(seconds)
        if s < 60:
            return f"{s}秒"
        if s < 3600:
            return f"{s // 60}分钟"
        if s < 86400:
            return f"{s // 3600}小时{(s % 3600) // 60}分钟"
        return f"{s // 86400}天"
