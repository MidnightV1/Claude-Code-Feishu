#!/usr/bin/env python3
"""claude-code-feishu control script — cron CRUD + deploy promote + reload signal.

Job definitions live in config/jobs.yaml (git-tracked, authoritative).
Runtime state lives in data/jobs.json (scheduler-managed).
Sends SIGUSR1 to hub process to trigger hot-reload after changes.

Usage:
    python3 .claude/skills/hub-ops/scripts/hub_ctl.py <command> [args]

Commands:
    cron list                              List all jobs
    cron add <name> <schedule>             Add job to config/jobs.yaml
         [--handler <name>]                  Handler job (e.g. briefing)
         [--prompt <text>]                   Prompt job (LLM-routed)
         [--model <provider/model>]          Override model
    cron remove <id>                       Remove job from config/jobs.yaml
    cron enable <id>                       Enable a job
    cron disable <id>                      Disable a job
    cron show <id>                         Show job details
    promote --preview                      Show merge preview (gate card)
    promote --execute                      Merge dev→master + deploy + notify + NAS sync
    reload                                 Send SIGUSR1 to hub (reload jobs)
    status                                 Show hub process status
"""

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import yaml

HUB_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent
SEED_PATH = HUB_DIR / "config" / "jobs.yaml"
STATE_PATH = HUB_DIR / "data" / "jobs.json"
PID_PATH = HUB_DIR / "data" / "hub.pid"


def load_seed() -> list[dict]:
    if not SEED_PATH.exists(): return []
    data = yaml.safe_load(SEED_PATH.read_text(encoding="utf-8")) or {}
    return data.get("jobs", [])

def save_seed(jobs: list[dict]):
    SEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.dump({"jobs": jobs}, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120)
    tmp = SEED_PATH.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(SEED_PATH))

def load_state_map() -> dict[str, dict]:
    if not STATE_PATH.exists(): return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if "states" in data: return data["states"]
        if "jobs" in data and isinstance(data["jobs"], list):
            return {j["id"]: j.get("state", {}) for j in data["jobs"] if "id" in j}
    except (json.JSONDecodeError, KeyError): pass
    return {}

def merge_jobs() -> list[dict]:
    jobs = load_seed()
    state_map = load_state_map()
    for j in jobs:
        j.setdefault("enabled", True)
        j["state"] = state_map.get(j.get("id", ""), {})
    return jobs

def find_job(jobs, job_id):
    for j in jobs:
        if j.get("id", "").startswith(job_id): return j
    return None

def get_hub_pid():
    if not PID_PATH.exists(): return None
    try:
        pid = int(PID_PATH.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError): return None

def send_reload():
    pid = get_hub_pid()
    if pid:
        os.kill(pid, signal.SIGUSR1)
        print(f"SIGUSR1 sent to hub (pid={pid}), reload triggered.")
    else:
        print("WARNING: Hub process not found. Changes saved but not loaded until next restart.")


def cmd_cron_list(jobs):
    if not jobs: print("No jobs configured."); return
    print(f"{'ID':<14} {'Name':<20} {'Enabled':<8} {'Schedule':<20} {'Handler':<10} {'Last':<8} {'Next run'}")
    print("-" * 100)
    for j in jobs:
        sched = j.get("schedule", "")
        if isinstance(sched, dict): sched = sched.get("expr") or sched.get("at_time") or "?"
        state = j.get("state", {})
        next_run = datetime.fromtimestamp(state["next_run_at"]).strftime("%Y-%m-%d %H:%M") if state.get("next_run_at") else ""
        last = state.get("last_status") or "-"
        handler = j.get("handler") or ""
        enabled = "yes" if j.get("enabled", True) else "no"
        print(f"{j.get('id','?'):<14} {j['name']:<20} {enabled:<8} {str(sched):<20} {handler:<10} {last:<8} {next_run}")

def cmd_cron_add(args):
    if len(args) < 2:
        print("Usage: hub_ctl.py cron add <name> <schedule> [--handler <h>] [--prompt <p>] [--model <m>]"); sys.exit(1)
    name, schedule = args[0], args[1]
    handler = prompt = model_str = ""
    i = 2
    while i < len(args):
        if args[i] == "--handler" and i+1 < len(args): handler = args[i+1]; i += 2
        elif args[i] == "--prompt" and i+1 < len(args): prompt = args[i+1]; i += 2
        elif args[i] == "--model" and i+1 < len(args): model_str = args[i+1]; i += 2
        else: prompt = " ".join(args[i:]); break
    job = {"id": uuid.uuid4().hex[:12], "name": name, "schedule": schedule}
    if handler: job["handler"] = handler; job["deliver_to_feishu"] = False
    if prompt: job["prompt"] = prompt
    if model_str: job["model"] = model_str
    jobs = load_seed(); jobs.append(job); save_seed(jobs)
    print(f"Job created: {job['id']}\n  Name:     {name}\n  Schedule: {schedule}")
    if handler: print(f"  Handler:  {handler}")
    if prompt: print(f"  Prompt:   {prompt[:80]}")
    send_reload()

def cmd_cron_remove(jobs, job_id):
    job = find_job(jobs, job_id)
    if not job: print(f"Job not found: {job_id}"); sys.exit(1)
    jobs = [j for j in jobs if j.get("id") != job["id"]]
    save_seed(jobs)
    print(f"Removed job: {job['id']} ({job['name']})"); send_reload()

def cmd_cron_toggle(jobs, job_id, enabled):
    job = find_job(jobs, job_id)
    if not job: print(f"Job not found: {job_id}"); sys.exit(1)
    job["enabled"] = enabled; save_seed(jobs)
    print(f"Job {'enabled' if enabled else 'disabled'}: {job['id']} ({job['name']})"); send_reload()

def cmd_cron_show(jobs, job_id):
    job = find_job(jobs, job_id)
    if not job: print(f"Job not found: {job_id}"); sys.exit(1)
    state_map = load_state_map()
    job["state"] = state_map.get(job.get("id", ""), {})
    print(yaml.dump(job, default_flow_style=False, allow_unicode=True, sort_keys=False))

def cmd_reload(): send_reload()

def cmd_status():
    pid = get_hub_pid()
    print(f"Hub is {'RUNNING' if pid else 'STOPPED'} (pid={pid})" if pid else "Hub is STOPPED (no PID file or process dead)")
    jobs = load_seed()
    enabled = sum(1 for j in jobs if j.get("enabled", True))
    print(f"Jobs: {enabled} enabled, {len(jobs)-enabled} disabled (from config/jobs.yaml)")


# ═══ Promote ═══
DEPLOY_LOG = HUB_DIR / "data" / "deploy.log"
PYTHON = "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"

def _git(*args, cwd=None):
    r = subprocess.run(["git"]+list(args), cwd=cwd or str(HUB_DIR), capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout.strip(), r.stderr.strip()

def _deploy_log(msg):
    DEPLOY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(DEPLOY_LOG, "a") as f: f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")

def _notify(msg):
    try: subprocess.run([PYTHON, str(HUB_DIR/"scripts"/"deploy_notify.py"), msg], cwd=str(HUB_DIR), timeout=15)
    except Exception: pass

def _classify_files(files):
    core, skills, config, docs, other = [], [], [], [], []
    for f in files:
        if f.startswith("agent/"): core.append(f)
        elif f.startswith(".claude/skills/") or f.startswith("scripts/"): skills.append(f)
        elif f.endswith((".yaml",".json")) and not f.startswith("data/"): config.append(f)
        elif f.endswith(".md") or f.startswith("docs/"): docs.append(f)
        else: other.append(f)
    return {"core": core, "skills": skills, "config": config, "docs": docs, "other": other}

def cmd_promote_preview():
    _, branch, _ = _git("branch", "--show-current")
    if branch != "dev": print(json.dumps({"error": f"Not on dev branch (on {branch})"})); sys.exit(1)
    rc, _, _ = _git("diff", "--quiet"); rc2, _, _ = _git("diff", "--cached", "--quiet")
    if rc != 0 or rc2 != 0: print(json.dumps({"error": "Uncommitted changes on dev"})); sys.exit(1)
    _, log_out, _ = _git("log", "master..dev", "--oneline", "--no-decorate")
    commits = [l for l in log_out.splitlines() if l.strip()] if log_out else []
    if not commits: print(json.dumps({"error": "No new commits on dev to merge"})); sys.exit(1)
    _, diff_out, _ = _git("diff", "--name-only", "master..dev")
    files = [f for f in diff_out.splitlines() if f.strip()] if diff_out else []
    classified = _classify_files(files)
    smoke_ok, smoke_detail, test_ok, test_detail = False, "", False, ""
    try:
        r = subprocess.run([PYTHON, str(HUB_DIR/"scripts"/"smoke_test.py")], cwd=str(HUB_DIR), capture_output=True, text=True, timeout=60)
        smoke_ok = r.returncode == 0
        for l in r.stdout.splitlines():
            if "Passed:" in l: smoke_detail = l.strip(); break
    except Exception as e: smoke_detail = str(e)
    try:
        r = subprocess.run([PYTHON, "-m", "pytest", "tests/unit/", "-q", "--tb=no"], cwd=str(HUB_DIR), capture_output=True, text=True, timeout=120)
        test_ok = r.returncode == 0
        for l in r.stdout.splitlines():
            if "passed" in l: test_detail = l.strip(); break
    except Exception as e: test_detail = str(e)
    print(json.dumps({"status": "preview", "commits": commits, "commit_count": len(commits), "files": files, "classified": classified, "needs_restart": len(classified["core"])>0, "smoke_test": {"ok": smoke_ok, "detail": smoke_detail}, "unit_test": {"ok": test_ok, "detail": test_detail}}, ensure_ascii=False, indent=2))

def cmd_promote_execute():
    _, branch, _ = _git("branch", "--show-current")
    if branch != "dev": print(json.dumps({"error": f"Not on dev (on {branch})"})); sys.exit(1)
    rc, _, _ = _git("diff", "--quiet"); rc2, _, _ = _git("diff", "--cached", "--quiet")
    if rc != 0 or rc2 != 0: print(json.dumps({"error": "Uncommitted changes on dev"})); sys.exit(1)
    _, old_master, _ = _git("rev-parse", "--short", "master")
    _, log_out, _ = _git("log", "master..dev", "--oneline", "--no-decorate")
    commits = [l for l in log_out.splitlines() if l.strip()] if log_out else []
    if not commits: print(json.dumps({"error": "No new commits to merge"})); sys.exit(1)
    _, diff_out, _ = _git("diff", "--name-only", "master..dev")
    files = [f for f in diff_out.splitlines() if f.strip()] if diff_out else []
    classified = _classify_files(files); needs_restart = len(classified["core"]) > 0
    rc, _, err = _git("checkout", "master")
    if rc != 0: print(json.dumps({"error": f"Failed to checkout master: {err}"})); sys.exit(1)
    _git("pull", "origin", "master")
    rc, _, err = _git("merge", "dev", "--no-edit")
    if rc != 0:
        _git("merge", "--abort"); _git("checkout", "dev")
        _notify(f"Promote FAILED: merge failed — {err}"); print(json.dumps({"error": f"Merge failed: {err}"})); sys.exit(1)
    _, new_master, _ = _git("rev-parse", "--short", "master")
    _deploy_log(f"Deploying master ({old_master} -> {new_master})...")
    smoke_ok = False
    try:
        r = subprocess.run([PYTHON, str(HUB_DIR/"scripts"/"smoke_test.py")], cwd=str(HUB_DIR), capture_output=True, text=True, timeout=60)
        smoke_ok = r.returncode == 0
    except Exception: pass
    if not smoke_ok:
        _deploy_log("SMOKE TEST FAILED — rolling back"); _git("reset", "--hard", "HEAD~1"); _git("checkout", "dev")
        _deploy_log(f"Reverted master to {old_master}"); _notify(f"Promote FAILED: smoke test failed — rolled back to {old_master}")
        print(json.dumps({"status": "failed", "reason": "smoke_test_failed", "rolled_back_to": old_master})); sys.exit(1)
    _deploy_log("Smoke test passed")
    if classified["core"]: _deploy_log(f"Core Python changed: {' '.join(classified['core'])}"); _deploy_log("Manual restart needed")
    elif classified["skills"]: _deploy_log(f"Skill/script changed: {' '.join(classified['skills'])}")
    else: _deploy_log("Deploy complete (no Python changes)")
    rc, _, err = _git("push", "origin", "master"); nas_ok = rc == 0
    _deploy_log(f"NAS sync: {'OK' if nas_ok else f'FAILED: {err}'}"); _git("checkout", "dev")
    _notify(f"Promote OK ({old_master}->{new_master}):\n" + "\n".join(commits))
    print(json.dumps({"status": "deployed", "old_master": old_master, "new_master": new_master, "commits": commits, "commit_count": len(commits), "classified": classified, "needs_restart": needs_restart, "nas_sync": nas_ok}, ensure_ascii=False, indent=2))


def main():
    if len(sys.argv) < 2: print(__doc__); sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "status": cmd_status()
    elif cmd == "reload": cmd_reload()
    elif cmd == "promote":
        if "--execute" in sys.argv: cmd_promote_execute()
        else: cmd_promote_preview()
    elif cmd == "cron":
        if len(sys.argv) < 3: print("Usage: hub_ctl.py cron {list|add|remove|enable|disable|show}"); sys.exit(1)
        sub = sys.argv[2]
        if sub == "add": cmd_cron_add(sys.argv[3:])
        elif sub == "list": cmd_cron_list(merge_jobs())
        elif sub == "remove":
            if len(sys.argv) < 4: print("Usage: hub_ctl.py cron remove <id>"); sys.exit(1)
            cmd_cron_remove(load_seed(), sys.argv[3])
        elif sub == "enable":
            if len(sys.argv) < 4: print("Usage: hub_ctl.py cron enable <id>"); sys.exit(1)
            cmd_cron_toggle(load_seed(), sys.argv[3], True)
        elif sub == "disable":
            if len(sys.argv) < 4: print("Usage: hub_ctl.py cron disable <id>"); sys.exit(1)
            cmd_cron_toggle(load_seed(), sys.argv[3], False)
        elif sub == "show":
            if len(sys.argv) < 4: print("Usage: hub_ctl.py cron show <id>"); sys.exit(1)
            cmd_cron_show(merge_jobs(), sys.argv[3])
        else: print(f"Unknown cron subcommand: {sub}"); sys.exit(1)
    else: print(f"Unknown command: {cmd}"); print(__doc__); sys.exit(1)

if __name__ == "__main__":
    main()
