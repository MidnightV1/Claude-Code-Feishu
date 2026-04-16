#!/usr/bin/env python3
"""Unit tests for doc_ctl.py Bug A + Bug B fixes.

Bug A: cmd_update/cmd_replace used _text_to_blocks + _insert_blocks, which
       passes pseudo-blocks (_table/_quote/_nested_list) directly to the
       children API → 99992402 field validation failed.

Bug B: cmd_replace / cmd_update delete blocks first then insert — on write
       failure the original content is permanently lost.

These tests mock the Feishu API and verify:
  1. Tables in new content produce _create_table_in_doc calls, NOT a direct
     children-API call with {"_table": ...} payload.
  2. cmd_update restores original content when _append_md raises.
  3. cmd_replace calls _append_md with index=heading_idx so content lands at
     the correct position (not appended to end).
  4. _eff_index in append_markdown_to_doc increments correctly across
     regular batches + special blocks.
"""

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# ── path setup ────────────────────────────────────────────
BASE = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(BASE))

from agent.platforms.feishu.utils import append_markdown_to_doc, text_to_blocks  # noqa: E402


# ── helpers ───────────────────────────────────────────────

def _make_api(children_code=0, descendant_code=0, patch_code=0,
              raw_content="backup content"):
    """Build a mock FeishuAPI with configurable response codes."""
    api = MagicMock()

    # children POST (regular blocks + table container)
    def _children_post(url, body, params=None):
        if "children" in url and body.get("children"):
            children = body["children"]
            # Fail if any child has a pseudo-block key
            for ch in children:
                for key in ("_table", "_quote", "_nested_list"):
                    if key in ch:
                        return {"code": 99992402, "msg": "field validation failed"}
            return {
                "code": children_code,
                "data": {"children": [{"block_id": f"blk_{i}"} for i in range(len(children))]},
            }
        if "descendant" in url:
            return {"code": descendant_code, "data": {"children": []}}
        return {"code": 0, "data": {}}

    api.post.side_effect = _children_post
    api.patch.return_value = {"code": patch_code, "data": {}}
    api.get.return_value = {
        "code": 0,
        "data": {
            "content": raw_content,
            "items": [],
            "has_more": False,
        }
    }
    api.delete.return_value = {"code": 0}
    return api


# ── golden data ───────────────────────────────────────────

TABLE_13_ROWS = """# Section heading

| Col A | Col B | Col C |
|-------|-------|-------|
| r1a   | r1b   | r1c   |
| r2a   | r2b   | r2c   |
| r3a   | r3b   | r3c   |
| r4a   | r4b   | r4c   |
| r5a   | r5b   | r5c   |
| r6a   | r6b   | r6c   |
| r7a   | r7b   | r7c   |
| r8a   | r8b   | r8c   |
| r9a   | r9b   | r9c   |
| r10a  | r10b  | r10c  |
| r11a  | r11b  | r11c  |
| r12a  | r12b  | r12c  |
"""

MIXED_CONTENT = """# Title

Some text paragraph.

| H1 | H2 |
|----|-----|
| a  | b   |

> A blockquote here

- item 1
  - nested item
- item 2
"""


# ── Test: Bug A — pseudo-blocks must not reach children API ──────────────

class TestBugA_NoPseudoBlocksInChildrenAPI(unittest.TestCase):
    """Verify that tables, quotes, nested lists never appear as raw pseudo-blocks
    in a children API payload (which would cause 99992402)."""

    def test_table_13rows_no_pseudo_block_in_children(self):
        api = _make_api()
        count = append_markdown_to_doc(api, "doc1", TABLE_13_ROWS)
        self.assertGreater(count, 0, "Should write at least 1 block")

        # Collect all POST payloads
        for c in api.post.call_args_list:
            body = c[0][1] if len(c[0]) > 1 else c[1].get("body", {})
            for child in body.get("children", []):
                for key in ("_table", "_quote", "_nested_list"):
                    self.assertNotIn(key, child,
                        f"Pseudo-block '{key}' must not appear in children API payload")

    def test_mixed_content_no_pseudo_block_in_children(self):
        api = _make_api()
        count = append_markdown_to_doc(api, "doc2", MIXED_CONTENT)
        self.assertGreater(count, 0)

        for c in api.post.call_args_list:
            body = c[0][1] if len(c[0]) > 1 else c[1].get("body", {})
            for child in body.get("children", []):
                for key in ("_table", "_quote", "_nested_list"):
                    self.assertNotIn(key, child,
                        f"Pseudo-block '{key}' must not appear in children API payload")


# ── Test: index parameter — positional insert ────────────────────────────

class TestPositionalIndex(unittest.TestCase):
    """Verify append_markdown_to_doc respects the index parameter."""

    def test_regular_blocks_use_given_index(self):
        api = _make_api()
        append_markdown_to_doc(api, "doc3", "Hello world\n\nSecond paragraph", index=5)

        # First children POST should use index=5
        first_call = api.post.call_args_list[0]
        body = first_call[0][1] if len(first_call[0]) > 1 else first_call[1].get("body", {})
        self.assertEqual(body.get("index"), 5,
            "First batch should be inserted at the given index=5")

    def test_index_minus1_appends_to_end(self):
        api = _make_api()
        append_markdown_to_doc(api, "doc4", "Hello world", index=-1)

        first_call = api.post.call_args_list[0]
        body = first_call[0][1] if len(first_call[0]) > 1 else first_call[1].get("body", {})
        self.assertEqual(body.get("index"), -1,
            "Default index=-1 should append to end")

    def test_offset_increments_across_batches(self):
        """Two regular-block batches: second should use index + first_batch_size."""
        api = _make_api()
        # 55 paragraphs → two chunks (50 + 5) with FLUSH_BATCH_SIZE=50
        content = "\n\n".join(f"para {i}" for i in range(55))
        append_markdown_to_doc(api, "doc5", content, index=10)

        children_calls = [
            c for c in api.post.call_args_list
            if "/children" in (c[0][0] if c[0] else "")
        ]
        self.assertGreaterEqual(len(children_calls), 2, "Should need at least 2 flush calls")
        body0 = children_calls[0][0][1]
        body1 = children_calls[1][0][1]
        self.assertEqual(body0["index"], 10, "First batch at index=10")
        self.assertEqual(body1["index"], 10 + len(body0["children"]),
            "Second batch offset by first batch size")

    def test_table_index_after_regular_blocks(self):
        """Regular blocks then a table: table uses index + regular_block_count."""
        api = _make_api()
        content = "para one\n\n| H |\n|---|\n| v |\n"
        append_markdown_to_doc(api, "doc6", content, index=3)

        # First children POST = regular block (heading / text) at index=3
        # Then table POST via _create_table_in_doc should use index=3+N
        children_calls = [
            c for c in api.post.call_args_list
            if "/children" in (c[0][0] if c[0] else "")
        ]
        self.assertGreaterEqual(len(children_calls), 1)
        body0 = children_calls[0][0][1]
        regular_count = len(body0["children"])

        # Table call: /children with block_type=31
        table_calls = [
            c for c in api.post.call_args_list
            if "/children" in (c[0][0] if c[0] else "")
            and any(ch.get("block_type") == 31 for ch in c[0][1].get("children", []))
        ]
        self.assertGreater(len(table_calls), 0, "Should have a table creation call")
        table_body = table_calls[0][0][1]
        self.assertEqual(table_body["index"], 3 + regular_count,
            "Table should be inserted after the regular blocks")


# ── Test: Bug B — cmd_update restores content on write failure ────────────

class TestBugB_UpdateRestoreOnFailure(unittest.TestCase):

    def _run_cmd_update_with_failing_append(self, backup_content):
        """Run cmd_update where _append_md raises on first call, succeeds on restore."""
        import importlib
        import io
        # Import cmd_update
        script_path = Path(__file__).parent / "doc_ctl.py"
        spec = importlib.util.spec_from_file_location("doc_ctl", script_path)
        doc_ctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(doc_ctl)

        api = MagicMock()
        api.get.return_value = {"code": 0, "data": {"content": backup_content, "items": [], "has_more": False}}
        api.delete.return_value = {"code": 0}

        call_count = [0]
        def failing_append(a, d, content, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("Simulated API failure")
            return 5  # restore succeeds

        args = MagicMock()
        args.doc_id = "testdoc"
        args.content = "New content with table\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"

        captured_stderr = io.StringIO()
        with patch.object(doc_ctl, "_append_md", side_effect=failing_append), \
             patch.object(doc_ctl, "_count_direct_children", return_value=3), \
             patch.object(doc_ctl, "_delete_blocks"), \
             patch.object(doc_ctl, "_resolve_content", return_value=args.content), \
             patch.object(doc_ctl, "_auto_archive_comments"), \
             patch("sys.exit") as mock_exit, \
             patch("sys.stderr", captured_stderr):
            doc_ctl.cmd_update(args, api, {})

        return mock_exit, call_count[0], captured_stderr.getvalue()

    def test_restore_attempted_on_write_failure(self):
        mock_exit, call_count, stderr = self._run_cmd_update_with_failing_append(
            backup_content="# Original content\n\nOriginal paragraph."
        )
        # _append_md should have been called twice: once for new content, once for restore
        self.assertEqual(call_count, 2, "Should attempt restore after write failure")
        self.assertIn("Restored", stderr, "Should print restore success message")
        mock_exit.assert_called_once_with(1)

    def test_no_restore_when_doc_was_empty(self):
        """If doc had no blocks (backup=None), no restore attempt."""
        import importlib
        import io
        script_path = Path(__file__).parent / "doc_ctl.py"
        spec = importlib.util.spec_from_file_location("doc_ctl", script_path)
        doc_ctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(doc_ctl)

        call_count = [0]
        def failing_append(a, d, content, **kwargs):
            call_count[0] += 1
            raise RuntimeError("Simulated failure")

        args = MagicMock()
        args.doc_id = "testdoc"
        args.content = "New content"

        captured_stderr = io.StringIO()
        with patch.object(doc_ctl, "_append_md", side_effect=failing_append), \
             patch.object(doc_ctl, "_count_direct_children", return_value=0), \
             patch.object(doc_ctl, "_delete_blocks"), \
             patch.object(doc_ctl, "_resolve_content", return_value=args.content), \
             patch.object(doc_ctl, "_auto_archive_comments"), \
             patch("sys.exit"), \
             patch("sys.stderr", captured_stderr):
            doc_ctl.cmd_update(args, api=MagicMock(), cfg={})

        # Only 1 call — no restore since there was nothing to restore
        self.assertEqual(call_count[0], 1, "Should not attempt restore for empty doc")


# ── Test: Bug B — cmd_replace uses positional index ──────────────────────

class TestBugB_ReplaceUsesPositionalIndex(unittest.TestCase):

    def test_replace_calls_append_md_with_heading_idx(self):
        """cmd_replace must call _append_md(api, doc_id, content, index=heading_idx)."""
        import importlib
        script_path = Path(__file__).parent / "doc_ctl.py"
        spec = importlib.util.spec_from_file_location("doc_ctl", script_path)
        doc_ctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(doc_ctl)

        captured_calls = []
        def mock_append(a, doc_id, content, index=-1):
            captured_calls.append({"doc_id": doc_id, "index": index})
            return 3

        # Simulate doc with 5 blocks: page + h1 at 0 + 2 body blocks + another section
        blocks = [
            {"block_id": "page", "block_type": 1},
            {"block_id": "h1", "block_type": 3,
             "heading1": {"elements": [{"text_run": {"content": "Target Section"}}]}},
            {"block_id": "body1", "block_type": 2,
             "text": {"elements": [{"text_run": {"content": "body"}}]}},
            {"block_id": "body2", "block_type": 2,
             "text": {"elements": [{"text_run": {"content": "body2"}}]}},
            {"block_id": "h2", "block_type": 3,
             "heading1": {"elements": [{"text_run": {"content": "Next Section"}}]}},
        ]

        api = MagicMock()
        api.get.return_value = {
            "code": 0,
            "data": {"items": blocks, "has_more": False},
        }
        api.delete.return_value = {"code": 0}

        args = MagicMock()
        args.doc_id = "testdoc"
        args.section = "Target Section"
        args.content = "| A | B |\n|---|---|\n| 1 | 2 |\n"

        with patch.object(doc_ctl, "_append_md", side_effect=mock_append), \
             patch.object(doc_ctl, "_auto_archive_comments"), \
             patch.object(doc_ctl, "_delete_blocks"):
            doc_ctl.cmd_replace(args, api, {})

        self.assertEqual(len(captured_calls), 1)
        # heading_idx = 0 (content_blocks[0] = the h1), so index should be 0
        self.assertEqual(captured_calls[0]["index"], 0,
            "cmd_replace should pass heading_idx as index to _append_md")


if __name__ == "__main__":
    unittest.main(verbosity=2)
