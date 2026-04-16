#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill auto_scores for existing exploration_log entries.

Usage:
  python3 scripts/backfill_scores.py              # Dry-run (show scores)
  python3 scripts/backfill_scores.py --apply       # Write scores to log file
"""

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.infra.exploration_scoring import rule_score

LOG_PATH = PROJECT_ROOT / "data" / "exploration_log.jsonl"


def main():
    apply = "--apply" in sys.argv

    if not LOG_PATH.exists():
        print("No exploration_log.jsonl found")
        return

    lines = []
    updated = 0
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                lines.append(line)
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                lines.append(line)
                continue

            # Skip if already scored
            if entry.get("auto_scores"):
                lines.append(line)
                scores = entry["auto_scores"]
                print(f"  [SKIP] {entry.get('title', '?')[:50]} → "
                      f"{scores.get('tier', '?')} ({scores.get('weighted', 0):.2f})")
                continue

            scores = rule_score(
                entry.get("summary", ""),
                duration_seconds=entry.get("duration_seconds", 0),
                messages_used=entry.get("messages_used", 1),
            )
            scores["method"] = "rule-backfill"
            entry["auto_scores"] = scores

            # Add duration_seconds if missing
            if "duration_seconds" not in entry:
                entry["duration_seconds"] = 0

            lines.append(json.dumps(entry, ensure_ascii=False) + "\n")
            updated += 1

            tier = scores["tier"]
            w = scores["weighted"]
            print(f"  [{tier:4s}] {w:.2f}  n={scores['novelty']:.1f} d={scores['depth']:.1f} "
                  f"a={scores['actionability']:.1f} e={scores['efficiency']:.1f}  "
                  f"| {entry.get('title', '?')[:55]}")

    print(f"\n{'Updated' if apply else 'Would update'}: {updated} entries")

    # Tier distribution
    all_entries = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            e = json.loads(stripped)
            if e.get("auto_scores"):
                all_entries.append(e)
        except json.JSONDecodeError:
            pass

    if all_entries:
        tiers = {"HIGH": 0, "MED": 0, "LOW": 0}
        for e in all_entries:
            t = e["auto_scores"].get("tier", "?")
            tiers[t] = tiers.get(t, 0) + 1
        total = len(all_entries)
        print(f"\nDistribution: HIGH={tiers['HIGH']} ({tiers['HIGH']/total:.0%}) "
              f"MED={tiers['MED']} ({tiers['MED']/total:.0%}) "
              f"LOW={tiers['LOW']} ({tiers['LOW']/total:.0%})")

    if apply:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines)
        print("✓ Scores written to exploration_log.jsonl")
    else:
        print("\nRun with --apply to write scores")


if __name__ == "__main__":
    main()
