---
name: feishu-doc
description: Create, read, and edit Feishu documents. Use when the user wants to create a document, write up discussion results, read a Feishu doc link, or save content to a Feishu document.
---

# Feishu Documents

Create and manage Feishu documents (new docx format).

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
```

## Content Format

The `--content` parameter accepts plain text with markdown-style headings:
- `# Heading 1` → H1 block
- `## Heading 2` → H2 block
- Regular lines → Text blocks
- Empty lines are skipped

For complex formatting, create the doc first, then use `append` for additional content.

## Behavior Notes

- Documents are created in the app's space by default. Use `--share` to grant access to a user.
- Use `--folder` to specify a target folder token. Configure `feishu.docs.default_folder` in config.yaml.
- The `read` command accepts both raw document IDs and full Feishu URLs.
- Created documents return a clickable Feishu URL.
