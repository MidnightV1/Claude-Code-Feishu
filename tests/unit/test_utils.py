# -*- coding: utf-8 -*-
"""Unit tests for agent.platforms.feishu.utils — text_to_blocks and helpers.

Test data strategy: real-world-like content (Chinese+English, markdown mix).
Edge cases from fixtures/real_messages/edge_cases.json when available.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent.platforms.feishu.utils import (
    TABLE_CREATE_ROWS,
    TABLE_MAX_COLS,
    _is_table_line,
    _parse_inline,
    _parse_markdown_table,
    _sanitize_doc_text,
    text_to_blocks,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "real_messages"


def _load_fixture(name: str):
    """Load a JSON fixture file; return None if it doesn't exist."""
    path = FIXTURES_DIR / name
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    return None


# ---------------------------------------------------------------------------
# TestSanitizeDocText
# ---------------------------------------------------------------------------

class TestSanitizeDocText:
    def test_null_byte_removed(self):
        assert "\x00" not in _sanitize_doc_text("hello\x00world")

    def test_control_chars_removed(self):
        # \x01-\x08 should be stripped
        for ch in "\x01\x02\x03\x04\x05\x06\x07\x08":
            result = _sanitize_doc_text(f"a{ch}b")
            assert ch not in result, f"char {repr(ch)} should be removed"

    def test_vertical_tab_and_form_feed_removed(self):
        # \x0b (VT) and \x0c (FF) should be stripped
        assert "\x0b" not in _sanitize_doc_text("a\x0bb")
        assert "\x0c" not in _sanitize_doc_text("a\x0cb")

    def test_0e_to_1f_removed(self):
        for code in range(0x0E, 0x20):
            ch = chr(code)
            result = _sanitize_doc_text(f"x{ch}y")
            assert ch not in result, f"char {repr(ch)} should be removed"

    def test_del_removed(self):
        assert "\x7f" not in _sanitize_doc_text("a\x7fb")

    def test_newline_preserved(self):
        text = "line1\nline2"
        assert _sanitize_doc_text(text) == text

    def test_tab_preserved(self):
        text = "col1\tcol2"
        assert _sanitize_doc_text(text) == text

    def test_carriage_return_preserved(self):
        text = "a\r\nb"
        assert "\r" in _sanitize_doc_text(text)

    def test_plain_ascii_unchanged(self):
        text = "Hello, world! 123 #$%"
        assert _sanitize_doc_text(text) == text

    def test_chinese_unchanged(self):
        text = "你好，世界！这是一段中文文本。"
        assert _sanitize_doc_text(text) == text

    def test_empty_string(self):
        assert _sanitize_doc_text("") == ""

    def test_mixed_valid_and_control(self):
        result = _sanitize_doc_text("正常\x01文字\x02内容")
        assert result == "正常文字内容"


# ---------------------------------------------------------------------------
# TestParseInline
# ---------------------------------------------------------------------------

class TestParseInline:
    def test_plain_text_returns_single_element(self):
        result = _parse_inline("这是普通文本")
        assert len(result) == 1
        assert result[0]["text_run"]["content"] == "这是普通文本"
        assert "text_element_style" not in result[0]["text_run"]

    def test_bold_single(self):
        result = _parse_inline("**重要**")
        assert len(result) == 1
        elem = result[0]["text_run"]
        assert elem["content"] == "重要"
        assert elem["text_element_style"]["bold"] is True

    def test_bold_with_surrounding_text(self):
        result = _parse_inline("前缀 **粗体内容** 后缀")
        assert len(result) == 3
        assert result[0]["text_run"]["content"] == "前缀 "
        assert result[1]["text_run"]["text_element_style"]["bold"] is True
        assert result[1]["text_run"]["content"] == "粗体内容"
        assert result[2]["text_run"]["content"] == " 后缀"

    def test_inline_code(self):
        result = _parse_inline("`git commit -m 'fix'`")
        assert len(result) == 1
        elem = result[0]["text_run"]
        assert elem["content"] == "git commit -m 'fix'"
        assert elem["text_element_style"]["inline_code"] is True

    def test_link_with_http(self):
        result = _parse_inline("[飞书文档](https://feishu.cn/docx/abc)")
        assert len(result) == 1
        elem = result[0]["text_run"]
        assert elem["content"] == "飞书文档"
        assert elem["text_element_style"]["link"]["url"] == "https://feishu.cn/docx/abc"

    def test_link_with_https(self):
        result = _parse_inline("[GitHub](https://github.com/foo/bar)")
        assert result[0]["text_run"]["text_element_style"]["link"]["url"] == "https://github.com/foo/bar"

    def test_relative_url_no_link(self):
        """Relative URLs should produce plain text_run without link style."""
        result = _parse_inline("[readme](/docs/README.md)")
        assert len(result) == 1
        elem = result[0]["text_run"]
        assert elem["content"] == "readme"
        assert "text_element_style" not in elem or "link" not in elem.get("text_element_style", {})

    def test_mixed_bold_code_link(self):
        text = "查看 **重要配置** 或 `config.yaml` 详见 [文档](https://example.com)"
        result = _parse_inline(text)
        # Should have: plain, bold, plain, code, plain, link
        contents = [e["text_run"]["content"] for e in result]
        assert "重要配置" in contents
        assert "config.yaml" in contents
        assert "文档" in contents

    def test_empty_string(self):
        result = _parse_inline("")
        assert len(result) == 1
        assert result[0]["text_run"]["content"] == ""

    def test_no_markdown_returns_plain(self):
        text = "No special formatting here. Just plain English."
        result = _parse_inline(text)
        assert result == [{"text_run": {"content": text}}]

    def test_multiple_bold_segments(self):
        result = _parse_inline("**A** and **B**")
        bold_elems = [e for e in result if e["text_run"].get("text_element_style", {}).get("bold")]
        assert len(bold_elems) == 2
        assert bold_elems[0]["text_run"]["content"] == "A"
        assert bold_elems[1]["text_run"]["content"] == "B"

    def test_inline_code_preserves_spaces(self):
        result = _parse_inline("`  spaced  `")
        assert result[0]["text_run"]["content"] == "  spaced  "

    def test_bold_chinese_content(self):
        result = _parse_inline("**中文粗体**文字")
        assert result[0]["text_run"]["content"] == "中文粗体"
        assert result[0]["text_run"]["text_element_style"]["bold"] is True
        assert result[1]["text_run"]["content"] == "文字"


# ---------------------------------------------------------------------------
# TestIsTableLine
# ---------------------------------------------------------------------------

class TestIsTableLine:
    def test_valid_simple_table_line(self):
        assert _is_table_line("| col1 | col2 |") is True

    def test_valid_separator_line(self):
        assert _is_table_line("|------|------|") is True

    def test_valid_with_leading_space(self):
        assert _is_table_line("  | a | b |  ") is True

    def test_empty_line(self):
        assert _is_table_line("") is False

    def test_whitespace_only(self):
        assert _is_table_line("   ") is False

    def test_no_leading_pipe(self):
        assert _is_table_line("col1 | col2 |") is False

    def test_no_trailing_pipe(self):
        assert _is_table_line("| col1 | col2") is False

    def test_single_pipe(self):
        # "|" starts and ends with | → True
        assert _is_table_line("|") is True

    def test_regular_text(self):
        assert _is_table_line("This is plain text") is False

    def test_chinese_table_line(self):
        assert _is_table_line("| 名称 | 版本 | 状态 |") is True


# ---------------------------------------------------------------------------
# TestParseMarkdownTable
# ---------------------------------------------------------------------------

class TestParseMarkdownTable:
    def test_simple_two_column_table(self):
        lines = [
            "| Name | Version |",
            "|------|---------|",
            "| Python | 3.13 |",
            "| Go | 1.22 |",
        ]
        result = _parse_markdown_table(lines)
        assert result is not None
        assert result[0] == ["Name", "Version"]
        assert result[1] == ["Python", "3.13"]
        assert result[2] == ["Go", "1.22"]

    def test_separator_row_skipped(self):
        lines = [
            "| A | B |",
            "|---|---|",
            "| 1 | 2 |",
        ]
        result = _parse_markdown_table(lines)
        assert len(result) == 2  # header + 1 data row (separator skipped)

    def test_column_normalization_pads_short_rows(self):
        lines = [
            "| A | B | C |",
            "|---|---|---|",
            "| 1 | 2 |",  # missing third cell
        ]
        result = _parse_markdown_table(lines)
        assert result is not None
        assert len(result[1]) == 3
        assert result[1][2] == ""

    def test_empty_lines_return_none(self):
        assert _parse_markdown_table([]) is None

    def test_only_separator_returns_none(self):
        lines = ["|---|---|"]
        result = _parse_markdown_table(lines)
        assert result is None

    def test_chinese_content(self):
        lines = [
            "| 组件 | 状态 | 版本 |",
            "|------|------|------|",
            "| 飞书 Bot | 运行中 | v2.1 |",
        ]
        result = _parse_markdown_table(lines)
        assert result is not None
        assert result[0][0] == "组件"
        assert result[1][0] == "飞书 Bot"
        assert result[1][1] == "运行中"

    def test_mixed_separator_patterns(self):
        """Separator with colons (alignment markers) should be skipped."""
        lines = [
            "| Left | Center | Right |",
            "|:-----|:------:|------:|",
            "| a    |   b    |     c |",
        ]
        result = _parse_markdown_table(lines)
        assert result is not None
        assert len(result) == 2

    def test_cells_stripped(self):
        lines = [
            "|  spaces  |  here  |",
            "| data     | more   |",
        ]
        result = _parse_markdown_table(lines)
        assert result[0] == ["spaces", "here"]
        assert result[1] == ["data", "more"]


# ---------------------------------------------------------------------------
# TestSplitTableRows
# ---------------------------------------------------------------------------

class TestSplitTableRows:
    def _make_rows(self, n_data: int, n_cols: int = 2) -> list[list[str]]:
        header = [f"H{c}" for c in range(n_cols)]
        data = [[f"r{r}c{c}" for c in range(n_cols)] for r in range(n_data)]
        return [header] + data

    def test_constants_have_expected_values(self):
        """Table constants used by _create_table_in_doc."""
        assert TABLE_CREATE_ROWS == 9  # initial create limit
        assert TABLE_MAX_COLS == 9     # column limit

    def test_text_to_blocks_large_table_produces_single_table_block(self):
        """A 20-row markdown table should produce a single _table pseudo-block."""
        rows = self._make_rows(20, 3)
        lines = []
        for i, row in enumerate(rows):
            lines.append("| " + " | ".join(row) + " |")
            if i == 0:
                lines.append("|" + "|".join(["---"] * 3) + "|")
        md = "\n".join(lines)
        blocks = text_to_blocks(md)
        table_blocks = [b for b in blocks if "_table" in b]
        assert len(table_blocks) == 1, "Should be a single table pseudo-block"
        assert len(table_blocks[0]["_table"]) == 21  # header + 20 data rows


# ---------------------------------------------------------------------------
# TestTextToBlocks
# ---------------------------------------------------------------------------

class TestTextToBlocks:
    def test_empty_string(self):
        assert text_to_blocks("") == []

    def test_whitespace_only(self):
        assert text_to_blocks("   \n\n\t\n") == []

    def test_plain_text_returns_block_type_2(self):
        blocks = text_to_blocks("Hello, world!")
        assert len(blocks) == 1
        assert blocks[0]["block_type"] == 2
        assert blocks[0]["text"]["elements"][0]["text_run"]["content"] == "Hello, world!"

    def test_h1_heading(self):
        blocks = text_to_blocks("# 标题一")
        assert len(blocks) == 1
        assert blocks[0]["block_type"] == 3  # 2 + 1
        assert "heading1" in blocks[0]

    def test_h2_heading(self):
        blocks = text_to_blocks("## 二级标题")
        assert blocks[0]["block_type"] == 4
        assert "heading2" in blocks[0]

    def test_h3_heading(self):
        blocks = text_to_blocks("### H3")
        assert blocks[0]["block_type"] == 5
        assert "heading3" in blocks[0]

    def test_h4_heading(self):
        blocks = text_to_blocks("#### H4")
        assert blocks[0]["block_type"] == 6

    def test_h5_heading(self):
        blocks = text_to_blocks("##### H5")
        assert blocks[0]["block_type"] == 7

    def test_h6_heading(self):
        blocks = text_to_blocks("###### H6")
        assert blocks[0]["block_type"] == 8

    def test_heading_content(self):
        blocks = text_to_blocks("# 架构设计文档")
        assert blocks[0]["heading1"]["elements"][0]["text_run"]["content"] == "架构设计文档"

    def test_divider(self):
        blocks = text_to_blocks("---")
        assert len(blocks) == 1
        assert blocks[0]["block_type"] == 22
        assert blocks[0]["divider"] == {}

    def test_divider_longer(self):
        blocks = text_to_blocks("------")
        assert blocks[0]["block_type"] == 22

    def test_code_block_python(self):
        md = "```python\nprint('hello')\n```"
        blocks = text_to_blocks(md)
        assert len(blocks) == 1
        assert blocks[0]["block_type"] == 14
        assert blocks[0]["code"]["style"]["language"] == 49  # python

    def test_code_block_go(self):
        md = "```go\nfmt.Println(\"hi\")\n```"
        blocks = text_to_blocks(md)
        assert blocks[0]["code"]["style"]["language"] == 22

    def test_code_block_bash(self):
        md = "```bash\ngit pull origin dev\n```"
        blocks = text_to_blocks(md)
        assert blocks[0]["code"]["style"]["language"] == 7

    def test_code_block_js(self):
        md = "```js\nconsole.log('hi')\n```"
        blocks = text_to_blocks(md)
        assert blocks[0]["code"]["style"]["language"] == 30

    def test_code_block_yaml(self):
        md = "```yaml\nkey: value\n```"
        blocks = text_to_blocks(md)
        assert blocks[0]["code"]["style"]["language"] == 67

    def test_code_block_unknown_lang_defaults_to_1(self):
        md = "```brainfuck\n++++\n```"
        blocks = text_to_blocks(md)
        assert blocks[0]["code"]["style"]["language"] == 1

    def test_code_block_no_lang(self):
        md = "```\nsome code\n```"
        blocks = text_to_blocks(md)
        assert blocks[0]["code"]["style"]["language"] == 1

    def test_unclosed_code_fence_still_produces_code_block(self):
        md = "```python\nprint('unclosed')\nno closing fence"
        blocks = text_to_blocks(md)
        assert len(blocks) == 1
        assert blocks[0]["block_type"] == 14
        code_content = blocks[0]["code"]["elements"][0]["text_run"]["content"]
        assert "print('unclosed')" in code_content

    def test_empty_code_block_gets_placeholder(self):
        md = "```\n```"
        blocks = text_to_blocks(md)
        assert blocks[0]["block_type"] == 14
        # Feishu rejects empty code blocks, so content must be non-empty
        content = blocks[0]["code"]["elements"][0]["text_run"]["content"]
        assert len(content) > 0

    def test_bullet_list_dash(self):
        blocks = text_to_blocks("- 第一项\n- 第二项")
        assert len(blocks) == 2
        assert blocks[0]["block_type"] == 12
        assert "bullet" in blocks[0]
        assert blocks[0]["bullet"]["elements"][0]["text_run"]["content"] == "第一项"

    def test_bullet_list_asterisk(self):
        blocks = text_to_blocks("* item A\n* item B")
        assert all(b["block_type"] == 12 for b in blocks)

    def test_ordered_list(self):
        blocks = text_to_blocks("1. First\n2. Second\n3. Third")
        assert len(blocks) == 3
        assert all(b["block_type"] == 13 for b in blocks)
        assert "ordered" in blocks[0]
        assert blocks[0]["ordered"]["elements"][0]["text_run"]["content"] == "First"

    def test_blockquote(self):
        blocks = text_to_blocks("> 这是引用内容")
        assert len(blocks) == 1
        assert "_quote" in blocks[0]
        quote_children = blocks[0]["_quote"]
        assert len(quote_children) == 1
        assert quote_children[0]["block_type"] == 2
        all_text = "".join(
            e["text_run"]["content"]
            for e in quote_children[0]["text"]["elements"]
        )
        assert "这是引用内容" in all_text

    def test_blockquote_multiline(self):
        blocks = text_to_blocks("> Line 1\n> Line 2")
        assert len(blocks) == 1
        assert "_quote" in blocks[0]
        assert len(blocks[0]["_quote"]) == 2

    def test_blockquote_empty(self):
        blocks = text_to_blocks(">")
        assert "_quote" in blocks[0]
        quote_children = blocks[0]["_quote"]
        assert quote_children[0]["block_type"] == 2

    def test_table_produces_table_entry(self):
        md = "| 列1 | 列2 |\n|-----|-----|\n| 值1 | 值2 |"
        blocks = text_to_blocks(md)
        assert len(blocks) == 1
        assert "_table" in blocks[0]
        table = blocks[0]["_table"]
        assert table[0] == ["列1", "列2"]
        assert table[1] == ["值1", "值2"]

    def test_table_with_10_cols_still_produces_table_entry(self):
        """Parser does NOT truncate columns — that's done in append_markdown_to_doc."""
        header = "| " + " | ".join([f"C{i}" for i in range(10)]) + " |"
        sep = "| " + " | ".join(["---"] * 10) + " |"
        row = "| " + " | ".join([str(i) for i in range(10)]) + " |"
        blocks = text_to_blocks(f"{header}\n{sep}\n{row}")
        assert "_table" in blocks[0]
        # All 10 columns should be present (no truncation by parser)
        assert len(blocks[0]["_table"][0]) == 10

    def test_empty_lines_skipped(self):
        md = "Line 1\n\n\nLine 2\n\n"
        blocks = text_to_blocks(md)
        assert len(blocks) == 2

    def test_consecutive_empty_lines(self):
        md = "\n\n\n\nSome text\n\n\n"
        blocks = text_to_blocks(md)
        assert len(blocks) == 1

    def test_literal_backslash_n_replaced(self):
        """\\n in source text should become actual newline before parsing."""
        md = "Line 1\\nLine 2"
        blocks = text_to_blocks(md)
        # Should produce 2 blocks after replacement
        assert len(blocks) == 2
        assert blocks[0]["text"]["elements"][0]["text_run"]["content"] == "Line 1"
        assert blocks[1]["text"]["elements"][0]["text_run"]["content"] == "Line 2"

    def test_inline_bold_in_heading(self):
        blocks = text_to_blocks("## **重要**标题")
        heading = blocks[0]["heading2"]["elements"]
        bold_elem = next(
            (e for e in heading if e["text_run"].get("text_element_style", {}).get("bold")),
            None,
        )
        assert bold_elem is not None
        assert bold_elem["text_run"]["content"] == "重要"

    def test_inline_code_in_paragraph(self):
        blocks = text_to_blocks("使用 `python3 scripts/promote.sh` 部署")
        elements = blocks[0]["text"]["elements"]
        code_elem = next(
            (e for e in elements if e["text_run"].get("text_element_style", {}).get("inline_code")),
            None,
        )
        assert code_elem is not None
        assert code_elem["text_run"]["content"] == "python3 scripts/promote.sh"

    def test_link_in_paragraph(self):
        blocks = text_to_blocks("参见 [架构文档](https://feishu.cn/docx/abc123)")
        elements = blocks[0]["text"]["elements"]
        link_elem = next(
            (e for e in elements
             if e["text_run"].get("text_element_style", {}).get("link")),
            None,
        )
        assert link_elem is not None
        assert link_elem["text_run"]["text_element_style"]["link"]["url"] == "https://feishu.cn/docx/abc123"

    def test_special_chars_do_not_crash(self):
        """Should not raise any exceptions."""
        text = "!@#$%^&*()[]{}|\\/<>?~`±§™©®€¥"
        blocks = text_to_blocks(text)
        assert len(blocks) == 1

    def test_chinese_english_mixed(self):
        md = "# 项目状态报告 Project Status\n\n这是一个混合内容 mixed content 的示例。\n\n- **关键指标** Key Metrics: `99.9%` uptime"
        blocks = text_to_blocks(md)
        assert blocks[0]["block_type"] == 3  # H1
        assert blocks[1]["block_type"] == 2  # plain text
        assert blocks[2]["block_type"] == 12  # bullet

    def test_mixed_content_ordering(self):
        md = (
            "# 标题\n"
            "---\n"
            "```python\ncode here\n```\n"
            "| A | B |\n|---|---|\n| 1 | 2 |\n"
            "- 列表项\n"
            "1. 有序项\n"
            "> 引用\n"
            "普通文本\n"
        )
        blocks = text_to_blocks(md)
        def block_type(b):
            if "_table" in b: return "_table"
            if "_quote" in b: return "_quote"
            return b.get("block_type")
        types = [block_type(b) for b in blocks]
        # Check order: heading(3), divider(22), code(14), table, bullet(12), ordered(13), quote, text(2)
        assert types[0] == 3
        assert types[1] == 22
        assert types[2] == 14
        assert types[3] == "_table"
        assert types[4] == 12
        assert types[5] == 13
        assert types[6] == "_quote"  # blockquote → quote container
        assert types[7] == 2  # plain text

    def test_real_technical_doc_excerpt(self):
        """Real-world-like Chinese technical documentation excerpt."""
        md = """# nas-claude-code-feishu 架构说明

## 核心组件

系统由以下核心模块组成：

- **agent/main.py** — 入口点，PID 管理，SIGUSR1 热加载
- **agent/platforms/feishu/bot.py** — WebSocket Bot，事件分发
- **agent/llm/router.py** — 多模型路由（claude/gemini）

## 部署流程

```bash
# 推送到生产
./scripts/promote.sh

# 检查服务状态
launchctl list | grep claude-code-feishu
```

## 版本信息

| 组件 | 版本 | 状态 |
|------|------|------|
| Python | 3.13.12 | 稳定 |
| Claude CLI | 最新 | 运行中 |
| 飞书 SDK | 固定版本 | 运行中 |

---

详见 [PLAN.md](https://feishu.cn/docx/plan123) 获取完整架构设计。
"""
        blocks = text_to_blocks(md)
        block_types = [b.get("block_type") for b in blocks]

        # Must have heading blocks
        assert 3 in block_types  # H1
        assert 4 in block_types  # H2
        # Must have bullet blocks
        assert 12 in block_types
        # Must have code block
        assert 14 in block_types
        # Must have divider
        assert 22 in block_types
        # Must have table
        table_blocks = [b for b in blocks if "_table" in b]
        assert len(table_blocks) == 1
        table = table_blocks[0]["_table"]
        assert table[0] == ["组件", "版本", "状态"]
        assert table[1][0] == "Python"

    def test_code_multiline_content_preserved(self):
        code = "def foo():\n    return 42\n\n\nprint(foo())"
        md = f"```python\n{code}\n```"
        blocks = text_to_blocks(md)
        content = blocks[0]["code"]["elements"][0]["text_run"]["content"]
        assert "def foo():" in content
        assert "return 42" in content
        assert "print(foo())" in content

    def test_heading_without_space_not_matched(self):
        """#NoSpace should be treated as plain text, not heading."""
        blocks = text_to_blocks("#NoSpace")
        # heading_match requires '# text' with space
        assert blocks[0]["block_type"] == 2

    def test_multiple_tables_in_one_document(self):
        md = (
            "| A | B |\n|---|---|\n| 1 | 2 |\n"
            "\n"
            "| X | Y | Z |\n|---|---|---|\n| a | b | c |\n"
        )
        blocks = text_to_blocks(md)
        table_blocks = [b for b in blocks if "_table" in b]
        assert len(table_blocks) == 2

    def test_unicode_emoji_in_text(self):
        """Emoji characters should pass through without errors."""
        blocks = text_to_blocks("✅ 部署成功 🚀 系统正常运行")
        assert len(blocks) == 1
        content = blocks[0]["text"]["elements"][0]["text_run"]["content"]
        assert "✅" in content
        assert "🚀" in content

    def test_control_chars_sanitized_in_input(self):
        """Null bytes in input should be stripped before block creation."""
        blocks = text_to_blocks("正常文字\x00隐藏内容")
        content = blocks[0]["text"]["elements"][0]["text_run"]["content"]
        assert "\x00" not in content

    # ── Nested lists ──

    def test_nested_unordered_list_2_levels(self):
        """Two-level nested bullet list emits _nested_list marker."""
        md = "- 第一层\n  - 第二层子项\n- 回到第一层"
        blocks = text_to_blocks(md)
        assert len(blocks) == 1
        assert "_nested_list" in blocks[0]
        items = blocks[0]["_nested_list"]
        assert len(items) == 3
        assert items[0]["depth"] == 0
        assert items[1]["depth"] == 1
        assert items[2]["depth"] == 0
        assert items[1]["elements"][0]["text_run"]["content"] == "第二层子项"

    def test_nested_unordered_list_3_levels(self):
        """Three-level nested bullet list emits _nested_list."""
        md = "- L1\n  - L2\n    - L3"
        blocks = text_to_blocks(md)
        assert len(blocks) == 1
        items = blocks[0]["_nested_list"]
        assert len(items) == 3
        assert [i["depth"] for i in items] == [0, 1, 2]
        assert items[2]["elements"][0]["text_run"]["content"] == "L3"

    def test_nested_ordered_list_2_levels(self):
        """Two-level nested ordered list emits _nested_list."""
        md = "1. 步骤一\n  1. 子步骤A\n  2. 子步骤B\n2. 步骤二"
        blocks = text_to_blocks(md)
        assert len(blocks) == 1
        items = blocks[0]["_nested_list"]
        assert len(items) == 4
        assert items[0]["depth"] == 0
        assert items[1]["depth"] == 1
        assert items[1]["elements"][0]["text_run"]["content"] == "子步骤A"

    def test_nested_mixed_list_complex(self):
        """Complex nested list with multiple branches emits _nested_list."""
        md = "- 功能模块\n  - 用户系统\n    - 注册\n    - 登录\n  - 数据管理\n- 非功能需求"
        blocks = text_to_blocks(md)
        assert len(blocks) == 1
        items = blocks[0]["_nested_list"]
        assert len(items) == 6
        assert [i["depth"] for i in items] == [0, 1, 2, 2, 1, 0]

    # ── Card directive ──

    def test_card_directive_stripped(self):
        """Card directive is stripped (chat-only, not for docs)."""
        md = "{{card:header=部署完成,color=green}}\n服务已更新"
        blocks = text_to_blocks(md)
        # Directive stripped, only content remains
        assert len(blocks) == 1
        assert blocks[0]["block_type"] == 2
        assert "服务已更新" in blocks[0]["text"]["elements"][0]["text_run"]["content"]

    def test_card_directive_header_only_stripped(self):
        """Card directive with header only is also stripped."""
        md = "{{card:header=测试标题}}\n内容"
        blocks = text_to_blocks(md)
        assert len(blocks) == 1
        assert "内容" in blocks[0]["text"]["elements"][0]["text_run"]["content"]

    def test_no_card_directive_passthrough(self):
        """Text without card directive should pass through unchanged."""
        md = "普通文本\n第二行"
        blocks = text_to_blocks(md)
        assert blocks[0]["block_type"] == 2
        assert "普通文本" in blocks[0]["text"]["elements"][0]["text_run"]["content"]


# ---------------------------------------------------------------------------
# TestTextToBlocksRealData — load from fixtures when available
# ---------------------------------------------------------------------------

class TestTextToBlocksRealData:
    """Tests that load from fixtures/real_messages/edge_cases.json if present."""

    def test_fixture_edge_cases(self):
        data = _load_fixture("edge_cases.json")
        if data is None:
            pytest.skip("fixtures/real_messages/edge_cases.json not found")

        for case in data:
            name = case.get("name", "unknown")
            text = case["input"]
            expected_count = case.get("expected_block_count")
            expected_types = case.get("expected_block_types")

            blocks = text_to_blocks(text)

            if expected_count is not None:
                assert len(blocks) == expected_count, f"Case '{name}': block count mismatch"

            if expected_types is not None:
                types = [b.get("block_type") for b in blocks]
                assert types == expected_types, f"Case '{name}': block types mismatch"

    def test_fixture_real_messages(self):
        data = _load_fixture("messages.json")
        if data is None:
            pytest.skip("fixtures/real_messages/messages.json not found")

        for msg in data:
            text = msg.get("text", "")
            # Should not raise — basic smoke test
            try:
                blocks = text_to_blocks(text)
                assert isinstance(blocks, list)
            except Exception as e:
                pytest.fail(f"text_to_blocks raised for message: {text[:80]!r} — {e}")
