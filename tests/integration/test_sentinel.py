# -*- coding: utf-8 -*-
"""Integration tests for the Sentinel entropy-control system.

Tests the real data paths that unit tests mock away:
- L1: SentinelStore full lifecycle on real filesystem
- L2: Orchestrator cycle with multi-scanner coordination + intake_queue.jsonl
- L3: CLI commands (list / stats / resolve) output format

Usage:
    pytest tests/integration/test_sentinel.py -v -m integration
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.jobs.sentinel import EntropySignal, SentinelOrchestrator, SentinelStore
from agent.jobs.sentinel.models import ScanContext

pytestmark = pytest.mark.integration


# ── Shared helpers ────────────────────────────────────────────────────────────

def _sig(
    source="test_scanner",
    category="stale_todo",
    severity="medium",
    summary="test summary",
    route="silent_log",
    evidence=None,
    autonomy_level=1,
) -> EntropySignal:
    return EntropySignal(
        source=source,
        category=category,
        severity=severity,
        autonomy_level=autonomy_level,
        summary=summary,
        evidence=evidence or [],
        route=route,
    )


class _FakeScanner:
    """Minimal scanner stub — no subprocess, no external calls."""

    def __init__(self, name: str, signals: list, change_rate: str = "daily"):
        self.name = name
        self._signals = signals
        self._change_rate = change_rate

    async def scan(self, context: ScanContext) -> list:
        return list(self._signals)

    def estimate_change_rate(self) -> str:
        return self._change_rate


# ── L1: Store persistence ─────────────────────────────────────────────────────

class TestStoreLifecycleIntegration:
    """L1 — Store append → query → resolve → stats on a real JSONL file.

    Distinct from unit tests: a single store instance is shared across all
    steps so cross-operation JSONL consistency is verified.
    """

    @pytest.fixture
    def store(self, tmp_path):
        (tmp_path / "data").mkdir()
        return SentinelStore(str(tmp_path / "data" / "sentinel.jsonl"))

    def test_full_lifecycle(self, store):
        """append × 3 → resolve 1 → query unresolved == 2 → stats consistent."""
        s1 = _sig(source="scanner_a", route="maqs", summary="signal one")
        s2 = _sig(source="scanner_a", route="maqs", summary="signal two")
        s3 = _sig(source="scanner_b", route="notify", summary="signal three")

        store.append(s1)
        store.append(s2)
        store.append(s3)

        # Resolve s1
        assert store.resolve(s1.id) is True

        # Unresolved query returns only 2
        unresolved = store.query(hours=24, unresolved_only=True)
        assert len(unresolved) == 2
        assert all(s.resolved_at is None for s in unresolved)

        # Stats are consistent with filesystem state
        stats = store.stats(hours=24)
        assert stats["total"] == 3
        assert stats["resolved"] == 1
        assert stats["unresolved"] == 2
        assert stats["by_source"]["scanner_a"] == 2
        assert stats["by_source"]["scanner_b"] == 1
        assert stats["by_route"]["maqs"] == 2
        assert stats["by_route"]["notify"] == 1

    def test_sequential_appends_preserve_order(self, store):
        summaries = [f"signal {i}" for i in range(8)]
        for s in summaries:
            store.append(_sig(summary=s))

        results = store.query(hours=24)
        assert [r.summary for r in results] == summaries

    def test_resolve_nonexistent_leaves_file_intact(self, store):
        s = _sig(summary="original")
        store.append(s)

        ok = store.resolve("000000000000")
        assert ok is False

        # File unchanged — original signal still queryable
        results = store.query(hours=24)
        assert len(results) == 1
        assert results[0].id == s.id

    def test_corrupt_line_skipped_adjacent_valid_preserved(self, store):
        valid = _sig(summary="valid signal")
        store.append(valid)

        # Inject a corrupt line between two valid ones
        with open(store._path, "a") as f:
            f.write("{CORRUPT}\n")
            f.write(json.dumps(_sig(summary="after corrupt").to_dict()) + "\n")

        results = store.query(hours=24)
        assert len(results) == 2
        assert results[0].summary == "valid signal"
        assert results[1].summary == "after corrupt"

    def test_atomic_rewrite_no_tmp_leftover(self, store):
        s = _sig()
        store.append(s)
        store.resolve(s.id)

        tmp_file = store._path.with_suffix(".tmp")
        assert not tmp_file.exists()


# ── L2: Orchestrator cycle ────────────────────────────────────────────────────

class TestOrchestratorCycleIntegration:
    """L2 — Full cycle with multiple fake scanners, real Store, real intake_queue."""

    @pytest.fixture
    def workspace(self, tmp_path):
        (tmp_path / "data").mkdir()
        return tmp_path

    def _run(self, coro):
        return asyncio.run(coro)

    def test_gold_standard_maqs_and_notify(self, workspace):
        """Gold standard: stale_todo(maqs) + uncommitted_stale(notify)
        → summary {total:2, maqs:1, notify:1}
        → intake_queue.jsonl has P2 record with correct signal_id
        → dispatcher.send_to_user called once for notify signal
        """
        store = SentinelStore(str(workspace / "data" / "sentinel.jsonl"))

        todo_sig = _sig(
            source="code_scanner",
            category="stale_todo",
            severity="medium",
            summary="TODO: remove deprecated API call",
            route="maqs",
            evidence=["agent/main.py:42"],
        )
        notify_sig = _sig(
            source="code_scanner",
            category="uncommitted_stale",
            severity="medium",
            summary="Uncommitted changes in agent/main.py (3 days)",
            route="notify",
        )

        scanner = _FakeScanner("code_scanner", [todo_sig, notify_sig])
        dispatcher = MagicMock()
        dispatcher.send_to_delivery_target = AsyncMock()

        orchestrator = SentinelOrchestrator(
            scanners=[scanner],
            store=store,
            dispatcher=dispatcher,
            workspace_dir=str(workspace),
            config={
                "maqs": {
                    "bitable_app_token": "fake_token",
                    "bitable_table_id": "fake_table",
                }
            },
            notify_open_id="ou_test_user",
        )

        with patch(
            "agent.jobs.mads.helpers.bitable_add",
            new_callable=AsyncMock,
            return_value="REC001",
        ):
            summary = self._run(orchestrator.run_cycle(trigger="manual"))

        # Route summary
        assert summary["total"] == 2
        assert summary["maqs"] == 1
        assert summary["notify"] == 1

        # Both signals persisted
        stored = store.query(hours=24)
        assert len(stored) == 2

        # intake_queue.jsonl written before bitable call
        queue_path = workspace / "data" / "intake_queue.jsonl"
        assert queue_path.exists()
        with open(queue_path) as f:
            records = [json.loads(line) for line in f if line.strip()]
        assert len(records) == 1
        assert records[0]["priority"] == "P2"
        assert records[0]["signal_id"] == todo_sig.id

        # Dispatcher called for notify signal (via delivery_target, not DM)
        dispatcher.send_to_delivery_target.assert_called_once()
        text = dispatcher.send_to_delivery_target.call_args[0][0]
        assert "Uncommitted" in text or "uncommitted" in text.lower()

    def test_dedup_across_scanners(self, workspace):
        """Signal already in store (unresolved) is deduped — not stored again."""
        store = SentinelStore(str(workspace / "data" / "sentinel.jsonl"))

        existing = _sig(source="code_scanner", category="stale_todo", summary="same signal")
        store.append(existing)

        duplicate = _sig(source="code_scanner", category="stale_todo", summary="same signal")
        scanner = _FakeScanner("code_scanner", [duplicate])

        orchestrator = SentinelOrchestrator(
            scanners=[scanner],
            store=store,
            workspace_dir=str(workspace),
        )

        summary = self._run(orchestrator.run_cycle(trigger="manual"))
        assert summary["total"] == 0

        # Store still has exactly one record
        assert len(store.query(hours=24)) == 1

    def test_multi_scanner_priority_ordering(self, workspace):
        """Critical signals from any scanner bubble to the top."""
        store = SentinelStore(str(workspace / "data" / "sentinel.jsonl"))

        scanner_a = _FakeScanner("scanner_a", [_sig(severity="low", summary="low")])
        scanner_b = _FakeScanner("scanner_b", [
            _sig(severity="critical", summary="critical"),
            _sig(severity="high", summary="high"),
        ])

        orchestrator = SentinelOrchestrator(
            scanners=[scanner_a, scanner_b],
            store=store,
            workspace_dir=str(workspace),
        )

        summary = self._run(orchestrator.run_cycle(trigger="manual"))

        assert summary["total"] == 3
        signals = summary["signals"]
        assert signals[0].severity == "critical"
        assert signals[1].severity == "high"
        assert signals[2].severity == "low"

    def test_failing_scanner_does_not_abort_cycle(self, workspace):
        """One scanner raising an exception does not prevent others from running."""
        store = SentinelStore(str(workspace / "data" / "sentinel.jsonl"))

        class _BrokenScanner:
            name = "broken"

            async def scan(self, ctx):
                raise RuntimeError("scanner exploded")

            def estimate_change_rate(self):
                return "daily"

        good_sig = _sig(source="good", summary="from good scanner")
        good = _FakeScanner("good", [good_sig])

        orchestrator = SentinelOrchestrator(
            scanners=[_BrokenScanner(), good],
            store=store,
            workspace_dir=str(workspace),
        )

        summary = self._run(orchestrator.run_cycle(trigger="manual"))
        assert summary["total"] == 1
        assert summary["signals"][0].summary == "from good scanner"

    def test_intake_queue_written_even_if_bitable_fails(self, workspace):
        """Local intake_queue.jsonl is written before the bitable call — survives API failure."""
        store = SentinelStore(str(workspace / "data" / "sentinel.jsonl"))

        maqs_sig = _sig(
            source="code_scanner",
            category="stale_todo",
            severity="high",
            summary="stale TODO critical path",
            route="maqs",
        )
        scanner = _FakeScanner("code_scanner", [maqs_sig])

        orchestrator = SentinelOrchestrator(
            scanners=[scanner],
            store=store,
            workspace_dir=str(workspace),
            config={
                "maqs": {
                    "bitable_app_token": "tok",
                    "bitable_table_id": "tbl",
                }
            },
        )

        with patch(
            "agent.jobs.mads.helpers.bitable_add",
            new_callable=AsyncMock,
            return_value=None,  # bitable returns None on failure, does not raise
        ):
            self._run(orchestrator.run_cycle(trigger="manual"))

        queue_path = workspace / "data" / "intake_queue.jsonl"
        assert queue_path.exists()
        with open(queue_path) as f:
            lines = [l for l in f if l.strip()]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["priority"] == "P1"
        assert entry["signal_id"] == maqs_sig.id

    def test_silent_log_persists_no_external_calls(self, workspace):
        store = SentinelStore(str(workspace / "data" / "sentinel.jsonl"))
        dispatcher = MagicMock()
        dispatcher.send_to_delivery_target = AsyncMock()

        sig = _sig(route="silent_log")
        scanner = _FakeScanner("test", [sig])

        orchestrator = SentinelOrchestrator(
            scanners=[scanner],
            store=store,
            dispatcher=dispatcher,
            workspace_dir=str(workspace),
        )

        summary = self._run(orchestrator.run_cycle(trigger="manual"))

        assert summary["total"] == 1
        assert summary["silent_log"] == 1
        dispatcher.send_to_delivery_target.assert_not_called()

        stored = store.query(hours=24)
        assert len(stored) == 1


# ── L3: CLI commands ──────────────────────────────────────────────────────────

class TestSentinelCLI:
    """L3 — CLI command output format for list / stats / resolve.

    Uses real SentinelStore on tmp_path, patches PROJECT_ROOT so the CLI
    resolves paths to the temp directory instead of the real data dir.
    """

    @pytest.fixture
    def populated(self, tmp_path):
        """Pre-populated store with 2 signals; returns (store, s1, s2, tmp_path)."""
        (tmp_path / "data").mkdir()
        store = SentinelStore(str(tmp_path / "data" / "sentinel.jsonl"))

        s1 = _sig(
            source="code_scanner",
            category="stale_todo",
            severity="medium",
            summary="TODO: remove deprecated call",
            route="maqs",
        )
        s2 = _sig(
            source="code_scanner",
            category="uncommitted_stale",
            severity="high",
            summary="Uncommitted changes in main.py",
            route="notify",
        )
        store.append(s1)
        store.append(s2)
        return store, s1, s2, tmp_path

    def test_list_shows_both_signals(self, populated, capsys):
        store, s1, s2, tmp_path = populated

        import scripts.sentinel_ctl as ctl

        with patch.object(ctl, "PROJECT_ROOT", tmp_path):
            args = argparse.Namespace(hours=24, source=None, unresolved=False, verbose=False)
            ctl.cmd_list(args)

        out = capsys.readouterr().out
        assert s1.id in out
        assert s2.id in out
        assert "TODO: remove deprecated call" in out
        assert "Uncommitted changes" in out
        assert "2 signal(s)" in out

    def test_list_unresolved_excludes_resolved(self, populated, capsys):
        store, s1, s2, tmp_path = populated
        store.resolve(s1.id)

        import scripts.sentinel_ctl as ctl

        with patch.object(ctl, "PROJECT_ROOT", tmp_path):
            args = argparse.Namespace(hours=24, source=None, unresolved=True, verbose=False)
            ctl.cmd_list(args)

        out = capsys.readouterr().out
        assert s1.id not in out
        assert s2.id in out

    def test_list_empty_store(self, tmp_path, capsys):
        (tmp_path / "data").mkdir()

        import scripts.sentinel_ctl as ctl

        with patch.object(ctl, "PROJECT_ROOT", tmp_path):
            args = argparse.Namespace(hours=24, source=None, unresolved=False, verbose=False)
            ctl.cmd_list(args)

        out = capsys.readouterr().out
        assert "No signals found" in out

    def test_stats_format(self, populated, capsys):
        store, s1, s2, tmp_path = populated

        import scripts.sentinel_ctl as ctl

        with patch.object(ctl, "PROJECT_ROOT", tmp_path):
            args = argparse.Namespace(hours=24)
            ctl.cmd_stats(args)

        out = capsys.readouterr().out
        assert "Total" in out
        assert "2" in out
        assert "code_scanner" in out
        assert "maqs" in out
        assert "notify" in out

    def test_resolve_marks_and_confirms(self, populated, capsys):
        store, s1, s2, tmp_path = populated

        import scripts.sentinel_ctl as ctl

        with patch.object(ctl, "PROJECT_ROOT", tmp_path):
            args = argparse.Namespace(signal_id=s1.id)
            ctl.cmd_resolve(args)

        out = capsys.readouterr().out
        assert s1.id in out
        assert "resolved" in out.lower()

        # Verify persistence
        unresolved = store.query(hours=24, unresolved_only=True)
        assert not any(s.id == s1.id for s in unresolved)

    def test_resolve_not_found_exits_1(self, populated, capsys):
        _, _, _, tmp_path = populated

        import scripts.sentinel_ctl as ctl

        with patch.object(ctl, "PROJECT_ROOT", tmp_path):
            args = argparse.Namespace(signal_id="000000000000")
            with pytest.raises(SystemExit) as exc_info:
                ctl.cmd_resolve(args)

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "not found" in out.lower()

    def test_list_verbose_shows_evidence(self, tmp_path, capsys):
        (tmp_path / "data").mkdir()
        store = SentinelStore(str(tmp_path / "data" / "sentinel.jsonl"))
        sig = _sig(evidence=["agent/main.py:42"], summary="stale TODO with evidence")
        store.append(sig)

        import scripts.sentinel_ctl as ctl

        with patch.object(ctl, "PROJECT_ROOT", tmp_path):
            args = argparse.Namespace(hours=24, source=None, unresolved=False, verbose=True)
            ctl.cmd_list(args)

        out = capsys.readouterr().out
        assert "agent/main.py:42" in out
