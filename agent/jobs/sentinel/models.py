# -*- coding: utf-8 -*-
"""Sentinel data models — core types shared across all components."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class EntropySignal:
    """A standardized 'something needs attention' signal."""

    source: str               # scanner name: "code_scanner", "doc_auditor", etc.
    category: str             # signal type: "todo_stale", "doc_duplicate", etc.
    severity: str             # "low", "medium", "high", "critical"
    autonomy_level: int       # 0-3 (maps to L0-L3 autonomy matrix)
    summary: str              # human-readable one-liner
    evidence: list[str] = field(default_factory=list)
    suggested_action: str = ""
    route: str = "silent_log"  # "maqs" | "explore" | "notify" | "silent_log"
    context: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    resolved_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> EntropySignal:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class FixResult:
    """Outcome of an automatic fix attempt."""

    success: bool
    action_taken: str          # what was done
    ticket_id: str = ""        # MAQS ticket ID (if routed through MAQS)
    error: str = ""            # failure reason

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScanContext:
    """Context passed to scanners for each cycle."""

    workspace_dir: str
    last_scan_at: float = 0.0                  # for incremental scans
    recent_signals: list[EntropySignal] = field(default_factory=list)  # for dedup
    user_config: dict[str, Any] = field(default_factory=dict)
