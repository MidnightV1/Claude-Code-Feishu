# -*- coding: utf-8 -*-
"""Unit tests for agent.jobs.mads.helpers — bitable wrappers and constants."""

import asyncio
import os
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ══════════════════════════════════════════════════════════════════════
#  Singleton / module-level constants
# ══════════════════════════════════════════════════════════════════════

def test_project_root_is_string():
    from agent.jobs.mads.helpers import PROJECT_ROOT
    assert isinstance(PROJECT_ROOT, str)
    assert len(PROJECT_ROOT) > 0


def test_project_root_resolves_to_real_directory():
    from agent.jobs.mads.helpers import PROJECT_ROOT
    # PROJECT_ROOT must point to an existing directory (the repo root)
    assert os.path.isdir(PROJECT_ROOT), f"PROJECT_ROOT not a directory: {PROJECT_ROOT}"


def test_worktree_base_is_under_project_root():
    from agent.jobs.mads.helpers import PROJECT_ROOT, WORKTREE_BASE
    assert isinstance(WORKTREE_BASE, str)
    # WORKTREE_BASE must be a child of PROJECT_ROOT
    assert WORKTREE_BASE.startswith(PROJECT_ROOT), (
        f"WORKTREE_BASE {WORKTREE_BASE!r} not under PROJECT_ROOT {PROJECT_ROOT!r}"
    )


def test_artifacts_dir_is_under_project_root():
    from agent.jobs.mads.helpers import PROJECT_ROOT, ARTIFACTS_DIR
    assert isinstance(ARTIFACTS_DIR, str)
    assert ARTIFACTS_DIR.startswith(PROJECT_ROOT), (
        f"ARTIFACTS_DIR {ARTIFACTS_DIR!r} not under PROJECT_ROOT {PROJECT_ROOT!r}"
    )


def test_worktree_base_basename():
    from agent.jobs.mads.helpers import WORKTREE_BASE
    assert os.path.basename(WORKTREE_BASE) == ".worktrees"


def test_artifacts_dir_basename():
    from agent.jobs.mads.helpers import ARTIFACTS_DIR
    # Should end with data/mads
    assert ARTIFACTS_DIR.endswith(os.path.join("data", "mads"))


# ══════════════════════════════════════════════════════════════════════
#  bitable_add
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_bitable_add_returns_record_id_on_success():
    fake_api = MagicMock()
    fake_api.post.return_value = {
        "code": 0,
        "data": {"record": {"record_id": "recvABC123"}},
    }
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api):
        from agent.jobs.mads.helpers import bitable_add
        result = await bitable_add("app_token", "table_id", {"field": "value"})

    assert result == "recvABC123"
    fake_api.post.assert_called_once()
    call_args = fake_api.post.call_args
    assert "app_token" in call_args[0][0]
    assert "table_id" in call_args[0][0]
    assert call_args[0][1] == {"fields": {"field": "value"}}


@pytest.mark.asyncio
async def test_bitable_add_returns_ok_when_record_id_missing():
    """API success but no record_id in response → return 'ok' sentinel."""
    fake_api = MagicMock()
    fake_api.post.return_value = {"code": 0, "data": {"record": {}}}
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api):
        from agent.jobs.mads.helpers import bitable_add
        result = await bitable_add("app", "tbl", {})

    assert result == "ok"


@pytest.mark.asyncio
async def test_bitable_add_returns_none_on_api_error():
    fake_api = MagicMock()
    fake_api.post.return_value = {"code": 99, "msg": "invalid token"}
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api):
        from agent.jobs.mads.helpers import bitable_add
        result = await bitable_add("app", "tbl", {})

    assert result is None


@pytest.mark.asyncio
async def test_bitable_add_returns_none_on_exception():
    fake_api = MagicMock()
    fake_api.post.side_effect = RuntimeError("network down")
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api):
        from agent.jobs.mads.helpers import bitable_add
        result = await bitable_add("app", "tbl", {})

    assert result is None


@pytest.mark.asyncio
async def test_bitable_add_returns_none_on_timeout():
    fake_api = MagicMock()

    def slow_post(*args, **kwargs):
        import time
        time.sleep(999)  # pragma: no cover

    fake_api.post.side_effect = slow_post

    async def instant_timeout(*args, **kwargs):
        for a in args:
            if hasattr(a, "close") and hasattr(a, "__await__"):
                a.close()
        raise asyncio.TimeoutError()

    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api), \
         patch("asyncio.wait_for", side_effect=instant_timeout):
        from agent.jobs.mads.helpers import bitable_add
        result = await bitable_add("app", "tbl", {})

    assert result is None


# ══════════════════════════════════════════════════════════════════════
#  bitable_update
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_bitable_update_calls_put_with_correct_url():
    fake_api = MagicMock()
    fake_api.put.return_value = {"code": 0}
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api):
        from agent.jobs.mads.helpers import bitable_update
        await bitable_update("app_tok", "tbl_id", "recvXYZ", {"status": "done"})

    fake_api.put.assert_called_once()
    url = fake_api.put.call_args[0][0]
    assert "app_tok" in url
    assert "tbl_id" in url
    assert "recvXYZ" in url
    assert fake_api.put.call_args[0][1] == {"fields": {"status": "done"}}


@pytest.mark.asyncio
async def test_bitable_update_logs_warning_on_api_error(caplog):
    import logging
    fake_api = MagicMock()
    fake_api.put.return_value = {"code": 9, "msg": "permission denied"}
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api):
        from agent.jobs.mads.helpers import bitable_update
        with caplog.at_level(logging.WARNING, logger="hub.mads"):
            await bitable_update("app", "tbl", "recvID", {})

    assert any("failed" in r.message.lower() or "update" in r.message.lower()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_bitable_update_returns_none_on_exception():
    fake_api = MagicMock()
    fake_api.put.side_effect = ConnectionError("lost")
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api):
        from agent.jobs.mads.helpers import bitable_update
        # Should not raise
        result = await bitable_update("app", "tbl", "recvID", {})

    assert result is None


@pytest.mark.asyncio
async def test_bitable_update_timeout():
    async def instant_timeout(*args, **kwargs):
        for a in args:
            if hasattr(a, "close") and hasattr(a, "__await__"):
                a.close()
        raise asyncio.TimeoutError()

    fake_api = MagicMock()
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api), \
         patch("asyncio.wait_for", side_effect=instant_timeout):
        from agent.jobs.mads.helpers import bitable_update
        result = await bitable_update("app", "tbl", "recvID", {})

    assert result is None


# ══════════════════════════════════════════════════════════════════════
#  bitable_query
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_bitable_query_returns_records():
    fake_api = MagicMock()
    fake_api.get.return_value = {
        "code": 0,
        "data": {
            "items": [
                {"record_id": "recvA", "fields": {"title": "T1"}},
                {"record_id": "recvB", "fields": {"title": "T2"}},
            ],
            "has_more": False,
        },
    }
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api):
        from agent.jobs.mads.helpers import bitable_query
        records = await bitable_query("app", "tbl")

    assert len(records) == 2
    assert records[0]["record_id"] == "recvA"
    assert records[0]["fields"] == {"title": "T1"}
    assert records[1]["record_id"] == "recvB"


@pytest.mark.asyncio
async def test_bitable_query_passes_filter():
    fake_api = MagicMock()
    fake_api.get.return_value = {
        "code": 0,
        "data": {"items": [], "has_more": False},
    }
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api):
        from agent.jobs.mads.helpers import bitable_query
        await bitable_query("app", "tbl", filter_str='CurrentValue.[status]="open"')

    params_passed = fake_api.get.call_args[0][1]
    assert params_passed.get("filter") == 'CurrentValue.[status]="open"'


@pytest.mark.asyncio
async def test_bitable_query_respects_limit():
    items = [{"record_id": f"recv{i}", "fields": {}} for i in range(10)]
    fake_api = MagicMock()
    fake_api.get.return_value = {
        "code": 0,
        "data": {"items": items, "has_more": False},
    }
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api):
        from agent.jobs.mads.helpers import bitable_query
        records = await bitable_query("app", "tbl", limit=3)

    assert len(records) == 3


@pytest.mark.asyncio
async def test_bitable_query_returns_empty_on_api_error():
    fake_api = MagicMock()
    fake_api.get.return_value = {"code": 5, "msg": "table not found"}
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api):
        from agent.jobs.mads.helpers import bitable_query
        records = await bitable_query("app", "tbl")

    assert records == []


@pytest.mark.asyncio
async def test_bitable_query_returns_empty_on_exception():
    fake_api = MagicMock()
    fake_api.get.side_effect = Exception("unexpected")
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api):
        from agent.jobs.mads.helpers import bitable_query
        records = await bitable_query("app", "tbl")

    assert records == []


@pytest.mark.asyncio
async def test_bitable_query_paginates():
    """has_more=True triggers a second page request."""
    page1 = {
        "code": 0,
        "data": {
            "items": [{"record_id": "recvA", "fields": {}}],
            "has_more": True,
            "page_token": "tok_next",
        },
    }
    page2 = {
        "code": 0,
        "data": {
            "items": [{"record_id": "recvB", "fields": {}}],
            "has_more": False,
        },
    }
    fake_api = MagicMock()
    fake_api.get.side_effect = [page1, page2]
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api):
        from agent.jobs.mads.helpers import bitable_query
        records = await bitable_query("app", "tbl", limit=50)

    assert len(records) == 2
    assert records[0]["record_id"] == "recvA"
    assert records[1]["record_id"] == "recvB"
    # Second call must pass page_token
    second_call_params = fake_api.get.call_args_list[1][0][1]
    assert second_call_params.get("page_token") == "tok_next"


# ══════════════════════════════════════════════════════════════════════
#  bitable_get_status
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_bitable_get_status_returns_string():
    fake_api = MagicMock()
    fake_api.get.return_value = {
        "code": 0,
        "data": {"record": {"fields": {"status": "open"}}},
    }
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api):
        from agent.jobs.mads.helpers import bitable_get_status
        status = await bitable_get_status("app", "tbl", "recvXYZ")

    assert status == "open"


@pytest.mark.asyncio
async def test_bitable_get_status_calls_correct_endpoint():
    fake_api = MagicMock()
    fake_api.get.return_value = {
        "code": 0,
        "data": {"record": {"fields": {"status": "closed"}}},
    }
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api):
        from agent.jobs.mads.helpers import bitable_get_status
        await bitable_get_status("myapp", "mytbl", "recvABC")

    url = fake_api.get.call_args[0][0]
    assert "myapp" in url
    assert "mytbl" in url
    assert "recvABC" in url


@pytest.mark.asyncio
async def test_bitable_get_status_returns_none_on_api_error():
    fake_api = MagicMock()
    fake_api.get.return_value = {"code": 404, "msg": "not found"}
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api):
        from agent.jobs.mads.helpers import bitable_get_status
        result = await bitable_get_status("app", "tbl", "recvMissing")

    assert result is None


@pytest.mark.asyncio
async def test_bitable_get_status_returns_none_when_status_not_string():
    """Status field present but not a str (e.g. list from Feishu option field)."""
    fake_api = MagicMock()
    fake_api.get.return_value = {
        "code": 0,
        "data": {"record": {"fields": {"status": ["open"]}}},
    }
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api):
        from agent.jobs.mads.helpers import bitable_get_status
        result = await bitable_get_status("app", "tbl", "recvOpt")

    assert result is None


@pytest.mark.asyncio
async def test_bitable_get_status_returns_none_on_exception():
    fake_api = MagicMock()
    fake_api.get.side_effect = Exception("boom")
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api):
        from agent.jobs.mads.helpers import bitable_get_status
        result = await bitable_get_status("app", "tbl", "recvErr")

    assert result is None


@pytest.mark.asyncio
async def test_bitable_get_status_timeout():
    async def instant_timeout(*args, **kwargs):
        for a in args:
            if hasattr(a, "close") and hasattr(a, "__await__"):
                a.close()
        raise asyncio.TimeoutError()

    fake_api = MagicMock()
    with patch("agent.jobs.mads.helpers._get_api", return_value=fake_api), \
         patch("asyncio.wait_for", side_effect=instant_timeout):
        from agent.jobs.mads.helpers import bitable_get_status
        result = await bitable_get_status("app", "tbl", "recvTimeout")

    assert result is None


# ══════════════════════════════════════════════════════════════════════
#  parse_qa_verdict — rate limit banner safety
# ══════════════════════════════════════════════════════════════════════

def test_parse_qa_verdict_rejects_claude_limit_banner():
    from agent.jobs.mads.helpers import parse_qa_verdict
    # Claude rate limit banners must never be mistaken for a valid PASS verdict
    assert parse_qa_verdict("You've hit your limit · resets 12am (Asia/Shanghai)") == "REJECT"
    assert parse_qa_verdict("You've hit your limit · resets 7pm (Asia/Shanghai)") == "REJECT"


def test_parse_qa_verdict_rejects_empty():
    from agent.jobs.mads.helpers import parse_qa_verdict
    assert parse_qa_verdict("") == "REJECT"
    assert parse_qa_verdict("   ") == "REJECT"


def test_parse_qa_verdict_accepts_pass():
    from agent.jobs.mads.helpers import parse_qa_verdict
    report = "<qa_verdict><result>PASS</result></qa_verdict>"
    assert parse_qa_verdict(report) == "PASS"


def test_parse_qa_verdict_accepts_reject():
    from agent.jobs.mads.helpers import parse_qa_verdict
    report = "some text\n<qa_verdict><result>REJECT</result></qa_verdict>\nmore text"
    assert parse_qa_verdict(report) == "REJECT"
