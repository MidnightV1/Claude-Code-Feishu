# -*- coding: utf-8 -*-
"""Claude Code CLI subprocess wrapper with streaming progress support."""

import asyncio
import json
import os
import random
import signal
import time
import logging
from pathlib import PurePosixPath
from typing import Awaitable, Callable

from agent.infra.models import LLMResult

log = logging.getLogger("hub.claude_cli")

# ── Personality-driven status words ──
# Inspired by Claude Code's 239 hidden spinner states.
# Each tool type maps to a pool of fun verbs; one is picked at random.

_TOOL_VERBS: dict[str, list[str]] = {
    "Read": [
        "正在考古...", "正在审查屎山...", "正在认真阅读",
        "正在一目十行", "正在偷看源码...", "好长，但我假装看完了",
    ],
    "Grep": [
        "正在掘地三尺", "正在大海捞针", "正在顺藤摸瓜",
        "排查线索中...", "正在查水表",
    ],
    "Glob": [
        "踩点中", "正在翻箱倒柜", "正在翻阅", "到处找人...",
    ],
    "Bash": [
        "搞事中...", "正在摇人", "正在鞭策主机",
        "听我口令...", "按下了不该按的按钮...",
    ],
    "Edit": [
        "雕花中...", "填坑中...", "修改中...",
        "外科手术般精准修改...", "我改了，别慌",
    ],
    "Write": [
        "正在编辑", "创作中...", "正在努力写bug...",
        "码字中...", "正在无中生有...",
    ],
    "Agent": [
        "召唤分身", "正在裂开", "影分身！",
        "分身去打工了", "派出小弟...",
    ],
    "Web": [
        "上网查找", "正在搜索", "网上冲浪中",
        "百度一下（才怪", "正在请教互联网",
    ],
    "mcp": [
        "呼叫外援", "连线场外观众", "正在搬救兵...", "找了个帮手",
    ],
    "Skill": ["上绝活", "放大招", "发动技能", "看招！"],
    "TodoWrite": ["立 flag 中...", "列清单", "写入小本本...", "先给自己画个饼"],
}

_FALLBACK_VERBS = ["搞事情中...", "施法中...", "炼丹中...", "整活中...", "正在变形..."]


_TOOL_ICONS: dict[str, str] = {
    "Read": "📖", "Grep": "🔍", "Glob": "📂", "Bash": "⚡",
    "Edit": "✏️", "Write": "📝", "Agent": "🤖", "Web": "🌐",
    "mcp": "🔌", "Skill": "🎯", "TodoWrite": "📋",
}
_FALLBACK_ICON = "🔧"

# Env vars to strip when spawning nested CLI (prevents "nested session" error)
_CC_ENV_STRIP = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"}


def _pick_verb(category: str) -> str:
    """Pick a random personality verb for a tool category."""
    pool = _TOOL_VERBS.get(category, _FALLBACK_VERBS)
    return random.choice(pool)


def _icon(category: str) -> str:
    return _TOOL_ICONS.get(category, _FALLBACK_ICON)


def _make_tool_label(tool_name: str, tool_input: dict) -> str:
    """Map a tool_use event to a personality-driven progress label."""
    if tool_name == "Read":
        path = tool_input.get("file_path", "")
        name = PurePosixPath(path).name if path else ""
        verb = _pick_verb("Read")
        return f"{_icon('Read')} {verb} {name}" if name else f"{_icon('Read')} {verb}"

    if tool_name == "Grep":
        pat = tool_input.get("pattern", "")
        verb = _pick_verb("Grep")
        i = _icon("Grep")
        return f"{i} {verb}「{pat[:20]}」" if pat else f"{i} {verb}"

    if tool_name == "Glob":
        pat = tool_input.get("pattern", "")
        verb = _pick_verb("Glob")
        i = _icon("Glob")
        return f"{i} {verb} {pat[:20]}" if pat else f"{i} {verb}"

    if tool_name == "Bash":
        desc = tool_input.get("description", "")
        verb = _pick_verb("Bash")
        i = _icon("Bash")
        return f"{i} {verb} {desc[:30]}" if desc else f"{i} {verb}"

    if tool_name in ("Edit", "Write"):
        path = tool_input.get("file_path", "")
        name = PurePosixPath(path).name if path else ""
        verb = _pick_verb(tool_name)
        i = _icon(tool_name)
        return f"{i} {verb} {name}" if name else f"{i} {verb}"

    if tool_name == "Agent":
        desc = tool_input.get("description", "")
        verb = _pick_verb("Agent")
        i = _icon("Agent")
        return f"{i} {verb} {desc[:30]}" if desc else f"{i} {verb}"

    if tool_name in ("WebFetch", "WebSearch"):
        query = tool_input.get("query", tool_input.get("prompt", ""))
        verb = _pick_verb("Web")
        i = _icon("Web")
        return f"{i} {verb}「{query[:20]}」" if query else f"{i} {verb}"

    if tool_name == "Skill":
        return f"{_icon('Skill')} {_pick_verb('Skill')}"

    if tool_name == "TodoWrite":
        return f"{_icon('TodoWrite')} {_pick_verb('TodoWrite')}"

    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        server = parts[1] if len(parts) > 1 else ""
        verb = _pick_verb("mcp")
        i = _icon("mcp")
        return f"{i} {verb} {server}" if server else f"{i} {verb}"

    return f"{_FALLBACK_ICON} {random.choice(_FALLBACK_VERBS)}"


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
    async def _kill_tree(proc):
        """Kill process and all children (process group). Prevents orphan subprocesses.

        SIGTERM first, wait up to 2s for graceful exit, then SIGKILL if still alive.
        """
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        # Wait briefly for graceful exit
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
            return  # exited cleanly
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        # Force kill
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except (asyncio.TimeoutError, ProcessLookupError):
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
        env = {k: v for k, v in os.environ.items() if k not in _CC_ENV_STRIP}

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
            await self._kill_tree(proc)
            stderr_task.cancel()
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
            await self._kill_tree(proc)
            stderr_task.cancel()
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
