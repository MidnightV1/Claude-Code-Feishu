# -*- coding: utf-8 -*-
"""Unit tests for the Sentinel autonomous entropy control system.

Covers: models, store, orchestrator.
"""
import asyncio
import json
import os
import time
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent.jobs.sentinel.models import EntropySignal, FixResult, ScanContext
from agent.jobs.sentinel.store import SentinelStore
from agent.jobs.sentinel.orchestrator import (
    SentinelOrchestrator,
    FREQUENCY_INTERVALS,
    SEVERITY_TO_PRIORITY,
)
from agent.jobs.sentinel.scanners.code_scanner import CodeScanner


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_signal(**overrides) -> EntropySignal:
    defaults = dict(
        source="test_scanner",
        category="test_cat",
        severity="medium",
        autonomy_level=0,
        summary="Test signal",
        route="silent_log",
    )
    defaults.update(overrides)
    return EntropySignal(**defaults)


class _DummyScanner:
    """Minimal scanner for orchestrator tests."""
    def __init__(self, name="dummy", signals=None, rate="daily"):
        self.name = name
        self._signals = signals or []
        self._rate = rate

    async def scan(self, context):
        return list(self._signals)

    def estimate_change_rate(self):
        return self._rate


# ── Models ───────────────────────────────────────────────────────────────────

class TestEntropySignal:
    def test_roundtrip(self):
        s = _make_signal(evidence=["file.py:42"], context={"key": "val"})
        d = s.to_dict()
        s2 = EntropySignal.from_dict(d)
        assert s2.source == s.source
        assert s2.evidence == ["file.py:42"]
        assert s2.context == {"key": "val"}
        assert s2.id == s.id

    def test_from_dict_ignores_extra_keys(self):
        d = _make_signal().to_dict()
        d["extra_field"] = "ignored"
        s = EntropySignal.from_dict(d)
        assert s.source == "test_scanner"

    def test_defaults(self):
        s = EntropySignal(
            source="x", category="y", severity="low",
            autonomy_level=0, summary="z",
        )
        assert s.route == "silent_log"
        assert s.resolved_at is None
        assert len(s.id) == 12
        assert s.created_at > 0


class TestFixResult:
    def test_to_dict(self):
        fr = FixResult(success=True, action_taken="deleted stale branch")
        d = fr.to_dict()
        assert d["success"] is True
        assert d["action_taken"] == "deleted stale branch"


class TestScanContext:
    def test_defaults(self):
        ctx = ScanContext(workspace_dir="/tmp")
        assert ctx.last_scan_at == 0.0
        assert ctx.recent_signals == []
        assert ctx.user_config == {}


# ── Store ────────────────────────────────────────────────────────────────────

class TestSentinelStore:
    @pytest.fixture
    def store(self, tmp_path):
        return SentinelStore(path=str(tmp_path / "sentinel.jsonl"))

    def test_append_and_query(self, store):
        s = _make_signal()
        store.append(s)
        results = store.query(hours=1)
        assert len(results) == 1
        assert results[0].id == s.id

    def test_query_time_filter(self, store):
        old = _make_signal(summary="old")
        old.created_at = time.time() - 7200  # 2 hours ago
        recent = _make_signal(summary="recent")
        store.append(old)
        store.append(recent)
        results = store.query(hours=1)
        assert len(results) == 1
        assert results[0].summary == "recent"

    def test_query_source_filter(self, store):
        store.append(_make_signal(source="a"))
        store.append(_make_signal(source="b"))
        results = store.query(hours=1, source="a")
        assert len(results) == 1
        assert results[0].source == "a"

    def test_query_unresolved_only(self, store):
        resolved = _make_signal(summary="resolved")
        resolved.resolved_at = time.time()
        store.append(resolved)
        store.append(_make_signal(summary="open"))
        results = store.query(hours=1, unresolved_only=True)
        assert len(results) == 1
        assert results[0].summary == "open"

    def test_resolve(self, store):
        s = _make_signal()
        store.append(s)
        assert store.resolve(s.id)
        results = store.query(hours=1, unresolved_only=True)
        assert len(results) == 0

    def test_resolve_nonexistent(self, store):
        assert not store.resolve("nonexistent_id")

    def test_get_last_scan_time_empty(self, store):
        assert store.get_last_scan_time("any") == 0.0

    def test_get_last_scan_time(self, store):
        s1 = _make_signal(source="scanner_a")
        s1.created_at = 100.0
        s2 = _make_signal(source="scanner_a")
        s2.created_at = 200.0
        s3 = _make_signal(source="scanner_b")
        s3.created_at = 300.0
        store.append(s1)
        store.append(s2)
        store.append(s3)
        assert store.get_last_scan_time("scanner_a") == 200.0
        assert store.get_last_scan_time("scanner_b") == 300.0

    def test_stats(self, store):
        store.append(_make_signal(source="a", route="maqs"))
        store.append(_make_signal(source="a", route="notify"))
        resolved = _make_signal(source="b", route="silent_log")
        resolved.resolved_at = time.time()
        store.append(resolved)
        s = store.stats(hours=1)
        assert s["total"] == 3
        assert s["resolved"] == 1
        assert s["unresolved"] == 2
        assert s["by_source"]["a"] == 2
        assert s["by_route"]["maqs"] == 1

    def test_corrupt_line_skipped(self, store):
        # Manually write corrupt data
        with open(store._path, "w") as f:
            f.write("not valid json\n")
            f.write(json.dumps(_make_signal().to_dict()) + "\n")
        results = store.query(hours=1)
        assert len(results) == 1  # corrupt line skipped


# ── Orchestrator ─────────────────────────────────────────────────────────────

class TestSentinelOrchestrator:
    @pytest.fixture
    def store(self, tmp_path):
        return SentinelStore(path=str(tmp_path / "sentinel.jsonl"))

    @pytest.fixture
    def orch(self, store):
        return SentinelOrchestrator(
            scanners=[],
            store=store,
            workspace_dir="/tmp/test",
        )

    def test_register_scanner(self, orch):
        s = _DummyScanner("new_one")
        orch.register_scanner(s)
        assert len(orch._scanners) == 1
        assert orch._scanners[0].name == "new_one"

    @pytest.mark.asyncio
    async def test_run_cycle_empty(self, orch):
        summary = await orch.run_cycle(trigger="manual")
        assert summary["total"] == 0

    @pytest.mark.asyncio
    async def test_run_cycle_collects_signals(self, store):
        sig = _make_signal(route="silent_log")
        scanner = _DummyScanner("test", signals=[sig])
        orch = SentinelOrchestrator(scanners=[scanner], store=store)
        summary = await orch.run_cycle(trigger="manual")
        assert summary["total"] == 1
        assert summary["silent_log"] == 1
        # Verify persisted
        assert len(store.query(hours=1)) == 1

    @pytest.mark.asyncio
    async def test_run_cycle_dedup(self, store):
        # Pre-existing unresolved signal
        existing = _make_signal(source="scanner", category="cat", summary="dup")
        store.append(existing)
        # Scanner produces identical signal
        new_dup = _make_signal(source="scanner", category="cat", summary="dup")
        scanner = _DummyScanner("scanner", signals=[new_dup])
        orch = SentinelOrchestrator(scanners=[scanner], store=store)
        summary = await orch.run_cycle(trigger="manual")
        assert summary["total"] == 0  # deduped

    @pytest.mark.asyncio
    async def test_run_cycle_severity_order(self, store):
        signals = [
            _make_signal(severity="low", summary="low"),
            _make_signal(severity="critical", summary="critical"),
            _make_signal(severity="medium", summary="medium"),
        ]
        # Each signal must have unique (source, category, summary) to avoid dedup
        for i, s in enumerate(signals):
            s.category = f"cat_{i}"
        scanner = _DummyScanner("test", signals=signals)
        orch = SentinelOrchestrator(scanners=[scanner], store=store)
        await orch.run_cycle(trigger="manual")
        persisted = store.query(hours=1)
        # Should be ordered: critical, medium, low
        assert persisted[0].severity == "critical"
        assert persisted[1].severity == "medium"
        assert persisted[2].severity == "low"

    @pytest.mark.asyncio
    async def test_scanner_error_isolation(self, store):
        class _FailScanner:
            name = "fail"
            async def scan(self, ctx):
                raise RuntimeError("boom")
            def estimate_change_rate(self):
                return "daily"

        good = _DummyScanner("good", signals=[_make_signal()])
        bad = _FailScanner()
        orch = SentinelOrchestrator(scanners=[bad, good], store=store)
        summary = await orch.run_cycle(trigger="manual")
        assert summary["total"] == 1  # good scanner still runs

    def test_due_scanners_manual_returns_all(self, store):
        scanners = [_DummyScanner(f"s{i}") for i in range(3)]
        orch = SentinelOrchestrator(scanners=scanners, store=store)
        due = orch._due_scanners("manual")
        assert len(due) == 3

    def test_due_scanners_frequency_check(self, store):
        # Scanner with "hourly" rate, last ran 30 min ago → not due
        scanner = _DummyScanner("hourly_scanner", rate="hourly")
        recent_signal = _make_signal(source="hourly_scanner")
        recent_signal.created_at = time.time() - 1800  # 30 min ago
        store.append(recent_signal)
        orch = SentinelOrchestrator(scanners=[scanner], store=store)
        due = orch._due_scanners("idle")
        assert len(due) == 0

    def test_due_scanners_overdue(self, store):
        # Scanner with "hourly" rate, last ran 2 hours ago → due
        scanner = _DummyScanner("old_scanner", rate="hourly")
        old_signal = _make_signal(source="old_scanner")
        old_signal.created_at = time.time() - 7200  # 2 hours ago
        store.append(old_signal)
        orch = SentinelOrchestrator(scanners=[scanner], store=store)
        due = orch._due_scanners("idle")
        assert len(due) == 1

    @pytest.mark.asyncio
    async def test_route_notify(self, store):
        dispatcher = MagicMock()
        sig = _make_signal(route="notify", severity="high")
        orch = SentinelOrchestrator(
            scanners=[_DummyScanner("test", signals=[sig])],
            store=store,
            dispatcher=dispatcher,
        )
        with patch.object(orch, "_notify_user", new_callable=AsyncMock) as mock_notify:
            await orch.run_cycle(trigger="manual")
            mock_notify.assert_called_once()


# ── MotivationReviewer ───────────────────────────────────────────────────────

# ── CodeScanner ──────────────────────────────────────────────────────────────

class TestCodeScanner:
    @pytest.fixture
    def scanner(self):
        return CodeScanner()

    def _make_proc(self, stdout_bytes, returncode=0):
        proc = MagicMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(stdout_bytes, b""))
        return proc

    def _blame_output(self, timestamp: int) -> bytes:
        return (
            f"abc123def456 1 1 1\n"
            f"author Test\n"
            f"author-time {timestamp}\n"
            f"filename agent/foo.py\n"
            f"\t# TODO: fix this\n"
        ).encode()

    @pytest.mark.asyncio
    async def test_gold_standard(self, scanner):
        """金标准：30+ 天 TODO → category=stale_todo, severity=medium, route=maqs, evidence=['agent/foo.py:42']"""
        workspace = "/workspace"
        context = ScanContext(workspace_dir=workspace)

        grep_out = b"/workspace/agent/foo.py:42:    # TODO: fix this\n"
        old_ts = int(time.time()) - 40 * 86400
        blame_out = self._blame_output(old_ts)

        async def fake_exec(*args, **kwargs):
            if args and args[0] == "grep":
                return self._make_proc(grep_out)
            return self._make_proc(blame_out)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await scanner._scan_todos(context)

        assert len(signals) == 1
        s = signals[0]
        assert s.source == "code_scanner"
        assert s.category == "stale_todo"
        assert s.severity == "medium"
        assert s.route == "maqs"
        assert s.evidence == ["agent/foo.py:42"]

    @pytest.mark.asyncio
    async def test_fresh_todo_no_signal(self, scanner):
        """5 天内的 TODO → 不产出信号"""
        workspace = "/workspace"
        context = ScanContext(workspace_dir=workspace)

        grep_out = b"/workspace/agent/foo.py:10:    # TODO: old but fresh\n"
        fresh_ts = int(time.time()) - 5 * 86400
        blame_out = self._blame_output(fresh_ts)

        async def fake_exec(*args, **kwargs):
            if args and args[0] == "grep":
                return self._make_proc(grep_out)
            return self._make_proc(blame_out)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await scanner._scan_todos(context)

        assert signals == []

    @pytest.mark.asyncio
    async def test_stale_fixme_is_medium(self, scanner):
        """30+ 天 FIXME → severity=medium"""
        workspace = "/workspace"
        context = ScanContext(workspace_dir=workspace)

        grep_out = b"/workspace/agent/bar.py:5:    # FIXME: broken\n"
        old_ts = int(time.time()) - 35 * 86400
        blame_out = (
            f"abc123 1 1 1\nauthor-time {old_ts}\nfilename agent/bar.py\n\t# FIXME: broken\n"
        ).encode()

        async def fake_exec(*args, **kwargs):
            if args and args[0] == "grep":
                return self._make_proc(grep_out)
            return self._make_proc(blame_out)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await scanner._scan_todos(context)

        assert len(signals) == 1
        assert signals[0].severity == "medium"
        assert signals[0].category == "stale_todo"

    @pytest.mark.asyncio
    async def test_evidence_pure_path(self, scanner):
        """evidence 格式为纯路径，不含注释文本"""
        workspace = "/workspace"
        context = ScanContext(workspace_dir=workspace)

        grep_out = b"/workspace/agent/foo.py:99:    # TODO: remove me please\n"
        old_ts = int(time.time()) - 60 * 86400
        blame_out = self._blame_output(old_ts)

        async def fake_exec(*args, **kwargs):
            if args and args[0] == "grep":
                return self._make_proc(grep_out)
            return self._make_proc(blame_out)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await scanner._scan_todos(context)

        assert len(signals) == 1
        ev = signals[0].evidence[0]
        assert ev == "agent/foo.py:99"
        assert "remove me" not in ev

    @pytest.mark.asyncio
    async def test_no_grep_output_returns_empty(self, scanner):
        """grep 无结果 → 空列表"""
        context = ScanContext(workspace_dir="/workspace")

        async def fake_exec(*args, **kwargs):
            return self._make_proc(b"")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await scanner._scan_todos(context)

        assert signals == []


# ── DocAuditor ────────────────────────────────────────────────────────────────

class TestDocAuditor:
    from agent.jobs.sentinel.scanners.doc_auditor import DocAuditor

    @pytest.fixture
    def auditor(self):
        from agent.jobs.sentinel.scanners.doc_auditor import DocAuditor
        return DocAuditor()

    def _make_proc(self, stdout_bytes, returncode=0):
        proc = MagicMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(stdout_bytes, b""))
        return proc

    @pytest.mark.asyncio
    async def test_gold_standard_stale_doc(self, auditor):
        """金标准：router.py 变更 → 搜索 'LLM router' → 找到 30 天前更新文档
        → EntropySignal(category='stale_doc', severity='low', route='notify')
        """
        context = ScanContext(workspace_dir="/workspace")
        stale_epoch = int(time.time()) - 31 * 86400  # 31 days ago

        # git log returns router.py as changed
        git_out = b"agent/llm/router.py\n"
        # native-search for any "router" keyword returns stale doc (TSV)
        search_out = f"tok_abc123\tLLM Router Architecture\t{stale_epoch}\n".encode()

        async def fake_exec(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "git":
                return self._make_proc(git_out)
            # doc_ctl.py native-search
            return self._make_proc(search_out)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await auditor._check_staleness(context)

        assert len(signals) >= 1
        s = signals[0]
        assert s.source == "doc_auditor"
        assert s.category == "stale_doc"
        assert s.severity == "low"
        assert s.route == "notify"
        assert "router" in s.summary.lower() or "router.py" in s.summary

    @pytest.mark.asyncio
    async def test_fresh_doc_no_signal(self, auditor):
        """30 天内更新的文档 → 不产出信号"""
        context = ScanContext(workspace_dir="/workspace")
        fresh_epoch = int(time.time()) - 5 * 86400  # 5 days ago

        git_out = b"agent/llm/router.py\n"
        search_out = f"tok_xyz\tLLM Router Doc\t{fresh_epoch}\n".encode()

        async def fake_exec(*args, **kwargs):
            if args and args[0] == "git":
                return self._make_proc(git_out)
            return self._make_proc(search_out)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await auditor._check_staleness(context)

        assert signals == []

    @pytest.mark.asyncio
    async def test_no_changed_modules_no_signal(self, auditor):
        """git log 无变更 → 空列表"""
        context = ScanContext(workspace_dir="/workspace")

        async def fake_exec(*args, **kwargs):
            return self._make_proc(b"")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await auditor._check_staleness(context)

        assert signals == []

    @pytest.mark.asyncio
    async def test_recall_monitor_native_search(self, auditor):
        """recall monitor 使用 native-search；有结果时不产出信号"""
        context = ScanContext(workspace_dir="/workspace")
        search_out = b"tok_plan\tPLAN Architecture\t1700000000\n"

        async def fake_exec(*args, **kwargs):
            return self._make_proc(search_out)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await auditor._monitor_recall(context)

        assert signals == []

    @pytest.mark.asyncio
    async def test_recall_monitor_zero_results_signals(self, auditor):
        """native-search 返回空 → search_recall_degradation 信号"""
        context = ScanContext(workspace_dir="/workspace")

        async def fake_exec(*args, **kwargs):
            return self._make_proc(b"")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await auditor._monitor_recall(context)

        assert len(signals) >= 1
        assert signals[0].category == "search_recall_degradation"


# ── MAQS ticket creation (gold standard) ─────────────────────────────────────

class TestCreateMaqsTicket:
    """金标准：signal(severity='high') → _create_maqs_ticket() → bitable_add()
    含 severity='P1', source='sentinel:health_pulse'
    """

    @pytest.mark.asyncio
    async def test_gold_standard_bitable_creation(self, tmp_path):
        store = SentinelStore(path=str(tmp_path / "sentinel.jsonl"))
        config = {
            "maqs": {
                "bitable_app_token": "fake_token",
                "bitable_table_id": "fake_table",
            }
        }
        orch = SentinelOrchestrator(
            scanners=[],
            store=store,
            workspace_dir=str(tmp_path),
            config=config,
        )

        signal = EntropySignal(
            source="health_pulse",
            category="error_spike",
            severity="high",
            autonomy_level=2,
            summary="Error spike",
            route="maqs",
        )

        mock_add = AsyncMock(return_value="rec_fake")
        with patch("agent.jobs.mads.helpers.bitable_add", new=mock_add):
            await orch._create_maqs_ticket(signal)

        mock_add.assert_called_once()
        fields = mock_add.call_args[0][2]
        assert fields["severity"] == "P1"
        assert fields["source"] == "sentinel:health_pulse"
        assert fields["type"] == "bug"

    @pytest.mark.asyncio
    async def test_skips_when_bitable_not_configured(self, tmp_path):
        """Bitable 未配置时应 warning 并跳过"""
        store = SentinelStore(path=str(tmp_path / "sentinel.jsonl"))
        orch = SentinelOrchestrator(
            scanners=[],
            store=store,
            workspace_dir=str(tmp_path),
            config={},
        )

        signal = EntropySignal(
            source="health_pulse",
            category="error_spike",
            severity="high",
            autonomy_level=2,
            summary="Error spike",
            route="maqs",
        )

        mock_add = AsyncMock()
        with patch("agent.jobs.mads.helpers.bitable_add", new=mock_add):
            await orch._create_maqs_ticket(signal)

        mock_add.assert_not_called()


# ── CLI output format (gold standard) ────────────────────────────────────────

class TestCmdScanOutput:
    """金标准：sentinel_ctl.py scan 一次输出完整报告，格式匹配金标准。"""

    @pytest.mark.asyncio
    async def test_gold_standard_output(self, capsys, tmp_path):
        """scan 输出格式匹配金标准：header / route 列表 / Signals 明细"""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
        import importlib
        import scripts.sentinel_ctl as sentinel_ctl

        signals = [
            EntropySignal(
                source="code_scanner",
                category="stale_todo",
                severity="medium",
                autonomy_level=1,
                summary="TODO older than 30 days",
                evidence=["agent/foo.py:42"],
                route="maqs",
            ),
            EntropySignal(
                source="doc_auditor",
                category="doc_stale",
                severity="low",
                autonomy_level=0,
                summary="Doc not updated for 60 days",
                evidence=["docs/PLAN.md"],
                route="notify",
            ),
            EntropySignal(
                source="doc_auditor",
                category="doc_missing",
                severity="low",
                autonomy_level=0,
                summary="Missing README",
                evidence=["agent/foo/"],
                route="notify",
            ),
        ]
        summary = {"total": 3, "maqs": 1, "explore": 0, "notify": 2, "silent_log": 0, "signals": signals}

        store = SentinelStore(str(tmp_path / "sentinel.jsonl"))
        with patch.object(SentinelOrchestrator, "run_cycle", new=AsyncMock(return_value=summary)):
            import argparse
            args = argparse.Namespace(scanner=None)
            with patch("scripts.sentinel_ctl.SentinelStore", return_value=store):
                with patch("scripts.sentinel_ctl.SentinelOrchestrator") as MockOrch:
                    MockOrch.return_value.run_cycle = AsyncMock(return_value=summary)
                    await sentinel_ctl.cmd_scan(args)

        captured = capsys.readouterr().out
        assert "Sentinel scan complete: 3 signals found" in captured
        assert "- maqs: 1" in captured
        assert "- notify: 2" in captured
        assert "Signals:" in captured
        assert "1. [code_scanner] stale_todo: TODO older than 30 days (agent/foo.py:42)" in captured
        # non-zero routes only
        assert "- explore:" not in captured
        assert "- silent_log:" not in captured
        # no old-style prompt
        assert "Run `sentinel_ctl.py list`" not in captured


# ── _create_exploration_task (gold standard) ─────────────────────────────────

class TestCreateExplorationTask:
    """金标准：signal(source='health_pulse', summary='Unused skill: foo')
    → _create_exploration_task() → ExplorationTask(pillar='ops', priority=P2_NORMAL)
    """

    @pytest.mark.asyncio
    async def test_gold_standard(self, tmp_path):
        from agent.infra.exploration import Priority

        store = SentinelStore(path=str(tmp_path / "sentinel.jsonl"))
        orch = SentinelOrchestrator(scanners=[], store=store, workspace_dir=str(tmp_path))

        signal = EntropySignal(
            source="health_pulse",
            category="low_usage_skill",
            severity="low",
            autonomy_level=0,
            summary="Unused skill: foo",
            route="explore",
            evidence=["skills/foo: 0 calls in 30d"],
        )

        mock_queue = MagicMock()
        mock_queue.load = AsyncMock()
        mock_queue.add = AsyncMock()

        with patch("agent.infra.exploration.ExplorationQueue", return_value=mock_queue):
            await orch._create_exploration_task(signal)

        mock_queue.load.assert_called_once()
        mock_queue.add.assert_called_once()
        task = mock_queue.add.call_args[0][0]
        assert task.pillar == "ops"
        assert task.priority == Priority.P2_NORMAL
        assert task.source == "sentinel:health_pulse"
        assert "Unused skill: foo" in task.title

    @pytest.mark.asyncio
    async def test_high_severity_gets_p1(self, tmp_path):
        from agent.infra.exploration import Priority

        store = SentinelStore(path=str(tmp_path / "sentinel.jsonl"))
        orch = SentinelOrchestrator(scanners=[], store=store, workspace_dir=str(tmp_path))

        signal = EntropySignal(
            source="health_pulse",
            category="critical_skill",
            severity="high",
            autonomy_level=0,
            summary="Critical skill degraded",
            route="explore",
        )

        mock_queue = MagicMock()
        mock_queue.load = AsyncMock()
        mock_queue.add = AsyncMock()

        with patch("agent.infra.exploration.ExplorationQueue", return_value=mock_queue):
            await orch._create_exploration_task(signal)

        task = mock_queue.add.call_args[0][0]
        assert task.priority == Priority.P1_HIGH


# ── HealthPulse ───────────────────────────────────────────────────────────────

class TestHealthPulse:
    @pytest.fixture
    def scanner(self):
        from agent.jobs.sentinel.scanners.health_pulse import HealthPulse
        return HealthPulse()

    def _make_proc(self, stdout_bytes, returncode=0):
        proc = MagicMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(stdout_bytes, b""))
        return proc

    @pytest.mark.asyncio
    async def test_error_spike_gold_standard(self, scanner, tmp_path):
        """金标准：过去5小时各1个错误(avg≈1.67/hr)，当前小时5个 → error_spike 信号"""
        now = time.time()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        error_file = data_dir / "error_tracker.jsonl"
        entries = []
        # Past: 1 error per hour for 5 hours (distinct buckets)
        for i in range(1, 6):
            entries.append(json.dumps({"timestamp": now - i * 3600 - 30, "error_type": "past_error"}))
        # Current hour: 5 errors → 5 > 2 × avg≈1.67
        for i in range(5):
            entries.append(json.dumps({"timestamp": now - i * 60, "error_type": "spike"}))
        error_file.write_text("\n".join(entries))

        context = ScanContext(workspace_dir=str(tmp_path))

        async def fake_exec(*args, **kwargs):
            return self._make_proc(b"abc123")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await scanner._check_error_rate(tmp_path, context)

        assert len(signals) == 1
        s = signals[0]
        assert s.category == "error_spike"
        assert s.severity == "high"
        assert s.route == "maqs"

    @pytest.mark.asyncio
    async def test_no_error_spike(self, scanner, tmp_path):
        """当前小时错误数未超过2x基线 → 无信号"""
        now = time.time()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        error_file = data_dir / "error_tracker.jsonl"
        entries = []
        # Past: 10 errors per hour × 5 hours
        for i in range(1, 6):
            for j in range(10):
                entries.append(json.dumps({"timestamp": now - i * 3600 - j * 100, "error_type": "e"}))
        # Current hour: 15 errors (< 2 × ~10.8 avg)
        for i in range(15):
            entries.append(json.dumps({"timestamp": now - i * 20, "error_type": "e"}))
        error_file.write_text("\n".join(entries))

        context = ScanContext(workspace_dir=str(tmp_path))

        async def fake_exec(*args, **kwargs):
            return self._make_proc(b"")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await scanner._check_error_rate(tmp_path, context)

        assert signals == []

    @pytest.mark.asyncio
    async def test_error_rate_no_file(self, scanner, tmp_path):
        """error_tracker.jsonl 不存在 → 空列表"""
        context = ScanContext(workspace_dir=str(tmp_path))
        signals = await scanner._check_error_rate(tmp_path, context)
        assert signals == []

    @pytest.mark.asyncio
    async def test_error_rate_iso_timestamp(self, scanner, tmp_path):
        """ISO 8601 时间戳可正常解析，spike 照常触发"""
        import datetime as _dt
        now = time.time()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        error_file = data_dir / "error_tracker.jsonl"
        entries = []
        # Past: 1 error per hour × 5 hours in ISO format
        for i in range(1, 6):
            ts = now - i * 3600 - 30
            iso = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()
            entries.append(json.dumps({"timestamp": iso, "error_type": "past"}))
        # Current hour: 5 errors in ISO format
        for i in range(5):
            ts = now - i * 60
            iso = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()
            entries.append(json.dumps({"timestamp": iso, "error_type": "spike"}))
        error_file.write_text("\n".join(entries))

        context = ScanContext(workspace_dir=str(tmp_path))

        async def fake_exec(*args, **kwargs):
            return self._make_proc(b"")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await scanner._check_error_rate(tmp_path, context)

        assert len(signals) == 1
        assert signals[0].category == "error_spike"

    @pytest.mark.asyncio
    async def test_skill_unused_gold_standard(self, scanner, tmp_path):
        """金标准：foo skill 无近期使用 → skill_unused 信号"""
        now = time.time()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        skills_dir = tmp_path / ".claude" / "skills" / "foo"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("# foo")

        usage_file = data_dir / "skill_usage.jsonl"
        usage_file.write_text(json.dumps({"timestamp": now - 1000, "skill": "bar"}) + "\n")

        context = ScanContext(workspace_dir=str(tmp_path))
        signals = await scanner._check_skill_usage(tmp_path, context)

        assert len(signals) == 1
        assert signals[0].category == "skill_unused"
        assert "foo" in signals[0].summary

    @pytest.mark.asyncio
    async def test_skill_all_active_no_signal(self, scanner, tmp_path):
        """所有安装 skill 均有近期使用 → 无信号"""
        now = time.time()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        skills_dir = tmp_path / ".claude" / "skills" / "foo"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("# foo")

        usage_file = data_dir / "skill_usage.jsonl"
        usage_file.write_text(json.dumps({"timestamp": now - 1000, "skill": "foo"}) + "\n")

        context = ScanContext(workspace_dir=str(tmp_path))
        signals = await scanner._check_skill_usage(tmp_path, context)

        assert signals == []

    @pytest.mark.asyncio
    async def test_skill_no_usage_file(self, scanner, tmp_path):
        """skill_usage.jsonl 不存在 → 空列表"""
        context = ScanContext(workspace_dir=str(tmp_path))
        signals = await scanner._check_skill_usage(tmp_path, context)
        assert signals == []

    @pytest.mark.asyncio
    async def test_branch_stale_gold_standard(self, scanner, tmp_path):
        """金标准：feature/old 无近期提交 → branch_stale 信号"""
        context = ScanContext(workspace_dir=str(tmp_path))
        branch_list = b"  dev\n  master\n  feature/old\n"
        recent_log = b"HEAD -> dev, origin/dev\n"

        def fake_exec(*args, **kwargs):
            proc = MagicMock()
            proc.returncode = 0
            if "branch" in args:
                proc.communicate = AsyncMock(return_value=(branch_list, b""))
            else:
                proc.communicate = AsyncMock(return_value=(recent_log, b""))
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await scanner._check_branch_hygiene(tmp_path, context)

        assert len(signals) >= 1
        assert any(s.context["branch"] == "feature/old" for s in signals)

    @pytest.mark.asyncio
    async def test_branch_protected_excluded(self, scanner, tmp_path):
        """dev/master/opensource 不产出 branch_stale 信号"""
        context = ScanContext(workspace_dir=str(tmp_path))
        branch_list = b"  dev\n  master\n  opensource\n"
        recent_log = b""

        def fake_exec(*args, **kwargs):
            proc = MagicMock()
            proc.returncode = 0
            if "branch" in args:
                proc.communicate = AsyncMock(return_value=(branch_list, b""))
            else:
                proc.communicate = AsyncMock(return_value=(recent_log, b""))
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await scanner._check_branch_hygiene(tmp_path, context)

        assert signals == []

    @pytest.mark.asyncio
    async def test_disk_usage_over_threshold(self, scanner, tmp_path):
        """data/ 超过 DISK_WARN_MB(100MB) → disk_usage_high 信号"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        big_file = data_dir / "big.jsonl"
        # Sparse file: reports 101 MB without allocating real disk space
        with open(big_file, "wb") as f:
            f.seek(101 * 1024 * 1024 - 1)
            f.write(b"\x00")

        context = ScanContext(workspace_dir=str(tmp_path))
        signals = await scanner._check_disk_usage(tmp_path, context)

        assert len(signals) == 1
        assert signals[0].category == "disk_usage_high"
        assert signals[0].severity == "medium"

    @pytest.mark.asyncio
    async def test_disk_usage_below_threshold(self, scanner, tmp_path):
        """data/ 未超阈值 → 无信号"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "small.txt").write_bytes(b"x" * 1024)

        context = ScanContext(workspace_dir=str(tmp_path))
        signals = await scanner._check_disk_usage(tmp_path, context)

        assert signals == []

    @pytest.mark.asyncio
    async def test_scan_isolates_sub_check_exceptions(self, scanner, tmp_path):
        """某子检查抛异常时，scan() 不传播，其余检查照常完成"""
        context = ScanContext(workspace_dir=str(tmp_path))

        async def boom(*args, **kwargs):
            raise RuntimeError("simulated")

        with patch.object(scanner, "_check_error_rate", side_effect=boom), \
             patch.object(scanner, "_check_skill_usage", return_value=[]), \
             patch.object(scanner, "_check_branch_hygiene", return_value=[]), \
             patch.object(scanner, "_check_disk_usage", return_value=[]):
            signals = await scanner.scan(context)

        assert isinstance(signals, list)


# ── CodeScanner (uncommitted + stale_branches) ───────────────────────────────

class TestCodeScannerUncommitted:
    @pytest.fixture
    def scanner(self):
        return CodeScanner()

    def _make_proc(self, stdout_bytes, returncode=0):
        proc = MagicMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(stdout_bytes, b""))
        return proc

    @pytest.mark.asyncio
    async def test_stale_uncommitted_gold_standard(self, scanner, tmp_path):
        """金标准：file.py 有未暂存修改且 mtime > 24h → uncommitted_stale 信号"""
        stale_file = tmp_path / "file.py"
        stale_file.write_text("# changed")
        stale_ts = time.time() - 25 * 3600
        os.utime(stale_file, (stale_ts, stale_ts))

        git_status = b" M file.py\n"

        async def fake_exec(*args, **kwargs):
            return self._make_proc(git_status)

        context = ScanContext(workspace_dir=str(tmp_path))
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await scanner._scan_uncommitted(context)

        assert len(signals) == 1
        s = signals[0]
        assert s.category == "uncommitted_stale"
        assert s.severity == "medium"
        assert s.route == "notify"

    @pytest.mark.asyncio
    async def test_no_uncommitted_changes(self, scanner, tmp_path):
        """git status 无变更 → 空列表"""
        async def fake_exec(*args, **kwargs):
            return self._make_proc(b"")

        context = ScanContext(workspace_dir=str(tmp_path))
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await scanner._scan_uncommitted(context)

        assert signals == []

    @pytest.mark.asyncio
    async def test_uncommitted_recent_file_no_signal(self, scanner, tmp_path):
        """mtime < 24h 的未暂存文件 → 无信号"""
        fresh_file = tmp_path / "fresh.py"
        fresh_file.write_text("# fresh")
        # mtime is now (well within 24h)

        git_status = b" M fresh.py\n"

        async def fake_exec(*args, **kwargs):
            return self._make_proc(git_status)

        context = ScanContext(workspace_dir=str(tmp_path))
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await scanner._scan_uncommitted(context)

        assert signals == []

    @pytest.mark.asyncio
    async def test_uncommitted_rename_uses_new_path(self, scanner, tmp_path):
        """rename 格式 (old.py -> new.py) → 以 new.py 的 mtime 判断"""
        new_file = tmp_path / "new.py"
        new_file.write_text("# renamed")
        stale_ts = time.time() - 25 * 3600
        os.utime(new_file, (stale_ts, stale_ts))

        git_status = b" R old.py -> new.py\n"

        async def fake_exec(*args, **kwargs):
            return self._make_proc(git_status)

        context = ScanContext(workspace_dir=str(tmp_path))
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await scanner._scan_uncommitted(context)

        assert len(signals) == 1
        assert "new.py" in signals[0].context["files"]


class TestCodeScannerStaleBranches:
    @pytest.fixture
    def scanner(self):
        return CodeScanner()

    def _make_proc(self, stdout_bytes, returncode=0):
        proc = MagicMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(stdout_bytes, b""))
        return proc

    @pytest.mark.asyncio
    async def test_merged_branch_gold_standard(self, scanner, tmp_path):
        """金标准：feature/done 已 merge → stale_branch 信号"""
        merged_out = b"  dev\n  feature/done\n"

        async def fake_exec(*args, **kwargs):
            return self._make_proc(merged_out)

        context = ScanContext(workspace_dir=str(tmp_path))
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await scanner._scan_stale_branches(context)

        assert len(signals) >= 1
        assert any(s.context["branch"] == "feature/done" for s in signals)

    @pytest.mark.asyncio
    async def test_protected_branches_excluded(self, scanner, tmp_path):
        """dev/master/opensource 不产出 stale_branch 信号"""
        merged_out = b"  dev\n  master\n  opensource\n"

        async def fake_exec(*args, **kwargs):
            return self._make_proc(merged_out)

        context = ScanContext(workspace_dir=str(tmp_path))
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await scanner._scan_stale_branches(context)

        assert signals == []

    @pytest.mark.asyncio
    async def test_no_merged_branches(self, scanner, tmp_path):
        """git branch --merged 无输出 → 空列表"""
        async def fake_exec(*args, **kwargs):
            return self._make_proc(b"")

        context = ScanContext(workspace_dir=str(tmp_path))
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            signals = await scanner._scan_stale_branches(context)

        assert signals == []


# ── DocAuditor (duplicates + helpers) ────────────────────────────────────────

class TestDocAuditorDuplicates:
    @pytest.fixture
    def auditor(self):
        from agent.jobs.sentinel.scanners.doc_auditor import DocAuditor
        return DocAuditor()

    def test_normalize_title_strips_square_brackets(self):
        from agent.jobs.sentinel.scanners.doc_auditor import _normalize_title
        assert _normalize_title("[MADS] 设计文档") == "设计文档"

    def test_normalize_title_strips_chinese_brackets(self):
        from agent.jobs.sentinel.scanners.doc_auditor import _normalize_title
        result = _normalize_title("【日报】2024-01-01")
        assert "【日报】" not in result

    def test_normalize_title_strips_hash_tags(self):
        from agent.jobs.sentinel.scanners.doc_auditor import _normalize_title
        result = _normalize_title("Meeting Notes #internal")
        assert "#internal" not in result

    def test_normalize_title_lowercases(self):
        from agent.jobs.sentinel.scanners.doc_auditor import _normalize_title
        assert _normalize_title("LLM Router") == "llm router"

    def test_titles_similar_identical(self):
        from agent.jobs.sentinel.scanners.doc_auditor import _titles_similar
        assert _titles_similar("foo bar baz", "foo bar baz") is True

    def test_titles_similar_high_overlap(self):
        from agent.jobs.sentinel.scanners.doc_auditor import _titles_similar
        # "router architecture design" vs "router architecture design guide" → 3/3 short overlap
        assert _titles_similar("router architecture design", "router architecture design guide") is True

    def test_titles_similar_low_overlap(self):
        from agent.jobs.sentinel.scanners.doc_auditor import _titles_similar
        assert _titles_similar("feishu bot setup", "sentinel scanner design") is False

    def test_check_duplicates_similar_titles(self, auditor):
        """相似标题的文档对 → doc_duplicate 信号"""
        context = ScanContext(workspace_dir="/workspace")
        docs = [
            {"doc_id": "doc_a", "title": "LLM Router Architecture", "updated_at": ""},
            {"doc_id": "doc_b", "title": "LLM Router Architecture Guide", "updated_at": ""},
            {"doc_id": "doc_c", "title": "Briefing Pipeline Setup", "updated_at": ""},
        ]
        signals = auditor._check_duplicates(docs, context)

        assert len(signals) >= 1
        assert signals[0].category == "doc_duplicate"
        assert signals[0].route == "notify"

    def test_check_duplicates_distinct_titles(self, auditor):
        """完全不同的标题 → 无信号"""
        context = ScanContext(workspace_dir="/workspace")
        docs = [
            {"doc_id": "doc_a", "title": "Feishu Bot Setup", "updated_at": ""},
            {"doc_id": "doc_b", "title": "Sentinel Scanner Design", "updated_at": ""},
            {"doc_id": "doc_c", "title": "Daily Briefing Pipeline", "updated_at": ""},
        ]
        signals = auditor._check_duplicates(docs, context)

        assert signals == []

    def test_check_duplicates_dedup_via_recent_signals(self, auditor):
        """recent_signals 中已有相同摘要的信号 → 不重复产出"""
        docs = [
            {"doc_id": "doc_a", "title": "LLM Router Architecture", "updated_at": ""},
            {"doc_id": "doc_b", "title": "LLM Router Architecture Guide", "updated_at": ""},
        ]
        recent = [_make_signal(
            source="doc_auditor",
            category="doc_duplicate",
            summary="Possible duplicate docs: 'LLM Router Architecture' ≈ 'LLM Router Architecture Guide'",
        )]
        context = ScanContext(workspace_dir="/workspace", recent_signals=recent)
        signals = auditor._check_duplicates(docs, context)

        assert signals == []
