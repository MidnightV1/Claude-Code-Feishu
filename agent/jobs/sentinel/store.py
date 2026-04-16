# -*- coding: utf-8 -*-
"""SentinelStore — unified JSONL persistence for all Sentinel signals."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from agent.jobs.sentinel.models import EntropySignal

log = logging.getLogger("hub.sentinel")


class SentinelStore:
    """Append-only JSONL store with query and resolve capabilities.

    All autonomous task signals (entropy, followups, scan results) live in
    a single file for simplicity — volume is low enough that linear scan is fine.
    """

    def __init__(self, path: str = "data/sentinel.jsonl"):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, signal: EntropySignal) -> None:
        """Append a signal to the store."""
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(signal.to_dict(), ensure_ascii=False) + "\n")

    def query(
        self,
        hours: float = 24,
        source: str | None = None,
        category: str | None = None,
        unresolved_only: bool = False,
    ) -> list[EntropySignal]:
        """Query signals within a time window, optionally filtered."""
        cutoff = time.time() - hours * 3600
        results = []
        for signal in self._read_all():
            if signal.created_at < cutoff:
                continue
            if source and signal.source != source:
                continue
            if category and signal.category != category:
                continue
            if unresolved_only and signal.resolved_at is not None:
                continue
            results.append(signal)
        return results

    def resolve(self, signal_id: str) -> bool:
        """Mark a signal as resolved. Returns True if found and updated."""
        signals = self._read_all()
        found = False
        for s in signals:
            if s.id == signal_id:
                s.resolved_at = time.time()
                found = True
                break
        if found:
            self._write_all(signals)
        return found

    def get_last_scan_time(self, scanner_name: str) -> float:
        """Get the most recent signal timestamp from a given scanner."""
        latest = 0.0
        for signal in self._read_all():
            if signal.source == scanner_name and signal.created_at > latest:
                latest = signal.created_at
        return latest

    def stats(self, hours: float = 24) -> dict:
        """Summary statistics for the given time window."""
        signals = self.query(hours=hours)
        by_source: dict[str, int] = {}
        by_route: dict[str, int] = {}
        resolved = 0
        for s in signals:
            by_source[s.source] = by_source.get(s.source, 0) + 1
            by_route[s.route] = by_route.get(s.route, 0) + 1
            if s.resolved_at is not None:
                resolved += 1
        return {
            "total": len(signals),
            "resolved": resolved,
            "unresolved": len(signals) - resolved,
            "by_source": by_source,
            "by_route": by_route,
        }

    def _read_all(self) -> list[EntropySignal]:
        if not self._path.exists():
            return []
        signals = []
        with open(self._path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    signals.append(EntropySignal.from_dict(json.loads(line)))
                except (json.JSONDecodeError, TypeError) as e:
                    log.warning("Corrupt sentinel record: %s", str(e)[:100])
        return signals

    def _write_all(self, signals: list[EntropySignal]) -> None:
        """Rewrite the full store (used for resolve operations)."""
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for s in signals:
                f.write(json.dumps(s.to_dict(), ensure_ascii=False) + "\n")
        tmp.replace(self._path)
