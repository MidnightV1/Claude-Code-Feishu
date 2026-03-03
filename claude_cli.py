# -*- coding: utf-8 -*-
"""Claude Code CLI subprocess wrapper."""

import asyncio
import json
import time
import os
import logging
from models import LLMResult

log = logging.getLogger("hub.claude_cli")


class ClaudeCli:
    def __init__(self, config: dict):
        self.path = os.path.expanduser(config.get("path", "claude"))
        self.default_timeout = config.get("timeout_seconds", 600)
        self.workspace_dir = os.path.expanduser(
            config.get("workspace_dir", ".")
        )

    async def run(
        self,
        prompt: str,
        session_id: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        """Execute Claude CLI. Retry logic is handled by the caller (LLMRouter)."""
        return await self._execute(
            prompt, session_id=session_id, model=model,
            system_prompt=system_prompt, timeout_seconds=timeout_seconds,
        )

    async def _execute(
        self,
        prompt: str,
        session_id: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        timeout = timeout_seconds or self.default_timeout
        args = [
            self.path, "-p",
            "--output-format", "json",
            "--dangerously-skip-permissions",
        ]
        # fallback-model must differ from main model
        effective_model = model or "opus"
        if effective_model != "opus":
            args.extend(["--fallback-model", "opus"])
        elif effective_model != "sonnet":
            args.extend(["--fallback-model", "sonnet"])
        if session_id:
            args.extend(["--resume", session_id])
        if model:
            args.extend(["--model", model])
        if system_prompt:
            args.extend(["--append-system-prompt", system_prompt])

        # Prevent "nested session" error: strip all Claude Code session markers
        _strip = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"}
        env = {k: v for k, v in os.environ.items() if k not in _strip}

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace_dir,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
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
            log.warning("Claude CLI timed out after %ds", timeout)
            return LLMResult(
                text=f"[Timeout after {timeout}s]",
                duration_ms=duration, is_error=True,
            )
        except asyncio.CancelledError:
            duration = int((time.monotonic() - start) * 1000)
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                proc.kill()
            log.info("Claude CLI cancelled after %dms", duration)
            return LLMResult(
                text="[Cancelled]",
                duration_ms=duration, is_error=True, cancelled=True,
            )
        except Exception as e:
            duration = int((time.monotonic() - start) * 1000)
            log.error("Claude CLI exec error: %s", e)
            return LLMResult(
                text=f"[Error: {e}]",
                duration_ms=duration, is_error=True,
            )

        # Parse JSONL output - take last result entry
        text = ""
        new_session_id = None
        cost = 0.0

        raw = stdout.decode("utf-8", errors="replace").strip()
        if not raw:
            if stderr:
                err = stderr.decode("utf-8", errors="replace").strip()
                log.warning("Claude CLI empty stdout, stderr: %s", err[:500])
                return LLMResult(
                    text=f"[CLI error: {err[:500]}]",
                    duration_ms=duration, is_error=True,
                )
            log.warning("Claude CLI returned empty stdout (session=%s, model=%s)", session_id, model)
            return LLMResult(text="", duration_ms=duration, is_error=True)

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("type") == "result":
                    text = obj.get("result", "")
                    new_session_id = obj.get("session_id")
                    cost = obj.get("total_cost_usd") or obj.get("cost_usd") or 0.0
            except json.JSONDecodeError:
                # Non-JSON line, might be raw text fallback
                if not text:
                    text = line

        if proc.returncode != 0 and not text:
            err = stderr.decode("utf-8", errors="replace").strip() if stderr else "unknown error"
            return LLMResult(
                text=f"[Exit code {proc.returncode}: {err[:500]}]",
                duration_ms=duration, is_error=True,
            )

        return LLMResult(
            text=text,
            session_id=new_session_id,
            duration_ms=duration,
            cost_usd=cost,
        )
