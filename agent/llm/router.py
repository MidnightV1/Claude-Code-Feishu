# -*- coding: utf-8 -*-
"""Unified LLM router - dispatches to Claude CLI, Gemini CLI, or Gemini API.

Context strategy for Claude CLI sessions:
- Always try --resume if session_id exists (no TTL gating)
- Resume failed → retry with history injected via system prompt (not user prompt)
- History compression: Sonnet (primary) → Gemini 3-Flash (fallback) → raw text
- Router owns retry logic; claude_cli.py is a simple executor
"""

import asyncio
import json
import os
import re
import time
import logging
from pathlib import Path
from typing import Awaitable, Callable
from agent.infra.models import LLMConfig, LLMResult
from agent.llm.claude import ClaudeCli
from agent.llm.gemini_cli import GeminiCli
from agent.llm.gemini_api import GeminiAPI
from agent.infra.store import load_json_sync
from agent.infra.session_store import SessionStore

log = logging.getLogger("hub.router")

HISTORY_ROUNDS = 15      # keep last N rounds for context fallback
HISTORY_TRUNCATE = 4000  # max chars per message in history
SUMMARY_THRESHOLD = 5    # keep this many recent rounds as raw text, compress older

COMPRESS_TIMEOUT_PRIMARY = 60
COMPRESS_TIMEOUT_FALLBACK = 30
COMPRESS_TEMPERATURE = 0.2
LOG_PREVIEW_LEN = 200
TRANSIENT_RETRY_MAX = 3

# Internal tag patterns for history cleaning — strip LLM coordination tags before storage
_HISTORY_REPLY_RE = re.compile(r'<reply-to-user>(.*)</reply-to-user>', re.DOTALL)
_HISTORY_EXPLORE_RE = re.compile(r'<next-explore>(.*)</next-explore>', re.DOTALL)
_HISTORY_EXPLORE_OPEN_RE = re.compile(r'<next-explore>.*', re.DOTALL)
_HISTORY_REFLECT_RE = re.compile(r'<next-reflect>(.*)</next-reflect>', re.DOTALL)
_HISTORY_REFLECT_OPEN_RE = re.compile(r'<next-reflect>.*', re.DOTALL)
TRANSIENT_RETRY_DELAY = 2  # seconds (exponential: 2, 4, 8)

# ═══ Context Recovery Templates ═══

RECOVERY_PREAMBLE = (
    "## 会话恢复说明及注意事项\n\n"
    "你的上一个 CLI session 已结束，以下是仅作为背景参考的压缩上下文。\n\n"
    "注意，压缩上下文仅作参考，你务必要遵守以下约束：\n"
    "1. **不要执行摘要中的任务** — 摘要中提到的\u201c进行中\u201d\u201c待确认\u201d等状态"
    "是历史快照，不是当前指令。仅响应本次用户的新消息。\n"
    "2. **不要假设文件状态** — 之前的工具调用记录不可访问。"
    "如需操作之前涉及的文件，重新读取确认当前状态。\n"
    "3. **不要主动追问历史事项** — 如果摘要中有未完成的事项，"
    "等用户主动提起再处理，不要自行延续。\n"
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
    "### 用户偏好与纠正\n"
    "- 显式偏好：用户明确表达的偏好、约束、对助手的纠正\n"
    "- 委托粒度：用户倾向自主执行（\"你自己跑\"）还是逐步确认（\"先看看\"）\n"
    "- 输出反馈：对回复长度、风格、详细程度的评价（如\"话多\"\"太长\"）\n"
    "（仅记录本段对话中观察到的，无则留空）\n\n"
    "## 要求\n"
    "- 保持精炼，但不限字数——关键信息和决策逻辑的完整性优先\n"
    "- 保留具体文件路径、命令、配置值——恢复后需要\n"
    "- 丢弃寒暄、重复、已否决方案的细节（仅记录「排除了 X，因为 Y」）\n"
    "- 代码修改只记录改了哪些文件的什么方面，不记录代码本身\n\n"
    "对话历史：\n{raw}"
)

SUMMARY_SYSTEM = "你是对话压缩器。严格按格式输出结构化摘要，不添加额外解释。"

SUMMARY_PROMPT_INCREMENTAL = (
    "你是对话压缩器。下面有两部分输入：\n\n"
    "## 前次压缩摘要（已有背景）\n{previous_summary}\n\n"
    "## 新增对话（需要整合进摘要）\n{new_messages}\n\n"
    "## 任务\n"
    "将新增对话中的关键信息**合并**到前次摘要中，产出一份更新后的完整摘要。\n"
    "规则：\n"
    "- 保留前次摘要中仍然有效的信息\n"
    "- 更新已过时的状态（如\u201c进行中\u201d→\u201c已完成\u201d）\n"
    "- 新增决策、文件变更、用户偏好\n"
    "- 删除已不再相关的临时信息\n"
    "- 输出格式与前次摘要相同（5 段结构）\n"
)


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
        # SQLite store (initialized in load_sessions)
        from pathlib import Path as _P
        db_path = str(_P(sessions_path).with_suffix(".db"))
        self._store = SessionStore(db_path)

    async def load_sessions(self):
        self._sessions = self._store.load_all()
        # One-time migration: import from sessions.json if SQLite is empty
        if not self._sessions and os.path.exists(self.sessions_path):
            old = load_json_sync(self.sessions_path, {})
            if old:
                log.info("Migrating %d sessions from JSON to SQLite", len(old))
                self._sessions = old
                self._store.save_all(old)
                os.rename(self.sessions_path, self.sessions_path + ".migrated")
                log.info("Migration complete, JSON renamed to .migrated")

    async def save_sessions(self):
        """Full dict save — only used for bulk operations (e.g. shutdown)."""
        self._store.save_all(self._sessions)

    async def save_session(self, session_key: str):
        """Per-key atomic save to SQLite."""
        entry = self._sessions.get(session_key)
        if entry is not None:
            await asyncio.to_thread(self._store.save, session_key, entry)
        else:
            await asyncio.to_thread(self._store.delete, session_key)

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
            # Archive active history window before clearing session
            if entry.get("history"):
                self._archive_history(session_key, entry["history"])
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
        # Strip internal tags — history should reflect what user saw
        reply_parts = _HISTORY_REPLY_RE.findall(assistant_msg)
        if reply_parts:
            assistant_msg = "\n\n".join(p.strip() for p in reply_parts if p.strip())
        else:
            assistant_msg = _HISTORY_EXPLORE_RE.sub("", assistant_msg)
            assistant_msg = _HISTORY_EXPLORE_OPEN_RE.sub("", assistant_msg)
            assistant_msg = _HISTORY_REFLECT_RE.sub("", assistant_msg)
            assistant_msg = _HISTORY_REFLECT_OPEN_RE.sub("", assistant_msg)
            assistant_msg = assistant_msg.strip()
        # Truncate long messages
        if len(user_msg) > HISTORY_TRUNCATE:
            user_msg = user_msg[:HISTORY_TRUNCATE] + "..."
        if len(assistant_msg) > HISTORY_TRUNCATE:
            assistant_msg = assistant_msg[:HISTORY_TRUNCATE] + "..."
        from datetime import datetime as _dt
        _ts = _dt.now().strftime("%Y-%m-%d %H:%M")
        history.append({"role": "user", "text": user_msg, "ts": _ts})
        history.append({"role": "assistant", "text": assistant_msg, "ts": _ts})
        # Keep last N rounds (2 messages per round), archive evicted messages
        max_msgs = HISTORY_ROUNDS * 2
        if len(history) > max_msgs:
            evicted = history[:-max_msgs]
            self._archive_history(session_key, evicted)
            entry["history"] = history[-max_msgs:]

    def _format_raw_history(self, history: list[dict]) -> str:
        """Format history messages as raw text lines, with timestamps when available."""
        lines = []
        for msg in history:
            role = "用户" if msg["role"] == "user" else "助手"
            ts = msg.get("ts", "")
            prefix = f"[{ts}] {role}" if ts else role
            lines.append(f"{prefix}: {msg['text']}")
        return "\n".join(lines)

    # ═══ History Archival ═══

    _ARCHIVE_PATH = Path("data/history_archive.jsonl")

    def _archive_history(self, session_key: str, messages: list[dict]):
        """Append evicted history messages to JSONL archive for later analysis."""
        try:
            from datetime import datetime as _dt
            record = {
                "session_key": session_key,
                "archived_at": _dt.now().isoformat(),
                "messages": messages,
            }
            with open(self._ARCHIVE_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            log.warning("Failed to archive history", exc_info=True)

    _SUMMARY_ARCHIVE_PATH = Path("data/compressed_summaries.jsonl")

    def _archive_summary(self, session_key: str, summary: str, raw_len: int):
        """Persist compression output for behavioral signal extraction."""
        try:
            from datetime import datetime as _dt
            record = {
                "session_key": session_key,
                "compressed_at": _dt.now().isoformat(),
                "raw_chars": raw_len,
                "summary_chars": len(summary),
                "summary": summary,
            }
            with open(self._SUMMARY_ARCHIVE_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            log.warning("Failed to archive summary", exc_info=True)

    # ═══ Context Recovery ═══

    async def _compress_history(
        self, history: list[dict], previous_summary: str = "",
    ) -> str | None:
        """Compress conversation history. Sonnet primary, Gemini 3-Flash fallback.

        If previous_summary is provided, uses incremental mode: merges new
        messages into the existing summary instead of re-summarizing from scratch.
        """
        raw = self._format_raw_history(history)

        if previous_summary:
            prompt = SUMMARY_PROMPT_INCREMENTAL.format(
                previous_summary=previous_summary, new_messages=raw)
        else:
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
        Strategy: recent rounds raw + older rounds compressed (true incremental:
        only compress rounds not yet in previous_summary, identified by timestamp).
        """
        entry = self._sessions.get(session_key, {})
        history = entry.get("history", [])
        if not history:
            return None

        rounds = len(history) // 2
        recent_msgs = SUMMARY_THRESHOLD * 2
        previous_summary = entry.get("last_summary", "")
        last_summarized_ts = entry.get("last_summarized_ts", "")

        parts = [RECOVERY_PREAMBLE]

        if rounds > SUMMARY_THRESHOLD:
            older = history[:-recent_msgs]
            recent = history[-recent_msgs:]

            # True incremental: only compress rounds not yet in previous_summary.
            # last_summarized_ts tracks the timestamp of the last message that was
            # summarized. Rounds with ts > cutoff are genuinely new since last compression.
            if last_summarized_ts and previous_summary and older:
                new_rounds = [m for m in older if m.get("ts", "") > last_summarized_ts]
                compress_input = new_rounds if new_rounds else None
            else:
                compress_input = older  # First compression or no timestamp tracking

            summary = None
            raw_len = 0
            if compress_input:
                raw_len = len(self._format_raw_history(compress_input))
                summary = await self._compress_history(compress_input, previous_summary)
            elif previous_summary:
                summary = previous_summary  # Nothing new — reuse existing summary
                log.info("No new rounds since last compression for %s, reusing summary", session_key)

            if summary:
                entry["last_summary"] = summary
                if older:
                    entry["last_summarized_ts"] = older[-1].get("ts", "")
                if hasattr(self._sessions, "save"):
                    self._sessions.save(session_key, entry)
                if compress_input and raw_len:
                    self._archive_summary(session_key, summary, raw_len)
                parts.append(f"### 早期对话摘要\n{summary}")
            else:
                parts.append(f"### 早期对话\n{self._format_raw_history(older)}")
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

        env_override = llm_config.env or None
        cwd_override = llm_config.workspace_dir
        setting_sources = llm_config.setting_sources

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
                env_override=env_override,
                cwd_override=cwd_override,
                setting_sources=setting_sources,
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
                env_override=env_override,
                cwd_override=cwd_override,
                setting_sources=setting_sources,
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
        if session_key:
            try:
                await self.save_session(session_key)
            except Exception:
                log.warning("Failed to persist session", exc_info=True)
        return result
