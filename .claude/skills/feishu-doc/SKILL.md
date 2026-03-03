---
name: feishu-doc
description: Create, read, edit, comment on, and analyze Feishu documents. Use when the user wants to create a document, write up discussion results, read a Feishu doc link, save content to a Feishu document, review/reply to document comments, or analyze document annotations.
---

# Feishu Documents

Create, manage, and collaborate on Feishu documents (new docx format).

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

# Read a document (by ID or URL)
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py read Ojo1de7diofBVxxCaEHcL7GnnFf
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py read "https://xxx.feishu.cn/docx/Ojo1de7diofBVxxCaEHcL7GnnFf"

# Append content to existing document
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py append <doc_id> "新增内容"

# List recent documents
python3 .claude/skills/feishu-doc/scripts/doc_ctl.py list

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

- Documents are created in the app's space by default. Use `--share` to grant access to a user.
- Use `--folder` to specify a target folder token. Configure `feishu.docs.default_folder` in config.yaml.
- The `read` command accepts both raw document IDs and full Feishu URLs.
- Created documents return a clickable Feishu URL.
- Comment commands default to `--file-type docx`. Use `--file-type sheet` for spreadsheets etc.
