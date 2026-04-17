# -*- coding: utf-8 -*-
"""Feishu message guard for tenant isolation, dedup, staleness, and rate limit."""

import time

from agent.infra.message_store import MessageStore, content_hash, media_hash


class MessageGuard:
    def __init__(
        self,
        message_store: MessageStore | None = None,
        dedup_ttl: int = 86400,
        dedup_max_size: int = 1000,
        rate_limit_per_min: int = 10,
    ):
        self.message_store = message_store
        self._dedup_ttl = dedup_ttl
        self._dedup_max_size = dedup_max_size
        self._rate_limit_per_min = rate_limit_per_min
        self._dedup: dict[str, float] = {}
        self._rate_limits: dict[str, list[float]] = {}
        self._tenant_key = ""

    def accept(
        self,
        message_id: str,
        sender_id: str,
        tenant_key: str,
        create_time: str | None,
    ) -> bool:
        if create_time:
            try:
                if time.time() - int(create_time) / 1000 > 120:
                    return False
            except (TypeError, ValueError):
                pass

        if tenant_key:
            if not self._tenant_key:
                self._tenant_key = tenant_key
            elif tenant_key != self._tenant_key:
                return False

        now = time.time()
        if message_id in self._dedup:
            return False
        if len(self._dedup) > self._dedup_max_size:
            self.sweep()
        self._dedup[message_id] = now

        window = self._rate_limits.setdefault(sender_id, [])
        cutoff = now - 60
        self._rate_limits[sender_id] = [ts for ts in window if ts > cutoff]
        window = self._rate_limits[sender_id]
        if len(window) >= self._rate_limit_per_min:
            return False
        window.append(now)
        return True

    def check_content_dup(
        self,
        message_id: str,
        sender_id: str,
        text: str,
        category: str,
        debounce_key: str = "",
    ) -> bool:
        if not self.message_store:
            return False
        if category in ("image", "file"):
            return self.message_store.check_dup(message_id, media_hash(sender_id, text), category)
        return self.message_store.check_dup(message_id, content_hash(sender_id, text), category)

    def record_content(
        self,
        message_id: str,
        sender_id: str,
        text: str,
        category: str,
        debounce_key: str = "",
    ) -> None:
        if not self.message_store:
            return
        if category in ("image", "file"):
            self.message_store.record(
                message_id, media_hash(sender_id, text), category, sender_id, debounce_key
            )
            return
        self.message_store.record(
            message_id, content_hash(sender_id, text), category, sender_id, debounce_key
        )

    @property
    def tenant_key(self) -> str:
        return self._tenant_key

    def sweep(self) -> None:
        now = time.time()
        dedup_cutoff = now - self._dedup_ttl
        self._dedup = {key: ts for key, ts in self._dedup.items() if ts > dedup_cutoff}
        rate_cutoff = now - 120
        for sender_id in list(self._rate_limits):
            window = [ts for ts in self._rate_limits[sender_id] if ts > rate_cutoff]
            if window:
                self._rate_limits[sender_id] = window
            else:
                del self._rate_limits[sender_id]
