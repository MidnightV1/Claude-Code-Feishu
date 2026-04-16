# Test Spec: agent.platforms.feishu.utils

## Purpose

`text_to_blocks` is the **single chokepoint** for ALL Feishu document output.
Any regression here breaks every document write, every card render, every briefing.
It is pure and deterministic — the same input always produces the same block list —
which makes golden-file comparison the definitive test oracle. The LLM reports diffs
computed by the runner; it cannot fabricate a pass.

## Module Location

`agent/platforms/feishu/utils.py`

## Functions Under Test

### text_to_blocks(text) -> list[dict]

Converts markdown-flavoured text into Feishu docx block children.
Block types: 2=text, 3=heading1, 4=heading2, …8=heading6, 12=bullet, 13=ordered,
14=code, 22=divider, `{"_table": rows}` for tables.

| ID | Scenario | Input | Expected | Priority |
|----|----------|-------|----------|----------|
| U01 | Empty input | `""` | `[]` | P0 |
| U02 | Plain text | `"hello"` | `[{block_type:2, text:{elements:[{text_run:{content:"hello"}}]}}]` | P0 |
| U03 | H1 heading | `"# 标题"` | `[{block_type:3, heading1:{elements:[…]}}]` | P0 |
| U04 | H2–H6 range | `"## H2"` … `"###### H6"` | block_type 4–8 respectively | P0 |
| U05 | Code block — known lang | `"```python\nprint(1)\n```"` | `[{block_type:14, code:{style:{language:50}, elements:[…]}}]` | P0 |
| U06 | Code block — unknown lang `xyz` | `"```xyz\ncode\n```"` | `language:1` (default fallback) | P1 |
| U07 | Unclosed code fence | `"```python\ncode"` (no closing fence) | `[{block_type:14}]` — collects to EOF | P1 |
| U08 | Empty code block body | `"```\n```"` | `language:1`, content `" "` (Feishu rejects empty) | P1 |
| U09 | Bullet list `- item` | `"- item"` | `[{block_type:12, bullet:{elements:[…]}}]` | P0 |
| U10 | Bullet list `* item` | `"* item"` | same as U09 | P1 |
| U11 | Ordered list | `"1. item"` | `[{block_type:13, ordered:{elements:[…]}}]` | P0 |
| U12 | Blockquote | `"> quote"` | `block_type:2`, content starts with `▎` | P1 |
| U13 | Empty blockquote | `">"` | `block_type:2`, content `"▎"` | P2 |
| U14 | Divider `---` | `"---"` | `[{block_type:22, divider:{}}]` | P1 |
| U15 | Divider must be exactly 3+ dashes | `"----"` | `block_type:22` | P2 |
| U16 | Table 2×2 | markdown table 2 cols | `[{"_table": [[header], [row]]}]` | P0 |
| U17 | Table 10+ cols | wide markdown table | `_table` entry present; wide cols are NOT truncated by text_to_blocks (truncation happens in append_markdown_to_doc) | P1 |
| U18 | Mixed content | heading + code + table + list | all four block types present | P0 |
| U19 | Inline bold `**text**` | `"**粗体**"` | `text_run` with `text_element_style:{bold:True}` | P0 |
| U20 | Inline code `` `code` `` | `"`code`"` | `text_run` with `text_element_style:{inline_code:True}` | P0 |
| U21 | Inline link https | `"[text](https://url)"` | `text_run` with `text_element_style:{link:{url:…}}` | P1 |
| U22 | Relative URL link — rejected | `"[text](./path)"` | plain `text_run`, no link key | P2 |
| U23 | Special chars `<>&\x00` | control chars present | no crash; `\x00`–`\x08`, `\x0b`, `\x0c`, `\x0e`–`\x1f` stripped | P0 |
| U24 | Literal `\n` in input string | `"line1\\nline2"` | treated as actual newline (replaced before split) | P1 |
| U25 | Real complex doc | fixture: `real_messages/mixed_content.txt` | block count > 5, multiple block types present | P0 |
| U26 | Real table doc | fixture: `real_messages/complex_table.txt` | contains `_table` entry | P0 |
| U27 | H7–H9 headings (beyond H6) | `"####### H7"` | block_type = 9 (7 hashes → 2+7=9; Feishu may reject, but parser produces it) | P2 |
| U28 | Blank lines skipped | `"\n\n\nhello\n\n"` | `[{block_type:2, …content:"hello"…}]` — empties skipped | P0 |

### _parse_inline(text) -> list[dict]

Parses inline markdown into `text_run` elements. No side effects.

| ID | Scenario | Input | Expected |
|----|----------|-------|----------|
| I01 | Plain text | `"hello"` | `[{text_run:{content:"hello"}}]` |
| I02 | Bold only | `"**bold**"` | `[{text_run:{content:"bold", text_element_style:{bold:True}}}]` |
| I03 | Inline code only | `` "`code`" `` | `[{text_run:{content:"code", text_element_style:{inline_code:True}}}]` |
| I04 | https link | `"[t](https://x.com)"` | `[{text_run:{content:"t", text_element_style:{link:{url:"https://x.com"}}}}]` |
| I05 | http link | `"[t](http://x.com)"` | link accepted (http:// allowed) |
| I06 | Relative link | `"[t](./foo)"` | `[{text_run:{content:"t"}}]` — no link (Feishu rejects non-http) |
| I07 | Mixed `a **b** c` | `"a **b** c"` | 3 elements: plain "a ", bold "b", plain " c" |
| I08 | Empty string | `""` | `[{text_run:{content:""}}]` (no match → single plain element) |
| I09 | Bold wrapping code | `"**`code`**"` | bold group wins (outer regex match) — no crash |

### _split_table_rows(rows) -> list[list[list[str]]]

Splits oversized tables into chunks respecting Feishu limits:
- `TABLE_MAX_ROWS = 100`
- `TABLE_MAX_COLS = 9`
- `TABLE_MAX_CELLS = 200`
- Header row repeated in each chunk.

| ID | Scenario | Rows | Cols | Expected Chunks |
|----|----------|------|------|----------------|
| T01 | Small table fits in one chunk | 5 | 3 | 1 chunk, all rows |
| T02 | 9-col table: cell limit bites first | 23 | 9 | 2 chunks (200//9=22 max total rows, chunk_size=21) |
| T03 | 2-col table: row limit bites first | 101 | 2 | 2 chunks (200//2=100, cap at 100, chunk_size=99) |
| T04 | Header repeated | 23 | 9 | each chunk starts with rows[0] |
| T05 | Single-row table (header only) | 1 | 3 | 1 chunk |
| T06 | Exactly at limit | 22 | 9 | 1 chunk (≤ chunk_size+1) |

### _sanitize_doc_text(text) -> str

Strips control characters to prevent Feishu API 400 errors.

| ID | Input | Expected |
|----|-------|----------|
| S01 | Null byte `\x00` in text | stripped |
| S02 | `\x08` (backspace) | stripped |
| S03 | `\n` (newline) | preserved |
| S04 | `\t` (tab) | preserved |
| S05 | `\r` (carriage return) | preserved |
| S06 | `\x1f` (unit separator) | stripped |
| S07 | Normal ASCII text | unchanged |
| S08 | Chinese text | unchanged |

### parse_dt(s) -> int

Parses datetime strings to Unix timestamps (seconds, Asia/Shanghai TZ).

| ID | Input | Expected |
|----|-------|----------|
| P01 | `"2026-03-23"` | midnight CST for that date |
| P02 | `"2026-03-23T09:00"` | 09:00 CST |
| P03 | `"2026-03-23 09:00"` (space sep) | same as P02 |
| P04 | `"09:00"` (HH:MM today) | today at 09:00 CST, or tomorrow if past |
| P05 | `"+2h"` | now + 2 hours |
| P06 | `"+30m"` | now + 30 minutes |
| P07 | `"tomorrow 09:00"` | tomorrow 09:00 CST |
| P08 | `"tomorrow"` (no time) | tomorrow 09:00 CST (default) |
| P09 | Invalid string `"notadate"` | sys.exit(1) |
| P10 | Invalid unit `"+5d"` | sys.exit(1) |

## Golden Files

- `golden/rendering/blocks_*.json` — snapshot of `text_to_blocks` output for specific inputs
- Updated via `pytest --update-golden` (flag sets env var `UPDATE_GOLDEN=1`)
- Diffs computed by runner (subprocess diff or `deepdiff`), not by the LLM

## Test File

`tests/unit/test_utils.py`

## Fixtures

| File | Purpose |
|------|---------|
| `fixtures/real_messages/mixed_content.txt` | Real complex markdown: headings, code blocks, tables, bullets |
| `fixtures/real_messages/complex_table.txt` | Real wide/long table to exercise `_split_table_rows` path |

## Risk: LLM Faking Results

`text_to_blocks` is a pure function — deterministic — so golden file comparison is the
definitive oracle. The runner computes the diff, not the LLM. The test suite uses
`pytest-json-report` to emit machine-readable `report.json`; the LLM reads that file,
it does not interpret pytest's human-readable stdout.

Reference: @geniusvczh tweet — Opus generated plausible-looking compiler output for a
program it had never actually run. The same failure mode applies here: an LLM asked
"did the tests pass?" can hallucinate "yes" while the binary never executed.
The XML state protocol + PID tracking + exit-code verification make this impossible.
