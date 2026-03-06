# -*- coding: utf-8 -*-
"""Shared utility functions for Feishu integration."""

import re
import sys
from datetime import datetime, timedelta, timezone

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
            elements.append({
                "text_run": {
                    "content": m.group(3),
                    "text_element_style": {
                        "link": {"url": m.group(4)},
                    },
                }
            })
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


def text_to_blocks(text: str) -> list[dict]:
    """Convert markdown-like text to Feishu docx block children.

    Supported syntax:
    - # H1 .. ###### H6 → heading blocks
    - --- → divider
    - - item → text block with bullet prefix (block_type 16 unsupported by create API)
    - 1. item → text block with number prefix (block_type 17 unsupported by create API)
    - > quote → text block with quote prefix (callout is container, loses inline content)
    - **bold**, `code`, [text](url) → inline formatting
    - | table | rows → plain text (table rendering not supported by block API)
    - Plain text → text block (block_type 2)
    """
    text = text.replace("\\n", "\n")
    text = _sanitize_doc_text(text)
    blocks = []

    for line in text.split("\n"):
        line = line.rstrip()
        if not line:
            continue

        # --- divider
        if re.match(r'^-{3,}$', line.strip()):
            blocks.append({"block_type": 22, "divider": {}})
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
            continue

        # Unordered list: - item or * item
        # Note: block_type 16 cannot be created via docx API, render as text with bullet prefix
        ul_match = re.match(r'^[-*]\s+(.+)$', line)
        if ul_match:
            elements = [{"text_run": {"content": "• "}}]
            elements.extend(_parse_inline(ul_match.group(1)))
            blocks.append({
                "block_type": 2,
                "text": {"elements": elements},
            })
            continue

        # Ordered list: 1. item
        # Note: block_type 17 cannot be created via docx API, render as text with number prefix
        ol_match = re.match(r'^(\d+)\.\s+(.+)$', line)
        if ol_match:
            elements = [{"text_run": {"content": f"{ol_match.group(1)}. "}}]
            elements.extend(_parse_inline(ol_match.group(2)))
            blocks.append({
                "block_type": 2,
                "text": {"elements": elements},
            })
            continue

        # Blockquote: > text → callout container (API creates empty child)
        # Note: callout is a container block; elements are ignored by API.
        # Render as text with quote prefix for reliable content display.
        quote_match = re.match(r'^>\s*(.*)$', line)
        if quote_match:
            content = quote_match.group(1) or ""
            elements = [{"text_run": {"content": f"▎{content}" if content else "▎"}}]
            blocks.append({
                "block_type": 2,
                "text": {"elements": elements},
            })
            continue

        # Table separator line (|---|---|): skip
        if re.match(r'^\|[-\s|:]+\|$', line):
            continue

        # Table row (| cell | cell |): render as plain text
        # (Feishu docx table blocks require complex multi-block structure)
        if line.startswith('|') and line.endswith('|'):
            blocks.append({
                "block_type": 2,
                "text": {
                    "elements": [{"text_run": {"content": line}}],
                },
            })
            continue

        # Regular text with inline formatting
        blocks.append({
            "block_type": 2,
            "text": {
                "elements": _parse_inline(line),
            },
        })

    return blocks


def parse_dt(s: str) -> int:
    """Parse datetime string to unix timestamp (seconds).

    Accepts: 'YYYY-MM-DD', 'YYYY-MM-DDTHH:MM', 'HH:MM' (today),
             'tomorrow HH:MM', '+2h', '+30m'.
    """
    s = s.strip()
    now = datetime.now(TZ)

    # relative: +2h, +30m
    if s.startswith("+"):
        unit = s[-1]
        val = int(s[1:-1])
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
