---
name: feishu-bitable
description: Manage Feishu Bitable / multidimensional tables (多维表格) — list apps/tables, query/add/update/delete records with filtering. Use when the user mentions bitable, multidimensional table (多维表格), spreadsheet database (数据表), structured data, or wants to query/update table records in Feishu. DO NOT TRIGGER for regular spreadsheets (电子表格/单元格) — use feishu-sheet for those. Bitable is a database with typed fields and views; Sheet is a cell-based spreadsheet.
---

# Feishu Bitable

CRUD for Feishu Bitable (multidimensional tables / Base). Query records, add rows, update fields, manage tables.

## Prerequisites

- **Feishu app permission**: `bitable:app` (read/write bitable data)
- Bot must be added as a collaborator on the target Bitable app (or the app must be in a shared folder accessible to the bot)

## Tool

```
python3 .claude/skills/feishu-bitable/scripts/bitable_ctl.py <group> <command> [args]
```

## Commands

```bash
# List tables in a bitable app
bitable_ctl.py table list <app_token>

# Get table fields (schema)
bitable_ctl.py table fields <app_token> <table_id>

# Query records (with optional filter)
bitable_ctl.py record list <app_token> <table_id> [--filter 'CurrentValue.[Status]="Done"'] [--limit 20]

# Get a single record
bitable_ctl.py record get <app_token> <table_id> <record_id>

# Add a record
bitable_ctl.py record add <app_token> <table_id> --fields '{"Name":"Test","Status":"Todo"}'

# Update a record
bitable_ctl.py record update <app_token> <table_id> <record_id> --fields '{"Status":"Done"}'

# Delete a record
bitable_ctl.py record delete <app_token> <table_id> <record_id>
```

## URL Handling

Bitable URLs like `https://xxx.feishu.cn/base/AbCdEfG123?table=tblXXX` are automatically parsed — extract `app_token` and `table_id` from the URL.

## Field Types

Common field value formats in `--fields` JSON:
- **Text**: `"field_name": "value"`
- **Number**: `"field_name": 123`
- **Select**: `"field_name": "option_name"`
- **Multi-select**: `"field_name": ["opt1", "opt2"]`
- **Date**: `"field_name": 1709539200000` (millisecond timestamp)
- **Checkbox**: `"field_name": true`
- **Person**: `"field_name": [{"id": "ou_xxx"}]`
- **URL**: `"field_name": {"link": "https://...", "text": "label"}`

## Behavior Notes

- `app_token` identifies the Bitable app (from URL path).
- `table_id` identifies a specific table within the app (starts with `tbl`).
- `record list` returns up to `--limit` records (default 20, max 500).
- `--filter` uses Feishu formula syntax (e.g., `CurrentValue.[Status]="Done"`).
- The bot needs `bitable:app` permission scope.
