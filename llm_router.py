# -*- coding: utf-8 -*-
"""Unified LLM router - dispatches to Claude CLI, Gemini CLI, or Gemini API."""

import time
import logging
from models import LLMConfig, LLMResult
from claude_cli import ClaudeCli
from gemini_cli import GeminiCli
from gemini_api import GeminiAPI
from store import load_json, save_json

log = logging.getLogger("hub.router")

SESSION_TTL = 1800  # 30 min — don't resume stale sessions (saves cost + avoids failures)
HISTORY_ROUNDS = 2  # keep last N rounds as context fallback for new sessions
HISTORY_TRUNCATE = 800  # max chars per message in history


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
        await save_json(self.sessions_path, self._sessions)

    def get_session_id(self, session_key: str) -> str | None:
        entry = self._sessions.get(session_key)
        if not entry or not entry.get("session_id"):
            return None
        # Expire stale sessions — avoids expensive context reads and resume failures
        updated = entry.get("updated_at", 0)
        if time.time() - updated > SESSION_TTL:
            log.info("Session expired for %s (age=%.0fs), starting fresh",
                     session_key, time.time() - updated)
            entry.pop("session_id", None)
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
            history = entry.get("history", [])
            self._sessions[session_key] = {"history": history} if history else {}
        else:
            self._sessions.pop(session_key, None)

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

    def _build_context_prompt(self, session_key: str, prompt: str) -> str:
        """Prepend recent history to prompt when starting a fresh session."""
        entry = self._sessions.get(session_key, {})
        history = entry.get("history", [])
        if not history:
            return prompt
        lines = ["[近期对话上下文]"]
        for msg in history:
            role = "用户" if msg["role"] == "user" else "助手"
            lines.append(f"{role}: {msg['text']}")
        lines.append(f"\n[当前消息]\n{prompt}")
        return "\n".join(lines)

    async def run(
        self,
        prompt: str,
        llm_config: LLMConfig,
        session_key: str | None = None,
        files: list[str] | None = None,
        image_src: str | None = None,
    ) -> LLMResult:
        provider = llm_config.provider
        log.info("Routing to %s/%s (session=%s)", provider, llm_config.model, session_key)

        if provider == "claude-cli":
            session_id = self.get_session_id(session_key) if session_key else None
            # No session to resume — inject history as context
            effective_prompt = prompt
            if not session_id and session_key:
                effective_prompt = self._build_context_prompt(session_key, prompt)
            result = await self.claude.run(
                effective_prompt,
                session_id=session_id,
                model=llm_config.model,
                system_prompt=llm_config.system_prompt,
                timeout_seconds=llm_config.timeout_seconds,
            )
            # Save session + history
            if session_key and not result.is_error:
                if session_key not in self._sessions:
                    self._sessions[session_key] = {}
                if result.session_id:
                    self._sessions[session_key]["session_id"] = result.session_id
                    self._sessions[session_key]["updated_at"] = time.time()
                if result.text:
                    self._append_history(session_key, prompt, result.text)
                await self.save_sessions()
            return result

        elif provider == "gemini-cli":
            result = await self.gemini_cli.run(
                prompt,
                model=llm_config.model,
                system_prompt=llm_config.system_prompt,
                timeout_seconds=llm_config.timeout_seconds,
            )
            return result

        elif provider == "gemini-api":
            result = await self.gemini_api.run(
                prompt,
                system_prompt=llm_config.system_prompt,
                model=llm_config.model,
                thinking=llm_config.thinking,
                temperature=llm_config.temperature,
                timeout_seconds=llm_config.timeout_seconds,
                files=files,
                image_src=image_src,
            )
            return result

        else:
            return LLMResult(
                text=f"[Unknown provider: {provider}]",
                is_error=True,
            )
