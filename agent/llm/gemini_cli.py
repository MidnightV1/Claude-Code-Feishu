# -*- coding: utf-8 -*-
"""Gemini CLI subprocess wrapper (headless stdin-pipe mode).

Pipe mode constraints (v0.31.0):
- --output-format json hangs → parse plain text stdout
- No session persistence → each invocation is stateless
- @/path/to/file syntax injects local files (cheap, no upload)
"""

import asyncio
import time
import os
import logging
from agent.infra.models import LLMResult

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
        """Run prompt via stdin pipe. Returns plain text output."""
        if not self.available:
            return LLMResult(
                text="[Gemini CLI not available on this system]",
                is_error=True,
            )

        timeout = timeout_seconds or self.default_timeout
        args = [self.path]
        if model:
            args.extend(["--model", model])

        # Gemini CLI has no separate system prompt channel; prepend to user prompt
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n---\n\n{full_prompt}"

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=full_prompt.encode("utf-8")),
                timeout=timeout,
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

        text = stdout.decode("utf-8", errors="replace").strip()

        if not text and proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[:500] if stderr else ""
            return LLMResult(
                text=f"[Exit {proc.returncode}: {err}]",
                duration_ms=duration, is_error=True,
            )

        return LLMResult(text=text or "", duration_ms=duration)

    async def run_with_file(
        self,
        prompt: str,
        file_path: str,
        model: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        """Run prompt with @file_path attachment via stdin pipe."""
        full_prompt = f"{prompt} @{file_path}"
        return await self.run(
            full_prompt, model=model, timeout_seconds=timeout_seconds,
        )
