---
name: feishu-wiki
description: Manage Feishu wiki spaces and pages (知识库) — list spaces, browse node trees, create/move/read/write wiki pages. Use when the user mentions wiki, knowledge base (知识库/知识空间), documentation organization (文档整理), or wants to manage wiki content in Feishu. DO NOT TRIGGER for one-off documents like proposals, meeting notes, or reports — use feishu-doc for those. Wiki is for persistent, reusable knowledge (规范/指南/FAQ).
---

# Feishu Wiki

知识沉淀——规范、指南、FAQ 等需要持续维护、反复查阅的内容。与 feishu-doc 的区别：doc 是沟通产物（方案、评审、纪要），有时效性；wiki 是持久知识库。

## Limitations

- **Search**: Wiki search API requires `user_access_token` (not available to bot). Use `node list` to browse, or search via Feishu client.
- **Delete**: Wiki v2 API does not provide a direct node delete endpoint.
- **Read/Write content**: Only `docx` type nodes support content read/write (via the Documents API).

## Tool

```
python3 .claude/skills/feishu-wiki/scripts/wiki_ctl.py <group> <command> [args]
```

## Commands

```bash
# List wiki spaces
wiki_ctl.py space list

# Browse wiki node tree
wiki_ctl.py node list <space_id>
wiki_ctl.py node list <space_id> --parent <node_token>
wiki_ctl.py node list <space_id> --depth 5

# Get node details
wiki_ctl.py node get <node_token_or_url>

# Create a wiki page
wiki_ctl.py node create <space_id> "Page Title"
wiki_ctl.py node create <space_id> "Sub Page" --parent <parent_token>
wiki_ctl.py node create <space_id> "Sheet" --type sheet

# Move a node
wiki_ctl.py node move <space_id> <node_token> --parent <new_parent_token>

# Read wiki page content (docx only)
wiki_ctl.py node read <node_token_or_url>

# Append content to wiki page (docx only)
wiki_ctl.py node write <node_token> "# Heading\nParagraph text\n## Subheading"
```

## Node Types

| Type | Description |
|------|-------------|
| `docx` | Document (default, supports read/write) |
| `sheet` | Spreadsheet |
| `bitable` | Multi-dimensional table |
| `file` | Uploaded file |
| `slides` | Presentation |

## Concepts

- **Space**: A wiki workspace. Bot must be added as a member to access.
- **Node**: A page in the wiki tree. Each node has:
  - `node_token`: Identifies the node in the wiki hierarchy
  - `obj_token`: Identifies the underlying document (used for content APIs)
  - `obj_type`: The document type (docx, sheet, etc.)
- **Tree structure**: Nodes form a hierarchy via `parent_node_token`. Use `node list` to browse the tree.

## URL Handling

Wiki URLs like `https://xxx.feishu.cn/wiki/AbCdEfG123` are automatically parsed — you can pass them directly to `node get` or `node read`.

## Behavior Notes

- `space list` returns all spaces where the bot is a member.
- `node list` shows a tree view with type, title, and node token. Default depth is 3.
- `node create` defaults to `docx` type. The node is created as a child of `--parent` or at the space root.
- `node read` / `node write` only work with `docx` (or `doc`) type nodes — they use the Documents API internally.
- `node move` may be asynchronous for large subtrees. The API returns a task_id in that case.
- The bot uses `tenant_access_token`. Wiki search requires `user_access_token` and is not available.
