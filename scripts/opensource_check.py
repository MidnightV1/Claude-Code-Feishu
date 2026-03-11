#!/usr/bin/env python3
"""Opensource demasking check — scans tracked files for sensitive patterns.

Usage:
    python3 scripts/opensource_check.py              # check all tracked files
    python3 scripts/opensource_check.py --staged      # check staged files only (for pre-commit)

Exit code 0 = clean, 1 = sensitive content found.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

# ── Sensitive patterns ──────────────────────────────────────────

PATTERNS = [
    # Project internal name
    (r"nas-claude-hub", "internal project name (use claude-code-feishu)"),
    # Personal paths
    (r"/Users/john\b", "personal home path"),
    (r"Agent.Space", "personal workspace path"),
    (r"Agent_Space", "personal workspace path"),
    # Network
    (r"192\.168\.\d+\.\d+", "private IP address"),
    (r"mac-mini\b", "internal hostname"),
    # Feishu user IDs (open_id format)
    (r"ou_[a-f0-9]{32}", "Feishu open_id"),
    # Hardware-specific
    (r"QNAP", "specific hardware reference"),
]

# Files to always skip (binary, config templates with intentional examples, etc.)
SKIP_FILES = {
    "scripts/opensource_check.py",  # this file itself
    ".gitignore",
}

# Directories to skip entirely
SKIP_DIRS = {
    ".git",
    "data",
    "__pycache__",
    ".claude/projects",
}


def get_tracked_files(staged_only: bool = False) -> list[str]:
    """Get list of files to check."""
    if staged_only:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            capture_output=True, text=True
        )
    else:
        result = subprocess.run(
            ["git", "ls-files"], capture_output=True, text=True
        )
    return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]


def should_skip(filepath: str) -> bool:
    """Check if file should be skipped."""
    if filepath in SKIP_FILES:
        return True
    for skip_dir in SKIP_DIRS:
        if filepath.startswith(skip_dir + "/"):
            return True
    return False


def check_file(filepath: str, compiled_patterns: list) -> list[tuple[int, str, str]]:
    """Check a file for sensitive patterns. Returns list of (line_num, pattern_desc, line_text)."""
    hits = []
    try:
        text = Path(filepath).read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeDecodeError):
        return hits

    for i, line in enumerate(text.split("\n"), 1):
        for regex, desc in compiled_patterns:
            if regex.search(line):
                hits.append((i, desc, line.strip()[:120]))
                break  # one hit per line is enough
    return hits


def main():
    parser = argparse.ArgumentParser(description="Check for sensitive content in opensource files")
    parser.add_argument("--staged", action="store_true", help="Check staged files only")
    args = parser.parse_args()

    compiled = [(re.compile(p, re.IGNORECASE), d) for p, d in PATTERNS]
    files = get_tracked_files(staged_only=args.staged)

    total_hits = 0
    files_with_hits = 0

    for filepath in files:
        if should_skip(filepath):
            continue
        hits = check_file(filepath, compiled)
        if hits:
            files_with_hits += 1
            total_hits += len(hits)
            print(f"\n{'='*60}")
            print(f"  {filepath}")
            print(f"{'='*60}")
            for line_num, desc, line_text in hits:
                print(f"  L{line_num}: [{desc}]")
                print(f"        {line_text}")

    print(f"\n{'─'*40}")
    if total_hits:
        print(f"FAILED: {total_hits} sensitive pattern(s) in {files_with_hits} file(s)")
        sys.exit(1)
    else:
        scope = "staged files" if args.staged else "all tracked files"
        print(f"PASSED: No sensitive patterns found ({len(files)} {scope} checked)")
        sys.exit(0)


if __name__ == "__main__":
    main()
