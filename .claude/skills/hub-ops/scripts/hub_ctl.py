#!/usr/bin/env python3
"""claude-code-feishu control script — cron CRUD + reload signal.

Operates directly on data/jobs.json without importing hub modules.
Sends SIGUSR1 to hub process to trigger hot-reload after changes.

Usage:
    python3 .claude/skills/hub-ops/scripts/hub_ctl.py <command> [args]

Commands:
    cron list                              List all jobs
    cron add <name> <schedule>             Add handler/prompt job
         [--handler <name>]                  Handler job (e.g. briefing)
         [--prompt <text>]                   Prompt job (LLM-routed)
         [--model <provider/model>]          Override model
    cron remove <id>                       Remove job by ID (prefix match)
    cron enable <id>                       Enable a job
    cron disable <id>                      Disable a job
    cron show <id>                         Show job details
    reload                                 Send SIGUSR1 to hub (reload jobs)
    status                                 Show hub process status
"""

import json
import os
import signal
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

# Paths relative to hub working directory
# hub_ctl.py → scripts/ → hub-ops/ → skills/ → .claude/ → hub root (5 levels)
HUB_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent
JOBS_PATH = HUB_DIR / "data" / "jobs.json"
PID_PATH = HUB_DIR / "data" / "hub.pid"


def load_jobs() -> dict:
    if not JOBS_PATH.exists():
        return {"version": 1, "jobs": []}
    return json.loads(JOBS_PATH.read_text(encoding="utf-8"))


def save_jobs(data: dict):
    JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = JOBS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(JOBS_PATH))


def get_hub_pid() -> int | None:
    if not PID_PATH.exists():
        return None
    try:
        pid = int(PID_PATH.read_text().strip())
        os.kill(pid, 0)  # check alive
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        return None


def send_reload():
    pid = get_hub_pid()
    if pid:
        os.kill(pid, signal.SIGUSR1)
        print(f"SIGUSR1 sent to hub (pid={pid}), reload triggered.")
    else:
        print("WARNING: Hub process not found. Changes saved but not loaded until next restart.")


def find_job(data: dict, job_id: str) -> dict | None:
    for j in data["jobs"]:
        if j["id"].startswith(job_id):
            return j
    return None


# ═══ Commands ═══

def cmd_cron_list(data: dict):
    jobs = data["jobs"]
    if not jobs:
        print("No jobs configured.")
        return
    print(f"{'ID':<14} {'Name':<20} {'Enabled':<8} {'Schedule':<20} {'Handler':<10} {'Last':<8} {'Next run'}")
    print("-" * 100)
    for j in jobs:
        sched = j["schedule"]
        sched_str = sched.get("expr") or ""
        if not sched_str and sched.get("every_seconds"):
            sched_str = f"every {sched['every_seconds']}s"
        if not sched_str:
            sched_str = sched.get("at_time") or "?"
        state = j.get("state", {})
        next_run = ""
        if state.get("next_run_at"):
            next_run = datetime.fromtimestamp(state["next_run_at"]).strftime("%Y-%m-%d %H:%M")
        last = state.get("last_status") or "-"
        handler = j.get("handler") or ""
        enabled = "yes" if j.get("enabled", True) else "no"
        print(f"{j['id']:<14} {j['name']:<20} {enabled:<8} {sched_str:<20} {handler:<10} {last:<8} {next_run}")


def cmd_cron_add(args: list[str]):
    if len(args) < 2:
        print("Usage: hub_ctl.py cron add <name> <schedule> [--handler <h>] [--prompt <p>] [--model <m>]")
        sys.exit(1)

    name = args[0]
    schedule = args[1]
    handler = ""
    prompt = ""
    model_str = ""

    # Parse flags
    i = 2
    while i < len(args):
        if args[i] == "--handler" and i + 1 < len(args):
            handler = args[i + 1]; i += 2
        elif args[i] == "--prompt" and i + 1 < len(args):
            prompt = args[i + 1]; i += 2
        elif args[i] == "--model" and i + 1 < len(args):
            model_str = args[i + 1]; i += 2
        else:
            prompt = " ".join(args[i:]); break

    # Parse schedule
    sched = parse_schedule(schedule)

    # Parse model
    llm = {"provider": "claude-cli", "model": "opus", "timeout_seconds": 600,
           "system_prompt": None, "temperature": 1.0, "thinking": None}
    if model_str:
        parts = model_str.split("/", 1)
        if len(parts) == 2:
            llm["provider"] = parts[0]
            llm["model"] = parts[1]

    job = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "enabled": True,
        "schedule": sched,
        "prompt": prompt,
        "handler": handler,
        "llm": llm,
        "deliver_to_feishu": not handler,
        "one_shot": sched["kind"] == "at",
        "created_at": time.time(),
        "updated_at": time.time(),
        "state": {
            "next_run_at": None, "last_run_at": None,
            "last_status": None, "last_error": None, "consecutive_errors": 0,
        },
    }

    data = load_jobs()
    data["jobs"].append(job)
    save_jobs(data)
    print(f"Job created: {job['id']}")
    print(f"  Name:     {name}")
    print(f"  Schedule: {schedule} ({sched['kind']})")
    if handler:
        print(f"  Handler:  {handler}")
    if prompt:
        print(f"  Prompt:   {prompt[:80]}")
    send_reload()


def cmd_cron_remove(data: dict, job_id: str):
    job = find_job(data, job_id)
    if not job:
        print(f"Job not found: {job_id}")
        sys.exit(1)
    data["jobs"] = [j for j in data["jobs"] if j["id"] != job["id"]]
    save_jobs(data)
    print(f"Removed job: {job['id']} ({job['name']})")
    send_reload()


def cmd_cron_toggle(data: dict, job_id: str, enabled: bool):
    job = find_job(data, job_id)
    if not job:
        print(f"Job not found: {job_id}")
        sys.exit(1)
    job["enabled"] = enabled
    job["updated_at"] = time.time()
    save_jobs(data)
    state = "enabled" if enabled else "disabled"
    print(f"Job {state}: {job['id']} ({job['name']})")
    send_reload()


def cmd_cron_show(data: dict, job_id: str):
    job = find_job(data, job_id)
    if not job:
        print(f"Job not found: {job_id}")
        sys.exit(1)
    print(json.dumps(job, indent=2, ensure_ascii=False))


def cmd_reload():
    send_reload()


def cmd_status():
    pid = get_hub_pid()
    if pid:
        print(f"Hub is RUNNING (pid={pid})")
    else:
        print("Hub is STOPPED (no PID file or process dead)")

    data = load_jobs()
    enabled = [j for j in data["jobs"] if j.get("enabled", True)]
    disabled = [j for j in data["jobs"] if not j.get("enabled", True)]
    print(f"Jobs: {len(enabled)} enabled, {len(disabled)} disabled")


def parse_schedule(expr: str) -> dict:
    expr = expr.strip()
    if expr[-1] in "smh" and expr[:-1].isdigit():
        multipliers = {"s": 1, "m": 60, "h": 3600}
        return {"kind": "every", "expr": None, "every_seconds": int(expr[:-1]) * multipliers[expr[-1]],
                "at_time": None, "tz": "Asia/Shanghai"}
    if "T" in expr or expr.count("-") >= 2:
        return {"kind": "at", "expr": None, "every_seconds": None,
                "at_time": expr, "tz": "Asia/Shanghai"}
    return {"kind": "cron", "expr": expr, "every_seconds": None,
            "at_time": None, "tz": "Asia/Shanghai"}


# ═══ Main ═══

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "status":
        cmd_status()
    elif cmd == "reload":
        cmd_reload()
    elif cmd == "cron":
        if len(sys.argv) < 3:
            print("Usage: hub_ctl.py cron {list|add|remove|enable|disable|show}")
            sys.exit(1)
        sub = sys.argv[2]
        data = load_jobs()
        if sub == "list":
            cmd_cron_list(data)
        elif sub == "add":
            cmd_cron_add(sys.argv[3:])
        elif sub == "remove":
            if len(sys.argv) < 4:
                print("Usage: hub_ctl.py cron remove <id>"); sys.exit(1)
            cmd_cron_remove(data, sys.argv[3])
        elif sub == "enable":
            if len(sys.argv) < 4:
                print("Usage: hub_ctl.py cron enable <id>"); sys.exit(1)
            cmd_cron_toggle(data, sys.argv[3], True)
        elif sub == "disable":
            if len(sys.argv) < 4:
                print("Usage: hub_ctl.py cron disable <id>"); sys.exit(1)
            cmd_cron_toggle(data, sys.argv[3], False)
        elif sub == "show":
            if len(sys.argv) < 4:
                print("Usage: hub_ctl.py cron show <id>"); sys.exit(1)
            cmd_cron_show(data, sys.argv[3])
        else:
            print(f"Unknown cron subcommand: {sub}")
            sys.exit(1)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
