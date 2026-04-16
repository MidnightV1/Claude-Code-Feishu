# -*- coding: utf-8 -*-
"""Unit tests for HealthPulse error spike detection + commit correlation.

Gold standard:
  Error spike: 10 errors/hour vs baseline 2/hour
  → EntropySignal(category='error_spike', severity='high', route='maqs',
                  context={'correlated_commit': 'abc123'})
"""
import asyncio
import json
import logging
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent.jobs.sentinel.models import ScanContext
from agent.jobs.sentinel.scanners.health_pulse import HealthPulse


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_error_tracker(path: Path, current_hour_count: int, baseline_hourly: float, days: int = 7):
    """Write synthetic error_tracker.jsonl: baseline spread over past N days + spike in current hour."""
    now = time.time()
    lines = []

    # Baseline: distribute baseline_hourly errors across each past hour (days*24 hours)
    total_baseline_hours = days * 24
    per_hour = int(baseline_hourly)
    for h in range(1, total_baseline_hours + 1):
        ts_base = now - h * 3600
        for _ in range(per_hour):
            lines.append(json.dumps({"timestamp": ts_base - 60, "level": "ERROR",
                                     "error_type": "ERROR", "message": "baseline error"}))

    # Spike: current_hour_count errors in the last 30 minutes
    for i in range(current_hour_count):
        lines.append(json.dumps({"timestamp": now - i * 60, "level": "ERROR",
                                 "error_type": "ValueError", "message": "spike error"}))

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestHealthPulseErrorSpike:
    def setup_method(self):
        self.scanner = HealthPulse()

    def _run(self, coro):
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)

    def test_no_file_returns_empty(self, tmp_path):
        ctx = ScanContext(workspace_dir=str(tmp_path))
        signals = self._run(self.scanner._check_error_rate(tmp_path, ctx))
        assert signals == []

    def test_gold_standard_spike_with_commit(self, tmp_path):
        """Gold standard: 10 errors/hour vs baseline 2/hour → error_spike signal with correlated_commit."""
        (tmp_path / "data").mkdir()
        error_file = tmp_path / "data" / "error_tracker.jsonl"
        _write_error_tracker(error_file, current_hour_count=10, baseline_hourly=2)

        ctx = ScanContext(workspace_dir=str(tmp_path))

        # Mock git to return commit abc123
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"abc123\n", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            signals = self._run(self.scanner._check_error_rate(tmp_path, ctx))

        assert len(signals) == 1
        sig = signals[0]
        assert sig.category == "error_spike"
        assert sig.severity == "high"
        assert sig.route == "maqs"
        assert sig.context["correlated_commit"] == "abc123"
        assert sig.context["current_hour_count"] == 10
        assert sig.context["avg_hourly_7d"] > 0

    def test_no_spike_below_threshold(self, tmp_path):
        """Normal rate (4/hour vs baseline 2/hour = 2× — exactly at threshold, should NOT fire)."""
        (tmp_path / "data").mkdir()
        error_file = tmp_path / "data" / "error_tracker.jsonl"
        _write_error_tracker(error_file, current_hour_count=4, baseline_hourly=2)

        ctx = ScanContext(workspace_dir=str(tmp_path))
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            signals = self._run(self.scanner._check_error_rate(tmp_path, ctx))

        assert signals == []

    def test_commit_correlation_empty_on_no_recent_commit(self, tmp_path):
        """If no commit in last hour, correlated_commit is empty string."""
        (tmp_path / "data").mkdir()
        error_file = tmp_path / "data" / "error_tracker.jsonl"
        _write_error_tracker(error_file, current_hour_count=10, baseline_hourly=2)

        ctx = ScanContext(workspace_dir=str(tmp_path))
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            signals = self._run(self.scanner._check_error_rate(tmp_path, ctx))

        assert len(signals) == 1
        assert signals[0].context["correlated_commit"] == ""


class TestErrorTrackerHandler:
    def test_handler_writes_jsonl(self, tmp_path):
        """ErrorTrackerHandler writes ERROR records to the JSONL file."""
        from agent.infra.error_tracker import ErrorTrackerHandler

        jsonl_path = str(tmp_path / "data" / "error_tracker.jsonl")
        handler = ErrorTrackerHandler(jsonl_path=jsonl_path)

        logger = logging.getLogger("test.error_tracker")
        logger.addHandler(handler)
        logger.setLevel(logging.ERROR)
        logger.propagate = False

        logger.error("test spike error")

        lines = Path(jsonl_path).read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["level"] == "ERROR"
        assert "test spike error" in entry["message"]
        assert "timestamp" in entry

    def test_handler_filters_noise(self, tmp_path):
        """Noise patterns are not written to JSONL."""
        from agent.infra.error_tracker import ErrorTrackerHandler

        jsonl_path = str(tmp_path / "data" / "error_tracker.jsonl")
        handler = ErrorTrackerHandler(jsonl_path=jsonl_path)

        logger = logging.getLogger("test.error_tracker_noise")
        logger.addHandler(handler)
        logger.setLevel(logging.ERROR)
        logger.propagate = False

        logger.error("Rate limited: retry later")

        assert not Path(jsonl_path).exists()

    def test_handler_ignores_below_error(self, tmp_path):
        """WARNING records are not written."""
        from agent.infra.error_tracker import ErrorTrackerHandler

        jsonl_path = str(tmp_path / "data" / "error_tracker.jsonl")
        handler = ErrorTrackerHandler(jsonl_path=jsonl_path)

        logger = logging.getLogger("test.error_tracker_warn")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        logger.warning("just a warning")

        assert not Path(jsonl_path).exists()
