#!/usr/bin/env python3
"""Feishu Spreadsheet CLI — read/write cells in Feishu Sheets (电子表格).

Usage:
    sheet_ctl.py info <spreadsheet_token>
    sheet_ctl.py sheets <spreadsheet_token>
    sheet_ctl.py read <spreadsheet_token> <range>
    sheet_ctl.py write <spreadsheet_token> <range> --values JSON
"""

import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(BASE))

from agent.platforms.feishu.api import FeishuAPI  # noqa: E402


def _extract_token(s: str) -> str:
    """Extract spreadsheet_token from URL or raw token.

    Handles both direct sheet URLs (/sheets/TOKEN) and wiki-embedded
    sheets (/wiki/TOKEN). Wiki tokens are resolved to the actual
    spreadsheet obj_token via the wiki API.
    """
    # Direct sheet URL
    m = re.search(r"/sheets/([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    # Wiki-embedded sheet URL
    m = re.search(r"/wiki/([A-Za-z0-9_-]+)", s)
    if m:
        return _resolve_wiki_token(m.group(1))
    return s.strip()


def _resolve_wiki_token(node_token: str) -> str:
    """Resolve wiki node_token to actual spreadsheet obj_token."""
    api = FeishuAPI.from_config()
    resp = api.get("/open-apis/wiki/v2/spaces/get_node",
                   params={"token": node_token})
    if resp.get("code") != 0:
        print(f"ERROR resolving wiki token: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    node = resp.get("data", {}).get("node", {})
    if node.get("obj_type") != "sheet":
        print(f"ERROR: wiki node is type '{node.get('obj_type')}', not sheet",
              file=sys.stderr)
        sys.exit(1)
    return node["obj_token"]


# ── Commands ─────────────────────────────────────────────

def cmd_info(args, api: FeishuAPI):
    token = _extract_token(args.token)
    resp = api.get(f"/open-apis/sheets/v3/spreadsheets/{token}")
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    ss = resp.get("data", {}).get("spreadsheet", {})
    print(f"Token:    {ss.get('spreadsheet_token', '?')}")
    print(f"Title:    {ss.get('title', '(untitled)')}")
    print(f"Owner:    {ss.get('owner_id', '?')}")
    print(f"URL:      {ss.get('url', '')}")
    print(f"Revision: {ss.get('revision', '?')}")


def cmd_sheets(args, api: FeishuAPI):
    token = _extract_token(args.token)
    resp = api.get(f"/open-apis/sheets/v3/spreadsheets/{token}/sheets/query")
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    sheets = resp.get("data", {}).get("sheets", [])
    if not sheets:
        print("No worksheets found.")
        return
    print(f"{'Sheet ID':<20} {'Title':<30} {'Index':<8} {'Rows':<8} Cols")
    print("-" * 80)
    for s in sheets:
        sid = s.get("sheet_id", "?")
        title = s.get("title", "(unnamed)")
        idx = s.get("index", "?")
        grid = s.get("grid_properties", {})
        rows = grid.get("row_count", "?")
        cols = grid.get("column_count", "?")
        print(f"{sid:<20} {title:<30} {str(idx):<8} {str(rows):<8} {cols}")


def cmd_read(args, api: FeishuAPI):
    token = _extract_token(args.token)
    range_spec = args.range
    resp = api.get(
        f"/open-apis/sheets/v2/spreadsheets/{token}/values/{range_spec}",
        params={"valueRenderOption": "ToString"},
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    vr = resp.get("data", {}).get("valueRange", {})
    values = vr.get("values", [])
    if not values:
        print("(empty range)")
        return
    # Print as table
    for row in values:
        cells = [str(c) if c is not None else "" for c in row]
        print("\t".join(cells))
    print(f"\n({len(values)} rows)")


def cmd_write(args, api: FeishuAPI):
    token = _extract_token(args.token)
    range_spec = args.range
    values = json.loads(args.values)
    body = {
        "valueRange": {
            "range": range_spec,
            "values": values,
        }
    }
    resp = api.put(
        f"/open-apis/sheets/v2/spreadsheets/{token}/values",
        body=body,
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    data = resp.get("data", {})
    updated = data.get("updatedCells", data.get("updatedRows", "?"))
    print(f"Written: {updated} cells updated")


# ── CLI ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Feishu Spreadsheet CLI")
    sub = parser.add_subparsers(dest="command")

    p_info = sub.add_parser("info", help="Get spreadsheet metadata")
    p_info.add_argument("token", help="Spreadsheet token or URL")

    p_sheets = sub.add_parser("sheets", help="List worksheets")
    p_sheets.add_argument("token", help="Spreadsheet token or URL")

    p_read = sub.add_parser("read", help="Read cell range")
    p_read.add_argument("token", help="Spreadsheet token or URL")
    p_read.add_argument("range", help="Range like sheetId!A1:D10")

    p_write = sub.add_parser("write", help="Write cell range")
    p_write.add_argument("token", help="Spreadsheet token or URL")
    p_write.add_argument("range", help="Range like sheetId!A1:D10")
    p_write.add_argument("--values", required=True,
                         help="2D JSON array, e.g. '[[1,\"a\"],[2,\"b\"]]'")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    api = FeishuAPI.from_config()

    dispatch = {
        "info": cmd_info,
        "sheets": cmd_sheets,
        "read": cmd_read,
        "write": cmd_write,
    }
    dispatch[args.command](args, api)


if __name__ == "__main__":
    main()
