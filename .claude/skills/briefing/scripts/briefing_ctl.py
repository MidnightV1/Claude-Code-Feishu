#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Briefing CLI for Claude Code skill invocation.

Thin wrapper around scripts/briefing_run.py — provides formatted output
suitable for CC to relay to users.
"""

import json
import subprocess
import sys
from pathlib import Path

HUB_DIR = Path(__file__).resolve().parents[4]  # .claude/skills/briefing/scripts/ → hub root
RUNNER = HUB_DIR / "scripts" / "briefing_run.py"
PYTHON = Path.home() / "python313/python/bin/python3"
CONFIG = HUB_DIR / "config.yaml"
DOMAINS_DIR = Path.home() / "briefing" / "domains"


def run_script(args: list[str], timeout: int = 900) -> str:
    """Run briefing_run.py and return stdout."""
    cmd = [str(PYTHON), str(RUNNER)] + args + ["--config", str(CONFIG)]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=str(HUB_DIR)
    )
    if result.returncode != 0 and result.stderr:
        return json.dumps({"status": "error", "error": result.stderr[-500:]})
    return result.stdout.strip()


def cmd_status(domain: str):
    raw = run_script(["status", "--domain", domain])
    try:
        s = json.loads(raw)
    except json.JSONDecodeError:
        print(raw)
        return
    if s.get("status") == "no data":
        print(f"No briefing run data for domain '{domain}'.")
        return
    print(f"Domain:  {s.get('domain', '?')}")
    print(f"Date:    {s.get('date', '?')}")
    print(f"Status:  {s.get('status', '?')}")
    print(f"Elapsed: {s.get('elapsed_s', '?')}s")
    print(f"Model:   {s.get('model', '?')}")
    print(f"Review:  {s.get('review_model', 'off')}")
    print(f"Cost:    ${s.get('cost_usd', 0):.4f}")
    if s.get("errors"):
        print(f"Errors:  {', '.join(s['errors'])}")


def cmd_domains():
    raw = run_script(["domains"])
    try:
        domains = json.loads(raw)
    except json.JSONDecodeError:
        print(raw)
        return
    if not domains:
        print("No domains configured.")
        return
    for d in domains:
        evo = " [evolution]" if d.get("evolution") else ""
        print(f"  {d['name']:20s} {d['display_name']}  {d.get('schedule', '')}{evo}")


def cmd_run(domain: str, date: str = None):
    args = ["run", "--domain", domain]
    if date:
        args.extend(["--date", date])
    print(f"Running briefing pipeline for {domain}...")
    raw = run_script(args, timeout=900)
    try:
        s = json.loads(raw)
        print(f"Result: {s.get('status')} | {s.get('elapsed_s', '?')}s | ${s.get('cost_usd', 0):.4f}")
    except json.JSONDecodeError:
        print(raw)


def cmd_evolve(domain: str, date: str = None):
    args = ["evolve", "--domain", domain]
    if date:
        args.extend(["--date", date])
    print(f"Running keyword evolution for {domain}...")
    raw = run_script(args, timeout=300)
    try:
        s = json.loads(raw)
        print(f"Result: {s.get('status')}")
    except json.JSONDecodeError:
        print(raw)


def cmd_history(domain: str, days: int = 7):
    meta_path = DOMAINS_DIR / domain / "data" / "keywords_meta.json"
    if not meta_path.exists():
        print(f"No evolution history for '{domain}'.")
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    history = meta.get("evolution_history", [])[-days:]
    if not history:
        print("No evolution history entries.")
        return
    for entry in history:
        added = len(entry.get("added", []))
        promoted = len(entry.get("promoted", []))
        deprecated = len(entry.get("deprecated", []))
        print(f"  {entry['date']}  +{added} added  {promoted} promoted  {deprecated} deprecated  ${entry.get('cost_usd', 0):.4f}")
        if entry.get("observations"):
            print(f"    → {entry['observations'][:120]}")


def main():
    if len(sys.argv) < 2:
        print("Usage: briefing_ctl.py <command> [options]")
        print("Commands: status, domains, run, evolve, history")
        sys.exit(1)

    cmd = sys.argv[1]
    domain = "ai-drama"
    date = None
    days = 7

    # Parse flags
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] in ("--domain", "-d") and i + 1 < len(args):
            domain = args[i + 1]
            i += 2
        elif args[i] == "--date" and i + 1 < len(args):
            date = args[i + 1]
            i += 2
        elif args[i] == "--days" and i + 1 < len(args):
            days = int(args[i + 1])
            i += 2
        else:
            # Positional: treat as domain or date
            if not date and "-" in args[i] and len(args[i]) == 10:
                date = args[i]
            else:
                domain = args[i]
            i += 1

    if cmd == "status":
        cmd_status(domain)
    elif cmd == "domains":
        cmd_domains()
    elif cmd == "run":
        cmd_run(domain, date)
    elif cmd == "evolve":
        cmd_evolve(domain, date)
    elif cmd == "history":
        cmd_history(domain, days)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
