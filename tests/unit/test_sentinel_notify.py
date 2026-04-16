# -*- coding: utf-8 -*-
"""Unit tests for SentinelOrchestrator._notify_user — delivery routing and friendly format.

After the cross-app fix, Sentinel always uses send_to_delivery_target (the
notify_open_id from heartbeat belongs to a different Feishu app than the
notifier dispatcher).
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent.jobs.sentinel.models import EntropySignal
from agent.jobs.sentinel.orchestrator import SentinelOrchestrator, CATEGORY_DISPLAY
from agent.jobs.sentinel.store import SentinelStore


def _make_store(tmp_path):
    store = SentinelStore(str(tmp_path / "signals.jsonl"))
    return store


def _make_orchestrator(tmp_path, notify_open_id=""):
    dispatcher = MagicMock()
    dispatcher.send_to_user = AsyncMock(return_value="msg_001")
    dispatcher.send_to_delivery_target = AsyncMock(return_value="msg_002")
    store = _make_store(tmp_path)
    return SentinelOrchestrator(
        scanners=[],
        store=store,
        dispatcher=dispatcher,
        notify_open_id=notify_open_id,
    ), dispatcher


# ── Golden standard test ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_notify_user_golden_stale_doc(tmp_path):
    """Gold standard: stale_doc → delivery_target with emoji, friendly title, action, buttons."""
    orchestrator, dispatcher = _make_orchestrator(tmp_path, notify_open_id="ou_test123")

    signal = EntropySignal(
        source="doc_auditor",
        category="stale_doc",
        severity="medium",
        autonomy_level=1,
        summary="设计文档可能过期",
        route="notify",
    )

    await orchestrator._notify_user(signal)

    dispatcher.send_to_delivery_target.assert_awaited_once()
    text = dispatcher.send_to_delivery_target.call_args[0][0]

    assert text == "📋 文档审计发现\n设计文档可能过期\n\n建议：检查是否需要更新"
    dispatcher.send_to_user.assert_not_awaited()


# ── Always uses delivery_target (cross-app fix) ─────────────────────────────

@pytest.mark.asyncio
async def test_notify_always_uses_delivery_target(tmp_path):
    """Even with open_id set, Sentinel uses delivery_target (cross-app fix)."""
    orchestrator, dispatcher = _make_orchestrator(tmp_path, notify_open_id="ou_abc")

    signal = EntropySignal(
        source="code_scanner",
        category="stale_todo",
        severity="low",
        autonomy_level=1,
        summary="发现 3 个过期 TODO",
        route="notify",
    )
    await orchestrator._notify_user(signal)

    dispatcher.send_to_delivery_target.assert_awaited_once()
    dispatcher.send_to_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_works_without_open_id(tmp_path):
    orchestrator, dispatcher = _make_orchestrator(tmp_path, notify_open_id="")

    signal = EntropySignal(
        source="code_scanner",
        category="stale_todo",
        severity="low",
        autonomy_level=1,
        summary="发现过期 TODO",
        route="notify",
    )
    await orchestrator._notify_user(signal)

    dispatcher.send_to_delivery_target.assert_awaited_once()
    dispatcher.send_to_user.assert_not_awaited()


# ── Message format ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_notify_uses_custom_suggested_action(tmp_path):
    orchestrator, dispatcher = _make_orchestrator(tmp_path, notify_open_id="ou_x")

    signal = EntropySignal(
        source="doc_auditor",
        category="stale_doc",
        severity="medium",
        autonomy_level=1,
        summary="API 文档三个月未更新",
        suggested_action="联系 API 负责人更新文档",
        route="notify",
    )
    await orchestrator._notify_user(signal)

    text = dispatcher.send_to_delivery_target.call_args[0][0]
    assert "联系 API 负责人更新文档" in text
    assert "" in text


@pytest.mark.asyncio
async def test_notify_unknown_category_uses_default(tmp_path):
    orchestrator, dispatcher = _make_orchestrator(tmp_path, notify_open_id="ou_x")

    signal = EntropySignal(
        source="custom_scanner",
        category="unknown_category_xyz",
        severity="low",
        autonomy_level=1,
        summary="未知信号",
        route="notify",
    )
    await orchestrator._notify_user(signal)

    text = dispatcher.send_to_delivery_target.call_args[0][0]
    assert "🔔" in text
    assert "未知信号" in text
    assert "" in text


@pytest.mark.asyncio
async def test_notify_no_dispatcher_logs_warning(tmp_path, caplog):
    store = _make_store(tmp_path)
    orchestrator = SentinelOrchestrator(
        scanners=[], store=store, dispatcher=None, notify_open_id=""
    )
    signal = EntropySignal(
        source="doc_auditor",
        category="stale_doc",
        severity="medium",
        autonomy_level=1,
        summary="无 dispatcher 测试",
        route="notify",
    )
    import logging
    with caplog.at_level(logging.WARNING, logger="hub.sentinel"):
        await orchestrator._notify_user(signal)
    assert "no dispatcher" in caplog.text


# ── CATEGORY_DISPLAY coverage ─────────────────────────────────────────────────

def test_category_display_stale_doc():
    emoji, title, action = CATEGORY_DISPLAY["stale_doc"]
    assert emoji == "📋"
    assert title == "文档审计发现"
    assert action == "检查是否需要更新"


def test_category_display_covers_major_categories():
    expected = {
        "stale_doc", "doc_duplicate", "search_recall_degradation",
        "stale_todo", "uncommitted_stale", "stale_branch",
        "error_spike", "skill_unused", "disk_usage_high",
    }
    assert expected.issubset(set(CATEGORY_DISPLAY.keys()))
