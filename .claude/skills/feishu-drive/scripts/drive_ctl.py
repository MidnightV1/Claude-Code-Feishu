#!/usr/bin/env python3
"""Feishu Drive CLI — manage cloud files and folders.

Usage:
    drive_ctl.py list [--folder TOKEN] [--type TYPE] [--limit N]
    drive_ctl.py info <token_or_url>
    drive_ctl.py mkdir "name" [--parent TOKEN]
    drive_ctl.py move <file_token> <dest_folder> --type TYPE
    drive_ctl.py delete <file_token> --type TYPE
    drive_ctl.py search "query" [--type TYPE] [--limit N]
"""

import argparse
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(BASE))

from agent.platforms.feishu.api import FeishuAPI  # noqa: E402


def _extract_token(s: str) -> str:
    """Extract file/folder token from Drive URL or raw token."""
    # https://xxx.feishu.cn/drive/folder/AbCdEfG
    m = re.search(r"/(?:drive|docs|sheets|base|docx|wiki)/(?:folder/)?([A-Za-z0-9]+)", s)
    return m.group(1) if m else s.strip()


_TYPE_DISPLAY = {
    "docx": "docx", "doc": "doc", "sheet": "sheet", "bitable": "bitable",
    "folder": "folder", "file": "file", "mindnote": "mind",
    "slides": "slides",
}


# ── Commands ─────────────────────────────────────────────

def cmd_list(args, api):
    folder_token = args.folder or ""
    params = {"page_size": str(args.limit or 20)}
    if folder_token:
        params["folder_token"] = folder_token

    resp = api.get("/open-apis/drive/v1/files", params=params)
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    files = resp.get("data", {}).get("files", [])
    if args.type:
        files = [f for f in files if f.get("type") == args.type]

    if not files:
        print("No files found.")
        return

    print(f"{'Type':<10} {'Name':<40} {'Token':<30} Modified")
    print("-" * 100)
    for f in files:
        ftype = _TYPE_DISPLAY.get(f.get("type", ""), f.get("type", "?"))
        name = f.get("name", "(unnamed)")[:38]
        token = f.get("token", "?")
        modified = f.get("modified_time", "?")
        if isinstance(modified, (int, float)) and modified > 1e9:
            from datetime import datetime, timezone, timedelta
            modified = datetime.fromtimestamp(
                modified, timezone(timedelta(hours=8))
            ).strftime("%Y-%m-%d %H:%M")
        print(f"{ftype:<10} {name:<40} {token:<30} {modified}")


def cmd_info(args, api):
    token = _extract_token(args.token)

    # Try as file first
    resp = api.post("/open-apis/drive/v1/metas/batch_query",
                    {"request_docs": [{"doc_token": token, "doc_type": "docx"}]})
    if resp.get("code") == 0:
        metas = resp.get("data", {}).get("metas", [])
        if metas:
            m = metas[0]
            print(f"Title: {m.get('title', '?')}")
            print(f"Type: {m.get('doc_type', '?')}")
            print(f"Token: {token}")
            print(f"Owner: {m.get('owner_id', '?')}")
            url = m.get("url", "")
            if url:
                print(f"URL: {url}")
            return

    # Try folder listing as fallback
    resp2 = api.get("/open-apis/drive/v1/files",
                    params={"folder_token": token, "page_size": "1"})
    if resp2.get("code") == 0:
        print(f"Folder token: {token}")
        total = resp2.get("data", {}).get("total_count")
        if total is not None:
            print(f"Contains: {total} item(s)")
        return

    print(f"Could not resolve token: {token}")


def cmd_mkdir(args, api):
    parent = args.parent or ""

    # Check for existing folder with the same name to avoid duplicates
    list_resp = api.get("/open-apis/drive/v1/files", params={
        "folder_token": parent,
        "page_size": "200",
    })
    if list_resp.get("code") == 0:
        for item in list_resp.get("data", {}).get("files", []):
            if item.get("name") == args.name and item.get("type") == "folder":
                token = item.get("token", "?")
                url = item.get("url", "")
                print(f"Folder already exists: {args.name}")
                print(f"  Token: {token}")
                if url:
                    print(f"  URL: {url}")
                return

    body = {"name": args.name, "folder_token": parent}
    resp = api.post("/open-apis/drive/v1/files/create_folder", body)
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    token = resp.get("data", {}).get("token", "?")
    url = resp.get("data", {}).get("url", "")
    print(f"Created folder: {args.name}")
    print(f"  Token: {token}")
    if url:
        print(f"  URL: {url}")


def cmd_move(args, api):
    if not args.type:
        print("ERROR: --type is required (docx, sheet, folder, ...)", file=sys.stderr)
        sys.exit(1)

    body = {
        "type": args.type,
        "folder_token": args.dest_folder,
    }
    resp = api.post(f"/open-apis/drive/v1/files/{args.file_token}/move", body)
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Moved {args.file_token} to {args.dest_folder}")


def cmd_delete(args, api):
    if not args.type:
        print("ERROR: --type is required (docx, sheet, folder, ...)", file=sys.stderr)
        sys.exit(1)

    resp = api.delete(
        f"/open-apis/drive/v1/files/{args.file_token}",
        params={"type": args.type})
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Deleted (to trash): {args.file_token}")


def cmd_search(args, api):
    body = {
        "search_key": args.query,
        "count": args.limit or 20,
        "owner_ids": [],
        "docs_types": [],
    }
    if args.type:
        body["docs_types"] = [args.type]

    resp = api.post("/open-apis/suite/docs-api/search/object", body)
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    results = resp.get("data", {}).get("docs_entities", [])
    if not results:
        print("No results found.")
        return

    print(f"{'Type':<10} {'Title':<40} Token")
    print("-" * 80)
    for r in results:
        ftype = _TYPE_DISPLAY.get(r.get("docs_type", ""), r.get("docs_type", "?"))
        title = r.get("title", "(unnamed)")[:38]
        token = r.get("docs_token", "?")
        print(f"{ftype:<10} {title:<40} {token}")


# ── IM send commands ─────────────────────────────────────

def cmd_send_image(args, api):
    path = Path(args.image).resolve()
    if not path.exists():
        print(f"ERROR: File not found: {path}", file=sys.stderr)
        sys.exit(1)
    msg_id = api.send_image(str(path), args.receive_id, args.id_type)
    print(f"Sent image: {msg_id}")


def cmd_send_file(args, api):
    path = Path(args.file).resolve()
    if not path.exists():
        print(f"ERROR: File not found: {path}", file=sys.stderr)
        sys.exit(1)
    msg_id = api.send_file(str(path), args.receive_id, args.id_type)
    print(f"Sent file: {msg_id}")


# ── CLI ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Feishu Drive CLI")
    sub = parser.add_subparsers(dest="command")

    ls = sub.add_parser("list")
    ls.add_argument("--folder", help="Folder token (default: root)")
    ls.add_argument("--type", help="Filter by file type")
    ls.add_argument("--limit", type=int, default=20)

    info = sub.add_parser("info")
    info.add_argument("token", help="File/folder token or URL")

    mk = sub.add_parser("mkdir")
    mk.add_argument("name", help="Folder name")
    mk.add_argument("--parent", help="Parent folder token (default: root)")

    mv = sub.add_parser("move")
    mv.add_argument("file_token")
    mv.add_argument("dest_folder")
    mv.add_argument("--type", required=True, help="File type (docx, sheet, folder, ...)")

    dl = sub.add_parser("delete")
    dl.add_argument("file_token")
    dl.add_argument("--type", required=True, help="File type")

    sr = sub.add_parser("search")
    sr.add_argument("query", help="Search keyword")
    sr.add_argument("--type", help="Filter by type")
    sr.add_argument("--limit", type=int, default=20)

    _id_args = {"default": "open_id", "choices": ["chat_id", "open_id", "user_id"]}

    si = sub.add_parser("send-image", help="Upload and send an image to a chat")
    si.add_argument("image", help="Path to image file")
    si.add_argument("receive_id", help="Chat/user ID to send to")
    si.add_argument("--id-type", dest="id_type", **_id_args)

    sf = sub.add_parser("send-file", help="Upload and send a file to a chat")
    sf.add_argument("file", help="Path to file")
    sf.add_argument("receive_id", help="Chat/user ID to send to")
    sf.add_argument("--id-type", dest="id_type", **_id_args)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    api = FeishuAPI.from_config()

    dispatch = {
        "list": cmd_list,
        "info": cmd_info,
        "mkdir": cmd_mkdir,
        "move": cmd_move,
        "delete": cmd_delete,
        "search": cmd_search,
        "send-image": cmd_send_image,
        "send-file": cmd_send_file,
    }

    dispatch[args.command](args, api)


if __name__ == "__main__":
    main()
