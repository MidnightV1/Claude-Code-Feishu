#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Comment archive — persistent storage for Feishu document comments.

Comments disappear when the quoted text is updated. This module captures
them before they're lost, storing structured snapshots in SQLite.

Usage:
  # Archive comments for a specific document
  python3 scripts/comment_archive.py archive <doc_id>

  # Query archived comments for a document
  python3 scripts/comment_archive.py query <doc_id>

  # Recent comments across all documents (last N hours)
  python3 scripts/comment_archive.py recent --hours 24

  # Digest for daily review (structured summary)
  python3 scripts/comment_archive.py digest --hours 24

  # Sweep all tracked documents (for cron/daily review)
  python3 scripts/comment_archive.py sweep
"""

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

log = logging.getLogger("hub.comment_archive")

DB_PATH = str(PROJECT_ROOT / "data" / "comment_archive.db")
DOC_CTL = str(PROJECT_ROOT / ".claude/skills/feishu-doc/scripts/doc_ctl.py")

# Documents to track — loaded from memory's doc index
# In practice, the sweep command reads this dynamically
TRACKED_DOCS_FALLBACK = []


class CommentArchive:
    """SQLite-backed comment archive."""

    def __init__(self, db_path: str = DB_PATH):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS comments (
                doc_id       TEXT NOT NULL,
                comment_id   TEXT NOT NULL,
                quote        TEXT,
                context_before TEXT,
                context_after  TEXT,
                thread       TEXT,
                resolved     INTEGER DEFAULT 0,
                first_seen   REAL NOT NULL,
                last_seen    REAL NOT NULL,
                PRIMARY KEY (doc_id, comment_id)
            );
            CREATE INDEX IF NOT EXISTS idx_doc_id
                ON comments(doc_id);
            CREATE INDEX IF NOT EXISTS idx_last_seen
                ON comments(last_seen);
        """)
        self._conn.commit()

    def archive_comments(self, doc_id: str, annotations: list[dict]) -> int:
        """Store/update comments from an analyze result.

        Args:
            doc_id: Document ID.
            annotations: List of annotation dicts from doc_ctl.py analyze.

        Returns:
            Number of new or updated comments.
        """
        now = time.time()
        count = 0

        for ann in annotations:
            comment_id = ann.get("comment_id", "")
            if not comment_id:
                continue

            quote = ann.get("quote", "")
            ctx = ann.get("context", {})
            thread_json = json.dumps(ann.get("thread", []), ensure_ascii=False)
            resolved = 1 if ann.get("resolved", False) else 0

            # Upsert: update last_seen and thread (may have new replies)
            existing = self._conn.execute(
                "SELECT first_seen FROM comments WHERE doc_id=? AND comment_id=?",
                (doc_id, comment_id),
            ).fetchone()

            if existing:
                self._conn.execute(
                    "UPDATE comments SET quote=?, context_before=?, context_after=?, "
                    "thread=?, resolved=?, last_seen=? "
                    "WHERE doc_id=? AND comment_id=?",
                    (quote, ctx.get("before", ""), ctx.get("after", ""),
                     thread_json, resolved, now,
                     doc_id, comment_id),
                )
            else:
                self._conn.execute(
                    "INSERT INTO comments "
                    "(doc_id, comment_id, quote, context_before, context_after, "
                    " thread, resolved, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (doc_id, comment_id, quote,
                     ctx.get("before", ""), ctx.get("after", ""),
                     thread_json, resolved, now, now),
                )
            count += 1

        self._conn.commit()
        return count

    def query_doc(self, doc_id: str) -> list[dict]:
        """Get all archived comments for a document."""
        rows = self._conn.execute(
            "SELECT comment_id, quote, context_before, context_after, "
            "       thread, resolved, first_seen, last_seen "
            "FROM comments WHERE doc_id=? ORDER BY first_seen",
            (doc_id,),
        ).fetchall()
        return [self._row_to_dict(doc_id, r) for r in rows]

    def query_recent(self, hours: float = 24) -> list[dict]:
        """Get comments seen in the last N hours, across all docs."""
        cutoff = time.time() - hours * 3600
        rows = self._conn.execute(
            "SELECT doc_id, comment_id, quote, context_before, context_after, "
            "       thread, resolved, first_seen, last_seen "
            "FROM comments WHERE last_seen >= ? ORDER BY last_seen DESC",
            (cutoff,),
        ).fetchall()
        return [self._row_to_dict(r[0], r[1:]) for r in rows]

    def digest(self, hours: float = 24) -> dict:
        """Generate a structured digest for daily review.

        Returns:
            {
                "period_hours": 24,
                "total_comments": N,
                "by_doc": {doc_id: [comments...]},
                "summary": "..."
            }
        """
        comments = self.query_recent(hours)
        by_doc: dict[str, list] = {}
        for c in comments:
            by_doc.setdefault(c["doc_id"], []).append(c)

        return {
            "period_hours": hours,
            "total_comments": len(comments),
            "docs_with_comments": len(by_doc),
            "by_doc": by_doc,
        }

    def stats(self) -> dict:
        """Archive statistics."""
        total = self._conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        docs = self._conn.execute(
            "SELECT COUNT(DISTINCT doc_id) FROM comments"
        ).fetchone()[0]
        unresolved = self._conn.execute(
            "SELECT COUNT(*) FROM comments WHERE resolved=0"
        ).fetchone()[0]
        return {"total": total, "docs": docs, "unresolved": unresolved}

    def close(self):
        self._conn.close()

    @staticmethod
    def _row_to_dict(doc_id: str, row: tuple) -> dict:
        comment_id, quote, ctx_before, ctx_after, thread_json, resolved, first_seen, last_seen = row
        try:
            thread = json.loads(thread_json) if thread_json else []
        except json.JSONDecodeError:
            thread = []

        return {
            "doc_id": doc_id,
            "comment_id": comment_id,
            "quote": quote,
            "context_before": ctx_before,
            "context_after": ctx_after,
            "thread": thread,
            "resolved": bool(resolved),
            "first_seen": first_seen,
            "last_seen": last_seen,
        }


def run_analyze(doc_id: str) -> list[dict]:
    """Run doc_ctl.py analyze and parse the result."""
    try:
        result = subprocess.run(
            [sys.executable, DOC_CTL, "analyze", doc_id, "--all"],
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode != 0:
            log.warning("analyze failed for %s: %s", doc_id, result.stderr[:200])
            return []

        data = json.loads(result.stdout)
        return data.get("annotations", [])

    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        log.warning("analyze error for %s: %s", doc_id, e)
        return []


def archive_doc(archive: CommentArchive, doc_id: str) -> int:
    """Archive comments for a single document."""
    annotations = run_analyze(doc_id)
    if not annotations:
        return 0
    count = archive.archive_comments(doc_id, annotations)
    return count


def get_tracked_doc_ids() -> list[str]:
    """Get list of tracked document IDs from MEMORY.md doc index."""
    memory_path = PROJECT_ROOT.parent.parent / ".claude/projects/-Users-john-Agent-Space-claude-code-feishu/memory/MEMORY.md"
    if not memory_path.exists():
        return TRACKED_DOCS_FALLBACK

    doc_ids = []
    in_doc_table = False
    try:
        with open(memory_path, "r", encoding="utf-8") as f:
            for line in f:
                if "doc_id" in line and "标题" in line and "位置" in line:
                    in_doc_table = True
                    continue
                if in_doc_table:
                    if line.startswith("|") and "`" in line:
                        # Extract doc_id from | `doc_id` | ... |
                        parts = line.split("|")
                        if len(parts) >= 2:
                            doc_id = parts[1].strip().strip("`").strip()
                            if doc_id and len(doc_id) > 10:
                                doc_ids.append(doc_id)
                    elif not line.strip() or (line.strip() and not line.startswith("|")):
                        in_doc_table = False
    except Exception as e:
        log.warning("Failed to read MEMORY.md: %s", e)

    return doc_ids


def cmd_archive(args):
    archive = CommentArchive()
    count = archive_doc(archive, args.doc_id)
    print(f"Archived {count} comments for {args.doc_id}")
    archive.close()


def cmd_query(args):
    archive = CommentArchive()
    comments = archive.query_doc(args.doc_id)
    if not comments:
        print(f"No archived comments for {args.doc_id}")
    else:
        for c in comments:
            resolved = " [RESOLVED]" if c["resolved"] else ""
            print(f"\n--- Comment {c['comment_id']}{resolved} ---")
            print(f"  Quote: \"{c['quote'][:100]}\"")
            for msg in c["thread"]:
                ts = msg.get("time", "")
                if isinstance(ts, (int, float)):
                    ts = datetime.fromtimestamp(ts, ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")
                text = msg.get("text", "")
                print(f"  [{ts}] {text[:200]}")
    archive.close()


def cmd_recent(args):
    archive = CommentArchive()
    comments = archive.query_recent(args.hours)
    if not comments:
        print(f"No comments in the last {args.hours} hours")
    else:
        current_doc = None
        for c in comments:
            if c["doc_id"] != current_doc:
                current_doc = c["doc_id"]
                print(f"\n=== {current_doc} ===")
            resolved = " [RESOLVED]" if c["resolved"] else ""
            print(f"  [{c['comment_id'][:8]}]{resolved} \"{c['quote'][:60]}\"")
            for msg in c["thread"]:
                print(f"    → {msg.get('text', '')[:150]}")
    print(f"\nTotal: {len(comments)} comments")
    archive.close()


def cmd_digest(args):
    archive = CommentArchive()
    d = archive.digest(args.hours)
    print(json.dumps(d, ensure_ascii=False, indent=2, default=str))
    archive.close()


def cmd_sweep(args):
    """Sweep all tracked documents."""
    doc_ids = get_tracked_doc_ids()
    if not doc_ids:
        print("No tracked documents found")
        return

    archive = CommentArchive()
    total = 0
    for doc_id in doc_ids:
        count = archive_doc(archive, doc_id)
        if count:
            print(f"  {doc_id}: {count} comments archived")
            total += count

    print(f"\nSwept {len(doc_ids)} documents, archived {total} comments")
    stats = archive.stats()
    print(f"Archive stats: {stats['total']} total, {stats['docs']} docs, {stats['unresolved']} unresolved")
    archive.close()


def main():
    parser = argparse.ArgumentParser(description="Comment archive management")
    sub = parser.add_subparsers(dest="command")

    p_archive = sub.add_parser("archive", help="Archive comments for a document")
    p_archive.add_argument("doc_id", help="Document ID")

    p_query = sub.add_parser("query", help="Query archived comments")
    p_query.add_argument("doc_id", help="Document ID")

    p_recent = sub.add_parser("recent", help="Recent comments across all docs")
    p_recent.add_argument("--hours", type=float, default=24)

    p_digest = sub.add_parser("digest", help="Structured digest for daily review")
    p_digest.add_argument("--hours", type=float, default=24)

    p_sweep = sub.add_parser("sweep", help="Sweep all tracked documents")

    args = parser.parse_args()

    if args.command == "archive":
        cmd_archive(args)
    elif args.command == "query":
        cmd_query(args)
    elif args.command == "recent":
        cmd_recent(args)
    elif args.command == "digest":
        cmd_digest(args)
    elif args.command == "sweep":
        cmd_sweep(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
