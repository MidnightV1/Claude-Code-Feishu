#!/usr/bin/env python3
"""Feishu Task CLI — create, list, update, complete, delete tasks.

Usage:
    task_ctl.py create "title" [--assignee "a,b"] [--due "time"] [--desc "..."]
    task_ctl.py list [--assignee "name"] [--completed]
    task_ctl.py get <task_guid>
    task_ctl.py update <task_guid> [--title T] [--due D] [--desc D]
    task_ctl.py complete <task_guid>
    task_ctl.py delete <task_guid>
    task_ctl.py tasklist create "name"
    task_ctl.py tasklist list
    task_ctl.py snapshot [--window-hours N]
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Project root: .claude/skills/feishu-task/scripts -> project root
BASE = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(BASE))

from feishu_api import FeishuAPI, ContactStore  # noqa: E402

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai


def _load_config():
    import yaml
    with open(BASE / "config.yaml") as f:
        return yaml.safe_load(f)


def _parse_dt(s: str) -> int:
    """Parse datetime string to unix timestamp (seconds).

    Accepts: 'YYYY-MM-DD', 'YYYY-MM-DDTHH:MM', 'HH:MM' (today),
             'tomorrow HH:MM', '+2h', '+30m'.
    """
    s = s.strip()
    now = datetime.now(TZ)

    # relative: +2h, +30m
    if s.startswith("+"):
        unit = s[-1]
        val = int(s[1:-1])
        if unit == "h":
            dt = now + timedelta(hours=val)
        elif unit == "m":
            dt = now + timedelta(minutes=val)
        else:
            dt = now + timedelta(minutes=val)
        return int(dt.timestamp())

    # "tomorrow HH:MM"
    if s.lower().startswith("tomorrow"):
        time_part = s.split(None, 1)[1] if " " in s else "09:00"
        h, m = map(int, time_part.split(":"))
        dt = (now + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)
        return int(dt.timestamp())

    # "HH:MM" — today (or next day if past)
    if len(s) <= 5 and ":" in s:
        h, m = map(int, s.split(":"))
        dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if dt < now:
            dt += timedelta(days=1)
        return int(dt.timestamp())

    # ISO formats
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=TZ)
            return int(dt.timestamp())
        except ValueError:
            continue

    print(f"ERROR: Cannot parse datetime: {s}", file=sys.stderr)
    sys.exit(1)


def _ms_to_str(ts) -> str:
    """Millisecond timestamp (string or int) to readable datetime."""
    return datetime.fromtimestamp(int(ts) / 1000, TZ).strftime("%Y-%m-%d %H:%M")


def _sec_to_ms(sec: int) -> str:
    """Convert seconds timestamp to milliseconds string (Feishu Task API format)."""
    return str(sec * 1000)


def _get_tasklist_guid(cfg: dict) -> str:
    guid = cfg.get("feishu", {}).get("tasks", {}).get("tasklist_guid", "")
    if not guid:
        print("ERROR: feishu.tasks.tasklist_guid not configured in config.yaml",
              file=sys.stderr)
        sys.exit(1)
    return guid


def _resolve_members(names: str, contacts: ContactStore) -> list[dict]:
    """Resolve comma-separated names to member list."""
    members = []
    for name in names.split(","):
        name = name.strip()
        if not name:
            continue
        oid = contacts.lookup(name)
        if not oid:
            print(f"WARNING: Contact '{name}' not found, skipping", file=sys.stderr)
            continue
        members.append({"id": oid, "type": "user", "role": "assignee"})
    return members


def _fetch_task(api: FeishuAPI, guid: str) -> dict | None:
    """Fetch full task details by guid. Returns None on error."""
    resp = api.get(f"/open-apis/task/v2/tasks/{guid}",
                   params={"user_id_type": "open_id"})
    if resp.get("code") != 0:
        return None
    return resp.get("data", {}).get("task")


def _list_tasklist_tasks(api: FeishuAPI, tasklist_guid: str,
                         max_pages: int = 10) -> list[dict]:
    """List tasks in a tasklist. Returns task dicts (summary-level fields)."""
    tasks = []
    page_token = None
    for _ in range(max_pages):
        params = {"page_size": "50", "user_id_type": "open_id"}
        if page_token:
            params["page_token"] = page_token
        resp = api.get(
            f"/open-apis/task/v2/tasklists/{tasklist_guid}/tasks",
            params=params,
        )
        if resp.get("code") != 0:
            print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
            break
        tasks.extend(resp.get("data", {}).get("items", []))
        if not resp.get("data", {}).get("has_more"):
            break
        page_token = resp["data"].get("page_token")
    return tasks


def _format_relative(ms: int) -> str:
    """Format milliseconds to human-readable relative time."""
    minutes = ms // 60000
    if minutes < 60:
        return f"{minutes}min"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


# ── Commands ─────────────────────────────────────────────

def cmd_create(args, api, cfg, contacts):
    body = {"summary": args.title}
    if args.desc:
        body["description"] = args.desc
    if args.due:
        body["due"] = {"timestamp": _sec_to_ms(_parse_dt(args.due)),
                       "is_all_day": False}
    if args.assignee:
        body["members"] = _resolve_members(args.assignee, contacts)

    # Auto-add to configured tasklist
    tasklist_guid = cfg.get("feishu", {}).get("tasks", {}).get("tasklist_guid")
    if tasklist_guid:
        body["tasklists"] = [{"tasklist_guid": tasklist_guid}]

    resp = api.post("/open-apis/task/v2/tasks", body,
                    params={"user_id_type": "open_id"})
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    task = resp["data"]["task"]
    print(f"Created: {task['guid']}")
    print(f"  Title: {task.get('summary')}")
    if task.get("due"):
        print(f"  Due: {_ms_to_str(task['due']['timestamp'])}")
    if task.get("url"):
        print(f"  URL: {task['url']}")


def cmd_list(args, api, cfg, contacts):
    tasklist_guid = _get_tasklist_guid(cfg)
    tasks = _list_tasklist_tasks(api, tasklist_guid)

    if not tasks:
        print("No tasks found.")
        return

    # Filter completed/open
    if not args.completed:
        tasks = [t for t in tasks
                 if not (t.get("completed_at") and t["completed_at"] != "0")]
    else:
        tasks = [t for t in tasks
                 if t.get("completed_at") and t["completed_at"] != "0"]

    # Filter by assignee — need full details for member info
    if args.assignee:
        target_oid = contacts.lookup(args.assignee)
        if target_oid:
            detailed = []
            for t in tasks:
                full = _fetch_task(api, t.get("guid", ""))
                if full and any(m.get("id") == target_oid
                                for m in full.get("members", [])):
                    detailed.append(full)
            tasks = detailed

    if not tasks:
        print("No tasks found.")
        return

    for t in tasks:
        guid = t.get("guid", "?")
        summary = t.get("summary", "(no title)")
        done = t.get("completed_at") and t["completed_at"] != "0"
        icon = "✅" if done else "⬜"

        parts = [f"{icon} {summary}"]
        if t.get("due") and t["due"].get("timestamp"):
            parts.append(f"due: {_ms_to_str(t['due']['timestamp'])}")
        parts.append(f"({guid[:8]})")
        print("  ".join(parts))


def cmd_get(args, api, cfg, contacts):
    task = _fetch_task(api, args.task_guid)
    if not task:
        print(f"ERROR: task {args.task_guid} not found", file=sys.stderr)
        sys.exit(1)

    print(f"GUID: {task.get('guid')}")
    print(f"Title: {task.get('summary')}")
    if task.get("description"):
        print(f"Description: {task['description']}")
    done = task.get("completed_at") and task["completed_at"] != "0"
    print(f"Status: {'done' if done else 'todo'}")
    if task.get("due"):
        print(f"Due: {_ms_to_str(task['due']['timestamp'])}")
    if done:
        print(f"Completed: {_ms_to_str(task['completed_at'])}")
    for m in task.get("members", []):
        role = m.get("role", "?")
        name = contacts.lookup_name(m.get("id", "")) or m.get("id", "?")
        print(f"  {role}: {name}")
    if task.get("url"):
        print(f"URL: {task['url']}")


def cmd_update(args, api, cfg, contacts):
    body = {}
    update_fields = []

    if args.title:
        body["summary"] = args.title
        update_fields.append("summary")
    if args.due:
        body["due"] = {"timestamp": _sec_to_ms(_parse_dt(args.due)),
                       "is_all_day": False}
        update_fields.append("due")
    if args.desc:
        body["description"] = args.desc
        update_fields.append("description")

    if not update_fields:
        print("Nothing to update.")
        return

    resp = api.patch(
        f"/open-apis/task/v2/tasks/{args.task_guid}",
        {"task": body, "update_fields": update_fields},
        params={"user_id_type": "open_id"},
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Updated: {args.task_guid}")


def cmd_complete(args, api, cfg, contacts):
    now_ms = str(int(datetime.now(TZ).timestamp() * 1000))
    resp = api.patch(
        f"/open-apis/task/v2/tasks/{args.task_guid}",
        {"task": {"completed_at": now_ms}, "update_fields": ["completed_at"]},
        params={"user_id_type": "open_id"},
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Completed: {args.task_guid}")


def cmd_delete(args, api, cfg, contacts):
    resp = api.delete(f"/open-apis/task/v2/tasks/{args.task_guid}")
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Deleted: {args.task_guid}")


def cmd_tasklist_create(args, api, cfg, contacts):
    resp = api.post("/open-apis/task/v2/tasklists", {"name": args.name})
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    tl = resp["data"]["tasklist"]
    print(f"Created tasklist: {tl['guid']}")
    print(f"  Name: {tl.get('name')}")
    print(f"  Add to config.yaml -> feishu.tasks.tasklist_guid: {tl['guid']}")


def cmd_tasklist_list(args, api, cfg, contacts):
    items = []
    page_token = None
    while True:
        params = {"page_size": "50"}
        if page_token:
            params["page_token"] = page_token
        resp = api.get("/open-apis/task/v2/tasklists", params=params)
        if resp.get("code") != 0:
            print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
            sys.exit(1)
        items.extend(resp.get("data", {}).get("items", []))
        if not resp.get("data", {}).get("has_more"):
            break
        page_token = resp["data"].get("page_token")

    if not items:
        print("No tasklists found.")
        return

    print(f"{'GUID':<40} Name")
    print("-" * 70)
    for tl in items:
        print(f"{tl.get('guid', '?'):<40} {tl.get('name', '?')}")


def cmd_snapshot(args, api, cfg, contacts):
    """Output overdue/upcoming task snapshot for heartbeat integration."""
    tasklist_guid = cfg.get("feishu", {}).get("tasks", {}).get("tasklist_guid")
    if not tasklist_guid:
        return  # silently skip — not configured

    window_hours = args.window_hours
    if window_hours is None:
        window_hours = cfg.get("heartbeat", {}).get("tasks", {}).get(
            "alert_window_hours", 2)

    # Fetch tasks from tasklist (summary-level, includes due + completed_at)
    tasks = _list_tasklist_tasks(api, tasklist_guid)
    if not tasks:
        return

    now_ms = int(datetime.now(TZ).timestamp() * 1000)
    window_ms = window_hours * 3600 * 1000

    overdue = []
    upcoming = []

    for t in tasks:
        # Skip completed
        if t.get("completed_at") and t["completed_at"] != "0":
            continue

        due = t.get("due")
        if not due or not due.get("timestamp"):
            continue

        due_ms = int(due["timestamp"])
        diff = due_ms - now_ms
        summary = t.get("summary", "?")

        if diff < 0:
            overdue.append(f'- "{summary}" (逾期 {_format_relative(-diff)})')
        elif diff < window_ms:
            upcoming.append(f'- "{summary}" ({_format_relative(diff)}后到期)')

    if not overdue and not upcoming:
        return

    lines = ["[任务快照]"]
    if overdue:
        lines.append(f"逾期 ({len(overdue)}):")
        lines.extend(overdue)
    if upcoming:
        lines.append(f"即将到期 ({len(upcoming)}):")
        lines.extend(upcoming)

    print("\n".join(lines))


# ── CLI ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Feishu Task CLI")
    sub = parser.add_subparsers(dest="group")

    # create
    cr = sub.add_parser("create")
    cr.add_argument("title")
    cr.add_argument("--assignee", help="Comma-separated assignee names")
    cr.add_argument("--due", help="Due time (ISO, HH:MM, +2h, tomorrow HH:MM)")
    cr.add_argument("--desc", help="Description")

    # list
    ls = sub.add_parser("list")
    ls.add_argument("--assignee", help="Filter by assignee name")
    ls.add_argument("--completed", action="store_true",
                    help="Show completed tasks (default: open only)")

    # get
    gt = sub.add_parser("get")
    gt.add_argument("task_guid")

    # update
    up = sub.add_parser("update")
    up.add_argument("task_guid")
    up.add_argument("--title")
    up.add_argument("--due")
    up.add_argument("--desc")

    # complete
    cp = sub.add_parser("complete")
    cp.add_argument("task_guid")

    # delete
    dl = sub.add_parser("delete")
    dl.add_argument("task_guid")

    # snapshot (heartbeat integration)
    sn = sub.add_parser("snapshot")
    sn.add_argument("--window-hours", type=int, default=None,
                    help="Alert window in hours (default: from config or 2)")

    # tasklist subcommands
    tl_p = sub.add_parser("tasklist")
    tl_sub = tl_p.add_subparsers(dest="action")
    tl_cr = tl_sub.add_parser("create")
    tl_cr.add_argument("name")
    tl_sub.add_parser("list")

    args = parser.parse_args()
    if not args.group:
        parser.print_help()
        sys.exit(1)

    cfg = _load_config()
    api = FeishuAPI.from_config()
    contacts = ContactStore(
        cfg.get("feishu", {}).get("contacts", {}).get("store_path")
    )

    dispatch = {
        ("create", None): cmd_create,
        ("list", None): cmd_list,
        ("get", None): cmd_get,
        ("update", None): cmd_update,
        ("complete", None): cmd_complete,
        ("delete", None): cmd_delete,
        ("snapshot", None): cmd_snapshot,
        ("tasklist", "create"): cmd_tasklist_create,
        ("tasklist", "list"): cmd_tasklist_list,
    }

    action = getattr(args, "action", None)
    handler = dispatch.get((args.group, action))
    if not handler:
        parser.print_help()
        sys.exit(1)

    handler(args, api, cfg, contacts)


if __name__ == "__main__":
    main()
