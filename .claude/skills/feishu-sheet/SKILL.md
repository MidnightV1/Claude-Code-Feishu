---
name: feishu-sheet
description: Manage Feishu Spreadsheets (电子表格) — read metadata, list worksheets, read/write cell ranges. Use when the user mentions spreadsheet (电子表格/表格), cells (单元格), rows/columns, or wants to read/write data in a Feishu spreadsheet. NOT for Bitable/multidimensional tables (多维表格) — use feishu-bitable for those.
---

# Feishu Sheet

Read and write Feishu Spreadsheets (电子表格). Supports metadata, worksheet listing, cell range read/write.

## Prerequisites

- **Feishu app permission**: `sheets:spreadsheet` (read/write spreadsheet data)
- Bot must have access to the target spreadsheet (collaborator or shared folder)

## Tool

```
python3 .claude/skills/feishu-sheet/scripts/sheet_ctl.py <command> [args]
```

## Commands

```bash
# Get spreadsheet metadata (title, owner, revision)
sheet_ctl.py info <spreadsheet_token>

# List all worksheets (tabs) in a spreadsheet
sheet_ctl.py sheets <spreadsheet_token>

# Read a cell range (returns values)
sheet_ctl.py read <spreadsheet_token> <range>

# Write values to a cell range
sheet_ctl.py write <spreadsheet_token> <range> --values '[[1,"hello"],[2,"world"]]'
```

## Range Format

Ranges use the format `sheetId!A1:B5` or `sheetId!A:C` (full columns):
- `sheetId` is the worksheet ID (from `sheets` command), NOT the tab name
- Examples: `abc123!A1:D10`, `abc123!A1:A` (entire column A)

For convenience, if no `!` is present, the first worksheet is assumed.

## URL Handling

Both URL formats are supported:
- Direct: `https://xxx.feishu.cn/sheets/AbCdEfG123`
- Wiki-embedded: `https://xxx.feishu.cn/wiki/AbCdEfG123` (auto-resolves to actual spreadsheet token via wiki API)

## Value Types

Values in `--values` JSON are 2D arrays (rows of cells):
- **String**: `"hello"`
- **Number**: `123` or `3.14`
- **Boolean**: `true` / `false`
- **Null/empty**: `null`

Example: `--values '[["Name","Score"],["Alice",95],["Bob",88]]'`

## Scope & Boundaries

This skill is a **data I/O tool** for Feishu Spreadsheets:

**Use this skill for:**
- Metadata overview (title, worksheets, row/column counts)
- Small-range reads/writes (individual cells to tens of rows)
- Extracting data for external scripts, or writing results back

**Do NOT use this skill for:**
- Batch processing, filtering, classification, or aggregation over large datasets
- Complex conditional queries (unlike Bitable, Sheets has no server-side filter)
- Cross-sheet joins or transformations

For bulk data operations (e.g., classifying 700+ rows, LLM-based tagging, statistical analysis), write a dedicated Python script that calls the Sheets API directly for I/O and implements the processing logic in code.

## Behavior Notes

- `spreadsheet_token` is from the URL path (after `/sheets/`).
- `sheetId` identifies a worksheet tab (from `sheets` command output).
- `read` returns the `valueRange` object with cell values as a 2D array.
- `write` uses PUT to overwrite the specified range.
- Feishu Sheets API v2 is used for data read/write, v3 for metadata.
- This skill handles **电子表格** (spreadsheets). For **多维表格** (Bitable), use feishu-bitable.
