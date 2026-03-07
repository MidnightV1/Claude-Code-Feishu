#!/usr/bin/env python3
"""Feishu Bitable CLI — manage multidimensional tables and records.

Usage:
    bitable_ctl.py table list <app_token>
    bitable_ctl.py table fields <app_token> <table_id>
    bitable_ctl.py record list <app_token> <table_id> [--filter F] [--limit N]
    bitable_ctl.py record get <app_token> <table_id> <record_id>
    bitable_ctl.py record add <app_token> <table_id> --fields JSON
    bitable_ctl.py record update <app_token> <table_id> <record_id> --fields JSON
    bitable_ctl.py record delete <app_token> <table_id> <record_id>
"""

import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(BASE))

from agent.platforms.feishu.api import FeishuAPI  # noqa: E402


def _extract_app_token(s: str) -> str:
    """Extract app_token from bitable URL or raw token."""
    m = re.search(r"/base/([A-Za-z0-9]+)", s)
    return m.group(1) if m else s.strip()


def _extract_table_id(s: str) -> str:
    """Extract table_id from URL query or raw ID."""
    m = re.search(r"[?&]table=(tbl[A-Za-z0-9]+)", s)
    return m.group(1) if m else s.strip()


# ── API helpers ──────────────────────────────────────────

def _list_tables(api: FeishuAPI, app_token: str) -> list[dict]:
    tables = []
    page_token = None
    for _ in range(20):
        params = {"page_size": "100"}
        if page_token:
            params["page_token"] = page_token
        resp = api.get(f"/open-apis/bitable/v1/apps/{app_token}/tables",
                       params=params)
        if resp.get("code") != 0:
            print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
            sys.exit(1)
        for t in resp.get("data", {}).get("items", []):
            tables.append(t)
        if not resp.get("data", {}).get("has_more"):
            break
        page_token = resp["data"].get("page_token")
    return tables


def _list_fields(api: FeishuAPI, app_token: str, table_id: str) -> list[dict]:
    fields = []
    page_token = None
    for _ in range(20):
        params = {"page_size": "100"}
        if page_token:
            params["page_token"] = page_token
        resp = api.get(
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            params=params)
        if resp.get("code") != 0:
            print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
            sys.exit(1)
        for f in resp.get("data", {}).get("items", []):
            fields.append(f)
        if not resp.get("data", {}).get("has_more"):
            break
        page_token = resp["data"].get("page_token")
    return fields


def _list_records(api: FeishuAPI, app_token: str, table_id: str,
                  filter_expr: str = "", limit: int = 20) -> list[dict]:
    records = []
    page_token = None
    remaining = limit
    for _ in range(20):
        params = {"page_size": str(min(remaining, 500))}
        if page_token:
            params["page_token"] = page_token
        if filter_expr:
            params["filter"] = filter_expr
        resp = api.get(
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            params=params)
        if resp.get("code") != 0:
            print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
            sys.exit(1)
        for r in resp.get("data", {}).get("items", []):
            records.append(r)
            remaining -= 1
            if remaining <= 0:
                return records
        if not resp.get("data", {}).get("has_more"):
            break
        page_token = resp["data"].get("page_token")
    return records


# ── Field type display ───────────────────────────────────

_FIELD_TYPES = {
    1: "text", 2: "number", 3: "select", 4: "multi_select",
    5: "date", 7: "checkbox", 11: "person", 13: "phone",
    15: "url", 17: "attachment", 18: "link", 19: "formula",
    20: "duplex_link", 21: "location", 22: "group_chat",
    23: "created_time", 1001: "created_by", 1002: "modified_by",
    1003: "modified_time", 1004: "auto_number",
}


# ── Create helpers ───────────────────────────────────────

def _create_app(api: FeishuAPI, name: str, folder_token: str = "") -> dict:
    """Create a new Bitable app."""
    body = {"name": name}
    if folder_token:
        body["folder_token"] = folder_token
    resp = api.post("/open-apis/bitable/v1/apps", body)
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    return resp.get("data", {}).get("app", {})


def _create_table(api: FeishuAPI, app_token: str, name: str,
                  fields: list[dict]) -> dict:
    """Create a table in a Bitable app with specified fields."""
    body = {
        "table": {
            "name": name,
            "default_view_name": "默认视图",
            "fields": fields,
        }
    }
    resp = api.post(f"/open-apis/bitable/v1/apps/{app_token}/tables", body)
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    return resp.get("data", {})


# ── Commands ─────────────────────────────────────────────

def cmd_app_create(args, api):
    app = _create_app(api, args.name, getattr(args, "folder_token", "") or "")
    token = app.get("app_token", "?")
    url = app.get("url", "")
    print(f"app_token: {token}")
    if url:
        print(f"url: {url}")


def cmd_table_create(args, api):
    app_token = _extract_app_token(args.app_token)
    fields = json.loads(args.fields)
    data = _create_table(api, app_token, args.name, fields)
    table_id = data.get("table_id", "?")
    print(f"table_id: {table_id}")


def cmd_table_list(args, api):
    app_token = _extract_app_token(args.app_token)
    tables = _list_tables(api, app_token)
    if not tables:
        print("No tables found.")
        return
    print(f"{'Table ID':<25} {'Name':<30} Revision")
    print("-" * 65)
    for t in tables:
        tid = t.get("table_id", "?")
        name = t.get("name", "(unnamed)")
        rev = t.get("revision", "?")
        print(f"{tid:<25} {name:<30} {rev}")


def cmd_table_fields(args, api):
    app_token = _extract_app_token(args.app_token)
    table_id = _extract_table_id(args.table_id)
    fields = _list_fields(api, app_token, table_id)
    if not fields:
        print("No fields found.")
        return
    print(f"{'Field Name':<25} {'Type':<15} {'Field ID'}")
    print("-" * 65)
    for f in fields:
        name = f.get("field_name", "?")
        ftype = _FIELD_TYPES.get(f.get("type", 0), str(f.get("type", "?")))
        fid = f.get("field_id", "?")
        print(f"{name:<25} {ftype:<15} {fid}")


def cmd_record_list(args, api):
    app_token = _extract_app_token(args.app_token)
    table_id = _extract_table_id(args.table_id)
    limit = args.limit or 20
    filter_expr = args.filter or ""

    records = _list_records(api, app_token, table_id, filter_expr, limit)
    if not records:
        print("No records found.")
        return

    print(f"{len(records)} record(s):")
    for r in records:
        rid = r.get("record_id", "?")
        fields = r.get("fields", {})
        # Show first 3 fields as preview
        preview_parts = []
        for k, v in list(fields.items())[:3]:
            if isinstance(v, list):
                v = f"[{len(v)} items]"
            elif isinstance(v, dict):
                v = v.get("text", v.get("link", str(v)[:30]))
            preview_parts.append(f"{k}={v}")
        preview = ", ".join(preview_parts)
        print(f"  {rid}  {preview}")


def cmd_record_get(args, api):
    app_token = _extract_app_token(args.app_token)
    table_id = _extract_table_id(args.table_id)

    resp = api.get(
        f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{args.record_id}")
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    record = resp.get("data", {}).get("record", {})
    print(f"Record: {record.get('record_id', '?')}")
    for k, v in record.get("fields", {}).items():
        print(f"  {k}: {json.dumps(v, ensure_ascii=False)}")


def cmd_record_add(args, api):
    app_token = _extract_app_token(args.app_token)
    table_id = _extract_table_id(args.table_id)
    fields = json.loads(args.fields)

    resp = api.post(
        f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
        {"fields": fields})
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    record = resp.get("data", {}).get("record", {})
    print(f"Created: {record.get('record_id', '?')}")


def cmd_record_update(args, api):
    app_token = _extract_app_token(args.app_token)
    table_id = _extract_table_id(args.table_id)
    fields = json.loads(args.fields)

    resp = api.patch(
        f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{args.record_id}",
        {"fields": fields})
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Updated: {args.record_id}")


def cmd_record_delete(args, api):
    app_token = _extract_app_token(args.app_token)
    table_id = _extract_table_id(args.table_id)

    resp = api.delete(
        f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{args.record_id}")
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Deleted: {args.record_id}")


# ── CLI ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Feishu Bitable CLI")
    sub = parser.add_subparsers(dest="group")

    # app commands
    app = sub.add_parser("app")
    app_sub = app.add_subparsers(dest="action")

    ac = app_sub.add_parser("create")
    ac.add_argument("name", help="Bitable app name")
    ac.add_argument("--folder-token", default="", help="Optional folder token")

    # table commands
    tbl = sub.add_parser("table")
    tbl_sub = tbl.add_subparsers(dest="action")

    tc = tbl_sub.add_parser("create")
    tc.add_argument("app_token")
    tc.add_argument("name", help="Table name")
    tc.add_argument("fields", help="JSON array of field definitions")

    tl = tbl_sub.add_parser("list")
    tl.add_argument("app_token", help="Bitable app token or URL")

    tf = tbl_sub.add_parser("fields")
    tf.add_argument("app_token")
    tf.add_argument("table_id", help="Table ID (tblXXX) or URL")

    # record commands
    rec = sub.add_parser("record")
    rec_sub = rec.add_subparsers(dest="action")

    rl = rec_sub.add_parser("list")
    rl.add_argument("app_token")
    rl.add_argument("table_id")
    rl.add_argument("--filter", help="Filter formula")
    rl.add_argument("--limit", type=int, default=20, help="Max records (default 20)")

    rg = rec_sub.add_parser("get")
    rg.add_argument("app_token")
    rg.add_argument("table_id")
    rg.add_argument("record_id")

    ra = rec_sub.add_parser("add")
    ra.add_argument("app_token")
    ra.add_argument("table_id")
    ra.add_argument("--fields", required=True, help="JSON field values")

    ru = rec_sub.add_parser("update")
    ru.add_argument("app_token")
    ru.add_argument("table_id")
    ru.add_argument("record_id")
    ru.add_argument("--fields", required=True, help="JSON field values")

    rd = rec_sub.add_parser("delete")
    rd.add_argument("app_token")
    rd.add_argument("table_id")
    rd.add_argument("record_id")

    args = parser.parse_args()
    if not args.group:
        parser.print_help()
        sys.exit(1)

    api = FeishuAPI.from_config()

    dispatch = {
        ("app", "create"): cmd_app_create,
        ("table", "create"): cmd_table_create,
        ("table", "list"): cmd_table_list,
        ("table", "fields"): cmd_table_fields,
        ("record", "list"): cmd_record_list,
        ("record", "get"): cmd_record_get,
        ("record", "add"): cmd_record_add,
        ("record", "update"): cmd_record_update,
        ("record", "delete"): cmd_record_delete,
    }

    action = getattr(args, "action", None)
    handler = dispatch.get((args.group, action))
    if not handler:
        parser.print_help()
        sys.exit(1)

    handler(args, api)


if __name__ == "__main__":
    main()
