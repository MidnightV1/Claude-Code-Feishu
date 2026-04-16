# -*- coding: utf-8 -*-
"""SentinelScanner — protocol and base class for all scanners."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from agent.jobs.sentinel.models import EntropySignal, ScanContext


@runtime_checkable
class SentinelScanner(Protocol):
    """Protocol that all scanners implement."""

    name: str

    async def scan(self, context: ScanContext) -> list[EntropySignal]:
        """Execute the scan, return discovered signals."""
        ...

    def estimate_change_rate(self) -> str:
        """Return estimated information change frequency: 'hourly', 'daily', 'weekly'.

        Used by the orchestrator for dynamic scan frequency adjustment.
        """
        ...


class BaseSentinelScanner(ABC):
    """Base class with common helpers for scanner implementations."""

    name: str = "base"

    @abstractmethod
    async def scan(self, context: ScanContext) -> list[EntropySignal]:
        ...

    def estimate_change_rate(self) -> str:
        return "daily"

    def _create_signal(
        self,
        category: str,
        severity: str,
        autonomy_level: int,
        summary: str,
        route: str = "silent_log",
        evidence: list[str] | None = None,
        suggested_action: str = "",
        context: dict | None = None,
    ) -> EntropySignal:
        """Helper to create a signal pre-filled with this scanner's source."""
        return EntropySignal(
            id=uuid.uuid4().hex[:12],
            source=self.name,
            category=category,
            severity=severity,
            autonomy_level=autonomy_level,
            summary=summary,
            route=route,
            evidence=evidence or [],
            suggested_action=suggested_action,
            context=context or {},
        )

    def _is_skill_path(self, path: str) -> bool:
        """Check if a file path is inside the skills directory."""
        return ".claude/skills/" in path or ".claude\\skills\\" in path

    def _is_duplicate(
        self, signal: EntropySignal, recent: list[EntropySignal]
    ) -> bool:
        """Check if a signal with same source+category+summary exists in recent."""
        for r in recent:
            if (
                r.source == signal.source
                and r.category == signal.category
                and r.summary == signal.summary
                and r.resolved_at is None
            ):
                return True
        return False
