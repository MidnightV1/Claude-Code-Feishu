---
name: feishu-wiki
description: Manage Feishu wiki spaces and pages — list spaces, browse node trees, create/move/read/write wiki pages. Use when the user mentions wiki, knowledge base, or wants to organize documentation in Feishu Wiki.
---

<!-- ONBOARDING:START — Delete this section after first-run setup is complete -->
## First-Time Setup

> **Agent**: On first load, walk the user through these items. Once all confirmed, delete from `ONBOARDING:START` to `ONBOARDING:END`.

### Prerequisites

- [x] **Feishu app permissions**: ✅ Already granted — `wiki:wiki`, `wiki:wiki:readonly`, `wiki:node:*`, `wiki:space:*`, `wiki:member:*`, `wiki:setting:*` (see `docs/feishu_scopes.md`)
- [ ] **Add bot to wiki space**: The bot must be added as a member (or admin) of the target wiki space. Go to Wiki Space Settings > Members > Add the bot app.
- [ ] **Verify access**:
  ```bash
  python3 .claude/skills/feishu-wiki/scripts/wiki_ctl.py space list
  ```
  Should return at least one wiki space.

### Why must the bot be a space member?

The Wiki v2 API uses `tenant_access_token`, but access is scoped to spaces where the bot app is explicitly added as a member. Without membership, all queries return empty results.

### Limitations

- **Search**: Wiki search API requires `user_access_token` (not available to bot). Use `node list` to browse, or search via Feishu client.
- **Delete**: Wiki v2 API does not provide a direct node delete endpoint.
- **Read/Write content**: Only `docx` type nodes support content read/write (via the Documents API).

### Verify

```bash
python3 .claude/skills/feishu-wiki/scripts/wiki_ctl.py space list
python3 .claude/skills/feishu-wiki/scripts/wiki_ctl.py node list <space_id>
```

Ask the user: "I need to set up wiki access. Can you add `wiki:wiki:readonly` and `wiki:wiki` permissions to the Feishu app, and add the bot as a member of the target wiki space?"
<!-- ONBOARDING:END -->

# Feishu Wiki

Browse, create, and manage wiki spaces and pages. Read and write content to wiki docx nodes.

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
