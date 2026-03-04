---
name: feishu-drive
description: Manage Feishu Drive (cloud storage) — list files/folders, create folders, move/delete files, get file info. Use when the user mentions cloud drive, file management, folders, or wants to organize files in Feishu Drive.
---

# Feishu Drive

Browse, organize, and manage files and folders in Feishu Drive.

## Tool

```
python3 .claude/skills/feishu-drive/scripts/drive_ctl.py <command> [args]
```

## Commands

```bash
# List files in root or a folder
drive_ctl.py list [--folder <folder_token>] [--type docx|sheet|bitable|folder|...] [--limit 20]

# Get file/folder metadata
drive_ctl.py info <token_or_url>

# Create a folder
drive_ctl.py mkdir "Folder Name" [--parent <folder_token>]

# Move a file/folder
drive_ctl.py move <file_token> <dest_folder_token> --type <file_type>

# Delete a file/folder (moves to trash)
drive_ctl.py delete <file_token> --type <file_type>

# Search files by name
drive_ctl.py search "query" [--type docx|sheet|...] [--limit 20]
```

## File Types

| Type | Description |
|------|-------------|
| `docx` | Document |
| `sheet` | Spreadsheet |
| `bitable` | Multidimensional table |
| `folder` | Folder |
| `file` | Uploaded file |
| `mindnote` | Mind map |
| `slides` | Presentation |

## URL Handling

Drive URLs are automatically parsed — pass full Feishu URLs directly to `info`.

## Behavior Notes

- `list` with no `--folder` shows root folder contents.
- `delete` moves to trash (recoverable), not permanent deletion.
- `search` uses the Drive file search API with keyword matching.
- Bot needs `drive:drive` and `drive:file` permission scopes.
