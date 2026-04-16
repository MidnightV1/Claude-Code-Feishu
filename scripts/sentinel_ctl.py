#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sentinel CLI — manual trigger and query for the Sentinel entropy-control system.

Usage:
  python3 scripts/sentinel_ctl.py scan
  python3 scripts/sentinel_ctl.py scan --scanner code_scanner
  python3 scripts/sentinel_ctl.py list
  python3 scripts/sentinel_ctl.py list --hours 48
  python3 scripts/sentinel_ctl.py list --source health_pulse
  python3 scripts/sentinel_ctl.py list --unresolved
  python3 scripts/sentinel_ctl.py stats
  python3 scripts/sentinel_ctl.py stats --hours 48
  python3 scripts/sentinel_ctl.py resolve <signal_id>
"""

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.jobs.sentinel import SentinelStore, SentinelOrchestrator
from agent.jobs.sentinel.scanners import (
    CodeScanner,
    DocAuditor,
    HealthPulse,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
)

# All available scanners by name
ALL_SCANNERS = {
    "code_scanner": CodeScanner,
    "doc_auditor": DocAuditor,
    "health_pulse": HealthPulse,
}

SEVERITY_COLOR = {
    "critical": "\033[91m",   # red
    "high":     "\033[93m",   # yellow
    "medium":   "\033[94m",   # blue
    "low":      "\033[37m",   # white/grey
}
RESET = "\033[0m"


def _age(ts: float) -> str:
    """Return a human-readable age string from a Unix timestamp."""
    delta = time.time() - ts
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


# ── scan ────────────────────────────────────────────────────────────────────

async def cmd_scan(args: argparse.Namespace) -> None:
    store = SentinelStore(str(PROJECT_ROOT / "data" / "sentinel.jsonl"))

    def _make_scanner(name, cls):
        return cls()

    if args.scanner:
        scanner_cls = ALL_SCANNERS.get(args.scanner)
        if not scanner_cls:
            print(f"Unknown scanner: {args.scanner!r}")
            print(f"Available: {', '.join(ALL_SCANNERS)}")
            sys.exit(1)
        scanners = [_make_scanner(args.scanner, scanner_cls)]
        print(f"Running scanner: {args.scanner}")
    else:
        scanners = [_make_scanner(n, c) for n, c in ALL_SCANNERS.items()]
        print(f"Running {len(scanners)} scanners: {', '.join(ALL_SCANNERS)}")

    # Load config for MAQS ticket creation (bitable credentials)
    config = {}
    config_path = PROJECT_ROOT / "config.yaml"
    if config_path.exists():
        import yaml
        with open(config_path) as _f:
            full_cfg = yaml.safe_load(_f) or {}
        config = {"maqs": full_cfg.get("maqs", {})}

    # No dispatcher in CLI context — notifications are skipped, signals still persisted
    orchestrator = SentinelOrchestrator(
        scanners=scanners,
        store=store,
        dispatcher=None,
        workspace_dir=str(PROJECT_ROOT),
        config=config,
    )

    print("Scanning...\n")
    summary = await orchestrator.run_cycle(trigger="manual")

    total = summary.get("total", 0)
    print(f"Sentinel scan complete: {total} signals found")
    for route in ("maqs", "explore", "notify", "silent_log"):
        count = summary.get(route, 0)
        if count:
            print(f"- {route}: {count}")

    signals = summary.get("signals", [])
    if signals:
        print("\nSignals:")
        for i, s in enumerate(signals, 1):
            evidence = s.evidence[0] if s.evidence else ""
            print(f"{i}. [{s.source}] {s.category}: {s.summary} ({evidence})")


# ── list ────────────────────────────────────────────────────────────────────

def cmd_list(args: argparse.Namespace) -> None:
    store = SentinelStore(str(PROJECT_ROOT / "data" / "sentinel.jsonl"))

    signals = store.query(
        hours=args.hours,
        source=args.source or None,
        unresolved_only=args.unresolved,
    )

    if not signals:
        print(f"No signals found (last {args.hours}h).")
        return

    print(f"── {len(signals)} signal(s) — last {args.hours}h ────────────────────")
    for s in signals:
        color = SEVERITY_COLOR.get(s.severity, "")
        resolved_marker = " [resolved]" if s.resolved_at else ""
        route_display = f"→{s.route}" if s.route != "silent_log" else ""
        age = _age(s.created_at)
        print(
            f"  [{s.id}] {color}{s.severity:<8}{RESET} "
            f"{s.source:<22} {s.summary[:60]}"
            f"  {route_display}{resolved_marker}  ({age})"
        )
        if args.verbose and s.evidence:
            for e in s.evidence[:3]:
                print(f"             evidence: {e}")
            if s.suggested_action:
                print(f"             action  : {s.suggested_action}")
    print()


# ── stats ───────────────────────────────────────────────────────────────────

def cmd_stats(args: argparse.Namespace) -> None:
    store = SentinelStore(str(PROJECT_ROOT / "data" / "sentinel.jsonl"))
    data = store.stats(hours=args.hours)

    print(f"── Sentinel stats — last {args.hours}h ──────────────────────")
    print(f"  Total      : {data['total']}")
    print(f"  Resolved   : {data['resolved']}")
    print(f"  Unresolved : {data['unresolved']}")

    if data["by_source"]:
        print("\n  By source:")
        for src, count in sorted(data["by_source"].items(), key=lambda x: -x[1]):
            print(f"    {src:<25} {count}")

    if data["by_route"]:
        print("\n  By route:")
        for route, count in sorted(data["by_route"].items(), key=lambda x: -x[1]):
            print(f"    {route:<20} {count}")
    print()


# ── resolve ─────────────────────────────────────────────────────────────────

def cmd_resolve(args: argparse.Namespace) -> None:
    store = SentinelStore(str(PROJECT_ROOT / "data" / "sentinel.jsonl"))
    ok = store.resolve(args.signal_id)
    if ok:
        print(f"Signal {args.signal_id} marked as resolved.")
    else:
        print(f"Signal {args.signal_id} not found.")
        sys.exit(1)


# ── CLI setup ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentinel_ctl.py",
        description="Sentinel — manual trigger and query for entropy-control signals.",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    # scan
    p_scan = sub.add_parser("scan", help="Run a scan cycle")
    p_scan.add_argument(
        "--scanner",
        metavar="NAME",
        help=f"Run only this scanner. Choices: {', '.join(ALL_SCANNERS)}",
    )

    # list
    p_list = sub.add_parser("list", help="List recent signals")
    p_list.add_argument("--hours", type=float, default=24, metavar="N",
                        help="Time window in hours (default: 24)")
    p_list.add_argument("--source", metavar="NAME",
                        help="Filter by scanner name")
    p_list.add_argument("--unresolved", action="store_true",
                        help="Show only unresolved signals")
    p_list.add_argument("-v", "--verbose", action="store_true",
                        help="Show evidence and suggested actions")

    # stats
    p_stats = sub.add_parser("stats", help="Show signal statistics")
    p_stats.add_argument("--hours", type=float, default=24, metavar="N",
                         help="Time window in hours (default: 24)")

    # resolve
    p_resolve = sub.add_parser("resolve", help="Mark a signal as resolved")
    p_resolve.add_argument("signal_id", help="Signal ID (12-char hex)")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "scan":
        asyncio.run(cmd_scan(args))
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "resolve":
        cmd_resolve(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
