---
name: feishu-perm
description: Manage Feishu document permissions — add/remove collaborators, set public sharing, check access levels. Use when the user mentions sharing, permissions, collaborators, access control, or wants to manage who can view/edit a document.
---

# Feishu Permission Manager

Manage document/file permissions — add collaborators, set public link sharing, update access levels.

## Tool

```
python3 .claude/skills/feishu-perm/scripts/perm_ctl.py <command> [args]
```

## Commands

```bash
# List collaborators on a document
perm_ctl.py list <token> --type <doc_type>

# Add a collaborator
perm_ctl.py add <token> --type <doc_type> --user <open_id_or_name> --perm <view|edit|full_access>

# Remove a collaborator
perm_ctl.py remove <token> --type <doc_type> --user <open_id> --member-type <openid|userid>

# Get public sharing settings
perm_ctl.py public-get <token> --type <doc_type>

# Set public sharing (link sharing)
perm_ctl.py public-set <token> --type <doc_type> --link <tenant_readable|tenant_editable|anyone_readable|anyone_editable|off>

# Transfer ownership
perm_ctl.py transfer <token> --type <doc_type> --user <open_id>
```

## Permission Levels

| Level | Description |
|-------|-------------|
| `view` | Can view only |
| `edit` | Can view and edit |
| `full_access` | Can view, edit, manage permissions, and share |

## Doc Types

Same as Drive: `docx`, `sheet`, `bitable`, `folder`, `file`, `slides`, `mindnote`, `wiki`.

## Link Sharing Options

| Value | Description |
|-------|-------------|
| `tenant_readable` | Anyone in organization can view |
| `tenant_editable` | Anyone in organization can edit |
| `anyone_readable` | Anyone with link can view |
| `anyone_editable` | Anyone with link can edit |
| `off` | Only collaborators can access |

## Behavior Notes

- `--user` accepts both raw open_id and contact names (resolved via ContactStore).
- `transfer` changes ownership. The bot must have `full_access` to transfer.
- `list` shows all collaborators with their permission levels.
- Bot needs `drive:permission`, `drive:permission:readonly` scopes.
