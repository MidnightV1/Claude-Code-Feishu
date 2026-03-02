#!/usr/bin/env python3
"""Feishu Document CLI — create, read, write, comment on documents.

Usage:
    doc_ctl.py create "title" [--content "text"] [--folder TOKEN] [--share OPEN_ID]
    doc_ctl.py read <doc_id_or_url>
    doc_ctl.py append <doc_id> "content"
    doc_ctl.py list [--folder TOKEN]
    doc_ctl.py comments <doc_id_or_url>
    doc_ctl.py reply <doc_id_or_url> <comment_id> "reply text"
"""

import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(BASE))

from feishu_api import FeishuAPI  # noqa: E402


def _load_config():
    import yaml
    with open(BASE / "config.yaml") as f:
        return yaml.safe_load(f)


def _extract_doc_id(s: str) -> str:
    """Extract document_id from URL or raw ID."""
    # URL: https://xxx.feishu.cn/docx/Ojo1de7diofBVxxCaEHcL7GnnFf
    m = re.search(r"/docx/([A-Za-z0-9]+)", s)
    if m:
        return m.group(1)
    # URL: https://xxx.feishu.cn/docs/doccnXXX (old format)
    m = re.search(r"/docs/(doccn[A-Za-z0-9]+)", s)
    if m:
        return m.group(1)
    return s.strip()


def _share_doc(api: FeishuAPI, doc_id: str, open_id: str):
    """Grant read/edit access to a user."""
    resp = api.post(
        f"/open-apis/drive/v1/permissions/{doc_id}/members",
        body={
            "member_type": "openid",
            "member_id": open_id,
            "perm": "full_access",
        },
        params={"type": "docx", "need_notification": "true"},
    )
    return resp.get("code") == 0


def _text_to_blocks(text: str) -> list[dict]:
    """Convert plain text (with markdown-like headings) to Feishu block children."""
    # Handle escaped newlines from shell arguments
    text = text.replace("\\n", "\n")
    blocks = []
    for line in text.split("\n"):
        line = line.rstrip()
        if not line:
            continue

        # Headings
        heading_match = re.match(r"^(#{1,9})\s+(.+)$", line)
        if heading_match:
            level = len(heading_match.group(1))
            # block_type: 3=H1, 4=H2, ..., 11=H9
            blocks.append({
                "block_type": 2 + level,
                "heading" + str(level): {
                    "elements": [{"text_run": {"content": heading_match.group(2)}}]
                },
            })
            continue

        # Regular text
        blocks.append({
            "block_type": 2,
            "text": {
                "elements": [{"text_run": {"content": line}}],
            },
        })
    return blocks


# ── Commands ─────────────────────────────────────────────

def cmd_create(args, api, cfg):
    body = {"title": args.title}
    if args.folder:
        body["folder_token"] = args.folder
    else:
        default_folder = cfg.get("feishu", {}).get("docs", {}).get("default_folder")
        if default_folder:
            body["folder_token"] = default_folder

    resp = api.post("/open-apis/docx/v1/documents", body)
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    doc = resp["data"]["document"]
    doc_id = doc["document_id"]
    print(f"Created: {doc_id}")
    print(f"  Title: {doc.get('title')}")

    # Write initial content
    if args.content:
        blocks = _text_to_blocks(args.content)
        if blocks:
            resp2 = api.post(
                f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                {"children": blocks, "index": 0},
                params={"document_revision_id": "-1"},
            )
            if resp2.get("code") == 0:
                print(f"  Content written ({len(blocks)} blocks)")
            else:
                print(f"  Content error: {resp2.get('msg')}", file=sys.stderr)

    # Auto-share
    share_to = args.share
    if not share_to:
        share_list = cfg.get("feishu", {}).get("docs", {}).get("share_to", [])
        if share_list:
            share_to = share_list[0] if isinstance(share_list, list) else share_list
    if share_to:
        if _share_doc(api, doc_id, share_to):
            print(f"  Shared to: {share_to}")
        else:
            print(f"  Share failed", file=sys.stderr)

    domain = cfg.get("feishu", {}).get("domain", "https://open.feishu.cn")
    doc_domain = domain.replace("open.", "")
    print(f"  URL: {doc_domain}/docx/{doc_id}")


def cmd_read(args, api, cfg):
    doc_id = _extract_doc_id(args.doc_id)

    resp = api.get(f"/open-apis/docx/v1/documents/{doc_id}/raw_content")
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    content = resp["data"]["content"]
    print(content)


def cmd_append(args, api, cfg):
    doc_id = _extract_doc_id(args.doc_id)
    blocks = _text_to_blocks(args.content)
    if not blocks:
        print("Nothing to append.")
        return

    resp = api.post(
        f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
        {"children": blocks, "index": -1},
        params={"document_revision_id": "-1"},
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Appended {len(blocks)} blocks to {doc_id}")


def cmd_comments(args, api, cfg):
    """List all comments (with replies) on a document."""
    doc_id = _extract_doc_id(args.doc_id)
    file_type = args.file_type or "docx"

    items = []
    page_token = None
    while True:
        params = {"file_type": file_type, "page_size": "50"}
        if page_token:
            params["page_token"] = page_token
        resp = api.get(f"/open-apis/drive/v1/files/{doc_id}/comments", params=params)
        if resp.get("code") != 0:
            print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
            sys.exit(1)
        items.extend(resp.get("data", {}).get("items", []))
        if not resp.get("data", {}).get("has_more"):
            break
        page_token = resp["data"].get("page_token")

    if not items:
        print("No comments found.")
        return

    for c in items:
        solved = " [RESOLVED]" if c.get("is_solved") else ""
        quote = c.get("quote", "").replace("\n", " ")
        if len(quote) > 80:
            quote = quote[:77] + "..."
        print(f"Comment {c['comment_id']}{solved}")
        if quote:
            print(f"  Quote: \"{quote}\"")
        replies = c.get("reply_list", {}).get("replies", [])
        for r in replies:
            text = _extract_reply_text(r)
            print(f"  [{r['reply_id']}] {text}")
        print()


def _extract_reply_text(reply: dict) -> str:
    """Extract plain text from a comment reply's content elements."""
    elements = reply.get("content", {}).get("elements", [])
    parts = []
    for el in elements:
        if el.get("type") == "text_run":
            parts.append(el.get("text_run", {}).get("text", ""))
        elif el.get("type") == "person":
            parts.append(f"@{el.get('person', {}).get('user_id', '?')}")
        elif el.get("type") == "docs_link":
            parts.append(el.get("docs_link", {}).get("url", "[link]"))
    return "".join(parts) or "(empty)"


def cmd_reply(args, api, cfg):
    """Reply to a specific comment on a document."""
    doc_id = _extract_doc_id(args.doc_id)
    file_type = args.file_type or "docx"

    body = {
        "content": {
            "elements": [
                {"type": "text_run", "text_run": {"text": args.content}}
            ]
        }
    }
    resp = api.post(
        f"/open-apis/drive/v1/files/{doc_id}/comments/{args.comment_id}/replies",
        body=body,
        params={"file_type": file_type},
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Replied to comment {args.comment_id} (reply_id: {resp['data']['reply_id']})")


def cmd_list(args, api, cfg):
    folder = args.folder
    if not folder:
        folder = cfg.get("feishu", {}).get("docs", {}).get("default_folder", "")

    # List files in folder (or root space)
    if folder:
        resp = api.get(
            "/open-apis/drive/v1/files",
            params={"folder_token": folder, "page_size": "20"},
        )
    else:
        # List from app's root folder
        resp = api.get(
            "/open-apis/drive/v1/files",
            params={"page_size": "20"},
        )

    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    files = resp.get("data", {}).get("files", [])
    if not files:
        print("No documents found.")
        return

    print(f"{'Type':<8} {'Token':<32} Name")
    print("-" * 70)
    for f in files:
        print(f"{f.get('type', '?'):<8} {f.get('token', '?'):<32} {f.get('name', '?')}")


# ── CLI ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Feishu Document CLI")
    sub = parser.add_subparsers(dest="action")

    cr = sub.add_parser("create")
    cr.add_argument("title")
    cr.add_argument("--content", help="Initial content (plain text / markdown headings)")
    cr.add_argument("--folder", help="Folder token")
    cr.add_argument("--share", help="Open ID to share with")

    rd = sub.add_parser("read")
    rd.add_argument("doc_id", help="Document ID or URL")

    ap = sub.add_parser("append")
    ap.add_argument("doc_id", help="Document ID or URL")
    ap.add_argument("content", help="Content to append")

    ls = sub.add_parser("list")
    ls.add_argument("--folder", help="Folder token")

    cm = sub.add_parser("comments")
    cm.add_argument("doc_id", help="Document ID or URL")
    cm.add_argument("--file-type", dest="file_type", default="docx",
                    help="File type (docx, doc, sheet, etc.)")

    rp = sub.add_parser("reply")
    rp.add_argument("doc_id", help="Document ID or URL")
    rp.add_argument("comment_id", help="Comment ID to reply to")
    rp.add_argument("content", help="Reply text")
    rp.add_argument("--file-type", dest="file_type", default="docx",
                    help="File type (docx, doc, sheet, etc.)")

    args = parser.parse_args()
    if not args.action:
        parser.print_help()
        sys.exit(1)

    cfg = _load_config()
    api = FeishuAPI.from_config()

    dispatch = {
        "create": cmd_create,
        "read": cmd_read,
        "append": cmd_append,
        "list": cmd_list,
        "comments": cmd_comments,
        "reply": cmd_reply,
    }
    dispatch[args.action](args, api, cfg)


if __name__ == "__main__":
    main()
