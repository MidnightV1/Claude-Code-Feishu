# -*- coding: utf-8 -*-
"""Claude Code CLI subprocess wrapper with streaming progress support."""

import asyncio
import json
import os
import signal
import time
import logging
from pathlib import PurePosixPath
from typing import Awaitable, Callable

from agent.infra.models import LLMResult

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
        # Idle timeout: kill only when no stream output for this long
        self.idle_timeout = config.get("idle_timeout_seconds", 600)
        # Hard cap: absolute maximum regardless of activity
        self.max_timeout = config.get("max_timeout_seconds", 1800)
        self.workspace_dir = os.path.expanduser(
            config.get("workspace_dir", ".")
        )

    @staticmethod
    def _kill_tree(proc):
        """Kill process and all children (process group). Prevents orphan subprocesses."""
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

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
        setting_sources: str | None = None,
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
            setting_sources=setting_sources,
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
        setting_sources: str | None = None,
    ) -> LLMResult:
        # Timeout strategy:
        # - Explicit timeout_seconds (heartbeat, compression) → absolute timeout (old behavior)
        # - No explicit timeout (chat) → idle-based: kill if no output for idle_timeout,
        #   hard cap at max_timeout
        use_idle_timeout = timeout_seconds is None
        idle_timeout = self.idle_timeout if use_idle_timeout else (timeout_seconds or self.default_timeout)
        hard_cap = self.max_timeout if use_idle_timeout else (timeout_seconds or self.default_timeout)

        args = [
            self.path, "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        # Always set --model explicitly to avoid fallback collision with CLI default
        effective_model = model or "opus"
        args.extend(["--model", effective_model])
        # fallback-model must differ from main model
        fallback = "sonnet" if effective_model == "opus" else "opus"
        args.extend(["--fallback-model", fallback])
        if session_id:
            args.extend(["--resume", session_id])
        if system_prompt:
            args.extend(["--append-system-prompt", system_prompt])
        if effort:
            args.extend(["--effort", effort])
        if setting_sources:
            args.extend(["--setting-sources", setting_sources])

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
                limit=8 * 1024 * 1024,  # 8MB; CC result events can be very large
                cwd=self.workspace_dir,
                env=env,
                start_new_session=True,  # own process group so we can kill children on timeout
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

            hard_deadline = time.monotonic() + hard_cap

            async def _read_stream():
                nonlocal text, new_session_id, cost
                while True:
                    remaining = hard_deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    try:
                        raw_line = await asyncio.wait_for(
                            proc.stdout.readline(),
                            timeout=min(idle_timeout, remaining),
                        )
                    except asyncio.TimeoutError:
                        raise  # idle or hard cap exceeded
                    except ValueError:
                        # "Separator is found, but chunk is longer than limit"
                        log.warning("Stream line exceeded buffer limit, skipping")
                        continue
                    if not raw_line:
                        break  # EOF
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

            await _read_stream()
            duration = int((time.monotonic() - start) * 1000)

            # Collect stderr
            stderr_data = await stderr_task

        except asyncio.TimeoutError:
            duration = int((time.monotonic() - start) * 1000)
            elapsed = int(duration / 1000)
            self._kill_tree(proc)
            if elapsed >= self.max_timeout - 5:
                reason = f"hard cap {self.max_timeout}s"
            else:
                reason = f"idle {idle_timeout}s"
            log.warning("Claude CLI timed out (%s, elapsed %ds)", reason, elapsed)
            return LLMResult(
                text=f"[Timeout: {reason}, elapsed {elapsed}s]",
                duration_ms=duration, is_error=True,
            )
        except asyncio.CancelledError:
            duration = int((time.monotonic() - start) * 1000)
            self._kill_tree(proc)
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
