# -*- coding: utf-8 -*-
"""Golden-standard test: heartbeat _maybe_explore sentinel log format."""
import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent.jobs.heartbeat import HeartbeatMonitor


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_hb() -> HeartbeatMonitor:
    cfg = {"enabled": True, "interval_seconds": 1800, "explore_idle_minutes": 30}
    router = MagicMock()
    dispatcher = MagicMock()
    hb = HeartbeatMonitor(config=cfg, router=router, dispatcher=dispatcher, workspace_dir="/tmp")
    # Inject idle checker that always reports idle (1800s > 30m threshold)
    hb._idle_checker = lambda: (True, 1800.0)
    return hb


def _make_sentinel(summary: dict) -> MagicMock:
    sentinel = MagicMock()
    sentinel.run_cycle = AsyncMock(return_value=summary)
    return sentinel


# ── Golden standard tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sentinel_log_format_with_signals(caplog):
    """Golden standard: 2 signals, maqs=1 notify=1 → correct log format."""
    hb = _make_hb()
    hb._sentinel = _make_sentinel(
        {"total": 2, "maqs": 1, "explore": 0, "notify": 1, "silent_log": 0}
    )

    with patch("asyncio.create_task"), \
         caplog.at_level(logging.INFO, logger="hub.heartbeat"):
        await hb._maybe_explore()

    sentinel_logs = [r.message for r in caplog.records if "Sentinel cycle" in r.message]
    assert len(sentinel_logs) == 1, f"Expected 1 sentinel log, got: {sentinel_logs}"
    msg = sentinel_logs[0]
    assert msg == "Sentinel cycle: 2 signals found, routed: {'maqs': 1, 'notify': 1}", \
        f"Log format mismatch: {msg!r}"


@pytest.mark.asyncio
async def test_sentinel_log_format_zero_signals(caplog):
    """Zero signals → routed dict is empty."""
    hb = _make_hb()
    hb._sentinel = _make_sentinel(
        {"total": 0, "maqs": 0, "explore": 0, "notify": 0, "silent_log": 0}
    )

    with patch("asyncio.create_task"), \
         caplog.at_level(logging.INFO, logger="hub.heartbeat"):
        await hb._maybe_explore()

    sentinel_logs = [r.message for r in caplog.records if "Sentinel cycle" in r.message]
    assert len(sentinel_logs) == 1
    msg = sentinel_logs[0]
    assert msg == "Sentinel cycle: 0 signals found, routed: {}", \
        f"Log format mismatch: {msg!r}"


@pytest.mark.asyncio
async def test_sentinel_log_no_zero_values(caplog):
    """Zero-value routes must not appear in the log output."""
    hb = _make_hb()
    hb._sentinel = _make_sentinel(
        {"total": 1, "maqs": 1, "explore": 0, "notify": 0, "silent_log": 0}
    )

    with patch("asyncio.create_task"), \
         caplog.at_level(logging.INFO, logger="hub.heartbeat"):
        await hb._maybe_explore()

    sentinel_logs = [r.message for r in caplog.records if "Sentinel cycle" in r.message]
    assert len(sentinel_logs) == 1
    msg = sentinel_logs[0]
    assert "explore" not in msg
    assert "notify" not in msg
    assert "silent_log" not in msg
    assert "maqs" in msg
