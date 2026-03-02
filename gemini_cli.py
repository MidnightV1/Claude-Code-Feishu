# -*- coding: utf-8 -*-
"""Gemini CLI subprocess wrapper (headless mode).

Currently unavailable on certain systems NAS due to tree-sitter native module build issues.
Interface is kept for future enablement.
"""

import asyncio
import json
import time
import os
import logging
from models import LLMResult

log = logging.getLogger("hub.gemini_cli")


class GeminiCli:
    def __init__(self, config: dict):
        self.path = os.path.expanduser(config.get("path", "gemini"))
        self.default_timeout = config.get("timeout_seconds", 300)
        self.available = self._check_available()

    def _check_available(self) -> bool:
        """Check if gemini CLI is installed and functional."""
        import shutil
        if not shutil.which(self.path):
            log.info("Gemini CLI not found at %s, provider disabled", self.path)
            return False
        log.info("Gemini CLI found at %s", self.path)
        return True

    async def run(
        self,
        prompt: str,
        model: str | None = None,
        system_prompt: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        if not self.available:
            return LLMResult(
                text="[Gemini CLI not available on this system]",
                is_error=True,
            )

        timeout = timeout_seconds or self.default_timeout
        args = [self.path, "--output-format", "json"]
        if model:
            args.extend(["--model", model])
        args.append(prompt)

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
            duration = int((time.monotonic() - start) * 1000)
        except asyncio.TimeoutError:
            duration = int((time.monotonic() - start) * 1000)
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
            return LLMResult(
                text=f"[Timeout after {timeout}s]",
                duration_ms=duration, is_error=True,
            )
        except Exception as e:
            duration = int((time.monotonic() - start) * 1000)
            return LLMResult(
                text=f"[Error: {e}]",
                duration_ms=duration, is_error=True,
            )

        # Parse JSONL - take last result event
        raw = stdout.decode("utf-8", errors="replace").strip()
        text = ""
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("type") == "result":
                    text = obj.get("result", "") or obj.get("text", "")
            except json.JSONDecodeError:
                if not text:
                    text = line

        if not text and proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[:500] if stderr else ""
            return LLMResult(
                text=f"[Exit {proc.returncode}: {err}]",
                duration_ms=duration, is_error=True,
            )

        return LLMResult(text=text or raw, duration_ms=duration)
