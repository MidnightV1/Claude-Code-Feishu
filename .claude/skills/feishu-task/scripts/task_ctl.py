#!/usr/bin/env python3
"""Feishu Task CLI — CRUD tasks, sections, and tasklists via Feishu Task v2 API.

Usage:
    task_ctl.py create "title" [--assignee "a,b"] [--due "time"] [--desc "..."] [--section "name"]
    task_ctl.py list [--assignee "name"] [--completed]
    task_ctl.py get <task_guid>
    task_ctl.py update <task_guid> [--title T] [--due D] [--desc D]
    task_ctl.py complete <task_guid>
    task_ctl.py assign <task_guid> "a,b"
    task_ctl.py unassign <task_guid> "a,b"
    task_ctl.py delete <task_guid>
    task_ctl.py move <task_guid> "section_name"
    task_ctl.py section create "name"
    task_ctl.py section list
    task_ctl.py section delete <section_guid>
    task_ctl.py tasklist create "name"
    task_ctl.py tasklist list
    task_ctl.py tasklist add-member "a,b" [--role editor|viewer]
    task_ctl.py tasklist remove-member "a,b"
    task_ctl.py snapshot [--window-hours N]
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Project root: .claude/skills/feishu-task/scripts -> project root
BASE = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(BASE))

from agent.platforms.feishu.api import FeishuAPI, ContactStore  # noqa: E402
from agent.platforms.feishu.utils import parse_dt  # noqa: E402

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai


def _load_config():
    import yaml
    with open(BASE / "config.yaml") as f:
        return yaml.safe_load(f)


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


def _list_sections(api: FeishuAPI, tasklist_guid: str) -> list[dict]:
    """List sections in a tasklist."""
    items = []
    page_token = None
    for _ in range(10):
        params = {"page_size": "50", "resource_type": "tasklist",
                  "resource_id": tasklist_guid}
        if page_token:
            params["page_token"] = page_token
        resp = api.get("/open-apis/task/v2/sections", params=params)
        if resp.get("code") != 0:
            break
        items.extend(resp.get("data", {}).get("items", []))
        if not resp.get("data", {}).get("has_more"):
            break
        page_token = resp["data"].get("page_token")
    return items


def _resolve_section_guid(api: FeishuAPI, tasklist_guid: str,
                           name: str) -> str | None:
    """Look up section GUID by name. Returns None if not found."""
    sections = _list_sections(api, tasklist_guid)
    for s in sections:
        if s.get("name") == name:
            return s.get("guid")
    return None


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


def _get_assignees(api: FeishuAPI, task_guid: str,
                    contacts: ContactStore) -> str:
    """Fetch task details and return assignee names string."""
    try:
        full = _fetch_task(api, task_guid)
    except Exception:
        return "未知"
    if not full:
        return "未知"
    names = []
    for m in full.get("members", []):
        if m.get("role") == "assignee":
            name = contacts.lookup_name(m.get("id", "")) or m.get("id", "?")[:8]
            names.append(name)
    return ", ".join(names) if names else "未指派"


# ── Commands ─────────────────────────────────────────────

def cmd_create(args, api, cfg, contacts):
    body = {"summary": args.title}
    if args.desc:
        body["description"] = args.desc
    if args.due:
        body["due"] = {"timestamp": _sec_to_ms(parse_dt(args.due)),
                       "is_all_day": False}
    if args.assignee:
        body["members"] = _resolve_members(args.assignee, contacts)

    # Auto-add to configured tasklist (with optional section)
    tasklist_guid = cfg.get("feishu", {}).get("tasks", {}).get("tasklist_guid")
    if tasklist_guid:
        tl_entry = {"tasklist_guid": tasklist_guid}
        if args.section:
            section_guid = _resolve_section_guid(api, tasklist_guid, args.section)
            if not section_guid:
                print(f"ERROR: section '{args.section}' not found", file=sys.stderr)
                sys.exit(1)
            tl_entry["section_guid"] = section_guid
        body["tasklists"] = [tl_entry]

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
        parts.append(f"({guid})")
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
        body["due"] = {"timestamp": _sec_to_ms(parse_dt(args.due)),
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


def cmd_assign(args, api, cfg, contacts):
    members = _resolve_members(args.names, contacts)
    if not members:
        print("ERROR: no valid contacts resolved", file=sys.stderr)
        sys.exit(1)
    resp = api.post(
        f"/open-apis/task/v2/tasks/{args.task_guid}/add_members",
        {"members": members},
        params={"user_id_type": "open_id"},
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Assigned {', '.join(args.names.split(','))} to {args.task_guid[:8]}")


def cmd_unassign(args, api, cfg, contacts):
    members = _resolve_members(args.names, contacts)
    if not members:
        print("ERROR: no valid contacts resolved", file=sys.stderr)
        sys.exit(1)
    resp = api.post(
        f"/open-apis/task/v2/tasks/{args.task_guid}/remove_members",
        {"members": members},
        params={"user_id_type": "open_id"},
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Unassigned {', '.join(args.names.split(','))} from {args.task_guid[:8]}")


def cmd_delete(args, api, cfg, contacts):
    resp = api.delete(f"/open-apis/task/v2/tasks/{args.task_guid}")
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Deleted: {args.task_guid}")


def cmd_move(args, api, cfg, contacts):
    tasklist_guid = _get_tasklist_guid(cfg)
    # "default" moves to the default (unnamed) section
    if args.section.lower() == "default":
        sections = _list_sections(api, tasklist_guid)
        section_guid = next(
            (s["guid"] for s in sections if s.get("is_default")), None)
    else:
        section_guid = _resolve_section_guid(api, tasklist_guid, args.section)
    if not section_guid:
        print(f"ERROR: section '{args.section}' not found", file=sys.stderr)
        sys.exit(1)
    resp = api.post(
        f"/open-apis/task/v2/tasks/{args.task_guid}/add_tasklist",
        {"tasklist_guid": tasklist_guid, "section_guid": section_guid},
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Moved {args.task_guid[:8]} to section '{args.section}'")


def cmd_section_create(args, api, cfg, contacts):
    tasklist_guid = _get_tasklist_guid(cfg)
    resp = api.post("/open-apis/task/v2/sections", {
        "name": args.name,
        "resource_type": "tasklist",
        "resource_id": tasklist_guid,
    })
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    sec = resp["data"]["section"]
    print(f"Created section: {sec['guid']}")
    print(f"  Name: {sec.get('name')}")


def cmd_section_list(args, api, cfg, contacts):
    tasklist_guid = _get_tasklist_guid(cfg)
    sections = _list_sections(api, tasklist_guid)
    if not sections:
        print("No sections found.")
        return
    print(f"{'GUID':<40} {'Default':<9} Name")
    print("-" * 70)
    for s in sections:
        default = "✓" if s.get("is_default") else ""
        name = s.get("name") or "(default)"
        print(f"{s.get('guid', '?'):<40} {default:<9} {name}")


def cmd_section_delete(args, api, cfg, contacts):
    resp = api.delete(f"/open-apis/task/v2/sections/{args.section_guid}")
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Deleted section: {args.section_guid}")


def cmd_tasklist_create(args, api, cfg, contacts):
    resp = api.post("/open-apis/task/v2/tasklists", {"name": args.name})
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    tl = resp["data"]["tasklist"]
    print(f"Created tasklist: {tl['guid']}")
    print(f"  Name: {tl.get('name')}")
    print(f"  Add to config.yaml -> feishu.tasks.tasklist_guid: {tl['guid']}")


def cmd_tasklist_add_member(args, api, cfg, contacts):
    tasklist_guid = _get_tasklist_guid(cfg)
    members = _resolve_members(args.names, contacts)
    if not members:
        print("ERROR: no valid contacts resolved", file=sys.stderr)
        sys.exit(1)
    # Override role to the specified one (default: editor)
    for m in members:
        m["role"] = args.role
    resp = api.post(
        f"/open-apis/task/v2/tasklists/{tasklist_guid}/add_members",
        {"members": members},
        params={"user_id_type": "open_id"},
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    names_str = ", ".join(args.names.split(","))
    print(f"Added {names_str} as {args.role} to tasklist {tasklist_guid[:8]}")


def cmd_tasklist_remove_member(args, api, cfg, contacts):
    tasklist_guid = _get_tasklist_guid(cfg)
    members = _resolve_members(args.names, contacts)
    if not members:
        print("ERROR: no valid contacts resolved", file=sys.stderr)
        sys.exit(1)
    resp = api.post(
        f"/open-apis/task/v2/tasklists/{tasklist_guid}/remove_members",
        {"members": members},
        params={"user_id_type": "open_id"},
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    names_str = ", ".join(args.names.split(","))
    print(f"Removed {names_str} from tasklist {tasklist_guid[:8]}")


def cmd_tasklist_list(args, api, cfg, contacts):
    items = []
    page_token = None
    for _ in range(10):
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
    """Output task snapshot for heartbeat integration.

    Includes: overdue, upcoming (within window), and open tasks without due date.
    Each task shows assignee info for actionable notifications.
    """
    tasklist_guid = cfg.get("feishu", {}).get("tasks", {}).get("tasklist_guid")
    if not tasklist_guid:
        return  # silently skip — not configured

    window_hours = args.window_hours
    if window_hours is None:
        window_hours = cfg.get("heartbeat", {}).get("alert_window_hours",
                       cfg.get("heartbeat", {}).get("tasks", {}).get(
                           "alert_window_hours", 2))

    tasks = _list_tasklist_tasks(api, tasklist_guid)
    if not tasks:
        return

    now = datetime.now(TZ)
    now_ms = int(now.timestamp() * 1000)
    window_ms = window_hours * 3600 * 1000

    overdue = []
    upcoming = []
    open_no_due = []

    for t in tasks:
        if t.get("completed_at") and t["completed_at"] != "0":
            continue

        guid = t.get("guid", "")
        summary = t.get("summary", "?")
        assignee = _get_assignees(api, guid, contacts)

        due = t.get("due")
        if not due or not due.get("timestamp"):
            open_no_due.append(f'- "{summary}" | 负责人: {assignee}')
            continue

        due_ms = int(due["timestamp"])
        diff = due_ms - now_ms

        if diff < 0:
            overdue.append(
                f'- "{summary}" | 逾期 {_format_relative(-diff)} | 负责人: {assignee}')
        elif diff < window_ms:
            upcoming.append(
                f'- "{summary}" | {_format_relative(diff)}后到期 | 负责人: {assignee}')

    if not overdue and not upcoming and not open_no_due:
        return

    lines = [f"[任务快照] {now.strftime('%Y-%m-%d %H:%M %A')}"]
    if overdue:
        lines.append(f"逾期 ({len(overdue)}):")
        lines.extend(overdue)
    if upcoming:
        lines.append(f"即将到期 ({len(upcoming)}):")
        lines.extend(upcoming)
    if open_no_due:
        lines.append(f"进行中 ({len(open_no_due)}):")
        lines.extend(open_no_due)

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
    cr.add_argument("--section", help="Section name to place task in")

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

    # assign / unassign
    asg = sub.add_parser("assign")
    asg.add_argument("task_guid")
    asg.add_argument("names", help="Comma-separated assignee names")

    uasg = sub.add_parser("unassign")
    uasg.add_argument("task_guid")
    uasg.add_argument("names", help="Comma-separated names to remove")

    # delete
    dl = sub.add_parser("delete")
    dl.add_argument("task_guid")

    # move to section
    mv = sub.add_parser("move")
    mv.add_argument("task_guid")
    mv.add_argument("section", help="Section name to move task to")

    # snapshot (heartbeat integration)
    sn = sub.add_parser("snapshot")
    sn.add_argument("--window-hours", type=int, default=None,
                    help="Alert window in hours (default: from config or 2)")

    # section subcommands
    sec_p = sub.add_parser("section")
    sec_sub = sec_p.add_subparsers(dest="action")
    sec_cr = sec_sub.add_parser("create")
    sec_cr.add_argument("name")
    sec_sub.add_parser("list")
    sec_dl = sec_sub.add_parser("delete")
    sec_dl.add_argument("section_guid")

    # tasklist subcommands
    tl_p = sub.add_parser("tasklist")
    tl_sub = tl_p.add_subparsers(dest="action")
    tl_cr = tl_sub.add_parser("create")
    tl_cr.add_argument("name")
    tl_sub.add_parser("list")
    tl_am = tl_sub.add_parser("add-member")
    tl_am.add_argument("names", help="Comma-separated member names")
    tl_am.add_argument("--role", default="editor",
                       choices=["editor", "viewer"],
                       help="Member role (default: editor)")
    tl_rm = tl_sub.add_parser("remove-member")
    tl_rm.add_argument("names", help="Comma-separated member names")

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
        ("assign", None): cmd_assign,
        ("unassign", None): cmd_unassign,
        ("delete", None): cmd_delete,
        ("move", None): cmd_move,
        ("snapshot", None): cmd_snapshot,
        ("section", "create"): cmd_section_create,
        ("section", "list"): cmd_section_list,
        ("section", "delete"): cmd_section_delete,
        ("tasklist", "create"): cmd_tasklist_create,
        ("tasklist", "list"): cmd_tasklist_list,
        ("tasklist", "add-member"): cmd_tasklist_add_member,
        ("tasklist", "remove-member"): cmd_tasklist_remove_member,
    }

    action = getattr(args, "action", None)
    handler = dispatch.get((args.group, action))
    if not handler:
        parser.print_help()
        sys.exit(1)

    handler(args, api, cfg, contacts)


if __name__ == "__main__":
    main()
