#!/usr/bin/env python3
"""Feishu Calendar CLI — manage events and contacts.

Usage:
    cal_ctl.py event create "title" "start" "end" [--attendees "a,b"] [--desc "..."] [--remind N]
    cal_ctl.py event list [--date YYYY-MM-DD] [--days N]
    cal_ctl.py event update <event_id> [--title T] [--start S] [--end E] [--desc D]
    cal_ctl.py event delete <event_id>
    cal_ctl.py freebusy <date> [--days N]
    cal_ctl.py contact add "name" <open_id>
    cal_ctl.py contact list
    cal_ctl.py contact remove "name"
    cal_ctl.py contact sync
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Project root: .claude/skills/feishu-cal/scripts -> project root
BASE = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(BASE))

from agent.platforms.feishu.api import FeishuAPI, ContactStore  # noqa: E402
from agent.platforms.feishu.utils import parse_dt  # noqa: E402

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai


def _load_config():
    import yaml
    with open(BASE / "config.yaml") as f:
        return yaml.safe_load(f)


def _safe_post(api: FeishuAPI, path: str, body: dict | None = None,
               params: dict | None = None) -> dict:
    """POST with graceful error handling — returns JSON even on HTTP errors."""
    import requests as _req
    try:
        return api.post(path, body, params)
    except _req.exceptions.HTTPError as e:
        # Try to extract JSON error body from Feishu
        try:
            return e.response.json()
        except Exception:
            return {"code": -1, "msg": str(e)}


def _safe_patch(api: FeishuAPI, path: str, body: dict | None = None,
                params: dict | None = None) -> dict:
    """PATCH with graceful error handling."""
    import requests as _req
    try:
        return api.patch(path, body, params)
    except _req.exceptions.HTTPError as e:
        try:
            return e.response.json()
        except Exception:
            return {"code": -1, "msg": str(e)}


def _get_calendar_id(api: FeishuAPI, cfg: dict) -> str:
    cal_cfg = cfg.get("feishu", {}).get("calendar", {})
    cal_id = cal_cfg.get("calendar_id", "auto")
    if cal_id and cal_id != "auto":
        return cal_id
    # auto-discover: first shared calendar owned by the app
    resp = api.get("/open-apis/calendar/v4/calendars")
    for cal in resp.get("data", {}).get("calendar_list", []):
        if cal.get("type") == "shared" and cal.get("role") == "owner":
            return cal["calendar_id"]
    # fallback to primary
    for cal in resp.get("data", {}).get("calendar_list", []):
        if cal.get("type") == "primary":
            return cal["calendar_id"]
    print("ERROR: No calendar found", file=sys.stderr)
    sys.exit(1)


def _ts_to_str(ts: str | int) -> str:
    """Unix timestamp (string or int) to readable datetime."""
    return datetime.fromtimestamp(int(ts), TZ).strftime("%Y-%m-%d %H:%M")


def _resolve_attendees(names: str, contacts: ContactStore) -> list[dict]:
    """Resolve comma-separated names to attendee list."""
    attendees = []
    for name in names.split(","):
        name = name.strip()
        if not name:
            continue
        oid = contacts.lookup(name)
        if not oid:
            print(f"WARNING: Contact '{name}' not found, skipping", file=sys.stderr)
            continue
        attendees.append({"type": "user", "user_id": oid})
    return attendees


# ── Commands ─────────────────────────────────────────────

def _create_event(api: FeishuAPI, cal_id: str, body: dict) -> dict:
    """Create event with fallback to 'primary' calendar."""
    params = {"user_id_type": "open_id"}
    resp = _safe_post(api, f"/open-apis/calendar/v4/calendars/{cal_id}/events",
                      body, params)
    if resp.get("code") != 0 and cal_id != "primary":
        print(f"  Calendar {cal_id} failed ({resp.get('msg')}), retrying on primary...",
              file=sys.stderr)
        resp = _safe_post(api, "/open-apis/calendar/v4/calendars/primary/events",
                          body, params)
    return resp


def cmd_event_create(args, api, cal_id, contacts):
    body = {
        "summary": args.title,
        "start_time": {"timestamp": str(parse_dt(args.start)), "timezone": "Asia/Shanghai"},
        "end_time": {"timestamp": str(parse_dt(args.end)), "timezone": "Asia/Shanghai"},
        "need_notification": True,
    }
    if args.desc:
        body["description"] = args.desc
    if args.remind is not None:
        body["reminders"] = [{"minutes": args.remind}]

    resp = _create_event(api, cal_id, body)
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    event = resp["data"]["event"]
    event_id = event["event_id"]
    actual_cal_id = event.get("calendar_id", cal_id)
    print(f"Created: {event_id}")
    print(f"  Title: {event.get('summary')}")
    print(f"  Start: {_ts_to_str(event['start_time']['timestamp'])}")
    print(f"  End:   {_ts_to_str(event['end_time']['timestamp'])}")

    # Add attendees
    if args.attendees:
        att_list = _resolve_attendees(args.attendees, contacts)
        if att_list:
            resp2 = _safe_post(
                api,
                f"/open-apis/calendar/v4/calendars/{actual_cal_id}/events/{event_id}/attendees",
                {"attendees": att_list, "need_notification": True},
                params={"user_id_type": "open_id"},
            )
            if resp2.get("code") == 0:
                names = [a.get("display_name", "?") for a in resp2["data"].get("attendees", [])]
                print(f"  Attendees: {', '.join(names)}")
                # auto-learn names
                for a in resp2["data"].get("attendees", []):
                    dn = a.get("display_name")
                    uid = a.get("user_id")
                    if dn and uid and not contacts.lookup_name(uid):
                        contacts.add(dn, uid, source="calendar_attendee")
            else:
                print(f"  Attendees error: {resp2.get('msg')}", file=sys.stderr)


def cmd_event_list(args, api, cal_id, contacts):
    if args.date:
        base = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=TZ)
    else:
        base = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)

    days = args.days or 7
    start_ts = int(base.timestamp())
    end_ts = int((base + timedelta(days=days)).timestamp())

    resp = api.get(
        f"/open-apis/calendar/v4/calendars/{cal_id}/events",
        params={"start_time": str(start_ts), "end_time": str(end_ts), "page_size": "50"},
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    events = resp.get("data", {}).get("items", [])
    active = [e for e in events if e.get("status") != "cancelled"]
    if not active:
        print("No events found.")
        return

    print(f"{'Start':<18} {'End':<18} {'ID':<44} Title")
    print("-" * 110)
    for ev in sorted(active, key=lambda e: e.get("start_time", {}).get("timestamp", "0")):
        s = _ts_to_str(ev["start_time"]["timestamp"])
        e = _ts_to_str(ev["end_time"]["timestamp"])
        eid = ev.get("event_id", "?")
        title = ev.get("summary") or "(no title)"
        print(f"{s:<18} {e:<18} {eid:<44} {title}")


def cmd_event_update(args, api, cal_id, contacts):
    body = {}
    if args.title:
        body["summary"] = args.title
    if args.start:
        body["start_time"] = {"timestamp": str(parse_dt(args.start)), "timezone": "Asia/Shanghai"}
    if args.end:
        body["end_time"] = {"timestamp": str(parse_dt(args.end)), "timezone": "Asia/Shanghai"}
    if args.desc:
        body["description"] = args.desc

    if not body:
        print("Nothing to update.")
        return

    resp = _safe_patch(
        api,
        f"/open-apis/calendar/v4/calendars/{cal_id}/events/{args.event_id}",
        body,
        params={"user_id_type": "open_id"},
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Updated: {args.event_id}")


def cmd_event_delete(args, api, cal_id, contacts):
    import requests as _req
    try:
        resp = api.delete(
            f"/open-apis/calendar/v4/calendars/{cal_id}/events/{args.event_id}",
            params={"user_id_type": "open_id"},
        )
    except _req.exceptions.HTTPError as e:
        try:
            resp = e.response.json()
        except Exception:
            resp = {"code": -1, "msg": str(e)}
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Deleted: {args.event_id}")


def cmd_freebusy(args, api, cal_id, contacts):
    base = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=TZ)
    days = args.days or 1
    end = base + timedelta(days=days)

    resp = _safe_post(
        api,
        "/open-apis/calendar/v4/freebusy/list",
        {
            "time_min": base.strftime("%Y-%m-%dT00:00:00+08:00"),
            "time_max": end.strftime("%Y-%m-%dT00:00:00+08:00"),
            "user_id": args.user_id or "",
        },
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        # Freebusy may require user_access_token, fallback to event list
        print("Falling back to event list...", file=sys.stderr)
        args.date = args.date
        args.days = days
        cmd_event_list(args, api, cal_id, contacts)
        return

    for block in resp.get("data", {}).get("freebusy_list", []):
        s = block.get("start_time", "")
        e = block.get("end_time", "")
        print(f"  Busy: {s} → {e}")


def cmd_contact_add(args, api, cal_id, contacts):
    contacts.add(args.name, args.open_id, source="manual")
    print(f"Added: {args.name} → {args.open_id}")


def cmd_contact_list(args, api, cal_id, contacts):
    all_contacts = contacts.list_all()
    if not all_contacts:
        print("No contacts stored.")
        return
    print(f"{'Name':<20} {'Open ID':<45} Source")
    print("-" * 80)
    for name, info in sorted(all_contacts.items()):
        print(f"{name:<20} {info['open_id']:<45} {info.get('source', '?')}")


def cmd_contact_remove(args, api, cal_id, contacts):
    if contacts.remove(args.name):
        print(f"Removed: {args.name}")
    else:
        print(f"Not found: {args.name}")


def cmd_contact_sync(args, api, cal_id, contacts):
    """Learn contacts from recent calendar events (attendees)."""
    now = datetime.now(TZ)
    start_ts = int((now - timedelta(days=30)).timestamp())
    end_ts = int((now + timedelta(days=30)).timestamp())

    resp = api.get(
        f"/open-apis/calendar/v4/calendars/{cal_id}/events",
        params={"start_time": str(start_ts), "end_time": str(end_ts), "page_size": "50"},
    )
    events = resp.get("data", {}).get("items", [])
    learned = 0
    for ev in events:
        eid = ev.get("event_id", "")
        att_resp = api.get(
            f"/open-apis/calendar/v4/calendars/{cal_id}/events/{eid}/attendees",
            params={"user_id_type": "open_id"},
        )
        for a in att_resp.get("data", {}).get("items", []):
            dn = a.get("display_name")
            uid = a.get("user_id")
            if dn and uid and not contacts.lookup(dn):
                contacts.add(dn, uid, source="calendar_sync")
                learned += 1
                print(f"  Learned: {dn} → {uid}")
    print(f"Sync complete. {learned} new contacts.")


# ── CLI ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Feishu Calendar CLI")
    sub = parser.add_subparsers(dest="group")

    # event subcommands
    event_p = sub.add_parser("event")
    event_sub = event_p.add_subparsers(dest="action")

    cr = event_sub.add_parser("create")
    cr.add_argument("title")
    cr.add_argument("start", help="Start time (ISO, HH:MM, +2h, tomorrow HH:MM)")
    cr.add_argument("end", help="End time")
    cr.add_argument("--attendees", help="Comma-separated names")
    cr.add_argument("--desc", help="Description")
    cr.add_argument("--remind", type=int, help="Reminder minutes before")

    ls = event_sub.add_parser("list")
    ls.add_argument("--date", help="Start date YYYY-MM-DD (default: today)")
    ls.add_argument("--days", type=int, help="Number of days (default: 7)")

    up = event_sub.add_parser("update")
    up.add_argument("event_id")
    up.add_argument("--title")
    up.add_argument("--start")
    up.add_argument("--end")
    up.add_argument("--desc")

    dl = event_sub.add_parser("delete")
    dl.add_argument("event_id")

    # freebusy
    fb = sub.add_parser("freebusy")
    fb.add_argument("date", help="Date YYYY-MM-DD")
    fb.add_argument("--days", type=int, help="Number of days (default: 1)")
    fb.add_argument("--user-id", help="User open_id (optional)")

    # contact subcommands
    contact_p = sub.add_parser("contact")
    contact_sub = contact_p.add_subparsers(dest="action")

    ca = contact_sub.add_parser("add")
    ca.add_argument("name")
    ca.add_argument("open_id")

    contact_sub.add_parser("list")

    crm = contact_sub.add_parser("remove")
    crm.add_argument("name")

    contact_sub.add_parser("sync")

    args = parser.parse_args()
    if not args.group:
        parser.print_help()
        sys.exit(1)

    cfg = _load_config()
    api = FeishuAPI.from_config()
    cal_id = _get_calendar_id(api, cfg)
    contacts = ContactStore(
        cfg.get("feishu", {}).get("contacts", {}).get("store_path")
    )

    dispatch = {
        ("event", "create"): cmd_event_create,
        ("event", "list"): cmd_event_list,
        ("event", "update"): cmd_event_update,
        ("event", "delete"): cmd_event_delete,
        ("freebusy", None): cmd_freebusy,
        ("contact", "add"): cmd_contact_add,
        ("contact", "list"): cmd_contact_list,
        ("contact", "remove"): cmd_contact_remove,
        ("contact", "sync"): cmd_contact_sync,
    }

    action = getattr(args, "action", None)
    handler = dispatch.get((args.group, action))
    if not handler:
        parser.print_help()
        sys.exit(1)

    handler(args, api, cal_id, contacts)


if __name__ == "__main__":
    main()
