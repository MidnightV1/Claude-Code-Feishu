#!/usr/bin/env python3
"""Feishu Board (画板/Whiteboard) CLI — create boards, add nodes, read content.

Usage:
    board_ctl.py create --doc <doc_id>                     # create empty board in doc
    board_ctl.py read <board_token_or_doc_url>             # read all nodes
    board_ctl.py add <board_token> --nodes '<json>'        # add nodes (raw JSON)
    board_ctl.py flowchart <board_token> --steps '<json>'  # add simple flowchart
    board_ctl.py delete_node <board_token> <node_id>       # delete a node

Known limitation: connector (连线) creation via API returns 'connector info empty'
for all tested field combinations. Flowcharts use text arrow labels (↓/→) as workaround.
"""

import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(BASE))

from agent.platforms.feishu.api import FeishuAPI  # noqa: E402

# ── Layout constants ──────────────────────────────────────────────
NODE_W = 180          # default node width
NODE_H = 60           # default node height
H_GAP = 80            # horizontal gap between nodes
V_GAP = 60            # vertical gap between rows
ARROW_H = 30          # arrow label height
DECISION_W = 160      # diamond width
DECISION_H = 100      # diamond height

# ── Shape mapping ─────────────────────────────────────────────────
SHAPE_MAP = {
    "start":    "flow_chart_round_rect",
    "end":      "flow_chart_round_rect",
    "process":  "rect",
    "decision": "flow_chart_diamond",
    "io":       "flow_chart_parallelogram",
    "sub":      "predefined_process",
    "delay":    "delay_shape",
    "database": "flow_chart_cylinder",
}

# ── Default styles (top-level on node, NOT inside composite_shape) ──
_STYLE_DEFAULT = {
    "fill_color": "#FFFFFF",
    "border_color": "#1F2329",
    "border_width": "narrow",
    "border_style": "solid",
}

_STYLE_START_END = {
    "fill_color": "#3370FF",
    "border_color": "#3370FF",
    "border_width": "narrow",
    "border_style": "solid",
}

_STYLE_DECISION = {
    "fill_color": "#FFF8E1",
    "border_color": "#FF9800",
    "border_width": "narrow",
    "border_style": "solid",
}

_TEXT_BASE = {
    "font_size": 14,
    "horizontal_align": "center",
    "vertical_align": "mid",
    "text_color": "#1F2329",
}


def _make_shape(shape_type, x, y, w, h, text, style=None, text_color="#1F2329"):
    """Build a composite_shape node payload with correct top-level structure."""
    return {
        "type": "composite_shape",
        "x": x, "y": y, "width": w, "height": h,
        "composite_shape": {"type": shape_type},
        "style": style or dict(_STYLE_DEFAULT),
        "text": {**_TEXT_BASE, "text": text, "text_color": text_color},
    }


def _make_arrow_label(x, y, text="↓"):
    """Build a text_shape node as arrow indicator between flow nodes."""
    return {
        "type": "text_shape",
        "x": x, "y": y, "width": 40, "height": ARROW_H,
        "text": {**_TEXT_BASE, "text": text, "font_size": 16, "text_color": "#8F959E"},
    }


def _extract_board_token(s):
    """Extract board token from URL or raw string."""
    m = re.search(r"/board/([A-Za-z0-9]+)", s)
    return m.group(1) if m else s.strip()


def _extract_doc_id(s):
    """Extract doc_id from URL or raw ID."""
    m = re.search(r"/docx/([A-Za-z0-9]+)", s)
    return m.group(1) if m else s.strip()


# ══════════════════════════════════════════════════════════════════
#  Commands
# ══════════════════════════════════════════════════════════════════

def cmd_create(args, api):
    """Create a board block inside an existing document."""
    doc_id = _extract_doc_id(args.doc)
    resp = api.post(
        f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
        {"children": [{"block_type": 43, "board": {}}], "index": -1},
        params={"document_revision_id": "-1"},
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    children = resp.get("data", {}).get("children", [])
    if not children:
        print("ERROR: no board block created", file=sys.stderr)
        sys.exit(1)

    board_block = children[0]
    token = board_block.get("board", {}).get("token", "")
    block_id = board_block.get("block_id", "")
    print(f"Board created in doc {doc_id}")
    print(f"  block_id: {block_id}")
    print(f"  token: {token}")
    print(f"  Use: board_ctl.py flowchart {token} --steps '[...]'")


def cmd_read(args, api):
    """Read all nodes from a board and output structured description."""
    token = _extract_board_token(args.token)
    if len(token) < 10:
        print(f"ERROR: invalid board token: {args.token}", file=sys.stderr)
        sys.exit(1)

    # Paginated fetch
    all_nodes = []
    page_token = None
    while True:
        params = {"page_size": "500"}
        if page_token:
            params["page_token"] = page_token
        resp = api.get(
            f"/open-apis/board/v1/whiteboards/{token}/nodes", params=params,
        )
        if resp.get("code") != 0:
            print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
            sys.exit(1)
        all_nodes.extend(resp.get("data", {}).get("nodes", []))
        if not resp.get("data", {}).get("has_more"):
            break
        page_token = resp["data"].get("page_token")

    if not all_nodes:
        print("Board is empty (no nodes).")
        return

    shapes, connectors, others = [], [], []
    for n in all_nodes:
        ntype = n.get("type", "")
        nid = n.get("id", "")
        x, y = n.get("x", 0), n.get("y", 0)
        w, h = n.get("width", 0), n.get("height", 0)
        text = n.get("text", {}).get("text", "") if isinstance(n.get("text"), dict) else ""

        if ntype == "composite_shape":
            cs = n.get("composite_shape", {})
            shape_type = cs.get("type", "unknown")
            shapes.append({"id": nid, "shape": shape_type, "text": text,
                           "x": x, "y": y, "w": w, "h": h})
        elif ntype == "text_shape":
            shapes.append({"id": nid, "shape": "text", "text": text,
                           "x": x, "y": y, "w": w, "h": h})
        elif ntype == "connector":
            cn = n.get("connector", {})
            connectors.append({
                "id": nid,
                "from": cn.get("start_object_id",
                               cn.get("start_object", {}).get("id", "")),
                "to": cn.get("end_object_id",
                             cn.get("end_object", {}).get("id", "")),
            })
        else:
            others.append({"id": nid, "type": ntype, "text": text})

    label_map = {s["id"]: s["text"] or s["shape"] for s in shapes}

    print(f"## Board: {token}")
    print(f"Nodes: {len(shapes)}, Connectors: {len(connectors)}")
    print()

    if shapes:
        print("### Nodes")
        for s in sorted(shapes, key=lambda x: (x["y"], x["x"])):
            label = s["text"] or "(empty)"
            print(f"  [{s['id']}] {s['shape']}: {label}  "
                  f"(x={s['x']}, y={s['y']}, {s['w']}x{s['h']})")
        print()

    if connectors:
        print("### Connections")
        for c in connectors:
            src = label_map.get(c["from"], c["from"])
            dst = label_map.get(c["to"], c["to"])
            print(f"  {src} → {dst}")
        print()

    if others:
        print("### Other elements")
        for o in others:
            print(f"  [{o['id']}] {o['type']}: {o.get('text', '')}")

    # Flow text reconstruction
    if connectors:
        print("\n### Flow (text)")
        all_targets = {c["to"] for c in connectors}
        roots = [s["id"] for s in shapes if s["id"] not in all_targets]
        if not roots and shapes:
            roots = [shapes[0]["id"]]
        visited = set()

        def _walk(nid, depth=0):
            if nid in visited:
                return
            visited.add(nid)
            print(f"{'  ' * depth}→ {label_map.get(nid, nid)}")
            for c in connectors:
                if c["from"] == nid:
                    _walk(c["to"], depth + 1)

        for root in roots:
            _walk(root)


def cmd_add(args, api):
    """Add raw nodes to a board."""
    token = _extract_board_token(args.token)
    try:
        nodes = json.loads(args.nodes)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(nodes, list):
        nodes = [nodes]

    resp = api.post(
        f"/open-apis/board/v1/whiteboards/{token}/nodes",
        {"nodes": nodes},
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    ids = resp.get("data", {}).get("ids", [])
    print(f"Created {len(ids)} node(s): {', '.join(ids)}")


def cmd_flowchart(args, api):
    """Create a flowchart from step definitions.

    Steps JSON:
    [
      {"text": "开始", "type": "start"},
      {"text": "处理数据", "type": "process"},
      {"text": "是否通过?", "type": "decision", "yes": "输出结果", "no": "错误处理"},
      {"text": "输出结果", "type": "process"},
      {"text": "错误处理", "type": "process"},
      {"text": "结束", "type": "end"}
    ]
    """
    token = _extract_board_token(args.token)
    try:
        steps = json.loads(args.steps)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    if not steps:
        print("ERROR: empty steps", file=sys.stderr)
        sys.exit(1)

    # ── Identify decision branches ──
    decision_branches = {}
    for i, step in enumerate(steps):
        if step.get("type") == "decision":
            decision_branches[i] = {
                "yes": step.get("yes", ""),
                "no": step.get("no", ""),
            }

    no_branch_targets = set()
    for db in decision_branches.values():
        if db["no"]:
            no_branch_targets.add(db["no"])

    # ── Layout: top-down, "no" branches offset right ──
    main_x = 0
    branch_x = NODE_W + H_GAP
    y_cursor = 0
    text_to_pos = {}   # text → (x, y, w, h)
    layout_order = []  # [(text, x, y, w, h)]

    for i, step in enumerate(steps):
        text = step.get("text", f"Step {i+1}")
        if text in text_to_pos:
            continue
        stype = step.get("type", "process")
        w = DECISION_W if stype == "decision" else NODE_W
        h = DECISION_H if stype == "decision" else NODE_H

        x = branch_x if text in no_branch_targets else main_x
        text_to_pos[text] = (x, y_cursor, w, h)
        layout_order.append((text, stype, x, y_cursor, w, h))
        y_cursor += h + V_GAP + ARROW_H  # extra space for arrow label

    # ── Build shape nodes ──
    nodes_payload = []
    for text, stype, x, y, w, h in layout_order:
        shape_type = SHAPE_MAP.get(stype, "rect")
        if stype in ("start", "end"):
            style, tc = dict(_STYLE_START_END), "#FFFFFF"
        elif stype == "decision":
            style, tc = dict(_STYLE_DECISION), "#1F2329"
        else:
            style, tc = dict(_STYLE_DEFAULT), "#1F2329"
        nodes_payload.append(_make_shape(shape_type, x, y, w, h, text, style, tc))

    # ── Build arrow labels between connected nodes ──
    arrows = []
    for i, step in enumerate(steps):
        text = step.get("text", "")
        stype = step.get("type", "process")
        if text not in text_to_pos:
            continue
        sx, sy, sw, sh = text_to_pos[text]

        if stype == "decision":
            yes_t = step.get("yes", "")
            no_t = step.get("no", "")
            if yes_t and yes_t in text_to_pos:
                ty = text_to_pos[yes_t][1]
                mid_y = sy + sh // 2 + (ty - sy - sh // 2) // 2
                arrows.append(_make_arrow_label(sx, mid_y, "是 ↓"))
            if no_t and no_t in text_to_pos:
                nx, ny = text_to_pos[no_t][0], text_to_pos[no_t][1]
                mid_x = sx + sw // 2 + (nx - sx - sw // 2) // 2
                mid_y = sy
                arrows.append(_make_arrow_label(mid_x, mid_y, "否 →"))
        else:
            # Find next linear step
            for j in range(i + 1, len(steps)):
                nt = steps[j].get("text", "")
                if nt in text_to_pos:
                    # Skip branch-only targets
                    is_branch = False
                    for di, db in decision_branches.items():
                        if di < j and (db.get("no") == nt or db.get("yes") == nt):
                            is_branch = True
                            break
                    if not is_branch:
                        ty = text_to_pos[nt][1]
                        mid_y = sy + sh // 2 + (ty - sy - sh // 2) // 2
                        arrows.append(_make_arrow_label(sx, mid_y, "↓"))
                    break

    # ── Create all nodes in one batch ──
    all_nodes = nodes_payload + arrows
    resp = api.post(
        f"/open-apis/board/v1/whiteboards/{token}/nodes",
        {"nodes": all_nodes},
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    ids = resp.get("data", {}).get("ids", [])
    print(f"Flowchart created: {len(layout_order)} shapes + {len(arrows)} arrows = {len(ids)} nodes")
    print(f"NOTE: Connectors (lines) not available via API — using text arrows as visual links")


def cmd_delete_node(args, api):
    """Delete a single node from a board."""
    token = _extract_board_token(args.token)
    resp = api.delete(
        f"/open-apis/board/v1/whiteboards/{token}/nodes/{args.node_id}",
    )
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Deleted node {args.node_id}")


# ══════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Feishu Board (画板) CLI")
    sub = parser.add_subparsers(dest="action")

    p_create = sub.add_parser("create", help="Create a board block in a document")
    p_create.add_argument("--doc", required=True, help="Document ID or URL")

    p_read = sub.add_parser("read", help="Read all nodes from a board")
    p_read.add_argument("token", help="Board token or URL")

    p_add = sub.add_parser("add", help="Add raw nodes (JSON)")
    p_add.add_argument("token", help="Board token")
    p_add.add_argument("--nodes", required=True, help="Nodes JSON array")

    p_flow = sub.add_parser("flowchart", help="Create a flowchart from steps")
    p_flow.add_argument("token", help="Board token")
    p_flow.add_argument("--steps", required=True, help="Steps JSON array")

    p_del = sub.add_parser("delete_node", help="Delete a node")
    p_del.add_argument("token", help="Board token")
    p_del.add_argument("node_id", help="Node ID to delete")

    args = parser.parse_args()
    if not args.action:
        parser.print_help()
        sys.exit(1)

    api = FeishuAPI.from_config()
    dispatch = {
        "create": cmd_create,
        "read": cmd_read,
        "add": cmd_add,
        "flowchart": cmd_flowchart,
        "delete_node": cmd_delete_node,
    }
    dispatch[args.action](args, api)


if __name__ == "__main__":
    main()
