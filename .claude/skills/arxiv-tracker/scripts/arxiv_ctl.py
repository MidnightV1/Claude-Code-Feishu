#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ArXiv Tracker CLI — paper tracking management tool.

Usage:
    python3 arxiv_ctl.py run [--date YYYY-MM-DD]
    python3 arxiv_ctl.py topics
    python3 arxiv_ctl.py history [--days N]
    python3 arxiv_ctl.py evolve [--date YYYY-MM-DD]
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
REPO_ROOT = SKILL_DIR.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPT_DIR))

CONFIG_PATH = SKILL_DIR / "config" / "topics.yaml"
DATA_DIR = REPO_ROOT / "data" / "arxiv"


def cmd_run(args):
    """Execute paper tracking."""
    from arxiv_engine import ArxivEngine

    engine = ArxivEngine(config_path=CONFIG_PATH, data_dir=DATA_DIR)
    result = asyncio.run(engine.run(date_str=args.date))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_topics(args):
    """List current topic configuration."""
    import yaml

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    print("=== ArXiv Tracker Topics ===")
    for i, topic in enumerate(config.get("topics", []), 1):
        priority = topic.get("priority", "-")
        print(f"\n{i}. [{priority}] {topic['name']} ({topic.get('name_en', '')})")
        print(f"   Categories: {', '.join(topic.get('categories', []))}")
        print(f"   Keywords: {', '.join(topic.get('keywords', []))}")
        if topic.get("author_orgs"):
            print(f"   Author/Orgs: {', '.join(topic['author_orgs'])}")
        print(f"   Description: {topic.get('description', '')}")

    settings = config.get("settings", {})
    print(f"\n--- Settings ---")
    print(f"Score threshold: {settings.get('score_threshold', 3.5)}")
    print(f"Batch size: {settings.get('batch_size', 8)}")
    print(f"Model: {settings.get('model', '3.1-Pro')}")


def cmd_history(args):
    """View tracking history."""
    import sqlite3
    from datetime import datetime, timedelta

    db_path = DATA_DIR / "history.db"
    if not db_path.exists():
        print("No history found.")
        return

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    days = args.days or 7
    cutoff = (datetime.now() - timedelta(days=days)).timestamp()

    cursor.execute(
        "SELECT digest_date, COUNT(*) FROM papers WHERE created_at > ? "
        "GROUP BY digest_date ORDER BY digest_date DESC",
        (cutoff,),
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print(f"No papers tracked in the last {days} days.")
        return

    print(f"=== ArXiv History (last {days} days) ===")
    for date, count in rows:
        print(f"  {date}: {count} papers tracked")


def cmd_evolve(args):
    """Trigger keyword evolution analysis."""
    from arxiv_engine import ArxivEngine

    engine = ArxivEngine(config_path=CONFIG_PATH, data_dir=DATA_DIR)

    feedback_path = DATA_DIR / "keyword_feedback.json"
    if not feedback_path.exists():
        print("No keyword feedback data yet. Run 'run' first.")
        return

    feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
    if not feedback:
        print("Keyword feedback is empty. Run 'run' first.")
        return

    latest_date = max(feedback.keys())
    latest = feedback[latest_date]

    asyncio.run(
        engine._evolve_keywords(
            date_str=args.date or latest_date,
            keyword_hits=latest.get("keyword_hits", {}),
            missed=[],
            low_score=[],
        )
    )
    print(f"Evolution suggestions saved. Check data/arxiv/evolution_suggestions/")


def main():
    parser = argparse.ArgumentParser(description="ArXiv Tracker CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run paper tracking")
    p_run.add_argument("--date", help="Target date (YYYY-MM-DD), default: yesterday")

    sub.add_parser("topics", help="List topic configuration")

    p_history = sub.add_parser("history", help="View tracking history")
    p_history.add_argument("--days", type=int, default=7, help="Days to look back")

    p_evolve = sub.add_parser("evolve", help="Trigger keyword evolution")
    p_evolve.add_argument("--date", help="Date for evolution analysis")

    args = parser.parse_args()

    dispatch = {
        "run": cmd_run,
        "topics": cmd_topics,
        "history": cmd_history,
        "evolve": cmd_evolve,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
