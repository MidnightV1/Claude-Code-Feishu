#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daily review data collector — gathers all signals for Opus analysis.

This script collects structured data from all sources. The actual analysis
is done by Opus (via CC session), not by this script.

Usage:
  python3 scripts/daily_review.py              # Collect all data, output JSON
  python3 scripts/daily_review.py --section conversations  # Just conversations
  python3 scripts/daily_review.py --section comments       # Just doc comments
  python3 scripts/daily_review.py --section tasks          # Just feishu tasks
  python3 scripts/daily_review.py --section exploration    # Just exploration log
  python3 scripts/daily_review.py --section git            # Just git changes
  python3 scripts/daily_review.py --section system         # Just system health
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

TZ = ZoneInfo("Asia/Shanghai")
HOURS = 24


def collect_conversations(hours: float = HOURS) -> dict:
    """Extract conversation history from sessions.db."""
    db_path = PROJECT_ROOT / "data" / "sessions.db"
    if not db_path.exists():
        return {"error": "sessions.db not found", "rounds": []}

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT session_key, history FROM sessions WHERE history IS NOT NULL"
    ).fetchall()
    conn.close()

    cutoff_ts = datetime.now(TZ) - timedelta(hours=hours)
    cutoff_str = cutoff_ts.strftime("%Y-%m-%d %H:%M")

    all_rounds = []
    for session_key, hist_json in rows:
        try:
            history = json.loads(hist_json)
        except (json.JSONDecodeError, TypeError):
            continue

        for entry in history:
            ts = entry.get("ts", "")
            # Filter by timestamp (string comparison works for ISO-ish format)
            if ts and ts >= cutoff_str:
                all_rounds.append({
                    "session": session_key,
                    "role": entry["role"],
                    "text": entry["text"],
                    "ts": ts,
                })

    all_rounds.sort(key=lambda r: r["ts"])
    return {
        "period_hours": hours,
        "total_messages": len(all_rounds),
        "rounds": all_rounds,
    }


def collect_comments(hours: float = HOURS) -> dict:
    """Get recent document comments from the archive."""
    try:
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "comment_archive.py"),
             "digest", "--hours", str(hours)],
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception as e:
        return {"error": str(e)}
    return {"error": "comment_archive.py failed"}


def collect_tasks() -> dict:
    """Get current feishu task status."""
    try:
        result = subprocess.run(
            [sys.executable,
             str(PROJECT_ROOT / ".claude/skills/feishu-task/scripts/task_ctl.py"),
             "list"],
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode == 0:
            return {"raw": result.stdout.strip(), "status": "ok"}
    except Exception as e:
        return {"error": str(e)}
    return {"error": "task_ctl.py failed"}


def collect_exploration(hours: float = HOURS) -> dict:
    """Get exploration queue and recent log entries."""
    queue_data = {}
    log_data = []

    # Queue
    queue_path = PROJECT_ROOT / "data" / "exploration_queue.json"
    if queue_path.exists():
        try:
            with open(queue_path, "r") as f:
                queue_data = json.load(f)
        except json.JSONDecodeError:
            queue_data = {"error": "corrupt"}

    # Log
    log_path = PROJECT_ROOT / "data" / "exploration_log.jsonl"
    cutoff = time.time() - hours * 3600
    if log_path.exists():
        with open(log_path, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if entry.get("timestamp", 0) >= cutoff:
                        log_data.append(entry)
                except (json.JSONDecodeError, KeyError):
                    continue

    # Autonomy log
    autonomy_log = []
    autonomy_path = PROJECT_ROOT / "data" / "autonomy_log.jsonl"
    if autonomy_path.exists():
        with open(autonomy_path, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if entry.get("ts", 0) >= cutoff:
                        autonomy_log.append(entry)
                except (json.JSONDecodeError, KeyError):
                    continue

    return {
        "queue": queue_data,
        "recent_explorations": log_data,
        "autonomy_actions": autonomy_log,
    }


def collect_git(hours: float = HOURS) -> dict:
    """Get git changes in the last N hours."""
    since = (datetime.now(TZ) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
    try:
        log_result = subprocess.run(
            ["git", "log", f"--since={since}", "--oneline", "--all"],
            capture_output=True, text=True, timeout=10,
            cwd=str(PROJECT_ROOT),
        )
        diff_result = subprocess.run(
            ["git", "diff", "--stat", "HEAD~5..HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=str(PROJECT_ROOT),
        )
        return {
            "recent_commits": log_result.stdout.strip(),
            "recent_diff_stat": diff_result.stdout.strip(),
        }
    except Exception as e:
        return {"error": str(e)}


def collect_system() -> dict:
    """Check system health."""
    data = {}

    # Hub process
    try:
        ps = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=5,
        )
        hub_procs = [l for l in ps.stdout.splitlines() if "agent.main" in l or "claude" in l.lower()]
        data["processes"] = hub_procs[:10]
    except Exception:
        data["processes"] = []

    # Memory
    try:
        import psutil
        vm = psutil.virtual_memory()
        data["memory"] = {
            "total_gb": round(vm.total / 1e9, 1),
            "available_gb": round(vm.available / 1e9, 1),
            "percent_used": vm.percent,
        }
    except ImportError:
        data["memory"] = {"note": "psutil not installed"}

    # Cron jobs status
    jobs_path = PROJECT_ROOT / "data" / "jobs.json"
    if jobs_path.exists():
        try:
            with open(jobs_path) as f:
                jobs = json.load(f)
            data["cron_jobs"] = [
                {"name": j.get("name"), "enabled": j.get("enabled"),
                 "last_status": j.get("state", {}).get("last_status"),
                 "last_error": j.get("state", {}).get("last_error")}
                for j in jobs.get("jobs", [])
            ]
        except Exception:
            data["cron_jobs"] = []

    # Recent errors in log
    log_file = PROJECT_ROOT / "data" / "hub.log"
    if log_file.exists():
        try:
            result = subprocess.run(
                ["tail", "-200", str(log_file)],
                capture_output=True, text=True, timeout=5,
            )
            errors = [l for l in result.stdout.splitlines()
                       if "ERROR" in l or "CRITICAL" in l]
            data["recent_errors"] = errors[-10:]  # Last 10 errors
        except Exception:
            data["recent_errors"] = []

    return data


COLLECTORS = {
    "conversations": collect_conversations,
    "comments": collect_comments,
    "tasks": collect_tasks,
    "exploration": collect_exploration,
    "git": collect_git,
    "system": collect_system,
}


def main():
    parser = argparse.ArgumentParser(description="Daily review data collector")
    parser.add_argument("--section", choices=list(COLLECTORS.keys()),
                        help="Collect only a specific section")
    parser.add_argument("--hours", type=float, default=HOURS,
                        help="Look-back period in hours")
    args = parser.parse_args()

    if args.section:
        fn = COLLECTORS[args.section]
        if args.section in ("conversations", "comments", "exploration", "git"):
            data = fn(args.hours)
        else:
            data = fn()
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        # Collect all sections
        review = {
            "generated_at": datetime.now(TZ).isoformat(),
            "period_hours": args.hours,
        }
        for name, fn in COLLECTORS.items():
            try:
                if name in ("conversations", "comments", "exploration", "git"):
                    review[name] = fn(args.hours)
                else:
                    review[name] = fn()
            except Exception as e:
                review[name] = {"error": str(e)}

        print(json.dumps(review, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
