# -*- coding: utf-8 -*-
"""Shared utility functions for Feishu integration."""

import logging
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone

log = logging.getLogger("hub.feishu.utils")

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai


def _sanitize_doc_text(text: str) -> str:
    """Sanitize text before sending to Feishu docx API.

    Strips control chars (except newline/tab), null bytes, and normalizes whitespace.
    Prevents 400 errors from malformed content.
    """
    # Remove null bytes and control chars except \n \t \r
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    return text


# Combined pattern for inline formatting: bold, italic, strikethrough, code, links
_INLINE_RE = re.compile(
    r'\*\*(.+?)\*\*'              # group 1: bold
    r'|~~(.+?)~~'                 # group 2: strikethrough
    r'|\*(.+?)\*'                 # group 3: italic
    r'|`([^`]+)`'                 # group 4: inline code
    r'|\[([^\]]+)\]\(([^)]+)\)'   # group 5,6: link
)

# HTML tags that appear in Feishu card markdown but not supported in doc blocks
_HTML_STRIP_RE = re.compile(
    r"<font\s+color=['\"]?\w+['\"]?>(.*?)</font>"
    r"|<text_tag\s+color=['\"]?\w+['\"]?>(.*?)</text_tag>"
    r"|<at\s+id=['\"]?[\w]+['\"]?>\s*</at>"
)


def _strip_html_tags(text: str) -> str:
    """Strip Feishu card HTML tags, keeping inner text content."""
    def _replace(m: re.Match) -> str:
        return m.group(1) or m.group(2) or ""
    return _HTML_STRIP_RE.sub(_replace, text)


def _parse_inline(text: str) -> list[dict]:
    """Parse inline markdown (bold, italic, strikethrough, code, link) into Feishu text_run elements."""
    text = _strip_html_tags(text)

    elements = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            plain = text[pos:m.start()]
            if plain:
                elements.append({"text_run": {"content": plain}})

        if m.group(1) is not None:  # bold
            elements.append({"text_run": {
                "content": m.group(1),
                "text_element_style": {"bold": True},
            }})
        elif m.group(2) is not None:  # strikethrough
            elements.append({"text_run": {
                "content": m.group(2),
                "text_element_style": {"strikethrough": True},
            }})
        elif m.group(3) is not None:  # italic
            elements.append({"text_run": {
                "content": m.group(3),
                "text_element_style": {"italic": True},
            }})
        elif m.group(4) is not None:  # inline code
            elements.append({"text_run": {
                "content": m.group(4),
                "text_element_style": {"inline_code": True},
            }})
        elif m.group(5) is not None:  # link
            url = m.group(6)
            if url.startswith("http://") or url.startswith("https://"):
                elements.append({"text_run": {
                    "content": m.group(5),
                    "text_element_style": {"link": {"url": url}},
                }})
            else:
                elements.append({"text_run": {"content": m.group(5)}})
        pos = m.end()

    if pos < len(text):
        remaining = text[pos:]
        if remaining:
            elements.append({"text_run": {"content": remaining}})

    if not elements:
        elements.append({"text_run": {"content": text}})

    return elements


TABLE_CREATE_ROWS = 9  # Feishu docx API: initial create limit (10+ → 1770001)
TABLE_MAX_COLS = 9     # Feishu docx API: column_size ≤ 9
TABLE_ROW_INSERT_DELAY = 0.05  # seconds between insert_table_row calls (rate limit)
BLOCK_CHUNK_DELAY = 0.1
TABLE_CELL_FILL_DELAY = 0.05


def _parse_markdown_table(lines: list[str]) -> list[list[str]] | None:
    """Parse consecutive markdown table lines into a 2D list of cell texts.

    Returns rows (list of lists), or None if parsing fails.
    First row is treated as header.
    """
    data_rows = []
    for line in lines:
        if re.match(r'^\|[-\s|:]+\|$', line):
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        if cells:
            data_rows.append(cells)

    if not data_rows:
        return None

    # Normalize: pad short rows
    col_count = max(len(r) for r in data_rows)
    for row in data_rows:
        while len(row) < col_count:
            row.append("")

    return data_rows


def _is_table_line(line: str) -> bool:
    """Check if a line is part of a markdown table."""
    stripped = line.strip()
    if not stripped:
        return False
    return (stripped.startswith('|') and stripped.endswith('|'))


_MARKDOWN_SYNTAX_RE = re.compile(
    r'\*\*(.+?)\*\*'              # bold → content
    r'|~~(.+?)~~'                 # strikethrough → content
    r'|\*(.+?)\*'                 # italic → content
    r'|`([^`]+)`'                 # inline code → content
    r'|\[([^\]]+)\]\([^)]+\)'     # link → link text only
)


def _display_width(text: str) -> int:
    """Calculate display width of cell text, stripping markdown syntax.

    CJK characters count as 2 units. Markdown formatting markers (**bold**,
    `code`, [text](url), etc.) are stripped to measure only visible content.
    """
    # Strip markdown syntax, keeping only visible text
    def _replace(m: re.Match) -> str:
        return m.group(1) or m.group(2) or m.group(3) or m.group(4) or m.group(5) or ""
    plain = _MARKDOWN_SYNTAX_RE.sub(_replace, text or "")
    return sum(2 if ord(c) > 0x7F else 1 for c in plain)


_CODE_LANG_MAP = {
    # Ref: DocxCodeLanguage enum (chyroc/lark type_docx.go)
    "": 1, "text": 1, "plaintext": 1,
    "bash": 7, "sh": 7, "shell": 60, "zsh": 7,
    "c": 10, "cpp": 9, "c++": 9, "csharp": 8, "c#": 8,
    "css": 12, "dart": 15, "dockerfile": 18,
    "go": 22, "groovy": 23, "html": 24, "http": 26,
    "java": 29, "javascript": 30, "js": 30,
    "json": 28, "kotlin": 32, "latex": 33, "lua": 36,
    "makefile": 38, "markdown": 39, "md": 39,
    "nginx": 40, "objc": 41, "objective-c": 41,
    "php": 43, "perl": 44, "powershell": 46,
    "python": 49, "py": 49, "r": 50, "ruby": 52, "rust": 53,
    "scss": 55, "sql": 56, "scala": 57, "swift": 61,
    "typescript": 63, "ts": 63, "xml": 66, "yaml": 67, "yml": 67,
}


def text_to_blocks(text: str) -> list[dict]:
    """Convert markdown-like text to Feishu docx block children.

    Supported syntax:
    - # H1 .. ###### H6 → heading blocks
    - --- → divider
    - ```lang ... ``` → code blocks (block_type 14)
    - - item → bullet list block (block_type 12)
    - 1. item → ordered list block (block_type 13)
    - > quote → quote_container (block_type 34) with text children
    - **bold**, `code`, [text](url) → inline formatting
    - | table | rows → native table blocks (via descendant API in append_markdown_to_doc)
    - Plain text → text block (block_type 2)

    Note: Tables are returned as special {"_table": payload} entries.
    Use append_markdown_to_doc() for proper table rendering, or filter them out
    for the simple children API.
    """
    text = text.replace("\\n", "\n")
    text = _sanitize_doc_text(text)

    # Strip card directive (chat-only, not renderable in docs)
    text = re.sub(
        r'^\{\{card:header=[^}]*\}\}\s*\n?',
        '',
        text,
    )

    blocks = []
    lines = text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i].rstrip()

        if not line:
            i += 1
            continue

        # Fenced code block: ```lang ... ```
        fence_match = re.match(r'^```(\w*)', line)
        if fence_match:
            lang = fence_match.group(1)
            i += 1
            code_lines = []
            while i < len(lines) and not re.match(r'^```\s*$', lines[i].rstrip()):
                code_lines.append(lines[i].rstrip('\r'))
                i += 1
            if i < len(lines):  # skip closing ```
                i += 1
            code_content = "\n".join(code_lines)
            if not code_content:
                code_content = " "  # Feishu rejects empty code blocks
            blocks.append({
                "block_type": 14,
                "code": {
                    "style": {
                        "language": _CODE_LANG_MAP.get(lang.lower(), 1),
                    },
                    "elements": [{"text_run": {"content": code_content}}],
                },
            })
            continue

        # Collect consecutive table lines
        if _is_table_line(line):
            table_lines = []
            while i < len(lines) and _is_table_line(lines[i].rstrip()):
                table_lines.append(lines[i].rstrip())
                i += 1
            table_data = _parse_markdown_table(table_lines)
            if table_data:
                blocks.append({"_table": table_data})
            else:
                for tl in table_lines:
                    blocks.append({
                        "block_type": 2,
                        "text": {"elements": [{"text_run": {"content": tl}}]},
                    })
            continue

        # --- divider
        if re.match(r'^-{3,}$', line.strip()):
            blocks.append({"block_type": 22, "divider": {}})
            i += 1
            continue

        # Headings: # H1 .. ###### H6
        heading_match = re.match(r"^(#{1,9})\s+(.+)$", line)
        if heading_match:
            level = len(heading_match.group(1))
            heading_text = heading_match.group(2)
            blocks.append({
                "block_type": 2 + level,
                "heading" + str(level): {
                    "elements": _parse_inline(heading_text),
                },
            })
            i += 1
            continue

        # List items: collect consecutive items, detect nesting
        ul_match = re.match(r'^(\s*)([-*])\s+(.+)$', line)
        ol_match = re.match(r'^(\s*)(\d+)\.\s+(.+)$', line) if not ul_match else None
        if ul_match or ol_match:
            list_items = []
            while i < len(lines):
                cur = lines[i].rstrip()
                if not cur:
                    break
                cur_ul = re.match(r'^(\s*)([-*])\s+(.+)$', cur)
                cur_ol = re.match(r'^(\s*)(\d+)\.\s+(.+)$', cur) if not cur_ul else None
                if cur_ul:
                    indent = len(cur_ul.group(1))
                    list_items.append({
                        "type": "bullet", "depth": indent // 2,
                        "elements": _parse_inline(cur_ul.group(3)),
                    })
                    i += 1
                elif cur_ol:
                    indent = len(cur_ol.group(1))
                    list_items.append({
                        "type": "ordered", "depth": indent // 2,
                        "elements": _parse_inline(cur_ol.group(3)),
                    })
                    i += 1
                else:
                    break

            has_nesting = any(item["depth"] > 0 for item in list_items)
            if has_nesting:
                blocks.append({"_nested_list": list_items})
            else:
                for item in list_items:
                    bt = 12 if item["type"] == "bullet" else 13
                    key = "bullet" if item["type"] == "bullet" else "ordered"
                    blocks.append({"block_type": bt, key: {"elements": item["elements"]}})
            continue

        # Blockquote: consecutive > lines → native quote container
        quote_match = re.match(r'^>\s*(.*)$', line)
        if quote_match:
            quote_lines: list[dict] = []
            while i < len(lines):
                qm = re.match(r'^>\s*(.*)$', lines[i])
                if not qm:
                    break
                content = qm.group(1) or ""
                elements = _parse_inline(content) if content else [
                    {"text_run": {"content": ""}}
                ]
                quote_lines.append({
                    "block_type": 2,
                    "text": {"elements": elements},
                })
                i += 1
            blocks.append({"_quote": quote_lines})
            continue

        # Regular text with inline formatting
        blocks.append({
            "block_type": 2,
            "text": {
                "elements": _parse_inline(line),
            },
        })
        i += 1

    return blocks


def _create_table_in_doc(api, doc_id: str, rows: list[list[str]], index: int = -1) -> str | None:
    """Create a native table in a Feishu document.

    For tables with more rows than TABLE_CREATE_ROWS, uses incremental approach:
    1. Create table with initial batch of rows (≤ TABLE_CREATE_ROWS)
    2. Append remaining rows via insert_table_row (batch_update API)
    3. Fill all cells with content via PATCH

    Args:
        api: FeishuAPI instance
        doc_id: Document ID
        rows: 2D list of cell texts (first row = header)
        index: Insert position (-1 = append)

    Returns table block_id on success, None on failure.
    """
    row_count = len(rows)
    col_count = len(rows[0]) if rows else 0
    if row_count == 0 or col_count == 0:
        return None

    # Calculate column widths based on content length
    TABLE_TOTAL_WIDTH = 700
    MIN_COL_WIDTH = 60
    max_lens = [0] * col_count
    for row in rows:
        for ci, cell in enumerate(row):
            w = _display_width(cell)
            if w > max_lens[ci]:
                max_lens[ci] = w
    max_lens = [max(l, 1) for l in max_lens]
    total_w = sum(max_lens)
    col_widths = [max(MIN_COL_WIDTH, int(TABLE_TOTAL_WIDTH * l / total_w)) for l in max_lens]

    # Step 1: Create table with initial rows (capped at TABLE_CREATE_ROWS)
    initial_rows = min(row_count, TABLE_CREATE_ROWS)
    try:
        resp = api.post(
            f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
            {
                "children": [{
                    "block_type": 31,
                    "table": {
                        "property": {
                            "row_size": initial_rows,
                            "column_size": col_count,
                            "column_width": col_widths,
                            "header_row": True,
                        }
                    }
                }],
                "index": index,
            },
            params={"document_revision_id": "-1"},
        )
    except Exception as e:
        log.error("Create table failed (HTTP): %s | rows=%d cols=%d doc=%s",
                  e, row_count, col_count, doc_id)
        return None
    if resp.get("code") != 0:
        log.error("Create table failed (API): %s | rows=%d cols=%d doc=%s",
                  resp.get("msg"), row_count, col_count, doc_id)
        return None

    table_block = resp["data"]["children"][0]
    table_bid = table_block.get("block_id")

    # Step 2: Insert remaining rows incrementally via batch_update
    extra_rows = row_count - initial_rows
    if extra_rows > 0:
        log.info("Table %d rows: created %d, inserting %d more | doc=%s",
                 row_count, initial_rows, extra_rows, doc_id)
        for i in range(extra_rows):
            if i > 0 and TABLE_ROW_INSERT_DELAY > 0:
                import time
                time.sleep(TABLE_ROW_INSERT_DELAY)
            try:
                r = api.patch(
                    f"/open-apis/docx/v1/documents/{doc_id}/blocks/batch_update",
                    {"requests": [{"block_id": table_bid,
                                   "insert_table_row": {"row_index": initial_rows + i}}]},
                    params={"document_revision_id": "-1"},
                )
                if r.get("code") != 0:
                    log.error("insert_table_row failed at %d: %s | doc=%s",
                              initial_rows + i, r.get("msg"), doc_id)
                    # Truncate rows to what we have
                    row_count = initial_rows + i
                    break
            except Exception as e:
                log.error("insert_table_row HTTP error at %d: %s | doc=%s",
                          initial_rows + i, e, doc_id)
                row_count = initial_rows + i
                break

    # Step 3: Get all cell IDs (re-fetch after row insertion)
    # Brief delay to let server finalize table mutations before reading
    import time
    time.sleep(0.3)
    try:
        block_resp = api.get(
            f"/open-apis/docx/v1/documents/{doc_id}/blocks/{table_bid}",
            params={"document_revision_id": "-1"},
        )
        cell_ids = block_resp.get("data", {}).get("block", {}).get("table", {}).get("cells", [])
    except Exception as e:
        log.error("Failed to fetch table cells: %s | doc=%s", e, doc_id)
        return table_bid

    expected_cells = row_count * col_count
    if len(cell_ids) < expected_cells:
        log.warning("Table cell count mismatch: expected %d, got %d",
                     expected_cells, len(cell_ids))
        row_count = len(cell_ids) // col_count  # fill what we can

    # Step 4: Fill cells
    _fill_table_cells(api, doc_id, table_bid, cell_ids, rows[:row_count], col_count)

    return table_bid


def _resolve_cell_text_blocks(api, doc_id: str, cell_ids: list[str]) -> dict[str, str]:
    """Batch-resolve cell_id -> text_block_id by listing all doc blocks once.

    Falls back to per-cell GET if the bulk approach fails.
    """
    cell_set = set(cell_ids)
    mapping: dict[str, str] = {}  # cell_id -> text_block_id

    try:
        # Fetch ALL blocks in the document (includes nested table cell children)
        blocks: list[dict] = []
        page_token = None
        while True:
            params = {"page_size": "500", "document_revision_id": "-1"}
            if page_token:
                params["page_token"] = page_token
            resp = api.get(f"/open-apis/docx/v1/documents/{doc_id}/blocks", params=params)
            if resp.get("code") != 0:
                break
            blocks.extend(resp.get("data", {}).get("items", []))
            if not resp.get("data", {}).get("has_more"):
                break
            page_token = resp["data"].get("page_token")

        # Build mapping: find text blocks whose parent_id is a cell
        for block in blocks:
            parent = block.get("parent_id", "")
            if parent in cell_set and block.get("block_type") == 2:
                if parent not in mapping:  # first text child only
                    mapping[parent] = block["block_id"]
    except Exception as e:
        log.warning("Bulk block listing failed, will fallback per-cell: %s", e)

    return mapping


def _fill_table_cells(api, doc_id: str, table_bid: str,
                      cell_ids: list[str], rows: list[list[str]], col_count: int):
    """Fill table cells with content using batch operations.

    Optimized: resolves all text_block_ids in one bulk call, then batches
    update_text_elements via batch_update API (up to 20 per call).
    """
    BATCH_SIZE = 20  # batch_update API limit per request

    # Phase 1: Bulk-resolve cell_id -> text_block_id
    cell_text_map = _resolve_cell_text_blocks(api, doc_id, cell_ids)

    # Phase 2: Build update requests
    update_requests: list[dict] = []
    fallback_cells: list[tuple[int, int, int, str]] = []  # (ri, ci, idx, cell_text)

    idx = 0
    for ri, row in enumerate(rows):
        for ci, cell_text in enumerate(row):
            if idx >= len(cell_ids):
                break
            cell_id = cell_ids[idx]
            idx += 1

            # Build elements
            if ri == 0:
                if cell_text:
                    elements = _parse_inline(cell_text)
                    for el in elements:
                        tr = el.get("text_run")
                        if tr:
                            style = tr.setdefault("text_element_style", {})
                            style["bold"] = True
                else:
                    elements = [{"text_run": {"content": ""}}]
            else:
                elements = _parse_inline(cell_text) if cell_text else [
                    {"text_run": {"content": ""}}
                ]

            text_bid = cell_text_map.get(cell_id)
            if text_bid:
                update_requests.append({
                    "block_id": text_bid,
                    "update_text_elements": {"elements": elements},
                })
            else:
                fallback_cells.append((ri, ci, idx - 1, cell_text))

    # Phase 3: Batch update via batch_update API
    for i in range(0, len(update_requests), BATCH_SIZE):
        batch = update_requests[i:i + BATCH_SIZE]
        try:
            resp = api.patch(
                f"/open-apis/docx/v1/documents/{doc_id}/blocks/batch_update",
                {"requests": batch},
                params={"document_revision_id": "-1"},
            )
            if resp.get("code") != 0:
                log.warning("batch_update failed (code %s): %s | doc=%s batch_offset=%d",
                            resp.get("code"), resp.get("msg"), doc_id, i)
                # Fallback: try individual updates for this batch
                for req in batch:
                    try:
                        api.patch(
                            f"/open-apis/docx/v1/documents/{doc_id}/blocks/{req['block_id']}",
                            {"update_text_elements": req["update_text_elements"]},
                            params={"document_revision_id": "-1"},
                        )
                    except Exception:
                        pass
        except Exception as e:
            log.warning("batch_update HTTP error: %s | doc=%s", e, doc_id)

    # Phase 4: Handle cells that weren't in the bulk mapping (fallback per-cell)
    for ri, ci, cell_idx, cell_text in fallback_cells:
        cell_id = cell_ids[cell_idx]
        try:
            child_resp = api.get(
                f"/open-apis/docx/v1/documents/{doc_id}/blocks/{cell_id}/children",
                params={"document_revision_id": "-1"},
            )
            items = child_resp.get("data", {}).get("items", [])
            if items:
                text_block_id = items[0]["block_id"]
            else:
                create_resp = api.post(
                    f"/open-apis/docx/v1/documents/{doc_id}/blocks/{cell_id}/children",
                    {"children": [{"block_type": 2, "text": {"elements": [
                        {"text_run": {"content": ""}}
                    ]}}]},
                    params={"document_revision_id": "-1"},
                )
                created = create_resp.get("data", {}).get("children", [])
                if not created:
                    log.warning("Create text block in cell [%d,%d] failed | doc=%s",
                                ri, ci, doc_id)
                    continue
                text_block_id = created[0]["block_id"]

            if ri == 0:
                if cell_text:
                    elements = _parse_inline(cell_text)
                    for el in elements:
                        tr = el.get("text_run")
                        if tr:
                            style = tr.setdefault("text_element_style", {})
                            style["bold"] = True
                else:
                    elements = [{"text_run": {"content": ""}}]
            else:
                elements = _parse_inline(cell_text) if cell_text else [
                    {"text_run": {"content": ""}}
                ]

            api.patch(
                f"/open-apis/docx/v1/documents/{doc_id}/blocks/{text_block_id}",
                {"update_text_elements": {"elements": elements}},
                params={"document_revision_id": "-1"},
            )
        except Exception as e:
            log.warning("Fill cell [%d,%d] fallback failed: %s", ri, ci, e)


def _build_descendant_payload(items: list[dict]) -> tuple[list[str], list[dict]]:
    """Build Feishu descendant API payload from flat list items with depth.

    Returns (children_id, descendants) where:
    - children_id: top-level temp block IDs
    - descendants: all block definitions with parent-child relationships
    """
    # Assign temp IDs
    for item in items:
        item["_tid"] = f"tmp_{uuid.uuid4().hex[:8]}"

    descendants = []
    top_level_ids = []
    # Stack: [(depth, temp_id, desc_dict)]
    stack: list[tuple[int, str, dict]] = []

    for item in items:
        depth = item["depth"]
        bt = 12 if item["type"] == "bullet" else 13
        key = "bullet" if item["type"] == "bullet" else "ordered"

        desc = {
            "block_id": item["_tid"],
            "block_type": bt,
            key: {"elements": item["elements"]},
        }

        # Pop stack to find parent (first item with depth < current)
        while stack and stack[-1][0] >= depth:
            stack.pop()

        if stack:
            # Attach as child of parent
            stack[-1][2].setdefault("children", []).append(item["_tid"])
        else:
            top_level_ids.append(item["_tid"])

        descendants.append(desc)
        stack.append((depth, item["_tid"], desc))

    # Clean up temp keys
    for item in items:
        item.pop("_tid", None)

    return top_level_ids, descendants


def _create_nested_list_in_doc(api, doc_id: str, items: list[dict], index: int = -1) -> int:
    """Create nested list blocks using the Feishu descendant API.

    Returns the number of top-level blocks created.
    """
    children_id, descendants = _build_descendant_payload(items)
    if not children_id:
        return 0

    try:
        resp = api.post(
            f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/descendant",
            {
                "children_id": children_id,
                "index": index,
                "descendants": descendants,
            },
            params={"document_revision_id": "-1"},
        )
    except Exception as e:
        log.error("Descendant API failed (HTTP): %s | doc=%s", e, doc_id)
        return 0

    if resp.get("code") != 0:
        log.error("Descendant API failed (code %s): %s | doc=%s",
                  resp.get("code"), resp.get("msg"), doc_id)
        return 0

    return len(children_id)


def _create_quote_in_doc(api, doc_id: str, text_blocks: list[dict], index: int = -1) -> str | None:
    """Create a native quote_container (block_type 34) for markdown blockquotes.

    Two-step: create empty quote_container, then add text children.
    Returns the container block_id, or None on failure.
    """
    if not text_blocks:
        return None

    # Step 1: Create quote_container
    try:
        resp = api.post(
            f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
            {
                "children": [{"block_type": 34, "quote_container": {}}],
                "index": index,
            },
            params={"document_revision_id": "-1"},
        )
    except Exception as e:
        log.error("Create quote_container failed (HTTP): %s | doc=%s", e, doc_id)
        return None

    if resp.get("code") != 0:
        log.error("Create quote_container failed (code %s): %s | doc=%s",
                  resp.get("code"), resp.get("msg"), doc_id)
        return None

    qc_block = resp["data"]["children"][0]
    qc_bid = qc_block.get("block_id")

    # Step 2: Overwrite the auto-generated empty child with our first block.
    # Feishu creates a default empty text block inside quote_container;
    # deleting it just re-creates another. Instead, PATCH it with our content.
    try:
        child_resp = api.get(
            f"/open-apis/docx/v1/documents/{doc_id}/blocks/{qc_bid}/children",
            params={"document_revision_id": "-1"},
        )
        auto_items = child_resp.get("data", {}).get("items", [])
        if auto_items and text_blocks:
            first_block = text_blocks[0]
            auto_bid = auto_items[0]["block_id"]
            # Overwrite the empty block with our first block's content
            elements = first_block.get("text", {}).get("elements", [])
            if elements:
                api.patch(
                    f"/open-apis/docx/v1/documents/{doc_id}/blocks/{auto_bid}",
                    {"update_text_elements": {"elements": elements}},
                    params={"document_revision_id": "-1"},
                )
            text_blocks = text_blocks[1:]  # remaining blocks
    except Exception as e:
        log.debug("Overwrite auto-child in quote_container: %s", e)

    # Step 3: Add remaining children inside the container
    if text_blocks:
        try:
            resp2 = api.post(
                f"/open-apis/docx/v1/documents/{doc_id}/blocks/{qc_bid}/children",
                {"children": text_blocks, "index": -1},
                params={"document_revision_id": "-1"},
            )
            if resp2.get("code") != 0:
                log.warning("Add quote children failed (code %s): %s | doc=%s",
                            resp2.get("code"), resp2.get("msg"), doc_id)
        except Exception as e:
            log.warning("Add quote children failed (HTTP): %s | doc=%s", e, doc_id)

    return qc_bid


def append_markdown_to_doc(api, doc_id: str, markdown: str, index: int = -1) -> int:
    """Append markdown content to a Feishu document, with native table support.

    Regular blocks use the children API (flat).
    Tables use a two-step create-then-fill flow.
    If table creation fails, degrades to plain-text pipe-delimited rows
    (no information loss). If a flush itself fails, attempts to rollback
    previously created blocks in this call (best-effort).

    Returns the total number of top-level blocks appended.
    """
    all_blocks = text_to_blocks(markdown)
    if not all_blocks:
        return 0

    total = 0
    _offset = 0  # blocks inserted so far at positional index (only used when index != -1)
    created_block_ids: list[str] = []  # track for rollback
    regular_batch: list[dict] = []

    FLUSH_BATCH_SIZE = 50  # Feishu API limit: too many children per request → 400

    def _eff_index() -> int:
        return -1 if index == -1 else index + _offset

    def _flush_regular():
        nonlocal total, _offset
        if not regular_batch:
            return
        # Split into chunks to stay within API limits
        chunks = [regular_batch[i:i + FLUSH_BATCH_SIZE]
                  for i in range(0, len(regular_batch), FLUSH_BATCH_SIZE)]
        sent = 0
        for chunk in chunks:
            if sent > 0 and BLOCK_CHUNK_DELAY > 0:
                import time
                time.sleep(BLOCK_CHUNK_DELAY)
            try:
                resp = api.post(
                    f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                    {"children": chunk, "index": _eff_index()},
                    params={"document_revision_id": "-1"},
                )
            except Exception as e:
                log.error("Flush %d blocks failed (HTTP): %s | doc=%s",
                          len(chunk), e, doc_id)
                # Remove only the successfully sent items, keep the rest
                del regular_batch[:sent]
                raise  # propagate for rollback
            if resp.get("code") != 0:
                log.error("Flush %d blocks failed (API code %s): %s | doc=%s",
                          len(chunk), resp.get("code"), resp.get("msg"), doc_id)
                # Don't count failed chunk but continue with next
            else:
                # Track created block IDs for potential rollback
                for child in resp.get("data", {}).get("children", []):
                    bid = child.get("block_id")
                    if bid:
                        created_block_ids.append(bid)
                total += len(chunk)
                _offset += len(chunk)
            sent += len(chunk)
        regular_batch.clear()

    try:
        for block in all_blocks:
            if "_table" in block:
                _flush_regular()
                rows = block["_table"]

                # Truncate columns exceeding API limit
                col_count = max(len(r) for r in rows) if rows else 0
                if col_count > TABLE_MAX_COLS:
                    log.warning("Table cols %d > max %d, truncating | doc=%s",
                                col_count, TABLE_MAX_COLS, doc_id)
                    rows = [r[:TABLE_MAX_COLS] for r in rows]

                # Single table with incremental row insertion for large tables
                table_bid = _create_table_in_doc(api, doc_id, rows, index=_eff_index())
                if table_bid:
                    created_block_ids.append(table_bid)
                    total += 1
                    _offset += 1
                else:
                    # Degrade: table failed → write as plain-text pipe rows
                    log.warning("Table degraded to text: %d rows | doc=%s",
                                len(rows), doc_id)
                    for row in rows:
                        line = "| " + " | ".join(row) + " |"
                        regular_batch.append({
                            "block_type": 2,
                            "text": {"elements": [{"text_run": {"content": line}}]},
                        })
            elif "_quote" in block:
                _flush_regular()
                quote_bid = _create_quote_in_doc(api, doc_id, block["_quote"], index=_eff_index())
                if quote_bid:
                    created_block_ids.append(quote_bid)
                    total += 1
                    _offset += 1
                else:
                    # Degrade: quote container failed → plain text with ▎ prefix
                    log.warning("Quote container degraded to text | doc=%s", doc_id)
                    for qblock in block["_quote"]:
                        elements = qblock.get("text", {}).get("elements", [])
                        degraded = [{"text_run": {"content": "▎"}}] + elements
                        regular_batch.append({
                            "block_type": 2,
                            "text": {"elements": degraded},
                        })
            elif "_nested_list" in block:
                _flush_regular()
                count = _create_nested_list_in_doc(api, doc_id, block["_nested_list"], index=_eff_index())
                if count:
                    total += count
                    _offset += count
                else:
                    # Degrade: descendant API failed → flat blocks
                    log.warning("Nested list degraded to flat | doc=%s", doc_id)
                    for item in block["_nested_list"]:
                        bt = 12 if item["type"] == "bullet" else 13
                        key = "bullet" if item["type"] == "bullet" else "ordered"
                        regular_batch.append({
                            "block_type": bt,
                            key: {"elements": item["elements"]},
                        })
            else:
                regular_batch.append(block)

        _flush_regular()
    except Exception:
        # Rollback: best-effort delete blocks created in this call
        if created_block_ids:
            log.error("Append failed mid-way, rolling back %d blocks | doc=%s",
                      len(created_block_ids), doc_id)
            for bid in reversed(created_block_ids):
                try:
                    api.delete(
                        f"/open-apis/docx/v1/documents/{doc_id}/blocks/{bid}",
                        params={"document_revision_id": "-1"},
                    )
                except Exception as e:
                    log.debug("Rollback delete %s failed: %s", bid, e)
        raise

    return total


def parse_dt(s: str) -> int:
    """Parse datetime string to unix timestamp (seconds).

    Accepts: 'YYYY-MM-DD', 'YYYY-MM-DDTHH:MM', 'HH:MM' (today),
             'tomorrow HH:MM', '+2h', '+30m'.
    """
    s = s.strip()
    now = datetime.now(TZ)

    # relative: +2h, +30m
    if s.startswith("+") and len(s) >= 3:
        unit = s[-1]
        try:
            val = int(s[1:-1])
        except ValueError:
            print(f"ERROR: Invalid relative time: {s}", file=sys.stderr)
            sys.exit(1)
        if unit == "h":
            dt = now + timedelta(hours=val)
        elif unit == "m":
            dt = now + timedelta(minutes=val)
        else:
            print(f"ERROR: Unknown time unit '{unit}', use 'h' or 'm'",
                  file=sys.stderr)
            sys.exit(1)
        return int(dt.timestamp())

    # "tomorrow HH:MM"
    if s.lower().startswith("tomorrow"):
        time_part = s.split(None, 1)[1] if " " in s else "09:00"
        h, m = map(int, time_part.split(":"))
        dt = (now + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)
        return int(dt.timestamp())

    # "HH:MM" — today (or next day if past)
    if len(s) <= 5 and ":" in s:
        h, m = map(int, s.split(":"))
        dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if dt < now:
            dt += timedelta(days=1)
        return int(dt.timestamp())

    # ISO formats
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=TZ)
            return int(dt.timestamp())
        except ValueError:
            continue

    print(f"ERROR: Cannot parse datetime: {s}", file=sys.stderr)
    sys.exit(1)
