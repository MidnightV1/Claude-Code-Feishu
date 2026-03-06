# -*- coding: utf-8 -*-
"""Unified LLM router - dispatches to Claude CLI, Gemini CLI, or Gemini API.

Context strategy for Claude CLI sessions:
- Always try --resume if session_id exists (no TTL gating)
- Resume failed → retry with history injected via system prompt (not user prompt)
- History compression: Sonnet (primary) → Gemini 3-Flash (fallback) → raw text
- Router owns retry logic; claude_cli.py is a simple executor
"""

import asyncio
import time
import logging
from typing import Awaitable, Callable
from agent.infra.models import LLMConfig, LLMResult
from agent.llm.claude import ClaudeCli
from agent.llm.gemini_cli import GeminiCli
from agent.llm.gemini_api import GeminiAPI
from agent.infra.store import load_json, save_json, update_json_key

log = logging.getLogger("hub.router")

HISTORY_ROUNDS = 15      # keep last N rounds for context fallback
HISTORY_TRUNCATE = 4000  # max chars per message in history
SUMMARY_THRESHOLD = 5    # keep this many recent rounds as raw text, compress older

COMPRESS_TIMEOUT_PRIMARY = 60
COMPRESS_TIMEOUT_FALLBACK = 30
COMPRESS_TEMPERATURE = 0.2
LOG_PREVIEW_LEN = 200
TRANSIENT_RETRY_MAX = 3
TRANSIENT_RETRY_DELAY = 2  # seconds (exponential: 2, 4, 8)

# ═══ Context Recovery Templates ═══

RECOVERY_PREAMBLE = (
    "## 会话恢复\n\n"
    "你的上一个 CLI session 已结束，以下是之前对话的压缩上下文。\n"
    "注意：之前的工具调用记录（文件读写、命令执行）不可访问。"
    "如需操作之前涉及的文件，请重新读取确认当前状态。\n"
)

SUMMARY_PROMPT = (
    "将以下 Claude Code 飞书对话历史压缩为结构化摘要。\n\n"
    "## 输出格式\n\n"
    "### 对话主题\n一句话概括\n\n"
    "### 关键决策与理由\n"
    "- 决策内容（保留文件路径、配置值、技术选型）\n"
    "- 决策理由和排除的备选方案\n\n"
    "### 当前状态\n"
    "- 已完成：...\n"
    "- 进行中：...\n"
    "- 待确认：...\n\n"
    "### 涉及的文件与变更\n- 文件路径 + 改了什么方面\n\n"
    "### 用户偏好与纠正\n- 用户明确表达的偏好、约束、对助手的纠正（如有）\n\n"
    "## 要求\n"
    "- 保持精炼，但不限字数——关键信息和决策逻辑的完整性优先\n"
    "- 保留具体文件路径、命令、配置值——恢复后需要\n"
    "- 丢弃寒暄、重复、已否决方案的细节（仅记录「排除了 X，因为 Y」）\n"
    "- 代码修改只记录改了哪些文件的什么方面，不记录代码本身\n\n"
    "对话历史：\n{raw}"
)

SUMMARY_SYSTEM = "你是对话压缩器。严格按格式输出结构化摘要，不添加额外解释。"


class LLMRouter:
    def __init__(
        self,
        claude: ClaudeCli,
        gemini_cli: GeminiCli,
        gemini_api: GeminiAPI,
        sessions_path: str = "data/sessions.json",
    ):
        self.claude = claude
        self.gemini_cli = gemini_cli
        self.gemini_api = gemini_api
        self.sessions_path = sessions_path
        self._sessions: dict = {}   # session_key -> {session_id, ...}

    async def load_sessions(self):
        self._sessions = await load_json(self.sessions_path, {})

    async def save_sessions(self):
        """Full dict save — only used for bulk operations (e.g. initial load rewrite)."""
        await save_json(self.sessions_path, self._sessions)

    async def save_session(self, session_key: str):
        """Per-key atomic save — safe under concurrent writes from different keys."""
        entry = self._sessions.get(session_key)
        if entry is not None:
            await update_json_key(self.sessions_path, session_key, entry)

    def get_session_id(self, session_key: str) -> str | None:
        """Get session_id for resume. No TTL — always try resume, let CLI handle failures."""
        entry = self._sessions.get(session_key)
        if not entry or not entry.get("session_id"):
            return None
        return entry["session_id"]

    def get_session_llm(self, session_key: str) -> dict | None:
        """Get per-conversation LLM config override."""
        entry = self._sessions.get(session_key)
        return entry.get("llm_config") if entry else None

    def set_session_llm(self, session_key: str, llm_config: dict):
        """Set per-conversation LLM config override."""
        if session_key not in self._sessions:
            self._sessions[session_key] = {}
        self._sessions[session_key]["llm_config"] = llm_config

    def clear_session(self, session_key: str):
        """Clear session but preserve history for context carryover."""
        entry = self._sessions.get(session_key)
        if entry:
            preserved = {}
            if entry.get("history"):
                preserved["history"] = entry["history"]
            if entry.get("llm_config"):
                preserved["llm_config"] = entry["llm_config"]
            self._sessions[session_key] = preserved if preserved else {}
        else:
            self._sessions.pop(session_key, None)

    # ═══ History Management ═══

    def remove_last_round(self, session_key: str):
        """Remove the last user+assistant round from history (e.g., on message recall)."""
        entry = self._sessions.get(session_key, {})
        history = entry.get("history", [])
        if len(history) >= 2 and history[-1]["role"] == "assistant" and history[-2]["role"] == "user":
            history.pop()  # assistant
            history.pop()  # user
            log.info("Recall: removed last history round for %s", session_key)

    def _append_history(self, session_key: str, user_msg: str, assistant_msg: str):
        """Append a round to session history, keeping last N rounds."""
        if not session_key:
            return
        entry = self._sessions.setdefault(session_key, {})
        history = entry.setdefault("history", [])
        # Truncate long messages
        if len(user_msg) > HISTORY_TRUNCATE:
            user_msg = user_msg[:HISTORY_TRUNCATE] + "..."
        if len(assistant_msg) > HISTORY_TRUNCATE:
            assistant_msg = assistant_msg[:HISTORY_TRUNCATE] + "..."
        history.append({"role": "user", "text": user_msg})
        history.append({"role": "assistant", "text": assistant_msg})
        # Keep last N rounds (2 messages per round)
        max_msgs = HISTORY_ROUNDS * 2
        if len(history) > max_msgs:
            entry["history"] = history[-max_msgs:]

    def _format_raw_history(self, history: list[dict]) -> str:
        """Format history messages as raw text lines."""
        lines = []
        for msg in history:
            role = "用户" if msg["role"] == "user" else "助手"
            lines.append(f"{role}: {msg['text']}")
        return "\n".join(lines)

    # ═══ Context Recovery ═══

    async def _compress_history(self, history: list[dict]) -> str | None:
        """Compress conversation history. Sonnet primary, Gemini 3-Flash fallback."""
        raw = self._format_raw_history(history)

        prompt = SUMMARY_PROMPT.format(raw=raw)

        # Primary: Sonnet — understands CC conversation patterns
        try:
            result = await self.claude.run(
                prompt, model="sonnet",
                system_prompt=SUMMARY_SYSTEM,
                timeout_seconds=COMPRESS_TIMEOUT_PRIMARY,
            )
            if not result.is_error and result.text.strip():
                log.info("History compressed via Sonnet: %d → %d chars",
                         len(raw), len(result.text))
                return result.text.strip()
            log.warning("Sonnet compression failed: %s", result.text[:LOG_PREVIEW_LEN])
        except Exception as e:
            log.warning("Sonnet compression error: %s", e)

        # Fallback: Gemini 3-Flash
        try:
            result = await self.gemini_api.run(
                prompt, model="3-Flash",
                temperature=COMPRESS_TEMPERATURE,
                timeout_seconds=COMPRESS_TIMEOUT_FALLBACK,
            )
            if not result.is_error and result.text.strip():
                log.info("History compressed via Gemini fallback: %d → %d chars",
                         len(raw), len(result.text))
                return result.text.strip()
        except Exception as e:
            log.warning("Gemini compression fallback error: %s", e)

        return None

    async def _build_recovery_context(self, session_key: str) -> str | None:
        """Build recovery context for system prompt injection when session is lost.

        Returns context string for --append-system-prompt, or None if no history.
        Strategy: recent rounds raw + older rounds compressed.
        """
        entry = self._sessions.get(session_key, {})
        history = entry.get("history", [])
        if not history:
            return None

        rounds = len(history) // 2
        recent_msgs = SUMMARY_THRESHOLD * 2

        parts = [RECOVERY_PREAMBLE]

        if rounds > SUMMARY_THRESHOLD:
            older = history[:-recent_msgs]
            recent = history[-recent_msgs:]
            summary = await self._compress_history(older)
            if summary:
                parts.append(f"### 早期对话摘要\n{summary}")
            else:
                # Compression failed — use raw for everything
                parts.append(f"### 对话历史\n{self._format_raw_history(history)}")
                return "\n\n".join(parts)
            parts.append(f"### 近期对话\n{self._format_raw_history(recent)}")
        else:
            parts.append(f"### 对话历史\n{self._format_raw_history(history)}")

        return "\n\n".join(parts)

    def _save_result(self, session_key: str, result: LLMResult, prompt: str):
        """Save session_id and history after a successful call."""
        if not session_key or result.is_error:
            return
        if session_key not in self._sessions:
            self._sessions[session_key] = {}
        if result.session_id:
            self._sessions[session_key]["session_id"] = result.session_id
            self._sessions[session_key]["updated_at"] = time.time()
        if result.text:
            self._append_history(session_key, prompt, result.text)

    @staticmethod
    def _is_transient(result: LLMResult) -> bool:
        """Check if a CLI error is transient and worth retrying."""
        if not result.is_error:
            return False
        t = result.text
        # ld.so dynamic linker crash
        if "ld.so" in t or "dl-open.c" in t:
            return True
        # Generic empty-result crash (no stderr info)
        if t == "":
            return True
        return False

    # ═══ Routing ═══

    async def run(
        self,
        prompt: str,
        llm_config: LLMConfig,
        session_key: str | None = None,
        files: list[str] | None = None,
        image_src: str | None = None,
        on_activity: Callable[[str], Awaitable[None]] | None = None,
        on_todo: Callable[[list[dict]], Awaitable[None]] | None = None,
    ) -> LLMResult:
        provider = llm_config.provider
        log.info("Routing to %s/%s (session=%s)", provider, llm_config.model, session_key)

        if provider == "claude-cli":
            return await self._run_claude(prompt, llm_config, session_key, on_activity, on_todo)

        elif provider == "gemini-cli":
            return await self.gemini_cli.run(
                prompt,
                model=llm_config.model,
                system_prompt=llm_config.system_prompt,
                timeout_seconds=llm_config.timeout_seconds,
            )

        elif provider == "gemini-api":
            return await self.gemini_api.run(
                prompt,
                system_prompt=llm_config.system_prompt,
                model=llm_config.model,
                thinking=llm_config.thinking,
                temperature=llm_config.temperature,
                timeout_seconds=llm_config.timeout_seconds,
                files=files,
                image_src=image_src,
            )

        else:
            return LLMResult(
                text=f"[Unknown provider: {provider}]",
                is_error=True,
            )

    async def _run_claude(
        self, prompt: str, llm_config: LLMConfig, session_key: str | None,
        on_activity: Callable[[str], Awaitable[None]] | None = None,
        on_todo: Callable[[list[dict]], Awaitable[None]] | None = None,
    ) -> LLMResult:
        """Claude CLI routing with resume → retry-with-context strategy.

        1. If session_id exists → try --resume
        2. If resume fails → retry as fresh session with history via system prompt
        3. If no session_id → start fresh with history via system prompt
        """
        session_id = self.get_session_id(session_key) if session_key else None

        # ── Step 1: Try resume ──
        if session_id:
            result = await self.claude.run(
                prompt,
                session_id=session_id,
                model=llm_config.model,
                system_prompt=llm_config.system_prompt,
                timeout_seconds=llm_config.timeout_seconds,
                effort=llm_config.effort,
                on_activity=on_activity,
                on_todo=on_todo,
            )
            if result.cancelled:
                return result
            if not result.is_error:
                self._save_result(session_key, result, prompt)
                try:
                    await self.save_session(session_key)
                except Exception:
                    log.warning("Failed to persist session", exc_info=True)
                return result
            # Resume failed — fall through to fresh session with recovery context
            log.warning("Resume failed for %s: %s", session_key, result.text[:LOG_PREVIEW_LEN])

        # ── Step 2: Fresh session with recovery context via system prompt ──
        effective_system = llm_config.system_prompt
        if session_key:
            recovery = await self._build_recovery_context(session_key)
            if recovery:
                parts = []
                if effective_system:
                    parts.append(effective_system)
                parts.append(recovery)
                effective_system = "\n\n".join(parts)

        # Retry loop for transient errors (e.g. ld.so crash)
        result = None
        for attempt in range(1 + TRANSIENT_RETRY_MAX):
            result = await self.claude.run(
                prompt,
                session_id=None,
                model=llm_config.model,
                system_prompt=effective_system,
                timeout_seconds=llm_config.timeout_seconds,
                effort=llm_config.effort,
                on_activity=on_activity,
                on_todo=on_todo,
            )
            if result.cancelled:
                return result
            if not result.is_error or not self._is_transient(result):
                break
            if attempt < TRANSIENT_RETRY_MAX:
                delay = TRANSIENT_RETRY_DELAY * (2 ** attempt)
                log.warning("Transient CLI error (attempt %d/%d), retrying in %ds: %s",
                            attempt + 1, TRANSIENT_RETRY_MAX + 1,
                            delay, result.text[:LOG_PREVIEW_LEN])
                await asyncio.sleep(delay)

        self._save_result(session_key, result, prompt)
        try:
            await self.save_session(session_key)
        except Exception:
            log.warning("Failed to persist session", exc_info=True)
        return result
