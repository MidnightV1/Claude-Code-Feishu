# -*- coding: utf-8 -*-
"""Heartbeat monitor. Pattern from OpenClaw src/infra/heartbeat-runner.ts.

Python collects system snapshots, LLM judges — no tool access needed.
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
DEFAULT_PROMPT_TEMPLATE = (
    "Current time: {current_time}\n\n"
    "Review the tasks below. If any need action now, do it and report briefly.\n"
    "If nothing needs attention, reply HEARTBEAT_OK.\n\n"
    "{heartbeat_content}"
)
ACK_MAX_CHARS = 300


class HeartbeatMonitor:
    def __init__(self, config: dict, router: LLMRouter, dispatcher: Dispatcher, workspace_dir: str):
        self.enabled = config.get("enabled", True)
        self.interval = config.get("interval_seconds", 1800)
        self.prompt_template = config.get("prompt") or DEFAULT_PROMPT_TEMPLATE
        self.workspace_dir = workspace_dir

        # LLM config for heartbeat (default: cheap gemini)
        llm_cfg = config.get("llm", {})
        self.llm = LLMConfig(
            provider=llm_cfg.get("provider", "gemini-api"),
            model=llm_cfg.get("model", "2.5-Flash-Lite"),
            timeout_seconds=llm_cfg.get("timeout_seconds", 120),
            temperature=llm_cfg.get("temperature", 1.0),
        )

        # Active hours
        ah = config.get("active_hours", {})
        self.active_start = ah.get("start", "00:00")
        self.active_end = ah.get("end", "23:59")
        self.tz_name = ah.get("timezone", "Asia/Shanghai")

        self.router = router
        self.dispatcher = dispatcher
        self._task: asyncio.Task | None = None
        self._last_text: str | None = None
        self._last_sent_at: float | None = None

    async def start(self):
        if not self.enabled:
            log.info("Heartbeat disabled")
            return
        self._task = asyncio.create_task(self._loop())
        log.info("Heartbeat started (interval=%ds, model=%s/%s)",
                 self.interval, self.llm.provider, self.llm.model)

    async def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Heartbeat stopped")

    async def run_once(self, reason: str = "manual") -> str:
        """Run a single heartbeat cycle. Returns: ran/skipped/suppressed."""
        if not self._is_within_active_hours():
            log.debug("Heartbeat skipped: outside active hours")
            return "skipped"

        hb_file = os.path.join(self.workspace_dir, "HEARTBEAT.md")
        if os.path.exists(hb_file):
            with open(hb_file, "r", encoding="utf-8") as f:
                content = f.read()
            if self._is_effectively_empty(content):
                log.debug("Heartbeat skipped: HEARTBEAT.md effectively empty")
                return "skipped"
        else:
            log.debug("Heartbeat skipped: no HEARTBEAT.md")
            return "skipped"

        now = datetime.now(ZoneInfo(self.tz_name))
        prompt = self.prompt_template.format(
            heartbeat_content=content,
            current_time=now.strftime("%Y-%m-%d %H:%M (%A)"),
        )

        result = await self.router.run(
            prompt=prompt,
            llm_config=self.llm,
            session_key="heartbeat",
        )

        if result.is_error:
            log.warning("Heartbeat error: %s", result.text[:200])
            return "error"

        should_skip, cleaned = self._strip_heartbeat_token(result.text)
        if should_skip:
            log.info("Heartbeat OK (suppressed) [%s]", reason)
            return "suppressed"

        # Dedup within 24h
        if self._is_duplicate(cleaned):
            log.info("Heartbeat dedup (same text within 24h)")
            return "suppressed"

        # Deliver
        header = f"**[Heartbeat]** ({self.llm.provider}/{self.llm.model})\n\n"
        ok = await self.dispatcher.send_to_delivery_target(header + cleaned)
        if ok:
            self._last_text = cleaned
            self._last_sent_at = time.time()
            log.info("Heartbeat delivered (%d chars) [%s]", len(cleaned), reason)
            return "ran"
        else:
            log.warning("Heartbeat delivery failed")
            return "error"

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "interval_seconds": self.interval,
            "model": f"{self.llm.provider}/{self.llm.model}",
            "active_hours": f"{self.active_start}-{self.active_end} ({self.tz_name})",
            "last_sent_at": self._last_sent_at,
            "last_text_preview": (self._last_text[:100] + "...") if self._last_text else None,
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
    def _is_effectively_empty(content: str) -> bool:
        """Check if HEARTBEAT.md has no actionable content."""
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                continue
            if re.match(r'^[-*]\s*(\[[ ]\])?\s*$', line):
                continue
            return False
        return True

    @staticmethod
    def _strip_heartbeat_token(text: str) -> tuple[bool, str]:
        """Strip HEARTBEAT_OK token. Returns (should_skip, cleaned_text)."""
        cleaned = HEARTBEAT_TOKEN_PATTERN.sub("", text).strip()
        if len(cleaned) <= ACK_MAX_CHARS:
            return True, cleaned
        return False, cleaned

    def _is_duplicate(self, text: str) -> bool:
        if not self._last_text or not self._last_sent_at:
            return False
        if time.time() - self._last_sent_at > 86400:  # 24h
            return False
        return text.strip() == self._last_text.strip()
