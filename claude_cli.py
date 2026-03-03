# -*- coding: utf-8 -*-
"""Claude Code CLI subprocess wrapper with streaming progress support."""

import asyncio
import json
import time
import os
import logging
from pathlib import PurePosixPath
from typing import Awaitable, Callable

from models import LLMResult

log = logging.getLogger("hub.claude_cli")


def _make_tool_label(tool_name: str, tool_input: dict) -> str:
    """Map a tool_use event to a human-readable progress label."""
    if tool_name == "Read":
        path = tool_input.get("file_path", "")
        name = PurePosixPath(path).name if path else ""
        return f"📖 读取 {name}" if name else "📖 读取文件"

    if tool_name == "Grep":
        pat = tool_input.get("pattern", "")
        return f"🔍 搜索 {pat[:30]}" if pat else "🔍 搜索代码"

    if tool_name == "Glob":
        pat = tool_input.get("pattern", "")
        return f"📂 查找 {pat[:30]}" if pat else "📂 查找文件"

    if tool_name == "Bash":
        desc = tool_input.get("description", "")
        return f"⚡ {desc[:40]}" if desc else "⚡ 执行命令"

    if tool_name in ("Edit", "Write"):
        path = tool_input.get("file_path", "")
        name = PurePosixPath(path).name if path else ""
        icon = "✏️" if tool_name == "Edit" else "📝"
        return f"{icon} {name}" if name else f"{icon} 编辑文件"

    if tool_name == "Agent":
        desc = tool_input.get("description", "")
        return f"🤖 {desc[:40]}" if desc else "🤖 子任务分析"

    if tool_name in ("WebFetch", "WebSearch"):
        query = tool_input.get("query", tool_input.get("prompt", ""))
        return f"🌐 {query[:30]}" if query else "🌐 搜索网页"

    if tool_name.startswith("mcp__"):
        # mcp__brave-search__brave_web_search → brave 搜索
        parts = tool_name.split("__")
        server = parts[1] if len(parts) > 1 else "扩展"
        return f"🔌 {server}"

    return f"🔧 {tool_name}"


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
        effort: str | None = None,
        on_activity: Callable[[str], Awaitable[None]] | None = None,
        on_todo: Callable[[list[dict]], Awaitable[None]] | None = None,
    ) -> LLMResult:
        """Execute Claude CLI with streaming progress.

        Args:
            effort: Reasoning effort level ("low", "medium", "high", or None for CLI default).
            on_activity: async callback receiving human-readable progress labels
                         when CC uses tools (e.g. "📖 读取 feishu_bot.py").
            on_todo: async callback receiving the full todo list whenever CC
                     calls TodoWrite (list of {content, status, activeForm}).
        """
        return await self._execute(
            prompt, session_id=session_id, model=model,
            system_prompt=system_prompt, timeout_seconds=timeout_seconds,
            effort=effort, on_activity=on_activity, on_todo=on_todo,
        )

    async def _execute(
        self,
        prompt: str,
        session_id: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        timeout_seconds: int | None = None,
        effort: str | None = None,
        on_activity: Callable[[str], Awaitable[None]] | None = None,
        on_todo: Callable[[list[dict]], Awaitable[None]] | None = None,
    ) -> LLMResult:
        timeout = timeout_seconds or self.default_timeout
        args = [
            self.path, "-p",
            "--output-format", "stream-json",
            "--verbose",
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
        if effort:
            args.extend(["--effort", effort])

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
                limit=1024 * 1024,  # 1MB; default 64KB is too small for large stream-json lines
                cwd=self.workspace_dir,
                env=env,
            )

            # Send prompt and close stdin
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            # Collect stderr in background
            stderr_task = asyncio.create_task(proc.stderr.read())

            # Stream stdout line by line, extracting tool events
            text = ""
            new_session_id = None
            cost = 0.0

            async def _read_stream():
                nonlocal text, new_session_id, cost
                async for raw_line in proc.stdout:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    evt_type = obj.get("type")

                    if evt_type == "assistant":
                        # Check for tool_use in content blocks
                        content = obj.get("message", {}).get("content", [])
                        for block in content:
                            if block.get("type") != "tool_use":
                                continue
                            name = block.get("name", "")
                            inp = block.get("input", {})
                            # TodoWrite → forward full todo list
                            if name == "TodoWrite" and on_todo:
                                try:
                                    await on_todo(inp.get("todos", []))
                                except Exception:
                                    pass
                            # All tool_use → activity label
                            if on_activity:
                                label = _make_tool_label(name, inp)
                                try:
                                    await on_activity(label)
                                except Exception:
                                    pass  # never let callback errors kill the stream

                    elif evt_type == "result":
                        text = obj.get("result", "")
                        new_session_id = obj.get("session_id")
                        cost = obj.get("total_cost_usd") or obj.get("cost_usd") or 0.0

                await proc.wait()

            await asyncio.wait_for(_read_stream(), timeout=timeout)
            duration = int((time.monotonic() - start) * 1000)

            # Collect stderr
            stderr_data = await stderr_task

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

        if not text:
            if stderr_data:
                err = stderr_data.decode("utf-8", errors="replace").strip()
                log.warning("Claude CLI empty result, stderr: %s", err[:500])
                return LLMResult(
                    text=f"[CLI error: {err[:500]}]",
                    duration_ms=duration, is_error=True,
                )
            log.warning("Claude CLI returned empty result (session=%s, model=%s)", session_id, model)
            return LLMResult(text="", duration_ms=duration, is_error=True)

        return LLMResult(
            text=text,
            session_id=new_session_id,
            duration_ms=duration,
            cost_usd=cost,
        )
