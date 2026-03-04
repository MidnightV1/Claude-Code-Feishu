#!/usr/bin/env python3
"""Feishu Document CLI — create, read, write, list, search, comment, analyze documents.

Usage:
    doc_ctl.py create "title" [--content "text"] [--folder TOKEN] [--share OPEN_ID] [--owner OPEN_ID]
    doc_ctl.py read <doc_id_or_url>
    doc_ctl.py append <doc_id> "content"
    doc_ctl.py transfer_owner <doc_id_or_url> <open_id> [--file-type TYPE]
    doc_ctl.py list [--folder TOKEN]
    doc_ctl.py search "keyword" [--folder TOKEN]
    doc_ctl.py comments <doc_id_or_url>
    doc_ctl.py reply <doc_id_or_url> <comment_id> "reply text"
    doc_ctl.py analyze <doc_id_or_url> [--all] [--context-chars N]
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


def _transfer_owner(api: FeishuAPI, doc_id: str, open_id: str,
                    file_type: str = "docx") -> tuple[bool, str]:
    """Transfer document ownership to a user. Bot must be current owner.

    Before transferring, adds the bot app as a collaborator so it retains
    edit access after losing ownership.
    """
    # Ensure bot retains access after ownership transfer
    api.post(
        f"/open-apis/drive/v1/permissions/{doc_id}/members",
        body={
            "member_type": "appid",
            "member_id": api.app_id,
            "perm": "full_access",
        },
        params={"type": file_type},
    )
    resp = api.post(
        f"/open-apis/drive/v1/permissions/{doc_id}/members/transfer_owner",
        body={
            "member_type": "openid",
            "member_id": open_id,
        },
        params={"type": file_type, "need_notification": "true"},
    )
    if resp.get("code") == 0:
        return True, ""
    return False, resp.get("msg", "unknown error")


from feishu_utils import text_to_blocks as _text_to_blocks  # noqa: E402


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

    # Transfer ownership (must happen before removing bot's own access)
    owner_id = getattr(args, "owner", None)
    if owner_id:
        ok, err = _transfer_owner(api, doc_id, owner_id)
        if ok:
            print(f"  Owner: {owner_id}")
        else:
            print(f"  Transfer owner failed: {err}", file=sys.stderr)

    # Auto-share (skip if same as owner — already has full access)
    share_to = args.share
    if not share_to:
        share_list = cfg.get("feishu", {}).get("docs", {}).get("share_to", [])
        if share_list:
            share_to = share_list[0] if isinstance(share_list, list) else share_list
    if share_to and share_to != owner_id:
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


def cmd_analyze(args, api, cfg):
    """Assemble document content + comments into structured analysis context."""
    doc_id = _extract_doc_id(args.doc_id)
    file_type = args.file_type or "docx"
    ctx_chars = args.context_chars or 200

    # 1. Pull document content
    resp = api.get(f"/open-apis/docx/v1/documents/{doc_id}/raw_content")
    if resp.get("code") != 0:
        print(f"ERROR fetching doc content: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    doc_content = resp["data"]["content"]

    # Pull document title
    meta_resp = api.get(f"/open-apis/docx/v1/documents/{doc_id}")
    doc_title = meta_resp.get("data", {}).get("document", {}).get("title", doc_id)

    # 2. Pull comments
    items = []
    page_token = None
    while True:
        params = {"file_type": file_type, "page_size": "50"}
        if page_token:
            params["page_token"] = page_token
        resp = api.get(f"/open-apis/drive/v1/files/{doc_id}/comments", params=params)
        if resp.get("code") != 0:
            print(f"ERROR fetching comments: {resp.get('msg')}", file=sys.stderr)
            sys.exit(1)
        items.extend(resp.get("data", {}).get("items", []))
        if not resp.get("data", {}).get("has_more"):
            break
        page_token = resp["data"].get("page_token")

    # Filter: unresolved only unless --all
    if not args.show_all:
        items = [c for c in items if not c.get("is_solved")]

    if not items:
        print(json.dumps({"doc_id": doc_id, "title": doc_title,
                          "annotations": [], "message": "No comments found."}))
        return

    # 3. Anchor each comment to document content
    annotations = []
    for c in items:
        quote = c.get("quote", "")
        context = _anchor_quote(doc_content, quote, ctx_chars)

        # Extract thread
        thread = []
        for r in c.get("reply_list", {}).get("replies", []):
            thread.append({
                "user_id": r.get("user_id", "?"),
                "text": _extract_reply_text(r),
                "time": int(r.get("create_time", 0)),
            })

        annotations.append({
            "comment_id": c["comment_id"],
            "resolved": bool(c.get("is_solved")),
            "quote": quote,
            "context": context,
            "thread": thread,
        })

    # 4. Output structured result
    result = {
        "doc_id": doc_id,
        "title": doc_title,
        "stats": {
            "total_comments": len(items) if args.show_all else None,
            "shown": len(annotations),
            "filter": "all" if args.show_all else "unresolved",
        },
        "annotations": annotations,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _anchor_quote(doc_content: str, quote: str, ctx_chars: int) -> dict:
    """Find quote in document and extract surrounding context."""
    if not quote:
        return {"before": "", "quoted": "", "after": "", "matched": False}

    # Normalize for matching (collapse whitespace)
    norm_content = re.sub(r"\s+", " ", doc_content)
    norm_quote = re.sub(r"\s+", " ", quote.strip())

    pos = norm_content.find(norm_quote)

    # Fallback: try matching first 40 chars of quote (doc may have been edited)
    if pos == -1 and len(norm_quote) > 40:
        pos = norm_content.find(norm_quote[:40])

    if pos == -1:
        return {"before": "", "quoted": quote, "after": "", "matched": False}

    # Extract surrounding context
    start = max(0, pos - ctx_chars)
    end = min(len(norm_content), pos + len(norm_quote) + ctx_chars)

    before = norm_content[start:pos].strip()
    matched_text = norm_content[pos:pos + len(norm_quote)]
    after = norm_content[pos + len(norm_quote):end].strip()

    # Add ellipsis indicators
    if start > 0:
        before = "..." + before
    if end < len(norm_content):
        after = after + "..."

    return {
        "before": before,
        "quoted": matched_text,
        "after": after,
        "matched": True,
    }


def _list_folder(api, folder_token: str, max_pages: int = 10) -> list[dict]:
    """List files in a folder with pagination (capped at max_pages)."""
    files = []
    page_token = None
    for _ in range(max_pages):
        params = {"folder_token": folder_token, "page_size": "50",
                  "order_by": "EditedTime", "direction": "DESC"}
        if page_token:
            params["page_token"] = page_token
        resp = api.get("/open-apis/drive/v1/files", params=params)
        if resp.get("code") != 0:
            print(f"ERROR listing folder {folder_token}: {resp.get('msg')}",
                  file=sys.stderr)
            break
        files.extend(resp.get("data", {}).get("files", []))
        if not resp.get("data", {}).get("has_more"):
            break
        page_token = resp["data"].get("page_token")
    return files


def _get_folders(args, cfg) -> list[tuple[str, str]]:
    """Get target folders: --folder flag > shared_folders config > default_folder."""
    if getattr(args, "folder", None):
        return [("", args.folder)]
    docs_cfg = cfg.get("feishu", {}).get("docs", {})
    shared = docs_cfg.get("shared_folders", [])
    if shared:
        return [(f.get("name", ""), f["token"]) for f in shared if f.get("token")]
    default = docs_cfg.get("default_folder", "")
    if default:
        return [("", default)]
    return []


def _format_time(ts) -> str:
    """Format a Unix timestamp (str or int) to MM-DD HH:MM."""
    from datetime import datetime
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return ""


def _print_files(files: list[dict], folder_name: str = "", folder_token: str = ""):
    """Print file list in human-readable format."""
    if folder_name or folder_token:
        label = folder_name or folder_token
        print(f"\n📁 {label}")
    if not files:
        print("  (empty)")
        return
    for f in files:
        ftype = f.get("type", "?")
        name = f.get("name", "?")
        mtime = _format_time(f.get("modified_time", ""))
        url = f.get("url", "")
        time_col = f"  {mtime}" if mtime else ""
        url_col = f"  {url}" if url else ""
        print(f"  {ftype:<7} {name}{time_col}{url_col}")


def cmd_list(args, api, cfg):
    folders = _get_folders(args, cfg)
    if not folders:
        print("No folders configured. Set feishu.docs.shared_folders in config.yaml "
              "or use --folder <token>.", file=sys.stderr)
        sys.exit(1)

    for name, token in folders:
        files = _list_folder(api, token)
        _print_files(files, name, token)


def cmd_transfer_owner(args, api, cfg):
    """Transfer document ownership to another user."""
    doc_id = _extract_doc_id(args.doc_id)
    file_type = getattr(args, "file_type", None) or "docx"
    ok, err = _transfer_owner(api, doc_id, args.open_id, file_type=file_type)
    if not ok:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)
    print(f"Ownership of {doc_id} transferred to {args.open_id}")


def cmd_search(args, api, cfg):
    """Search files by keyword in name across all configured folders."""
    keyword = args.keyword.lower()
    folders = _get_folders(args, cfg)
    if not folders:
        print("No folders configured. Set feishu.docs.shared_folders in config.yaml "
              "or use --folder <token>.", file=sys.stderr)
        sys.exit(1)

    matches = []
    for name, token in folders:
        for f in _list_folder(api, token):
            if keyword in f.get("name", "").lower():
                matches.append((name, f))

    if not matches:
        print(f'No documents matching "{args.keyword}" found.')
        return

    print(f'Found {len(matches)} document(s) matching "{args.keyword}":')
    for folder_name, f in matches:
        ftype = f.get("type", "?")
        name = f.get("name", "?")
        mtime = _format_time(f.get("modified_time", ""))
        url = f.get("url", "")
        prefix = f"[{folder_name}] " if folder_name else ""
        time_col = f"  {mtime}" if mtime else ""
        url_col = f"  {url}" if url else ""
        print(f"  {prefix}{ftype:<7} {name}{time_col}{url_col}")


# ── CLI ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Feishu Document CLI")
    sub = parser.add_subparsers(dest="action")

    cr = sub.add_parser("create")
    cr.add_argument("title")
    cr.add_argument("--content", help="Initial content (plain text / markdown headings)")
    cr.add_argument("--folder", help="Folder token")
    cr.add_argument("--share", help="Open ID to share with")
    cr.add_argument("--owner", help="Open ID to transfer ownership to after creation")

    rd = sub.add_parser("read")
    rd.add_argument("doc_id", help="Document ID or URL")

    ap = sub.add_parser("append")
    ap.add_argument("doc_id", help="Document ID or URL")
    ap.add_argument("content", help="Content to append")

    ls = sub.add_parser("list")
    ls.add_argument("--folder", help="Folder token")

    to = sub.add_parser("transfer_owner")
    to.add_argument("doc_id", help="Document ID or URL")
    to.add_argument("open_id", help="Open ID of the new owner")
    to.add_argument("--file-type", dest="file_type", default="docx",
                    help="File type (docx, doc, sheet, etc.)")

    sr = sub.add_parser("search")
    sr.add_argument("keyword", help="Search keyword (matches file name)")
    sr.add_argument("--folder", help="Limit search to a specific folder token")

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

    az = sub.add_parser("analyze")
    az.add_argument("doc_id", help="Document ID or URL")
    az.add_argument("--all", dest="show_all", action="store_true",
                    help="Include resolved comments (default: unresolved only)")
    az.add_argument("--context-chars", dest="context_chars", type=int, default=200,
                    help="Characters of surrounding context per quote (default: 200)")
    az.add_argument("--file-type", dest="file_type", default="docx",
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
        "transfer_owner": cmd_transfer_owner,
        "list": cmd_list,
        "search": cmd_search,
        "comments": cmd_comments,
        "reply": cmd_reply,
        "analyze": cmd_analyze,
    }
    dispatch[args.action](args, api, cfg)


if __name__ == "__main__":
    main()
