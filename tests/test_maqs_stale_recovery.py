# -*- coding: utf-8 -*-
"""Unit tests for MAQS stale intermediate-state recovery."""

import pytest
from unittest.mock import AsyncMock, patch, call


pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_reset_stale_intermediate_tickets_resets_all_states():
    """All four intermediate states are queried and reset to open."""
    from agent.jobs.maqs import _reset_stale_intermediate_tickets, _STALE_INTERMEDIATE_STATES

    # One orphaned ticket per intermediate state
    def make_query_result(state):
        return [{"record_id": f"rec_{state[:4]}", "fields": {"status": state}}]

    query_calls = []

    async def fake_query(app_token, table_id, filter_str="", limit=20):
        query_calls.append(filter_str)
        for state in _STALE_INTERMEDIATE_STATES:
            if f'"{state}"' in filter_str:
                return make_query_result(state)
        return []

    update_calls = []

    async def fake_update(app_token, table_id, record_id, fields):
        update_calls.append((record_id, fields))

    git_calls = []

    async def fake_git(*args, **kwargs):
        git_calls.append(args)
        return (0, "", "")

    with (
        patch("agent.jobs.maqs._bitable_query", side_effect=fake_query),
        patch("agent.jobs.maqs._bitable_update", side_effect=fake_update),
        patch("agent.jobs.maqs._git", side_effect=fake_git),
    ):
        count = await _reset_stale_intermediate_tickets("app_tok", "tbl_id")

    assert count == len(_STALE_INTERMEDIATE_STATES), (
        f"Expected {len(_STALE_INTERMEDIATE_STATES)} resets, got {count}"
    )

    # All reset updates set status=open
    for _, fields in update_calls:
        assert fields == {"status": "open"}

    # branch -D called for each ticket
    branch_deletes = [a for a in git_calls if "branch" in a and "-D" in a]
    assert len(branch_deletes) == len(_STALE_INTERMEDIATE_STATES)


@pytest.mark.asyncio
async def test_reset_stale_intermediate_tickets_no_orphans():
    """Returns 0 when no intermediate-state tickets exist."""
    from agent.jobs.maqs import _reset_stale_intermediate_tickets

    async def fake_query(*args, **kwargs):
        return []

    with patch("agent.jobs.maqs._bitable_query", side_effect=fake_query):
        count = await _reset_stale_intermediate_tickets("app_tok", "tbl_id")

    assert count == 0


@pytest.mark.asyncio
async def test_run_maqs_pipeline_calls_stale_recovery_before_open_query():
    """run_maqs_pipeline calls _reset_stale_intermediate_tickets before querying open tickets."""
    from agent.jobs import maqs

    call_order = []

    async def fake_reset(app_token, table_id):
        call_order.append("reset")
        return 0

    async def fake_query(app_token, table_id, filter_str="", limit=50):
        call_order.append(f"query:{filter_str}")
        return []

    with (
        patch.object(maqs, "_reset_stale_intermediate_tickets", side_effect=fake_reset),
        patch.object(maqs, "_bitable_query", side_effect=fake_query),
    ):
        config = {"bitable_app_token": "tok", "bitable_table_id": "tbl"}
        await maqs.run_maqs_pipeline(router=None, dispatcher=None, config=config)

    assert call_order[0] == "reset", "stale recovery must run before open query"
    assert any("open" in c for c in call_order), "open tickets query must still run"
