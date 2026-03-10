---
name: feishu-doc
description: Structured communication via Feishu documents (飞书文档/结构化沟通). Use when the user wants to create a document (写个文档/建个文档), write up discussion results, read a Feishu doc link, save content to a Feishu document, review/reply to document comments (评论), or analyze document annotations. Also auto-trigger when output contains 2+ structured points/sections (优先用文档而非聊天刷屏), or when a proposal needs discussion/confirmation (方案讨论用文档).
---

# Feishu Documents

不只是写文档——是结构化沟通工具。持久、可引用、可评论。

**自动触发规则**：
1. 输出包含 2+ 要点/结构化内容 → 优先用文档（避免聊天刷屏）
2. 方案需要讨论确认 → 优先用文档（用户可在文档内评论批注）

**与 feishu-wiki 的边界**：doc 是沟通产物（方案、评审、会议纪要）——有时效性，写完讨论完可能不再更新。wiki 是知识沉淀（规范、指南、FAQ）——持续维护，供反复查阅。

## Tool

```
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py <command> [args]
```

## Commands

```bash
# Create a document
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py create "文档标题"
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py create "会议纪要" --content "# 议题\n讨论内容\n# 结论\n决定事项"
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py create "项目方案" --share ou_xxxxx
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py create "项目方案" --owner ou_xxxxx  # create + transfer ownership

# Read a document (by ID or URL)
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py read Ojo1de7diofBVxxCaEHcL7GnnFf
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py read "https://xxx.feishu.cn/docx/Ojo1de7diofBVxxCaEHcL7GnnFf"

# Append content to existing document
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py append <doc_id> "新增内容"

# Replace entire document content (delete all blocks, write new)
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py update <doc_id> "# 新标题\n新内容"

# Replace a specific section (heading + body until next same-level heading)
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py replace <doc_id> --section "目标章节标题" "## 目标章节标题\n替换后的内容"

# Transfer document ownership (bot must be current owner)
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py transfer_owner <doc_id_or_url> <open_id>

# List documents in all shared folders (or a specific folder)
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py list
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py list --folder fldcnXXXXXXXX

# Search documents by keyword (matches file name)
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py search "预算"
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py search "项目" --folder fldcnXXXXXXXX

# List all comments on a document (with replies)
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py comments <doc_id_or_url>
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py comments "https://xxx.feishu.cn/docx/S6d8dO6r5oNl1bxCwBZcoUlUnke"

# Reply to a specific comment
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py reply <doc_id_or_url> <comment_id> "回复内容"

# Analyze document comments (structured context assembly for CC analysis)
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py analyze <doc_id_or_url>
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py analyze <doc_id_or_url> --all              # include resolved
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py analyze <doc_id_or_url> --context-chars 300 # wider context window
```

## Content Format

The `--content` parameter accepts plain text with markdown-style headings:
- `# Heading 1` → H1 block
- `## Heading 2` → H2 block
- Regular lines → Text blocks
- Empty lines are skipped

For complex formatting, create the doc first, then use `append` for additional content.

## Comments

The `comments` command lists all comments with their quoted text and replies. Output format:
```
Comment <comment_id> [RESOLVED]
  Quote: "被评论的原文"
  [reply_id] 回复内容
```

Use the `comment_id` from the output to reply with the `reply` command.

## Comment Analysis (analyze)

The `analyze` command is a **data assembly module** — it pulls document content + comments, anchors each comment to its document location, and outputs structured JSON for CC to reason over.

**Workflow** (triggered when user says "看看我在文档里的评论" or similar):
1. Run `analyze <doc_id>` → get structured JSON
2. Read the analysis prompt: `.claude/skills/feishu-doc/prompts/analyze_comments.md`
3. Apply the prompt's framework to analyze the JSON output
4. Reply in IM with structured analysis (overview → per-annotation → action items)

**Output structure:**
```json
{
  "doc_id": "...", "title": "...",
  "stats": {"shown": 3, "filter": "unresolved"},
  "annotations": [{
    "comment_id": "...",
    "quote": "被评论的原文",
    "context": {
      "before": "...前文...",
      "quoted": "被评论的原文",
      "after": "...后文...",
      "matched": true
    },
    "thread": [{"user_id": "ou_xxx", "text": "评论内容", "time": 1234567890}]
  }]
}
```

Default: only unresolved comments. Use `--all` for full history.

## Behavior Notes

- Documents created by the bot are owned by the app. Use `--owner` to transfer ownership to a user after creation (recommended for user-requested docs).
- Use `--share` to grant access without transferring ownership. `--owner` and `--share` can be used together (different users).
- Use `--folder` to specify a target folder token. Configure `feishu.docs.default_folder` in config.yaml.
- The `read` command accepts both raw document IDs and full Feishu URLs.
- Created documents return a clickable Feishu URL.
- `transfer_owner` requires the bot to be the current owner (true for bot-created docs). Requires `drive:drive:permission` scope.
- Comment commands default to `--file-type docx`. Use `--file-type sheet` for spreadsheets etc.

## Document Discovery

The `list` and `search` commands operate on **shared folders** — folders that have been explicitly shared with the bot app.

Configure in `config.yaml`:
```yaml
feishu:
  docs:
    shared_folders:
      - name: 工作文档
        token: fldcnXXXXXXXX
      - name: 项目资料
        token: fldcnYYYYYYYY
```

Without `--folder`, both commands scan all `shared_folders`. Search matches file names (case-insensitive).

**Limitation**: Server-side search API requires user OAuth token (not implemented). Current search does client-side keyword filtering within accessible folders.
