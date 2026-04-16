# -*- coding: utf-8 -*-
"""Unit tests for doc_ctl cmd_update / cmd_replace — table handling and atomicity.

Tests cover:
  - cmd_update: routes through append_markdown_to_doc (not _insert_blocks)
  - cmd_replace: write-first atomicity (no delete if write fails)
  - append_markdown_to_doc: table blocks are not sent raw to children API
  - append_markdown_to_doc: index parameter is forwarded correctly
"""
import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------

DOC_ID = "FakeDOCid1234"
_SKILL_SCRIPTS = (
    Path(__file__).parent.parent.parent
    / ".claude" / "skills" / "feishu-doc" / "scripts"
)
sys.path.insert(0, str(_SKILL_SCRIPTS))

import doc_ctl  # noqa: E402

TABLE_MARKDOWN_13_ROWS = """\
| 列A | 列B | 列C |
|-----|-----|-----|
| r1a | r1b | r1c |
| r2a | r2b | r2c |
| r3a | r3b | r3c |
| r4a | r4b | r4c |
| r5a | r5b | r5c |
| r6a | r6b | r6c |
| r7a | r7b | r7c |
| r8a | r8b | r8c |
| r9a | r9b | r9c |
| r10a | r10b | r10c |
| r11a | r11b | r11c |
| r12a | r12b | r12c |
"""


def _make_api(post_return=None):
    """Return a mock FeishuAPI that returns success responses by default."""
    api = MagicMock()
    if post_return is None:
        post_return = {"code": 0, "data": {"children": [{"block_id": "blk001"}]}}
    api.post.return_value = post_return
    api.get.return_value = {"code": 0, "data": {"items": [], "content": ""}}
    api.patch.return_value = {"code": 0}
    return api


def _make_args(**kwargs):
    """Build an argparse.Namespace for testing."""
    defaults = {
        "doc_id": DOC_ID,
        "content": "Hello world",
        "section": "Overview",
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# TestCmdUpdateUsesAppendMarkdown
# ---------------------------------------------------------------------------

class TestCmdUpdateUsesAppendMarkdown:
    """cmd_update must call append_markdown_to_doc, NOT _insert_blocks."""

    def test_calls_append_markdown_to_doc(self):
        api = _make_api()
        cfg = {}

        with patch.object(doc_ctl, "_append_md", return_value=3) as mock_append, \
             patch.object(doc_ctl, "_insert_blocks") as mock_insert, \
             patch.object(doc_ctl, "_count_direct_children", return_value=0), \
             patch.object(doc_ctl, "_auto_archive_comments"):
            doc_ctl.cmd_update(_make_args(content="plain text"), api, cfg)

        mock_append.assert_called_once()
        mock_insert.assert_not_called()

    def test_table_content_calls_append(self):
        """13-row markdown table must not reach _insert_blocks."""
        api = _make_api()
        cfg = {}

        with patch.object(doc_ctl, "_append_md", return_value=1) as mock_append, \
             patch.object(doc_ctl, "_insert_blocks") as mock_insert, \
             patch.object(doc_ctl, "_count_direct_children", return_value=2), \
             patch.object(doc_ctl, "_delete_blocks"), \
             patch.object(doc_ctl, "_auto_archive_comments"):
            doc_ctl.cmd_update(_make_args(content=TABLE_MARKDOWN_13_ROWS), api, cfg)

        mock_append.assert_called_once()
        mock_insert.assert_not_called()

    def test_no_raw_table_block_to_api(self):
        """When cmd_update processes a table, the children API must not receive
        a block with a '_table' key (which would cause 99992402)."""
        api = _make_api()
        cfg = {}

        with patch.object(doc_ctl, "_count_direct_children", return_value=0), \
             patch.object(doc_ctl, "_auto_archive_comments"), \
             patch("agent.platforms.feishu.utils._create_table_in_doc", return_value="tbl001"):
            doc_ctl.cmd_update(_make_args(content=TABLE_MARKDOWN_13_ROWS), api, cfg)

        for c in api.post.call_args_list:
            body = c.args[1] if len(c.args) > 1 else c.kwargs.get("body", {})
            children = body.get("children", [])
            for block in children:
                assert "_table" not in block, (
                    f"Raw '_table' pseudo-block sent to API: {block}"
                )


# ---------------------------------------------------------------------------
# TestCmdReplaceAtomicity
# ---------------------------------------------------------------------------

class TestCmdReplaceAtomicity:
    """cmd_replace must write first, then delete — never delete if write fails."""

    def _mock_list_blocks(self):
        """Return a doc structure with an 'Overview' heading at index 0 (content)."""
        return [
            {"block_type": 1, "block_id": "page"},   # page block
            {"block_type": 3, "block_id": "h1", "heading1": {"elements": [
                {"text_run": {"content": "Overview"}}
            ]}},
            {"block_type": 2, "block_id": "p1", "text": {"elements": [
                {"text_run": {"content": "old content"}}
            ]}},
        ]

    def test_delete_not_called_when_write_raises(self):
        """If append_markdown_to_doc raises, _delete_blocks must not be called."""
        api = _make_api()
        cfg = {}

        with patch.object(doc_ctl, "_list_blocks", return_value=self._mock_list_blocks()), \
             patch.object(doc_ctl, "_append_md", side_effect=RuntimeError("API down")), \
             patch.object(doc_ctl, "_delete_blocks") as mock_delete, \
             patch.object(doc_ctl, "_auto_archive_comments"):
            with pytest.raises(SystemExit):
                doc_ctl.cmd_replace(_make_args(content="new content"), api, cfg)

        mock_delete.assert_not_called()

    def test_delete_still_called_when_write_returns_zero(self):
        """If append_markdown_to_doc returns 0 (no blocks to write), replace proceeds
        with delete — this is valid when replacing with empty content."""
        api = _make_api()
        cfg = {}

        with patch.object(doc_ctl, "_list_blocks", return_value=self._mock_list_blocks()), \
             patch.object(doc_ctl, "_append_md", return_value=0), \
             patch.object(doc_ctl, "_delete_blocks") as mock_delete, \
             patch.object(doc_ctl, "_auto_archive_comments"):
            doc_ctl.cmd_replace(_make_args(content="new content"), api, cfg)

        mock_delete.assert_called_once()

    def test_write_before_delete_on_success(self):
        """On success, append must be called before delete."""
        api = _make_api()
        cfg = {}
        call_order = []

        with patch.object(doc_ctl, "_list_blocks", return_value=self._mock_list_blocks()), \
             patch.object(doc_ctl, "_append_md",
                          side_effect=lambda *a, **kw: call_order.append("write") or 2), \
             patch.object(doc_ctl, "_delete_blocks",
                          side_effect=lambda *a, **kw: call_order.append("delete")), \
             patch.object(doc_ctl, "_auto_archive_comments"):
            doc_ctl.cmd_replace(_make_args(content="new content"), api, cfg)

        assert call_order == ["write", "delete"], (
            f"Expected write→delete, got {call_order}"
        )

    def test_write_uses_section_end_as_index(self):
        """append_markdown_to_doc must be called with index=section_end."""
        api = _make_api()
        cfg = {}

        with patch.object(doc_ctl, "_list_blocks", return_value=self._mock_list_blocks()), \
             patch.object(doc_ctl, "_append_md", return_value=1) as mock_append, \
             patch.object(doc_ctl, "_delete_blocks"), \
             patch.object(doc_ctl, "_auto_archive_comments"):
            doc_ctl.cmd_replace(_make_args(content="new content"), api, cfg)

        # section_end = 2 (heading at 0, body at 1, section_end = 2)
        _, kwargs = mock_append.call_args
        assert kwargs.get("index") == 2, (
            f"Expected index=2 (section_end), got {kwargs.get('index')}"
        )


# ---------------------------------------------------------------------------
# TestAppendMarkdownIndexParam (unit-level, mocked API)
# ---------------------------------------------------------------------------

class TestAppendMarkdownIndexParam:
    """append_markdown_to_doc must forward index param to API calls."""

    def test_default_index_minus_one(self):
        from agent.platforms.feishu.utils import append_markdown_to_doc

        api = _make_api(post_return={
            "code": 0,
            "data": {"children": [{"block_id": "b1"}]},
        })

        append_markdown_to_doc(api, DOC_ID, "hello world")

        body = api.post.call_args_list[0].args[1]
        assert body["index"] == -1

    def test_explicit_index_forwarded(self):
        from agent.platforms.feishu.utils import append_markdown_to_doc

        api = _make_api(post_return={
            "code": 0,
            "data": {"children": [{"block_id": "b1"}]},
        })

        append_markdown_to_doc(api, DOC_ID, "hello world", index=7)

        body = api.post.call_args_list[0].args[1]
        assert body["index"] == 7

    def test_table_index_forwarded(self):
        """Table creation must use the provided index, not -1."""
        from agent.platforms.feishu.utils import append_markdown_to_doc

        captured_indices = []

        def fake_post(url, body=None, **kw):
            if body and "children" in body:
                captured_indices.append(body.get("index"))
            return {"code": 0, "data": {"children": [
                {"block_id": "tbl001", "table": {"cells": [
                    "c00", "c01", "c10", "c11"
                ]}}
            ]}}

        api = MagicMock()
        api.post.side_effect = fake_post
        api.get.return_value = {"code": 0, "data": {"items": [{"block_id": "txt001"}]}}
        api.patch.return_value = {"code": 0}

        table_md = "| A | B |\n|---|---|\n| 1 | 2 |\n"
        append_markdown_to_doc(api, DOC_ID, table_md, index=5)

        # The first children POST (table creation) must use index=5
        assert 5 in captured_indices, (
            f"Expected index=5 in children POSTs, got {captured_indices}"
        )
