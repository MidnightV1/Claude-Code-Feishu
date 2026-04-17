# -*- coding: utf-8 -*-
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from agent.jobs.mads.helpers import build_status_card_json, send_status_card, update_status_card

# Full 6-phase status dict matching _PHASE_LABELS
_ALL_PENDING = {
    "diagnosing": "pending", "contracting": "pending", "fixing": "pending",
    "hardgate": "pending", "reviewing": "pending", "merging": "pending",
}


def test_build_status_card_gold_standard():
    card_str = build_status_card_json(
        ticket_id="recvfjK7", title="except block notification",
        phase="diagnosing", severity="P1", ticket_type="bug",
        phases_status={**_ALL_PENDING, "diagnosing": "running"},
    )
    content = json.loads(card_str)["body"]["elements"][0]["content"]
    assert "\u25cf diagnosing" in content
    assert "\u25cb contracting" in content
    assert "\u25cb fixing" in content
    assert "\u25cb hardgate" in content
    assert "\u25cb reviewing" in content
    assert "\u25cb merging" in content


def test_build_status_card_severity_colors():
    phases = {**_ALL_PENDING, "diagnosing": "running"}
    for severity, expected_color in [("P0", "red"), ("P1", "orange"), ("P2", "wathet"), ("P3", "grey")]:
        card_str = build_status_card_json(
            ticket_id="x", title="t", phase="diagnosing", severity=severity,
            ticket_type="bug", phases_status=phases,
        )
        assert json.loads(card_str)["header"]["template"] == expected_color


def test_build_status_card_all_done():
    all_done = {k: "done" for k in _ALL_PENDING}
    card_str = build_status_card_json(
        ticket_id="x", title="t", phase="closed", severity="P2",
        ticket_type="bug", phases_status=all_done,
    )
    content = json.loads(card_str)["body"]["elements"][0]["content"]
    assert content.count("\u2705") == 6


def test_build_status_card_failed_state():
    phases = {**_ALL_PENDING, "diagnosing": "done", "contracting": "done",
              "fixing": "done", "hardgate": "failed"}
    card_str = build_status_card_json(
        ticket_id="x", title="t", phase="hardgate", severity="P1",
        ticket_type="bug", phases_status=phases,
    )
    content = json.loads(card_str)["body"]["elements"][0]["content"]
    assert "\u274c hardgate" in content
    assert "\u2705 diagnosing" in content
    assert "\u25cb reviewing" in content


def test_build_status_card_header_title():
    card_str = build_status_card_json(
        ticket_id="recvfjK7", title="except block notification", phase="diagnosing",
        severity="P1", ticket_type="bug",
        phases_status={**_ALL_PENDING, "diagnosing": "running"},
    )
    assert "except block notification" in json.loads(card_str)["header"]["title"]["content"]


def test_build_status_card_content_includes_ticket_id():
    card_str = build_status_card_json(
        ticket_id="recvfjK7", title="t", phase="diagnosing", severity="P1",
        ticket_type="bug", phases_status={**_ALL_PENDING, "diagnosing": "running"},
    )
    assert "recvfjK7" in json.loads(card_str)["body"]["elements"][0]["content"]


@pytest.mark.asyncio
async def test_send_status_card_returns_message_id():
    dispatcher = MagicMock()
    dispatcher.send_card_raw_to_delivery = AsyncMock(return_value="om_test123")
    mid = await send_status_card(
        dispatcher, "recvfjK7", "except block notification",
        "diagnosing", "P1", "bug", {**_ALL_PENDING, "diagnosing": "running"},
    )
    assert mid == "om_test123"
    call_arg = dispatcher.send_card_raw_to_delivery.call_args[0][0]
    assert "diagnosing" in json.loads(call_arg)["body"]["elements"][0]["content"]


@pytest.mark.asyncio
async def test_send_status_card_dispatcher_error_returns_none():
    dispatcher = MagicMock()
    dispatcher.send_card_raw_to_delivery = AsyncMock(side_effect=Exception("network error"))
    mid = await send_status_card(
        dispatcher, "x", "t", "diagnosing", "P1", "bug",
        {**_ALL_PENDING, "diagnosing": "running"},
    )
    assert mid is None


@pytest.mark.asyncio
async def test_update_status_card_calls_update_card_raw():
    dispatcher = MagicMock()
    dispatcher.update_card_raw = AsyncMock(return_value=True)
    result = await update_status_card(
        dispatcher, "om_test123", "recvfjK7", "t", "fixing", "P1", "bug",
        {**_ALL_PENDING, "diagnosing": "done", "contracting": "done", "fixing": "running"},
    )
    assert result is True
    call_args = dispatcher.update_card_raw.call_args[0]
    assert call_args[0] == "om_test123"
    assert "\u25cf fixing" in json.loads(call_args[1])["body"]["elements"][0]["content"]


@pytest.mark.asyncio
async def test_update_status_card_dispatcher_error_returns_false():
    dispatcher = MagicMock()
    dispatcher.update_card_raw = AsyncMock(side_effect=RuntimeError("timeout"))
    result = await update_status_card(
        dispatcher, "om_x", "x", "t", "reviewing", "P0", "bug",
        {**_ALL_PENDING, "diagnosing": "done", "contracting": "done",
         "fixing": "done", "hardgate": "done", "reviewing": "running"},
    )
    assert result is False


def test_build_status_card_queued_all_pending():
    card_str = build_status_card_json(
        ticket_id="recvfkSi", title="Wire Status Card Lifecycle",
        phase="queued", severity="P1", ticket_type="bug",
        phases_status=_ALL_PENDING,
    )
    content = json.loads(card_str)["body"]["elements"][0]["content"]
    for phase in _ALL_PENDING:
        assert f"\u25cb {phase}" in content


@pytest.mark.asyncio
async def test_maqs_process_ticket_emits_queued_card():
    from unittest.mock import patch, AsyncMock, MagicMock
    dispatcher = MagicMock()
    card_calls = []

    async def fake_send_card(dispatcher, ticket_id, title, phase, severity, ticket_type, phases_status, **kwargs):
        card_calls.append(("send", phase, dict(phases_status)))
        return "om_queued_mid"

    async def fake_update_card(dispatcher, mid, ticket_id, title, phase, severity, ticket_type, phases_status, **kwargs):
        card_calls.append(("update", phase, dict(phases_status)))
        return True

    ticket = {"title": "test-ticket", "phenomenon": "test", "source": "test",
              "severity": "P1", "type": "bug"}

    async def fake_git(*args, **kwargs):
        return (0, "", "")

    with patch("agent.jobs.maqs._send_status_card", side_effect=fake_send_card), \
         patch("agent.jobs.maqs._update_status_card", side_effect=fake_update_card), \
         patch("agent.jobs.maqs._bitable_update", new_callable=AsyncMock), \
         patch("agent.jobs.maqs._worktree_create", new_callable=AsyncMock, return_value=None), \
         patch("agent.jobs.maqs.diagnose_ticket", new_callable=AsyncMock, return_value="diagnosis result"):
        from agent.jobs.maqs import process_ticket
        await process_ticket(MagicMock(), dispatcher, "app", "tbl", "recvfkSi", ticket, "")

    assert len(card_calls) >= 2
    assert card_calls[0][1] == "queued"
    assert card_calls[0][2]["diagnosing"] == "pending"
    assert card_calls[1][1] == "diagnosing"
    assert card_calls[1][2]["diagnosing"] == "running"


@pytest.mark.asyncio
async def test_mads_process_atomic_with_contract_emits_queued_and_diagnosing_cards():
    from unittest.mock import patch, AsyncMock, MagicMock
    dispatcher = MagicMock()
    card_calls = []

    async def fake_send_card(dispatcher, ticket_id, title, phase, severity, ticket_type, phases_status, **kwargs):
        card_calls.append(("send", phase, dict(phases_status)))
        return "om_mads_mid"

    async def fake_update_card(dispatcher, mid, ticket_id, title, phase, severity, ticket_type, phases_status, **kwargs):
        card_calls.append(("update", phase, dict(phases_status)))
        return True

    ticket = {"title": "MADS test", "phenomenon": "test", "source": "test",
              "severity": "P1", "type": "bug", "status": "open"}

    with patch("agent.jobs.mads.pipeline.send_status_card", side_effect=fake_send_card), \
         patch("agent.jobs.mads.pipeline.update_status_card", side_effect=fake_update_card), \
         patch("agent.jobs.mads.pipeline.bitable_update", new_callable=AsyncMock), \
         patch("agent.jobs.maqs.diagnose_ticket", new_callable=AsyncMock, return_value="[ERROR] test"), \
         patch("agent.jobs.mads.pipeline.notify", new_callable=AsyncMock), \
         patch("agent.jobs.mads.pipeline.write_artifact"):
        from agent.jobs.mads.pipeline import process_atomic_with_contract
        await process_atomic_with_contract(MagicMock(), dispatcher, "app", "tbl", "recvfkSi", ticket, "")

    assert len(card_calls) >= 2
    first = card_calls[0]
    assert first[1] == "queued"
    assert first[2]["contracting"] == "pending"
    assert first[2]["hardgate"] == "pending"
    assert first[2]["merging"] == "pending"
    second = card_calls[1]
    assert second[1] == "diagnosing"
    assert second[2]["diagnosing"] == "running"


def test_build_status_card_stalled_phase():
    """When phase='stalled', card color must be orange and content must include '❌ stalled'."""
    phases = {**_ALL_PENDING, "diagnosing": "done", "contracting": "done",
              "fixing": "done", "hardgate": "done", "reviewing": "failed"}
    card_str = build_status_card_json(
        ticket_id="MAQS-abc12345", title="stall test", phase="stalled",
        severity="P1", ticket_type="bug", phases_status=phases,
    )
    card = json.loads(card_str)
    assert card["header"]["template"] == "orange"
    content = card["body"]["elements"][0]["content"]
    assert "❌ stalled" in content


@pytest.mark.asyncio
async def test_mads_passes_card_mid_to_maqs():
    from unittest.mock import patch, AsyncMock, MagicMock
    dispatcher = MagicMock()
    captured_ticket = {}

    async def fake_send_card(dispatcher, ticket_id, title, phase, severity, ticket_type, phases_status, **kwargs):
        return "om_mads_mid"

    async def fake_update_card(dispatcher, mid, ticket_id, title, phase, severity, ticket_type, phases_status, **kwargs):
        return True

    async def fake_process_ticket(router, dispatcher, app_token, table_id,
                                   record_id, ticket, notify_open_id, skip_diagnosis=False):
        captured_ticket.update(ticket)

    ticket = {"title": "card_mid test", "phenomenon": "test", "source": "test",
              "severity": "P1", "type": "bug", "status": "open", "diagnosis": "pre-diagnosed"}

    with patch("agent.jobs.mads.pipeline.send_status_card", side_effect=fake_send_card), \
         patch("agent.jobs.mads.pipeline.update_status_card", side_effect=fake_update_card), \
         patch("agent.jobs.mads.pipeline.bitable_update", new_callable=AsyncMock), \
         patch("agent.jobs.maqs.diagnose_ticket", new_callable=AsyncMock, return_value="<diagnosis_meta>\n<affected-files>\n- agent/foo.py\n</affected-files>\n</diagnosis_meta>\nDiagnosis passes garbage check."), \
         patch("agent.jobs.mads.pipeline.negotiate_contract", new_callable=AsyncMock, return_value={"contract": "ok"}), \
         patch("agent.jobs.maqs.process_ticket", side_effect=fake_process_ticket), \
         patch("agent.jobs.mads.pipeline.notify", new_callable=AsyncMock), \
         patch("agent.jobs.mads.pipeline.write_artifact"):
        from agent.jobs.mads.pipeline import process_atomic_with_contract
        await process_atomic_with_contract(MagicMock(), dispatcher, "app", "tbl", "recvfkSi", ticket, "")

    assert captured_ticket.get("status_card_mid") == "om_mads_mid"
