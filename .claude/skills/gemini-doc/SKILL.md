---
name: gemini-doc
description: Analyze documents (PDF, etc.) using Gemini CLI as a document co-pilot. Use when the user asks follow-up questions about a previously uploaded document, wants deeper analysis, or needs specific sections extracted. Keeps CC context clean by delegating to Gemini CLI (subscription-based, no API cost).
---

# Document Analysis (Gemini CLI)

Delegate document questions to Gemini CLI — keeps CC context clean by avoiding full-text injection.

## Tool

```bash
python3 .claude/skills/gemini-doc/scripts/gemini_doc_ctl.py <command> [args]
```

## Commands

```bash
# Summarize / analyze a document
python3 .claude/skills/gemini-doc/scripts/gemini_doc_ctl.py analyze <file_path>
python3 .claude/skills/gemini-doc/scripts/gemini_doc_ctl.py analyze <file_path> --prompt "Focus on financial data"

# Ask a question about a document
python3 .claude/skills/gemini-doc/scripts/gemini_doc_ctl.py query <file_path> "What are the key findings in section 3?"

# Check Gemini CLI availability
python3 .claude/skills/gemini-doc/scripts/gemini_doc_ctl.py status
```

## When to Use

- User uploaded a PDF and later asks follow-up questions about its content
- User wants specific information extracted from a document
- User asks "what does the document say about X?"
- Need to re-analyze a document with a different focus

## How It Works

Each invocation passes the file to Gemini CLI via `@file_path` syntax. Gemini CLI runs **stateless** (no session persistence in pipe mode), so the file is re-read each time. This is cheap because:
1. File is local (no upload latency)
2. Gemini subscription pricing (no per-token API cost)

## Context Strategy

- **Initial upload**: Pipeline injects a short summary into CC context (not full text)
- **Follow-up Q&A**: CC invokes this skill, gets targeted answer, relays to user
- **CC never sees full document text** — Gemini CLI is the document reader

## File Locations

Documents uploaded via Feishu are stored at:
```
data/files/<session_key>/<timestamp>_<filename>
```

Use file_store metadata (shown in session context) to find the file path.

## Fallback

If Gemini CLI is unavailable (exit code 1), fall back to reading the file directly with the Read tool (PDF: `pages` parameter required, max 20 pages per request).
