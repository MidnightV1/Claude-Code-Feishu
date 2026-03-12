---
name: feishu-drive
description: Manage Feishu Drive / cloud storage (云盘/云空间) — list files/folders, create folders, move/delete files, search, get file info, send images/files to chats. Use when the user mentions cloud drive (云盘/云空间), file management (文件管理), folders (文件夹), file organization (整理文件), finding files (找文件/找个文档), sending files/images (发文件/发图片/传文件), or wants to browse/organize files in Feishu Drive. DO NOT TRIGGER for reading/writing document content — use feishu-doc for that. Drive manages the file tree; doc manages content inside a document.
---

# Feishu Drive

Browse, organize, and manage files and folders in Feishu Drive. Send images and files to chats.

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

# Send an image to a chat/user
drive_ctl.py send-image /path/to/image.png <receive_id> [--id-type open_id|chat_id|user_id]

# Send a file to a chat/user (PDF, JSON, etc.)
drive_ctl.py send-file /path/to/file.pdf <receive_id> [--id-type open_id|chat_id|user_id]
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
