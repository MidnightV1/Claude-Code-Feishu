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


def text_to_blocks(text: str) -> list[dict]:
    """Convert plain text (with markdown-like headings) to Feishu docx block children.

    Handles escaped newlines from shell arguments.
    """
    text = text.replace("\\n", "\n")
    text = _sanitize_doc_text(text)
    blocks = []
    for line in text.split("\n"):
        line = line.rstrip()
        if not line:
            continue

        heading_match = re.match(r"^(#{1,9})\s+(.+)$", line)
        if heading_match:
            level = len(heading_match.group(1))
            # block_type: 3=H1, 4=H2, ..., 11=H9
            blocks.append({
                "block_type": 2 + level,
                "heading" + str(level): {
                    "elements": [{"text_run": {"content": heading_match.group(2)}}]
                },
            })
            continue

        blocks.append({
            "block_type": 2,
            "text": {
                "elements": [{"text_run": {"content": line}}],
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
