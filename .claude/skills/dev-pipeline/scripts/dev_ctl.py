#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dev_ctl.py — Unified MADS/MAQS skill CLI.

Provides the conversation-facing entry point for the development pipeline:
intake signals from dialogue, query ticket status, and manage pipeline execution.

Usage:
  python3 .claude/skills/dev-pipeline/scripts/dev_ctl.py intake --phenomenon "..." [--severity P2] [--source chat]
  python3 .claude/skills/dev-pipeline/scripts/dev_ctl.py classify --phenomenon "..."
  python3 .claude/skills/dev-pipeline/scripts/dev_ctl.py status
  python3 .claude/skills/dev-pipeline/scripts/dev_ctl.py list [--status open] [--type bug] [--severity P0] [--limit 20]
  python3 .claude/skills/dev-pipeline/scripts/dev_ctl.py show <record_id>
  python3 .claude/skills/dev-pipeline/scripts/dev_ctl.py run <record_id>
  python3 .claude/skills/dev-pipeline/scripts/dev_ctl.py simulate --file <scenarios.json>
"""

import argparse
import asyncio
import json
import os
import sys
import time

PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)
sys.path.insert(0, PROJECT_ROOT)

# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """Load maqs config from config.yaml."""
    config_path = os.path.join(PROJECT_ROOT, "config.yaml")
    if not os.path.exists(config_path):
        print("ERROR: config.yaml not found", file=sys.stderr)
        sys.exit(1)
    try:
        import yaml
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("maqs", {})
    except ImportError:
        # Fallback: try to extract bitable tokens from yaml manually
        import re
        with open(config_path) as f:
            content = f.read()
        result = {}
        for key in ("bitable_app_token", "bitable_table_id", "notify_open_id"):
            m = re.search(rf'{key}:\s*["\']?(\S+?)["\']?\s*$', content, re.M)
            if m:
                result[key] = m.group(1)
        return result


# ── Bitable helpers (sync wrappers) ──────────────────────────────────────────

_BITABLE_SCRIPT = os.path.join(
    PROJECT_ROOT, ".claude", "skills", "feishu-bitable", "scripts", "bitable_ctl.py"
)


def _bitable_query_sync(app_token: str, table_id: str,
                         filter_str: str = "", limit: int = 50) -> list[dict]:
    """Synchronous bitable query."""
    cmd = [sys.executable, _BITABLE_SCRIPT, "record", "list",
           app_token, table_id, "--limit", str(limit), "--json"]
    if filter_str:
        cmd.extend(["--filter", filter_str])
    import subprocess
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                            cwd=PROJECT_ROOT)
    if result.returncode == 0:
        try:
            data = json.loads(result.stdout)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
    return []


def _bitable_add_sync(app_token: str, table_id: str, fields: dict) -> str | None:
    """Synchronous bitable record creation."""
    import subprocess
    result = subprocess.run(
        [sys.executable, _BITABLE_SCRIPT, "record", "add", app_token, table_id,
         "--fields", json.dumps(fields, ensure_ascii=False)],
        capture_output=True, text=True, timeout=30, cwd=PROJECT_ROOT,
    )
    if result.returncode == 0:
        out = result.stdout.strip()
        if out.startswith("Created: "):
            return out.split("Created: ", 1)[1].strip()
        return "ok"
    print(f"ERROR: bitable add failed: {result.stderr[:200]}", file=sys.stderr)
    return None


# ── Immediate pipeline trigger ─────────────────────────────────────────────

def _trigger_pipeline_immediate() -> bool:
    """Set mads-pipeline cron next_run_at to now and send SIGUSR1.

    This causes the scheduler to fire mads-pipeline on the next event loop
    iteration (typically within seconds after the current CLI session completes).
    """
    import signal

    jobs_path = os.path.join(PROJECT_ROOT, "data", "jobs.json")
    pid_path = os.path.join(PROJECT_ROOT, "data", "hub.pid")

    try:
        with open(jobs_path) as f:
            data = json.load(f)

        for job in data["jobs"]:
            if job["name"] == "mads-pipeline":
                job["state"]["next_run_at"] = time.time() - 1
                break
        else:
            print("WARN: mads-pipeline job not found in jobs.json", file=sys.stderr)
            return False

        with open(jobs_path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # Send SIGUSR1 to hub process to reload scheduler
        if os.path.exists(pid_path):
            with open(pid_path) as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGUSR1)
            return True
        else:
            print("WARN: hub.pid not found, pipeline will run on next cron cycle",
                  file=sys.stderr)
            return False

    except Exception as e:
        print(f"WARN: immediate trigger failed: {e}", file=sys.stderr)
        return False


# ── Classification (local, no LLM) ──────────────────────────────────────────

# Keyword-based fast classifier — supplements LLM triage for simulation/dry-run
_BUG_KEYWORDS = [
    "bug", "报错", "异常", "错误", "crash", "失败", "不工作", "不生效",
    "断了", "挂了", "error", "fail", "broken", "不对", "问题",
    "没有触发", "未触发", "丢失", "丢了", "缺失", "遗漏", "没生效",
    "不触发", "不显示", "不发送", "无法", "不能",
]
_FEATURE_KEYWORDS = [
    "功能", "需求", "feature", "新增", "添加", "实现", "加一个", "做一个",
    "写一个", "支持", "能不能", "可以",
]
_REFACTOR_KEYWORDS = [
    "重构", "refactor", "优化", "整理", "清理", "拆分", "合并", "简化",
]
_SKILL_KEYWORDS = [
    "skill", "技能",
]
_CONFIG_KEYWORDS = [
    "配置", "config", "参数", "阈值", "调整",
]

_COMPOSITE_MARKERS = [
    "多个", "整体", "架构", "设计", "系统", "链路", "框架",
    "跨文件", "多文件", "跨模块", "多模块", "统一", "方案", "重新设计",
]


def classify_signal(phenomenon: str) -> dict:
    """Rule-based signal classification (no LLM). For dry-run/simulation."""
    text = phenomenon.lower()

    # Type detection (priority order)
    sig_type = "feature"  # default
    for kw in _BUG_KEYWORDS:
        if kw in text:
            sig_type = "bug"
            break
    else:
        for kw in _REFACTOR_KEYWORDS:
            if kw in text:
                sig_type = "refactor"
                break
        else:
            for kw in _SKILL_KEYWORDS:
                if kw in text:
                    sig_type = "skill"
                    break
            else:
                for kw in _CONFIG_KEYWORDS:
                    if kw in text:
                        sig_type = "config"
                        break

    # Complexity detection
    complexity = "atomic"
    for kw in _COMPOSITE_MARKERS:
        if kw in text:
            complexity = "composite"
            break

    # Severity heuristic
    severity = "P2"  # default
    if sig_type == "bug":
        if any(k in text for k in ["crash", "挂了", "服务不可用", "不工作"]):
            severity = "P0"
        elif any(k in text for k in ["失败", "报错", "错误", "error"]):
            severity = "P1"
    elif sig_type == "feature":
        severity = "P2"

    return {
        "type": sig_type,
        "complexity": complexity,
        "phenomenon": phenomenon,
        "severity": severity,
        "classifier": "rule-based",
    }


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_classify(args):
    """Dry-run classification without creating a ticket."""
    result = classify_signal(args.phenomenon)
    pipeline = "MADS" if result["complexity"] == "composite" else "MAQS"
    result["pipeline"] = pipeline
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_intake(args):
    """Create a ticket from a signal description."""
    cfg = _load_config()
    app_token = cfg.get("bitable_app_token", "")
    table_id = cfg.get("bitable_table_id", "")
    if not app_token or not table_id:
        print("ERROR: bitable not configured in config.yaml", file=sys.stderr)
        sys.exit(1)

    # Classify
    classification = classify_signal(args.phenomenon)

    # Override with explicit args
    if args.severity:
        classification["severity"] = args.severity
    if args.type:
        classification["type"] = args.type
    if args.complexity:
        classification["complexity"] = args.complexity

    pipeline = "MADS" if classification["complexity"] == "composite" else "MAQS"

    fields = {
        "title": classification["phenomenon"][:100],
        "type": classification["type"],
        "complexity": classification["complexity"],
        "source": args.source or "chat",
        "phenomenon": classification["phenomenon"],
        "severity": classification["severity"],
        "status": "open",
        "reject_count": 0,
    }

    record_id = _bitable_add_sync(app_token, table_id, fields)
    if record_id:
        result = {
            "status": "created",
            "record_id": record_id,
            "pipeline": pipeline,
            "ticket": fields,
        }

        # Immediate pipeline trigger (for chat scenarios)
        if getattr(args, "immediate", False):
            triggered = _trigger_pipeline_immediate()
            result["immediate_trigger"] = triggered

        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("ERROR: failed to create ticket", file=sys.stderr)
        sys.exit(1)


def cmd_status(args):
    """Show active tickets (open, diagnosing, fixing, reviewing, etc.)."""
    cfg = _load_config()
    app_token = cfg.get("bitable_app_token", "")
    table_id = cfg.get("bitable_table_id", "")
    if not app_token or not table_id:
        print("ERROR: bitable not configured", file=sys.stderr)
        sys.exit(1)

    # Query non-closed tickets
    active_statuses = [
        "open", "diagnosing", "diagnosed", "contracting", "contracted",
        "fixing", "reviewing", "designing", "awaiting_review",
        "review_approved", "review_feedback", "decomposing", "sub_tickets_created",
    ]

    records = _bitable_query_sync(app_token, table_id, limit=50)

    active = []
    for r in records:
        f = r.get("fields", {})
        status = f.get("status", "")
        if isinstance(status, list):
            status = status[0] if status else ""
        if status in active_statuses:
            active.append(r)

    if not active:
        print("No active tickets.")
        return

    # Sort by severity
    sev_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    active.sort(key=lambda r: sev_order.get(
        r.get("fields", {}).get("severity", "P3"), 3))

    print(f"Active tickets: {len(active)}\n")
    for r in active:
        f = r.get("fields", {})
        rid = r.get("record_id", "?")[:8]
        title = f.get("title", "untitled")
        if isinstance(title, list):
            title = title[0] if title else "untitled"
        status = f.get("status", "?")
        if isinstance(status, list):
            status = status[0] if status else "?"
        severity = f.get("severity", "?")
        if isinstance(severity, list):
            severity = severity[0] if severity else "?"
        complexity = f.get("complexity", "?")
        if isinstance(complexity, list):
            complexity = complexity[0] if complexity else "?"
        pipeline = "MADS" if complexity == "composite" else "MAQS"
        source = f.get("source", "?")
        if isinstance(source, list):
            source = source[0] if source else "?"
        print(f"  [{severity}] {rid}  {title}")
        print(f"        status={status}  pipeline={pipeline}  source={source}")


def cmd_list(args):
    """List tickets with optional filters."""
    cfg = _load_config()
    app_token = cfg.get("bitable_app_token", "")
    table_id = cfg.get("bitable_table_id", "")
    if not app_token or not table_id:
        print("ERROR: bitable not configured", file=sys.stderr)
        sys.exit(1)

    # Build filter
    filters = []
    if args.status:
        filters.append(f'CurrentValue.[status]="{args.status}"')
    if args.type:
        filters.append(f'CurrentValue.[type]="{args.type}"')
    if args.severity:
        filters.append(f'CurrentValue.[severity]="{args.severity}"')

    filter_str = ""
    if filters:
        filter_str = "AND(" + ",".join(filters) + ")" if len(filters) > 1 else filters[0]

    records = _bitable_query_sync(app_token, table_id, filter_str=filter_str,
                                   limit=args.limit)

    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
        return

    if not records:
        print("No tickets found.")
        return

    print(f"Found {len(records)} tickets:\n")
    for r in records:
        f = r.get("fields", {})
        rid = r.get("record_id", "?")[:8]
        title = f.get("title", "untitled")
        if isinstance(title, list):
            title = title[0] if title else "untitled"
        status = f.get("status", "?")
        if isinstance(status, list):
            status = status[0] if status else "?"
        severity = f.get("severity", "?")
        if isinstance(severity, list):
            severity = severity[0] if severity else "?"
        complexity = f.get("complexity", "?")
        if isinstance(complexity, list):
            complexity = complexity[0] if complexity else "?"
        pipeline = "MADS" if complexity == "composite" else "MAQS"
        print(f"  [{severity}] {rid}  {status:<20} {pipeline:<5}  {title}")


def cmd_show(args):
    """Show detailed ticket information."""
    cfg = _load_config()
    app_token = cfg.get("bitable_app_token", "")
    table_id = cfg.get("bitable_table_id", "")
    if not app_token or not table_id:
        print("ERROR: bitable not configured", file=sys.stderr)
        sys.exit(1)

    records = _bitable_query_sync(app_token, table_id, limit=100)

    target = None
    for r in records:
        if r.get("record_id", "").startswith(args.record_id):
            target = r
            break

    if not target:
        print(f"Ticket not found: {args.record_id}", file=sys.stderr)
        sys.exit(1)

    f = target.get("fields", {})
    print(json.dumps(f, ensure_ascii=False, indent=2, default=str))


def cmd_run(args):
    """Manually trigger pipeline progression for a ticket.

    This is a lightweight trigger — it prints instructions for CC to execute
    the appropriate pipeline function, since the full pipeline requires
    the router and dispatcher which are only available in the hub runtime.
    """
    cfg = _load_config()
    app_token = cfg.get("bitable_app_token", "")
    table_id = cfg.get("bitable_table_id", "")

    records = _bitable_query_sync(app_token, table_id, limit=100)

    target = None
    for r in records:
        if r.get("record_id", "").startswith(args.record_id):
            target = r
            break

    if not target:
        print(f"Ticket not found: {args.record_id}", file=sys.stderr)
        sys.exit(1)

    f = target.get("fields", {})
    status = f.get("status", "")
    if isinstance(status, list):
        status = status[0] if status else ""
    complexity = f.get("complexity", "")
    if isinstance(complexity, list):
        complexity = complexity[0] if complexity else ""
    title = f.get("title", "")
    if isinstance(title, list):
        title = title[0] if title else ""
    record_id = target["record_id"]

    pipeline = "MADS" if complexity == "composite" else "MAQS"

    print(json.dumps({
        "action": "run_pipeline",
        "record_id": record_id,
        "title": title,
        "status": status,
        "pipeline": pipeline,
        "complexity": complexity,
        "instruction": (
            f"Trigger {pipeline} pipeline for ticket '{title}' "
            f"(status: {status}). The cron handler will pick this up "
            f"on next cycle, or use the hub runtime to call "
            f"{'run_maqs_pipeline' if pipeline == 'MAQS' else 'run_mads_pipeline'} directly."
        ),
    }, ensure_ascii=False, indent=2))


def cmd_simulate(args):
    """Run simulation tests: classify a batch of scenarios and verify routing.

    Input: JSON file with array of {phenomenon, expected_type, expected_complexity, expected_pipeline}
    Output: pass/fail per scenario with mismatches highlighted.
    """
    with open(args.file) as f:
        scenarios = json.load(f)

    passed = 0
    failed = 0
    results = []

    for i, scenario in enumerate(scenarios):
        phenomenon = scenario["phenomenon"]
        expected = {
            "type": scenario.get("expected_type"),
            "complexity": scenario.get("expected_complexity"),
            "pipeline": scenario.get("expected_pipeline"),
        }

        actual = classify_signal(phenomenon)
        actual_pipeline = "MADS" if actual["complexity"] == "composite" else "MAQS"

        mismatches = []
        if expected["type"] and actual["type"] != expected["type"]:
            mismatches.append(f"type: {actual['type']} != {expected['type']}")
        if expected["complexity"] and actual["complexity"] != expected["complexity"]:
            mismatches.append(f"complexity: {actual['complexity']} != {expected['complexity']}")
        if expected["pipeline"] and actual_pipeline != expected["pipeline"]:
            mismatches.append(f"pipeline: {actual_pipeline} != {expected['pipeline']}")

        status = "PASS" if not mismatches else "FAIL"
        if status == "PASS":
            passed += 1
        else:
            failed += 1

        result = {
            "scenario": i + 1,
            "phenomenon": phenomenon,
            "status": status,
            "actual": {**actual, "pipeline": actual_pipeline},
        }
        if mismatches:
            result["mismatches"] = mismatches
        results.append(result)

    print(json.dumps({
        "summary": {"total": len(scenarios), "passed": passed, "failed": failed},
        "results": results,
    }, ensure_ascii=False, indent=2))

    sys.exit(0 if failed == 0 else 1)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Unified MADS/MAQS development pipeline CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # classify (dry-run)
    p_cls = sub.add_parser("classify", help="Classify a signal without creating a ticket")
    p_cls.add_argument("--phenomenon", "-p", required=True, help="Signal description")

    # intake
    p_in = sub.add_parser("intake", help="Create a ticket from a signal")
    p_in.add_argument("--phenomenon", "-p", required=True, help="Signal description")
    p_in.add_argument("--severity", "-s", choices=["P0", "P1", "P2", "P3"])
    p_in.add_argument("--type", "-t", choices=["bug", "feature", "refactor", "skill", "config"])
    p_in.add_argument("--complexity", "-c", choices=["atomic", "composite"])
    p_in.add_argument("--source", default="chat", help="Signal source (default: chat)")
    p_in.add_argument("--immediate", action="store_true",
                       help="Immediately trigger pipeline (for chat scenarios)")

    # status
    sub.add_parser("status", help="Show active tickets")

    # list
    p_ls = sub.add_parser("list", help="List tickets with filters")
    p_ls.add_argument("--status", help="Filter by status")
    p_ls.add_argument("--type", help="Filter by type")
    p_ls.add_argument("--severity", help="Filter by severity")
    p_ls.add_argument("--limit", type=int, default=20)
    p_ls.add_argument("--json", action="store_true", help="JSON output")

    # show
    p_sh = sub.add_parser("show", help="Show ticket details")
    p_sh.add_argument("record_id", help="Record ID (prefix match)")

    # run
    p_run = sub.add_parser("run", help="Trigger pipeline for a ticket")
    p_run.add_argument("record_id", help="Record ID (prefix match)")

    # simulate
    p_sim = sub.add_parser("simulate", help="Run simulation tests")
    p_sim.add_argument("--file", "-f", required=True, help="Scenarios JSON file")

    args = parser.parse_args()

    cmd_map = {
        "classify": cmd_classify,
        "intake": cmd_intake,
        "status": cmd_status,
        "list": cmd_list,
        "show": cmd_show,
        "run": cmd_run,
        "simulate": cmd_simulate,
    }
    cmd_map[args.command](args)


if __name__ == "__main__":
    main()
