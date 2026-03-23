# -*- coding: utf-8 -*-
"""Feishu outbound message dispatcher."""

import re
import json
import asyncio
import logging
from typing import TypeVar, Callable, Awaitable

log = logging.getLogger("hub.dispatcher")

MAX_MSG_LEN = 4000  # Feishu card markdown content limit per chunk
MAX_RETRIES = 3

# ── Card header directive ──
# CC can prefix output with {{card:header=...,color=...}} to add a card header.
# Parsed and stripped before sending; backward compatible (no directive = plain card).
_CARD_DIRECTIVE_RE = re.compile(
    r'^\s*\{\{card:([^}]+)\}\}\s*\n?', re.IGNORECASE
)


def _parse_card_directive(text: str) -> tuple[str, str | None, str | None]:
    """Extract card header directive from text start.

    Returns (remaining_text, header, color). If no directive, header/color are None.
    """
    m = _CARD_DIRECTIVE_RE.match(text)
    if not m:
        return text, None, None
    params_str = m.group(1)
    remaining = text[m.end():]
    header = None
    color = None
    for part in params_str.split(","):
        part = part.strip()
        if part.startswith("header="):
            header = part[7:].strip()
        elif part.startswith("color="):
            color = part[6:].strip()
    return remaining, header, color

# ── Secret scanning ─────────────────────────────────────
_SECRET_PATTERNS = [
    re.compile(r'sk-ant-api03-[a-zA-Z0-9\-_]{20,}'),          # Anthropic
    re.compile(r'sk-[a-zA-Z0-9]{20,}'),                        # OpenAI
    re.compile(r'ghp_[a-zA-Z0-9]{10,}'),                       # GitHub PAT
    re.compile(r'gho_[a-zA-Z0-9]{10,}'),                       # GitHub OAuth
    re.compile(r'xox[baprs]-[a-zA-Z0-9\-]{10,}'),              # Slack
    re.compile(r'AIza[a-zA-Z0-9\-_]{30,}'),                    # Google API key
    re.compile(r'-----BEGIN [A-Z]+ PRIVATE KEY-----'),          # Private keys
    re.compile(r'AKIA[A-Z0-9]{16}'),                            # AWS access key
]


def _contains_secret(text: str) -> str | None:
    """Check text for known secret patterns. Returns matched pattern name or None."""
    for pat in _SECRET_PATTERNS:
        if pat.search(text):
            return pat.pattern[:30]
    return None

T = TypeVar("T")


class Dispatcher:
    def __init__(self, config: dict):
        self.app_id = config.get("app_id", "")
        self.app_secret = config.get("app_secret", "")
        self.domain = config.get("domain", "https://open.feishu.cn")
        self.delivery_chat_id = config.get("delivery_chat_id", "")
        self._client = None

    async def start(self):
        import lark_oapi as lark
        domain = self.domain
        if "larksuite" in domain:
            lark_domain = lark.LARK_DOMAIN
        else:
            lark_domain = lark.FEISHU_DOMAIN
        self._client = lark.Client.builder() \
            .app_id(self.app_id) \
            .app_secret(self.app_secret) \
            .domain(lark_domain) \
            .build()
        log.info("Feishu dispatcher started (app_id=%s)", self.app_id[:8])

    async def stop(self):
        self._client = None

    def _ensure_client(self) -> None:
        """Raise RuntimeError if start() has not been called."""
        if self._client is None:
            raise RuntimeError(
                "Dispatcher not started — call await dispatcher.start() first"
            )

    # ── Card header color presets ──
    # Maps semantic levels to Feishu card template colors.
    CARD_COLORS = {
        "blue": "blue",          # 信息/常规
        "wathet": "wathet",      # 浅蓝/通知
        "green": "green",        # 成功/完成
        "turquoise": "turquoise",  # 浅绿
        "yellow": "yellow",      # 警告/注意
        "orange": "orange",      # 重要
        "red": "red",            # 危险/错误
        "carmine": "carmine",    # 深红
        "violet": "violet",      # 紫色
        "purple": "purple",      # 紫色
        "indigo": "indigo",      # 靛蓝
        "grey": "grey",          # 灰色/次要
        "default": "default",    # 无色
    }

    @staticmethod
    def _build_card_json(text: str, *, header: str | None = None,
                         color: str | None = None) -> str:
        """Build Feishu interactive card JSON 2.0 with markdown content.

        Args:
            text: Markdown content.
            header: Optional card header title.
            color: Optional header color template (blue/green/yellow/red/...).
        """
        card: dict = {
            "schema": "2.0",
            "body": {
                "elements": [
                    {"tag": "markdown", "content": text}
                ]
            }
        }
        if header or color:
            card["header"] = {}
            if header:
                card["header"]["title"] = {
                    "tag": "plain_text", "content": header
                }
            if color:
                card["header"]["template"] = color
        return json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_interactive_card(elements: list[dict], *,
                               header: str | None = None,
                               color: str | None = None) -> str:
        """Build a card with arbitrary elements (markdown + buttons + etc).

        Args:
            elements: List of card element dicts (markdown, action, etc.).
            header: Optional header title.
            color: Optional header color template.
        """
        card: dict = {
            "schema": "2.0",
            "body": {"elements": elements}
        }
        if header or color:
            card["header"] = {}
            if header:
                card["header"]["title"] = {
                    "tag": "plain_text", "content": header
                }
            if color:
                card["header"]["template"] = color
        return json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_button_group(buttons: list[dict],
                           layout: str = "bisected") -> dict:
        """Build a column_set element with buttons (JSON 2.0 compatible).

        Args:
            buttons: List of dicts with keys:
                text (str), value (dict), type (str: primary/default/danger).
            layout: "bisected" (2-col) or "trisection" (3-col) or "flow".
        """
        cols_per_row = {"bisected": 2, "trisection": 3, "flow": 4}.get(layout, 2)
        columns = []
        for btn in buttons:
            columns.append({
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": btn["text"]},
                    "type": btn.get("type", "default"),
                    "value": btn.get("value", {}),
                }],
            })
        # Split into rows if more buttons than columns per row
        if len(columns) <= cols_per_row:
            return {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "default",
                "columns": columns,
            }
        # Multiple rows: return a list wrapper (caller must flatten)
        rows = []
        for i in range(0, len(columns), cols_per_row):
            rows.append({
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "default",
                "columns": columns[i:i + cols_per_row],
            })
        return rows

    @staticmethod
    def build_confirm_card(text: str, confirm_value: dict,
                           cancel_value: dict, *,
                           confirm_text: str = "确认",
                           cancel_text: str = "取消",
                           header: str | None = None,
                           color: str = "red") -> str:
        """Build a confirmation card with confirm/cancel buttons."""
        elements = [
            {"tag": "markdown", "content": text},
            {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "default",
                "columns": [
                    {
                        "tag": "column", "width": "weighted", "weight": 1,
                        "elements": [{
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": confirm_text},
                            "type": "danger",
                            "value": confirm_value,
                        }],
                    },
                    {
                        "tag": "column", "width": "weighted", "weight": 1,
                        "elements": [{
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": cancel_text},
                            "type": "default",
                            "value": cancel_value,
                        }],
                    },
                ],
            },
        ]
        card: dict = {
            "schema": "2.0",
            "header": {
                "template": color,
            },
            "body": {"elements": elements},
        }
        if header:
            card["header"]["title"] = {
                "tag": "plain_text", "content": header
            }
        return json.dumps(card, ensure_ascii=False)

    async def _with_retry(
        self,
        operation: str,
        fn: Callable[[], Awaitable[T]],
        *,
        success_check: Callable[[T], bool] | None = None,
        log_failure: Callable[[T], None] | None = None,
    ) -> T | None:
        """Execute fn() with exponential backoff retry.

        Args:
            operation: Name for log messages (e.g. "send_card").
            fn: Async callable to attempt.
            success_check: If provided, called with the result to determine success.
                           If it returns False, the attempt is considered failed.
            log_failure: If provided, called with the result on non-success for logging.

        Returns:
            The result of fn() on success, or None after all retries exhausted.
        """
        # Non-retryable exceptions: programming errors, not transient failures
        _NO_RETRY = (TypeError, ValueError, AttributeError, KeyError, RuntimeError)
        for attempt in range(MAX_RETRIES):
            try:
                result = await fn()
                if success_check is None or success_check(result):
                    return result
                if log_failure:
                    log_failure(result)
                else:
                    log.warning("%s failed (attempt %d/%d)", operation, attempt + 1, MAX_RETRIES)
            except _NO_RETRY as e:
                log.error("%s non-retryable error: %s", operation, e)
                return None
            except Exception as e:
                log.error("%s error (attempt %d/%d): %s", operation, attempt + 1, MAX_RETRIES, e)
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
        return None

    async def send_text(
        self,
        chat_id: str,
        text: str,
        reply_to: str | None = None,
    ) -> str | None:
        """Send markdown text via Feishu interactive card (JSON 2.0).

        Supports card header directive at text start:
            {{card:header=标题,color=blue}}
            正文内容...

        Returns the message_id of the first sent message, or None on failure.
        """
        if not text.strip():
            return None

        secret = _contains_secret(text)
        if secret:
            log.error("Secret leak blocked (pattern: %s), message NOT sent", secret)
            return None

        # Parse card header directive (if present)
        text, header, color = _parse_card_directive(text)

        # Chunk if too long
        if len(text) > MAX_MSG_LEN:
            return await self._send_chunked(
                chat_id, text, reply_to, header=header, color=color
            )

        return await self._send_card(
            chat_id, text, reply_to, header=header, color=color
        )

    async def send_to_delivery_target(self, text: str) -> str | None:
        """Send to the configured delivery chat (for heartbeat/cron results)."""
        if not self.delivery_chat_id:
            log.warning("No delivery_chat_id configured, skipping delivery")
            return None
        return await self.send_text(self.delivery_chat_id, text)

    async def send_card_to_delivery(self, text: str) -> str | None:
        """Send a card to delivery chat, returning message_id for later update_card()."""
        if not self.delivery_chat_id:
            log.warning("No delivery_chat_id configured, skipping delivery")
            return None
        text, header, color = _parse_card_directive(text)
        return await self.send_card_return_id(
            self.delivery_chat_id, text, header=header, color=color
        )

    async def send_to_user(self, open_id: str, text: str) -> str | None:
        """Send a DM to a user by open_id. Returns message_id or None."""
        if not text.strip():
            return None
        secret = _contains_secret(text)
        if secret:
            log.error("Secret leak blocked in DM (pattern: %s), message NOT sent", secret)
            return None
        text, header, color = _parse_card_directive(text)
        if len(text) > MAX_MSG_LEN:
            return await self._send_chunked_to_user(open_id, text)
        return await self._send_card_to_user(
            open_id, text, header=header, color=color
        )

    async def _send_card_to_user(self, open_id: str, text: str, *,
                                  header: str | None = None,
                                  color: str | None = None) -> str | None:
        """Send a card to a user by open_id."""
        self._ensure_client()
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        content = self._build_card_json(text, header=header, color=color)

        async def _attempt():
            req = CreateMessageRequest.builder() \
                .receive_id_type("open_id") \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(open_id)
                    .msg_type("interactive")
                    .content(content)
                    .build()
                ).build()
            return await asyncio.to_thread(
                self._client.im.v1.message.create, req
            )

        def _check(resp) -> bool:
            return resp.success() and resp.data

        def _log_fail(resp):
            log.warning("send_to_user failed: code=%s msg=%s", resp.code, resp.msg)

        resp = await self._with_retry(
            "send_to_user", _attempt,
            success_check=_check, log_failure=_log_fail,
        )
        if resp and resp.data:
            return resp.data.message_id
        return None

    async def _send_chunked_to_user(self, open_id: str, text: str) -> str | None:
        """Split long text and send as multiple DMs."""
        chunks = self._chunk_text(text)
        first_mid = None
        for i, chunk in enumerate(chunks):
            mid = await self._send_card_to_user(open_id, chunk)
            if i == 0:
                first_mid = mid
            if i < len(chunks) - 1:
                await asyncio.sleep(0.5)
        return first_mid

    async def send_card_return_id(
        self,
        chat_id: str,
        text: str,
        reply_to: str | None = None,
        *, header: str | None = None,
        color: str | None = None,
    ) -> str | None:
        """Send a card and return its message_id (for later updates). None on failure."""
        self._ensure_client()
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody,
            ReplyMessageRequest, ReplyMessageRequestBody,
        )

        content = self._build_card_json(text, header=header, color=color)
        _reply_ref = [reply_to]  # mutable ref for closure

        async def _attempt():
            if _reply_ref[0]:
                req = ReplyMessageRequest.builder() \
                    .message_id(_reply_ref[0]) \
                    .request_body(
                        ReplyMessageRequestBody.builder()
                        .msg_type("interactive")
                        .content(content)
                        .build()
                    ).build()
                return await asyncio.to_thread(
                    self._client.im.v1.message.reply, req
                )
            else:
                req = CreateMessageRequest.builder() \
                    .receive_id_type("chat_id") \
                    .request_body(
                        CreateMessageRequestBody.builder()
                        .receive_id(chat_id)
                        .msg_type("interactive")
                        .content(content)
                        .build()
                    ).build()
                return await asyncio.to_thread(
                    self._client.im.v1.message.create, req
                )

        def _check(resp) -> bool:
            return resp.success() and resp.data

        def _log_fail(resp):
            log.warning("send_card failed: code=%s msg=%s", resp.code, resp.msg)
            # 230011 = message withdrawn; drop reply_to and send as new message
            if resp.code == 230011 and _reply_ref[0]:
                log.info("Reply target withdrawn, falling back to non-reply send")
                _reply_ref[0] = None

        resp = await self._with_retry(
            "send_card", _attempt,
            success_check=_check, log_failure=_log_fail,
        )
        if resp and resp.data:
            return resp.data.message_id
        return None

    async def send_card_raw(
        self,
        chat_id: str,
        card_json: str,
        reply_to: str | None = None,
    ) -> str | None:
        """Send a pre-built card JSON and return message_id."""
        self._ensure_client()
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody,
            ReplyMessageRequest, ReplyMessageRequestBody,
        )
        _reply_ref = [reply_to]

        async def _attempt():
            if _reply_ref[0]:
                req = ReplyMessageRequest.builder() \
                    .message_id(_reply_ref[0]) \
                    .request_body(
                        ReplyMessageRequestBody.builder()
                        .msg_type("interactive")
                        .content(card_json)
                        .build()
                    ).build()
                return await asyncio.to_thread(
                    self._client.im.v1.message.reply, req
                )
            else:
                req = CreateMessageRequest.builder() \
                    .receive_id_type("chat_id") \
                    .request_body(
                        CreateMessageRequestBody.builder()
                        .receive_id(chat_id)
                        .msg_type("interactive")
                        .content(card_json)
                        .build()
                    ).build()
                return await asyncio.to_thread(
                    self._client.im.v1.message.create, req
                )

        def _check(resp) -> bool:
            return resp.success() and resp.data

        def _log_fail(resp):
            log.warning("send_card_raw failed: code=%s msg=%s",
                        resp.code, resp.msg)
            if resp.code == 230011 and _reply_ref[0]:
                _reply_ref[0] = None

        resp = await self._with_retry(
            "send_card_raw", _attempt,
            success_check=_check, log_failure=_log_fail,
        )
        if resp and resp.data:
            return resp.data.message_id
        return None

    async def send_card_raw_to_user(
        self,
        open_id: str,
        card_json: str,
    ) -> str | None:
        """Send a pre-built card JSON to a user by open_id."""
        self._ensure_client()
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        async def _attempt():
            req = CreateMessageRequest.builder() \
                .receive_id_type("open_id") \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(open_id)
                    .msg_type("interactive")
                    .content(card_json)
                    .build()
                ).build()
            return await asyncio.to_thread(
                self._client.im.v1.message.create, req
            )

        def _check(resp) -> bool:
            return resp.success() and resp.data

        def _log_fail(resp):
            log.warning("send_card_raw_to_user failed: code=%s msg=%s",
                        resp.code, resp.msg)

        resp = await self._with_retry(
            "send_card_raw_to_user", _attempt,
            success_check=_check, log_failure=_log_fail,
        )
        if resp and resp.data:
            return resp.data.message_id
        return None

    async def send_card_raw_to_delivery(self, card_json: str) -> str | None:
        """Send a pre-built card JSON to the delivery chat."""
        if not self.delivery_chat_id:
            log.warning("No delivery_chat_id configured, skipping delivery")
            return None
        return await self.send_card_raw(self.delivery_chat_id, card_json)

    async def update_card_raw(self, message_id: str, card_json: str) -> bool:
        """Update an existing card with pre-built card JSON."""
        self._ensure_client()

        async def _attempt():
            from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
            req = PatchMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(card_json)
                    .build()
                ).build()
            return await asyncio.to_thread(
                self._client.im.v1.message.patch, req
            )

        def _check(resp) -> bool:
            return resp.success()

        def _log_fail(resp):
            log.warning("update_card_raw failed: code=%s msg=%s",
                        resp.code, resp.msg)

        resp = await self._with_retry(
            "update_card_raw", _attempt,
            success_check=_check, log_failure=_log_fail,
        )
        return resp is not None

    async def update_card(self, message_id: str, text: str) -> bool:
        """Update an existing card message via PATCH. Returns success."""
        self._ensure_client()
        text, header, color = _parse_card_directive(text)
        content = self._build_card_json(text, header=header, color=color)

        async def _attempt():
            from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
            req = PatchMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(content)
                    .build()
                ).build()
            return await asyncio.to_thread(
                self._client.im.v1.message.patch, req
            )

        def _check(resp) -> bool:
            return resp.success()

        def _log_fail(resp):
            log.warning("update_card failed: code=%s msg=%s", resp.code, resp.msg)

        resp = await self._with_retry(
            "update_card", _attempt,
            success_check=_check, log_failure=_log_fail,
        )
        return resp is not None

    async def delete_message(self, message_id: str) -> bool:
        """Delete a message by message_id. Returns success."""
        self._ensure_client()

        async def _attempt():
            from lark_oapi.api.im.v1 import DeleteMessageRequest
            req = DeleteMessageRequest.builder() \
                .message_id(message_id) \
                .build()
            return await asyncio.to_thread(
                self._client.im.v1.message.delete, req
            )

        def _check(resp) -> bool:
            return resp.success()

        def _log_fail(resp):
            log.warning("delete_message failed: code=%s msg=%s", resp.code, resp.msg)

        resp = await self._with_retry(
            "delete_message", _attempt,
            success_check=_check, log_failure=_log_fail,
        )
        return resp is not None

    async def _send_card(
        self, chat_id: str, text: str, reply_to: str | None = None,
        *, header: str | None = None, color: str | None = None,
    ) -> str:
        """Thin wrapper over send_card_return_id for internal use.

        Returns message_id on success, empty string on failure.
        The _send_chunked method depends on this returning a string (possibly empty).
        """
        result = await self.send_card_return_id(
            chat_id, text, reply_to, header=header, color=color
        )
        return result or ""

    async def _send_chunked(
        self, chat_id: str, text: str, reply_to: str | None = None,
        *, header: str | None = None, color: str | None = None,
    ) -> str | None:
        """Split long text and send as multiple messages.

        Returns the message_id of the first chunk, or None if first chunk failed.
        """
        chunks = self._chunk_text(text)
        first_mid = None
        for i, chunk in enumerate(chunks):
            # Header/color only on the first chunk
            mid = await self._send_card(
                chat_id, chunk,
                reply_to=reply_to if i == 0 else None,
                header=header if i == 0 else None,
                color=color if i == 0 else None,
            )
            if i == 0:
                first_mid = mid
            if i < len(chunks) - 1:
                await asyncio.sleep(0.5)  # rate limit courtesy
        return first_mid

    @staticmethod
    def _chunk_text(text: str) -> list[str]:
        """Split text at paragraph boundaries, respecting max length."""
        chunks = []
        current = ""
        for para in text.split("\n\n"):
            if len(current) + len(para) + 2 > MAX_MSG_LEN:
                if current:
                    chunks.append(current.strip())
                    current = ""
                if len(para) > MAX_MSG_LEN:
                    # Force split very long paragraphs
                    while para:
                        chunks.append(para[:MAX_MSG_LEN])
                        para = para[MAX_MSG_LEN:]
                else:
                    current = para
            else:
                current = current + "\n\n" + para if current else para
        if current.strip():
            chunks.append(current.strip())
        return chunks or [""]
