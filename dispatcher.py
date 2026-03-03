# -*- coding: utf-8 -*-
"""Feishu outbound message dispatcher."""

import json
import asyncio
import logging

log = logging.getLogger("hub.dispatcher")

MAX_MSG_LEN = 4000  # Feishu card markdown content limit per chunk


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

    async def send_text(
        self,
        chat_id: str,
        text: str,
        reply_to: str | None = None,
    ) -> str | None:
        """Send markdown text via Feishu interactive card (JSON 2.0).

        Returns the message_id of the first sent message, or None on failure.
        """
        if not text.strip():
            return None

        # Chunk if too long
        if len(text) > MAX_MSG_LEN:
            return await self._send_chunked(chat_id, text, reply_to)

        return await self._send_card(chat_id, text, reply_to)

    async def send_to_delivery_target(self, text: str) -> str | None:
        """Send to the configured delivery chat (for heartbeat/cron results)."""
        if not self.delivery_chat_id:
            log.warning("No delivery_chat_id configured, skipping delivery")
            return None
        return await self.send_text(self.delivery_chat_id, text)

    async def add_reaction(self, message_id: str, emoji: str = "Typing") -> str | None:
        """Add a reaction emoji to a message. Returns reaction_id or None."""
        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import CreateMessageReactionRequest, CreateMessageReactionRequestBody

            req = CreateMessageReactionRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type({"emoji_type": emoji})
                    .build()
                ).build()
            resp = await asyncio.to_thread(
                self._client.im.v1.message_reaction.create, req
            )
            if resp.success() and resp.data:
                return resp.data.reaction_id
            log.debug("add_reaction failed: code=%s msg=%s", resp.code, resp.msg)
        except Exception as e:
            log.debug("add_reaction error: %s", e)
        return None

    async def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        """Remove a reaction emoji from a message."""
        try:
            from lark_oapi.api.im.v1 import DeleteMessageReactionRequest

            req = DeleteMessageReactionRequest.builder() \
                .message_id(message_id) \
                .reaction_id(reaction_id) \
                .build()
            await asyncio.to_thread(
                self._client.im.v1.message_reaction.delete, req
            )
        except Exception as e:
            log.debug("remove_reaction error: %s", e)

    async def send_card_return_id(
        self,
        chat_id: str,
        text: str,
        reply_to: str | None = None,
    ) -> str | None:
        """Send a card and return its message_id (for later updates). None on failure."""
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody,
            ReplyMessageRequest, ReplyMessageRequestBody,
        )

        content = json.dumps({
            "schema": "2.0",
            "body": {
                "elements": [
                    {"tag": "markdown", "content": text}
                ]
            }
        }, ensure_ascii=False)

        for attempt in range(3):
            try:
                if reply_to:
                    req = ReplyMessageRequest.builder() \
                        .message_id(reply_to) \
                        .request_body(
                            ReplyMessageRequestBody.builder()
                            .msg_type("interactive")
                            .content(content)
                            .build()
                        ).build()
                    resp = await asyncio.to_thread(
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
                    resp = await asyncio.to_thread(
                        self._client.im.v1.message.create, req
                    )

                if resp.success() and resp.data:
                    return resp.data.message_id
                log.warning("send_card_return_id failed: code=%s msg=%s", resp.code, resp.msg)
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                log.error("send_card_return_id error (attempt %d): %s", attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        return None

    async def delete_message(self, message_id: str) -> bool:
        """Delete a message by message_id. Returns success."""
        try:
            from lark_oapi.api.im.v1 import DeleteMessageRequest
            req = DeleteMessageRequest.builder() \
                .message_id(message_id) \
                .build()
            resp = await asyncio.to_thread(
                self._client.im.v1.message.delete, req
            )
            if resp.success():
                return True
            log.debug("delete_message failed: code=%s msg=%s", resp.code, resp.msg)
        except Exception as e:
            log.debug("delete_message error: %s", e)
        return False

    async def update_card(self, message_id: str, text: str) -> bool:
        """Update an existing card message via PATCH. Returns success."""
        content = json.dumps({
            "schema": "2.0",
            "body": {
                "elements": [
                    {"tag": "markdown", "content": text}
                ]
            }
        }, ensure_ascii=False)

        for attempt in range(3):
            try:
                from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
                req = PatchMessageRequest.builder() \
                    .message_id(message_id) \
                    .request_body(
                        PatchMessageRequestBody.builder()
                        .content(content)
                        .build()
                    ).build()
                resp = await asyncio.to_thread(
                    self._client.im.v1.message.patch, req
                )
                if resp.success():
                    return True
                log.warning("update_card failed: code=%s msg=%s", resp.code, resp.msg)
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                log.error("update_card error (attempt %d): %s", attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        return False

    async def _send_card(
        self, chat_id: str, text: str, reply_to: str | None = None,
    ) -> str | None:
        """Send markdown content as Feishu interactive card (JSON 2.0 schema).

        Returns message_id on success, None on failure.
        """
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody,
            ReplyMessageRequest, ReplyMessageRequestBody,
        )

        # Card JSON 2.0 with native markdown rendering (supports tables, colored text, etc.)
        content = json.dumps({
            "schema": "2.0",
            "body": {
                "elements": [
                    {"tag": "markdown", "content": text}
                ]
            }
        }, ensure_ascii=False)

        for attempt in range(3):
            try:
                if reply_to:
                    req = ReplyMessageRequest.builder() \
                        .message_id(reply_to) \
                        .request_body(
                            ReplyMessageRequestBody.builder()
                            .msg_type("interactive")
                            .content(content)
                            .build()
                        ).build()
                    resp = await asyncio.to_thread(
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
                    resp = await asyncio.to_thread(
                        self._client.im.v1.message.create, req
                    )

                if resp.success():
                    mid = getattr(resp.data, "message_id", None) if resp.data else None
                    return mid or ""
                log.warning("Feishu send failed: code=%s msg=%s", resp.code, resp.msg)
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                log.error("Feishu send error (attempt %d): %s", attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)

        return None

    async def _send_chunked(
        self, chat_id: str, text: str, reply_to: str | None = None,
    ) -> str | None:
        """Split long text and send as multiple messages.

        Returns the message_id of the first chunk, or None if first chunk failed.
        """
        chunks = self._chunk_text(text)
        first_mid = None
        for i, chunk in enumerate(chunks):
            mid = await self._send_card(
                chat_id, chunk,
                reply_to=reply_to if i == 0 else None,
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
