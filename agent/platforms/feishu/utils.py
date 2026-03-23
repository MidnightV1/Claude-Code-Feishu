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


def _parse_inline(text: str) -> list[dict]:
    """Parse inline markdown (bold, inline code, links) into Feishu text_run elements."""
    elements = []
    # Pattern: **bold**, `code`, [text](url)
    pattern = re.compile(
        r'\*\*(.+?)\*\*'        # bold
        r'|`([^`]+)`'           # inline code
        r'|\[([^\]]+)\]\(([^)]+)\)'  # link
    )
    pos = 0
    for m in pattern.finditer(text):
        # Add plain text before match
        if m.start() > pos:
            plain = text[pos:m.start()]
            if plain:
                elements.append({"text_run": {"content": plain}})

        if m.group(1) is not None:  # bold
            elements.append({
                "text_run": {
                    "content": m.group(1),
                    "text_element_style": {"bold": True},
                }
            })
        elif m.group(2) is not None:  # inline code
            elements.append({
                "text_run": {
                    "content": m.group(2),
                    "text_element_style": {"inline_code": True},
                }
            })
        elif m.group(3) is not None:  # link
            url = m.group(4)
            if url.startswith("http://") or url.startswith("https://"):
                elements.append({
                    "text_run": {
                        "content": m.group(3),
                        "text_element_style": {
                            "link": {"url": url},
                        },
                    }
                })
            else:
                # Relative path — Feishu API rejects non-http URLs
                elements.append({"text_run": {"content": m.group(3)}})
        pos = m.end()

    # Remaining text after last match
    if pos < len(text):
        remaining = text[pos:]
        if remaining:
            elements.append({"text_run": {"content": remaining}})

    # No matches at all — return single plain element
    if not elements:
        elements.append({"text_run": {"content": text}})

    return elements


TABLE_MAX_ROWS = 100   # Feishu docx API: row_size ≤ 100
TABLE_MAX_COLS = 9     # Feishu docx API: column_size ≤ 9
TABLE_MAX_CELLS = 200  # Feishu docx API: row_size × column_size ≤ 200


def _split_table_rows(rows: list[list[str]]) -> list[list[list[str]]]:
    """Split a table into chunks respecting all Feishu table limits.

    Three constraints apply simultaneously:
      - row_size ≤ TABLE_MAX_ROWS (100)
      - column_size ≤ TABLE_MAX_COLS (9)
      - row_size × column_size ≤ TABLE_MAX_CELLS (200)

    E.g. 9-col table: 200//9=22 rows max → data rows per chunk = 21
         2-col table: 200//2=100 rows max, but capped at 100 → data rows = 99
    Header is repeated in each chunk.
    """
    col_count = max(len(rows[0]), 1) if rows else 1
    # Max total rows (incl. header) per chunk, driven by cell-count limit
    max_total_rows = max(1, TABLE_MAX_CELLS // col_count)
    # Cap by absolute row limit, then subtract 1 slot for header
    chunk_size = min(max_total_rows, TABLE_MAX_ROWS) - 1

    if len(rows) <= chunk_size + 1:
        return [rows]
    header = rows[0]
    data = rows[1:]
    return [
        [header] + data[i:i + chunk_size]
        for i in range(0, len(data), chunk_size)
    ]


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


_CODE_LANG_MAP = {
    "": 1, "text": 1, "plaintext": 1,
    "bash": 7, "sh": 7, "shell": 61, "zsh": 7,
    "c": 10, "cpp": 9, "c++": 9, "csharp": 8, "c#": 8,
    "css": 12, "dart": 15, "dockerfile": 18,
    "go": 23, "groovy": 24, "html": 25, "http": 27,
    "java": 30, "javascript": 31, "js": 31,
    "json": 29, "kotlin": 33, "latex": 34, "lua": 37,
    "makefile": 39, "markdown": 40, "md": 40,
    "nginx": 41, "objc": 42, "objective-c": 42,
    "php": 44, "perl": 45, "powershell": 47,
    "python": 50, "py": 50, "r": 51, "ruby": 53, "rust": 54,
    "scss": 56, "sql": 57, "scala": 58, "swift": 62,
    "typescript": 64, "ts": 64, "xml": 67, "yaml": 68, "yml": 68,
}


def text_to_blocks(text: str) -> list[dict]:
    """Convert markdown-like text to Feishu docx block children.

    Supported syntax:
    - # H1 .. ###### H6 → heading blocks
    - --- → divider
    - ```lang ... ``` → code blocks (block_type 14)
    - - item → bullet list block (block_type 12)
    - 1. item → ordered list block (block_type 13)
    - > quote → text block with quote prefix (callout is container, loses inline content)
    - **bold**, `code`, [text](url) → inline formatting
    - | table | rows → native table blocks (via descendant API in append_markdown_to_doc)
    - Plain text → text block (block_type 2)

    Note: Tables are returned as special {"_table": payload} entries.
    Use append_markdown_to_doc() for proper table rendering, or filter them out
    for the simple children API.
    """
    text = text.replace("\\n", "\n")
    text = _sanitize_doc_text(text)
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

        # Unordered list: - item or * item → native bullet block (block_type 12)
        ul_match = re.match(r'^[-*]\s+(.+)$', line)
        if ul_match:
            blocks.append({
                "block_type": 12,
                "bullet": {
                    "elements": _parse_inline(ul_match.group(1)),
                },
            })
            i += 1
            continue

        # Ordered list: 1. item → native ordered block (block_type 13)
        ol_match = re.match(r'^(\d+)\.\s+(.+)$', line)
        if ol_match:
            blocks.append({
                "block_type": 13,
                "ordered": {
                    "elements": _parse_inline(ol_match.group(2)),
                },
            })
            i += 1
            continue

        # Blockquote: > text
        quote_match = re.match(r'^>\s*(.*)$', line)
        if quote_match:
            content = quote_match.group(1) or ""
            elements = [{"text_run": {"content": f"▎{content}" if content else "▎"}}]
            blocks.append({
                "block_type": 2,
                "text": {"elements": elements},
            })
            i += 1
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


def _create_table_in_doc(api, doc_id: str, rows: list[list[str]]) -> str | None:
    """Create a native table in a Feishu document.

    Two-step process:
    1. Create empty table → get cell block_ids
    2. Fill each cell with content via PATCH

    Args:
        api: FeishuAPI instance
        doc_id: Document ID
        rows: 2D list of cell texts (first row = header)

    Returns table block_id on success, None on failure.
    """
    row_count = len(rows)
    col_count = len(rows[0]) if rows else 0
    if row_count == 0 or col_count == 0:
        return None

    # Step 1: Create empty table
    try:
        resp = api.post(
            f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
            {
                "children": [{
                    "block_type": 31,
                    "table": {
                        "property": {
                            "row_size": row_count,
                            "column_size": col_count,
                            "header_row": True,
                        }
                    }
                }],
                "index": -1,
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

    # Extract cell block_ids (ordered left→right, top→bottom)
    table_block = resp["data"]["children"][0]
    cell_ids = table_block.get("table", {}).get("cells", [])
    table_bid = table_block.get("block_id")
    if len(cell_ids) != row_count * col_count:
        log.warning("Table cell count mismatch: expected %d, got %d",
                     row_count * col_count, len(cell_ids))
        return table_bid  # table created but can't fill

    # Step 2: Fill cells
    idx = 0
    for ri, row in enumerate(rows):
        for ci, cell_text in enumerate(row):
            cell_id = cell_ids[idx]
            idx += 1

            # Get or create the text block inside the cell
            try:
                child_resp = api.get(
                    f"/open-apis/docx/v1/documents/{doc_id}/blocks/{cell_id}/children",
                    params={"document_revision_id": "-1"},
                )
                items = child_resp.get("data", {}).get("items", [])
                if items:
                    text_block_id = items[0]["block_id"]
                else:
                    # No auto-generated text block — create one
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

                # Build elements (header row = bold)
                if ri == 0:
                    elements = [{"text_run": {
                        "content": cell_text,
                        "text_element_style": {"bold": True},
                    }}]
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
                log.warning("Fill cell [%d,%d] failed: %s", ri, ci, e)

    return table_bid


def append_markdown_to_doc(api, doc_id: str, markdown: str) -> int:
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
    created_block_ids: list[str] = []  # track for rollback
    regular_batch: list[dict] = []

    FLUSH_BATCH_SIZE = 50  # Feishu API limit: too many children per request → 400

    def _flush_regular():
        nonlocal total
        if not regular_batch:
            return
        # Split into chunks to stay within API limits
        chunks = [regular_batch[i:i + FLUSH_BATCH_SIZE]
                  for i in range(0, len(regular_batch), FLUSH_BATCH_SIZE)]
        sent = 0
        for chunk in chunks:
            try:
                resp = api.post(
                    f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                    {"children": chunk, "index": -1},
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

                # Split rows exceeding API limit (header repeated in each chunk)
                chunks = _split_table_rows(rows)
                if len(chunks) > 1:
                    log.info("Table %d rows split into %d chunks | doc=%s",
                             len(rows), len(chunks), doc_id)

                for chunk in chunks:
                    table_bid = _create_table_in_doc(api, doc_id, chunk)
                    if table_bid:
                        created_block_ids.append(table_bid)
                        total += 1
                    else:
                        # Degrade: table failed → write as plain-text pipe rows
                        log.warning("Table degraded to text: %d rows | doc=%s",
                                    len(chunk), doc_id)
                        for row in chunk:
                            line = "| " + " | ".join(row) + " |"
                            regular_batch.append({
                                "block_type": 2,
                                "text": {"elements": [{"text_run": {"content": line}}]},
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
