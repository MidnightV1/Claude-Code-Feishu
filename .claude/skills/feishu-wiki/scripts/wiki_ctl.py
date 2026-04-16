#!/usr/bin/env python3
"""Feishu Wiki CLI — manage wiki spaces and nodes.

Usage:
    wiki_ctl.py space list
    wiki_ctl.py node list <space_id> [--parent TOKEN] [--depth N]
    wiki_ctl.py node get <token>
    wiki_ctl.py node create <space_id> "title" [--parent TOKEN] [--type docx]
    wiki_ctl.py node move <space_id> <node_token> --parent TOKEN
    wiki_ctl.py node read <token>
    wiki_ctl.py node write <token> "content"
"""

import argparse
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(BASE))

from agent.platforms.feishu.api import FeishuAPI  # noqa: E402


def _load_config():
    import yaml
    with open(BASE / "config.yaml") as f:
        return yaml.safe_load(f)


def _extract_token(s: str) -> str:
    """Extract node_token from wiki URL or raw token."""
    # https://xxx.feishu.cn/wiki/AbCdEfG123
    m = re.search(r"/wiki/([A-Za-z0-9]+)", s)
    if m:
        return m.group(1)
    return s.strip()


def _format_node_type(t: str) -> str:
    """Abbreviate node obj_type for display."""
    return {
        "docx": "docx",
        "doc": "doc",
        "sheet": "sheet",
        "bitable": "bitable",
        "mindnote": "mind",
        "file": "file",
        "slides": "slides",
    }.get(t, t or "?")


# ── API helpers ──────────────────────────────────────────

def _list_spaces(api: FeishuAPI) -> list[dict]:
    """List all wiki spaces visible to the bot."""
    spaces = []
    page_token = None
    for _ in range(20):
        params = {"page_size": "50"}
        if page_token:
            params["page_token"] = page_token
        resp = api.get("/open-apis/wiki/v2/spaces", params=params)
        if resp.get("code") != 0:
            print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
            sys.exit(1)
        for s in resp.get("data", {}).get("items", []):
            spaces.append(s)
        if not resp.get("data", {}).get("has_more"):
            break
        page_token = resp["data"].get("page_token")
    return spaces


def _list_nodes(api: FeishuAPI, space_id: str,
                parent_token: str = "") -> list[dict]:
    """List child nodes in a space (single level)."""
    nodes = []
    page_token = None
    for _ in range(20):
        params = {"page_size": "50"}
        if parent_token:
            params["parent_node_token"] = parent_token
        if page_token:
            params["page_token"] = page_token
        resp = api.get(f"/open-apis/wiki/v2/spaces/{space_id}/nodes",
                       params=params)
        if resp.get("code") != 0:
            print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
            sys.exit(1)
        for n in resp.get("data", {}).get("items", []):
            nodes.append(n)
        if not resp.get("data", {}).get("has_more"):
            break
        page_token = resp["data"].get("page_token")
    return nodes


def _get_node(api: FeishuAPI, token: str) -> dict | None:
    """Get a single node by token."""
    resp = api.get("/open-apis/wiki/v2/spaces/get_node",
                   params={"token": token})
    if resp.get("code") != 0:
        return None
    return resp.get("data", {}).get("node")


def _tree_walk(api: FeishuAPI, space_id: str,
               parent_token: str, depth: int, max_depth: int,
               prefix: str = ""):
    """Recursively print node tree."""
    nodes = _list_nodes(api, space_id, parent_token)
    for i, n in enumerate(nodes):
        is_last = (i == len(nodes) - 1)
        connector = "`-- " if is_last else "|-- "
        child_prefix = "    " if is_last else "|   "

        title = n.get("title", "(untitled)")
        ntype = _format_node_type(n.get("obj_type", ""))
        token = n.get("node_token", "?")
        has_child = n.get("has_child", False)

        print(f"{prefix}{connector}[{ntype}] {title}  ({token})")

        if has_child and depth < max_depth:
            _tree_walk(api, space_id, token, depth + 1, max_depth,
                       prefix + child_prefix)


# ── Commands ─────────────────────────────────────────────

def cmd_space_list(args, api, cfg):
    spaces = _list_spaces(api)
    if not spaces:
        print("No wiki spaces found. Is the bot added as a space member?")
        return
    for s in spaces:
        name = s.get("name", "(unnamed)")
        sid = s.get("space_id", "?")
        desc = s.get("description", "")
        visibility = s.get("visibility", "?")
        print(f"{name}  (space_id: {sid}, {visibility})")
        if desc:
            print(f"  {desc}")


def cmd_node_list(args, api, cfg):
    space_id = args.space_id
    parent = getattr(args, "parent", "") or ""
    max_depth = getattr(args, "depth", 3) or 3

    if parent:
        print(f"Nodes under {parent}:")
        _tree_walk(api, space_id, parent, 0, max_depth)
    else:
        # List root nodes
        print(f"Wiki space {space_id}:")
        _tree_walk(api, space_id, "", 0, max_depth)


def cmd_node_get(args, api, cfg):
    token = _extract_token(args.token)
    node = _get_node(api, token)
    if not node:
        print(f"ERROR: Node {token} not found.", file=sys.stderr)
        sys.exit(1)

    title = node.get("title", "(untitled)")
    ntype = _format_node_type(node.get("obj_type", ""))
    space_id = node.get("space_id", "?")
    obj_token = node.get("obj_token", "?")
    parent = node.get("parent_node_token", "")
    has_child = node.get("has_child", False)
    create_time = node.get("node_create_time", "")
    edit_time = node.get("obj_edit_time", "")

    print(f"Title: {title}")
    print(f"Type: {ntype}")
    print(f"Node token: {token}")
    print(f"Obj token: {obj_token}")
    print(f"Space: {space_id}")
    if parent:
        print(f"Parent: {parent}")
    print(f"Has children: {has_child}")
    if create_time:
        print(f"Created: {_format_ts(create_time)}")
    if edit_time:
        print(f"Edited: {_format_ts(edit_time)}")

    # Build URL
    domain = cfg.get("feishu", {}).get("domain", "https://open.feishu.cn")
    doc_domain = domain.replace("open.", "")
    print(f"URL: {doc_domain}/wiki/{token}")


def cmd_node_create(args, api, cfg):
    space_id = args.space_id
    title = args.title
    obj_type = getattr(args, "type", "docx") or "docx"
    parent = getattr(args, "parent", "") or ""

    body = {
        "obj_type": obj_type,
        "title": title,
    }
    if parent:
        body["parent_node_token"] = parent

    resp = api.post(f"/open-apis/wiki/v2/spaces/{space_id}/nodes", body)
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    node = resp.get("data", {}).get("node", {})
    node_token = node.get("node_token", "?")
    obj_token = node.get("obj_token", "?")

    print(f"Created: {title}")
    print(f"  Node token: {node_token}")
    print(f"  Obj token: {obj_token}")
    print(f"  Type: {obj_type}")

    domain = cfg.get("feishu", {}).get("domain", "https://open.feishu.cn")
    doc_domain = domain.replace("open.", "")
    print(f"  URL: {doc_domain}/wiki/{node_token}")


def cmd_node_move(args, api, cfg):
    space_id = args.space_id
    node_token = _extract_token(args.node_token)
    parent = getattr(args, "parent", "") or ""

    body = {}
    if parent:
        body["target_parent_token"] = parent
    else:
        # Move to space root
        body["target_parent_token"] = ""

    resp = api.post(
        f"/open-apis/wiki/v2/spaces/{space_id}/nodes/{node_token}/move",
        body)
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    # Move may be async — check for task_id
    task_id = resp.get("data", {}).get("task_id")
    if task_id:
        print(f"Move initiated (async task: {task_id})")
        print("Use 'node get' to verify after a few seconds.")
    else:
        print(f"Moved {node_token} to {parent or 'root'}")


def cmd_node_read(args, api, cfg):
    """Read wiki node content via block tree traversal (supports images)."""
    import os
    doc_scripts = Path(__file__).resolve().parents[2] / "feishu-doc" / "scripts"
    if str(doc_scripts) not in sys.path:
        sys.path.insert(0, str(doc_scripts))
    from doc_ctl import _list_blocks, _walk_blocks

    token = _extract_token(args.token)
    node = _get_node(api, token)
    if not node:
        print(f"ERROR: Node {token} not found.", file=sys.stderr)
        sys.exit(1)

    obj_token = node.get("obj_token", "")
    obj_type = node.get("obj_type", "")

    if obj_type not in ("docx", "doc"):
        print(f"ERROR: read only supports docx/doc, got {obj_type}",
              file=sys.stderr)
        sys.exit(1)

    blocks = _list_blocks(api, obj_token)
    if not blocks:
        print("(empty document)")
        return

    block_map = {b["block_id"]: b for b in blocks}

    img_dir = os.path.join(os.environ.get("TMPDIR", "/tmp"), "feishu_doc_images", obj_token)
    if any(b.get("block_type") == 27 for b in blocks):
        os.makedirs(img_dir, exist_ok=True)

    root = block_map.get(obj_token)
    if not root:
        resp = api.get(f"/open-apis/docx/v1/documents/{obj_token}/raw_content")
        if resp.get("code") == 0:
            print(resp["data"]["content"])
        return

    lines: list[str] = []
    _walk_blocks(root, block_map, api, img_dir, lines)
    print("\n".join(lines))


def cmd_node_write(args, api, cfg):
    """Append content to a wiki docx node."""
    token = _extract_token(args.token)
    node = _get_node(api, token)
    if not node:
        print(f"ERROR: Node {token} not found.", file=sys.stderr)
        sys.exit(1)

    obj_token = node.get("obj_token", "")
    obj_type = node.get("obj_type", "")

    if obj_type != "docx":
        print(f"ERROR: write only supports docx, got {obj_type}",
              file=sys.stderr)
        sys.exit(1)

    blocks = _text_to_blocks(args.content)
    if not blocks:
        print("Nothing to write.")
        return

    resp = api.post(
        f"/open-apis/docx/v1/documents/{obj_token}/blocks/{obj_token}/children",
        {"children": blocks, "index": -1},
        params={"document_revision_id": "-1"},
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Appended {len(blocks)} blocks to {token}")


# ── Utilities ────────────────────────────────────────────

def _format_ts(ts) -> str:
    """Format Unix timestamp (str or int) to YYYY-MM-DD HH:MM."""
    from datetime import datetime
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return str(ts)


from agent.platforms.feishu.utils import text_to_blocks as _text_to_blocks  # noqa: E402


# ── CLI ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Feishu Wiki CLI")
    sub = parser.add_subparsers(dest="group")

    # space commands
    sp = sub.add_parser("space")
    sp_sub = sp.add_subparsers(dest="action")
    sp_sub.add_parser("list")

    # node commands
    nd = sub.add_parser("node")
    nd_sub = nd.add_subparsers(dest="action")

    nl = nd_sub.add_parser("list")
    nl.add_argument("space_id", help="Wiki space ID")
    nl.add_argument("--parent", help="Parent node token (default: root)")
    nl.add_argument("--depth", type=int, default=3,
                    help="Max tree depth (default: 3)")

    ng = nd_sub.add_parser("get")
    ng.add_argument("token", help="Node token or wiki URL")

    nc = nd_sub.add_parser("create")
    nc.add_argument("space_id", help="Wiki space ID")
    nc.add_argument("title", help="Node title")
    nc.add_argument("--parent", help="Parent node token")
    nc.add_argument("--type", default="docx",
                    help="Node type: docx, sheet, bitable, file (default: docx)")

    nm = nd_sub.add_parser("move")
    nm.add_argument("space_id", help="Wiki space ID")
    nm.add_argument("node_token", help="Node token to move")
    nm.add_argument("--parent", help="Target parent node token (omit for root)")

    nr = nd_sub.add_parser("read")
    nr.add_argument("token", help="Node token or wiki URL")

    nw = nd_sub.add_parser("write")
    nw.add_argument("token", help="Node token or wiki URL")
    nw.add_argument("content", help="Content to append (plain text / markdown headings)")

    args = parser.parse_args()
    if not args.group:
        parser.print_help()
        sys.exit(1)

    cfg = _load_config()
    api = FeishuAPI.from_config()

    if args.group == "space":
        if args.action == "list":
            cmd_space_list(args, api, cfg)
        else:
            sub.parse_args(["space", "--help"])
    elif args.group == "node":
        dispatch = {
            "list": cmd_node_list,
            "get": cmd_node_get,
            "create": cmd_node_create,
            "move": cmd_node_move,
            "read": cmd_node_read,
            "write": cmd_node_write,
        }
        if args.action in dispatch:
            dispatch[args.action](args, api, cfg)
        else:
            sub.parse_args(["node", "--help"])


if __name__ == "__main__":
    main()
