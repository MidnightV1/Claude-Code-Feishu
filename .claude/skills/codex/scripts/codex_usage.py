#!/usr/bin/env python3
"""Codex usage monitor — track local session activity and estimate quota consumption.

Parses ~/.codex/sessions/ JSONL files to report:
- Session count and duration
- Model usage breakdown
- 5-hour rolling window activity
- Weekly activity trend
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SESSIONS_DIR = Path.home() / ".codex" / "sessions"


def parse_session(path: Path) -> dict | None:
    """Extract metadata from a session JSONL file."""
    try:
        with open(path) as f:
            first_line = f.readline()
            if not first_line.strip():
                return None
            meta = json.loads(first_line)
            if meta.get("type") != "session_meta":
                return None

            payload = meta.get("payload", {})
            start_ts = meta.get("timestamp", "")

            # Count events and find last timestamp
            event_count = 0
            last_ts = start_ts
            model = payload.get("model_provider", "unknown")
            source_raw = payload.get("source", "unknown")
            source_val = source_raw.get("type", "unknown") if isinstance(source_raw, dict) else str(source_raw)

            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                    event_count += 1
                    if "timestamp" in evt:
                        last_ts = evt["timestamp"]
                except json.JSONDecodeError:
                    continue

            # Parse timestamps
            start_dt = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            duration_s = (end_dt - start_dt).total_seconds()

            return {
                "id": payload.get("id", ""),
                "start": start_dt,
                "end": end_dt,
                "duration_s": max(duration_s, 0),
                "model": model,
                "source": source_val,
                "cwd": payload.get("cwd", ""),
                "cli_version": payload.get("cli_version", ""),
                "events": event_count,
                "path": str(path),
            }
    except Exception as e:
        return None


def collect_sessions(days: int = 7) -> list[dict]:
    """Collect all sessions from the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    sessions = []

    if not SESSIONS_DIR.exists():
        return sessions

    for jsonl in sorted(SESSIONS_DIR.rglob("*.jsonl")):
        info = parse_session(jsonl)
        if info and info["start"] >= cutoff:
            sessions.append(info)

    return sorted(sessions, key=lambda s: s["start"])


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds/60:.1f}m"
    else:
        return f"{seconds/3600:.1f}h"


def print_summary(sessions: list[dict], detail: bool = False):
    now = datetime.now(timezone.utc)

    # Overall stats
    total = len(sessions)
    total_duration = sum(s["duration_s"] for s in sessions)
    total_events = sum(s["events"] for s in sessions)

    print("=" * 50)
    print("  Codex Usage Summary (last 7 days)")
    print("=" * 50)
    print(f"  Sessions:       {total}")
    print(f"  Total duration: {format_duration(total_duration)}")
    print(f"  Total events:   {total_events}")
    print()

    # 5-hour rolling window
    window_5h = now - timedelta(hours=5)
    recent = [s for s in sessions if s["start"] >= window_5h]
    recent_duration = sum(s["duration_s"] for s in recent)
    print("  5-hour rolling window:")
    print(f"    Sessions: {len(recent)}")
    print(f"    Duration: {format_duration(recent_duration)}")
    print()

    # Daily breakdown
    print("  Daily breakdown:")
    by_day = {}
    for s in sessions:
        day = s["start"].strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(s)

    for day in sorted(by_day.keys(), reverse=True):
        day_sessions = by_day[day]
        day_duration = sum(s["duration_s"] for s in day_sessions)
        print(f"    {day}: {len(day_sessions)} sessions, {format_duration(day_duration)}")

    # Model breakdown
    by_model = {}
    for s in sessions:
        by_model.setdefault(s["model"], []).append(s)

    if by_model:
        print()
        print("  By provider:")
        for model, model_sessions in sorted(by_model.items()):
            print(f"    {model}: {len(model_sessions)} sessions")

    # Source breakdown
    by_source = {}
    for s in sessions:
        by_source.setdefault(s["source"], []).append(s)

    if by_source:
        print()
        print("  By source:")
        for source, source_sessions in sorted(by_source.items()):
            print(f"    {source}: {len(source_sessions)} sessions")

    if detail and sessions:
        print()
        print("-" * 50)
        print("  Session details:")
        print("-" * 50)
        for s in sessions:
            start_local = s["start"].astimezone().strftime("%m-%d %H:%M")
            print(f"  [{start_local}] {s['source']:8s} "
                  f"duration={format_duration(s['duration_s']):>6s} "
                  f"events={s['events']:3d} "
                  f"id={s['id'][:12]}")

    print()
    print("  Note: Token/cost data not available in local logs.")
    print("  Quota tied to ChatGPT subscription (5h rolling + weekly cap).")
    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(description="Codex usage monitor")
    parser.add_argument("--detail", action="store_true", help="Show per-session details")
    parser.add_argument("--days", type=int, default=7, help="Days to look back (default: 7)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    sessions = collect_sessions(days=args.days)

    if args.json:
        output = []
        for s in sessions:
            output.append({
                "id": s["id"],
                "start": s["start"].isoformat(),
                "end": s["end"].isoformat(),
                "duration_s": s["duration_s"],
                "model": s["model"],
                "source": s["source"],
                "events": s["events"],
            })
        print(json.dumps(output, indent=2))
    else:
        print_summary(sessions, detail=args.detail)


if __name__ == "__main__":
    main()
