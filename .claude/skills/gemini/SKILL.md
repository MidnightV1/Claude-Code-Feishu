---
name: gemini
description: Search the web (搜索/搜一下), read URLs (看看这个链接), analyze images/documents, and summarize long content (总结一下) via Gemini CLI (subscription-based, no API cost). Default web search tool — use for any search task. Also handles URL reading, image understanding, document analysis (文件分析), and long content summarization that would bloat CC context.
---

# Gemini (Search / Web / Analyze / Summarize)

Unified Gemini CLI interface. Leverages Gemini's built-in tools (google_web_search, web_fetch, @file) — all at zero API cost (subscription-based).

## Tool

```
python3 .claude/skills/gemini/scripts/gemini_ctl.py <command> [args]
```

## Commands

```bash
# Web search (Gemini uses google_web_search internally)
python3 .claude/skills/gemini/scripts/gemini_ctl.py search "latest developments in AI agents"
python3 .claude/skills/gemini/scripts/gemini_ctl.py search "OpenAI o3 发布" --lang zh

# Read and process a URL (Gemini uses web_fetch internally)
python3 .claude/skills/gemini/scripts/gemini_ctl.py web "https://example.com/article"
python3 .claude/skills/gemini/scripts/gemini_ctl.py web "https://example.com/api-docs" --prompt "Extract authentication methods and rate limits"

# Analyze a file (image, PDF, code, text — any format)
python3 .claude/skills/gemini/scripts/gemini_ctl.py analyze /path/to/document.pdf
python3 .claude/skills/gemini/scripts/gemini_ctl.py analyze /path/to/image.png --prompt "Describe what's in this image"
python3 .claude/skills/gemini/scripts/gemini_ctl.py analyze /path/to/data.csv --prompt "What trends do you see?"

# Summarize long content (auto-detects URL vs file)
python3 .claude/skills/gemini/scripts/gemini_ctl.py summarize /path/to/long-report.pdf
python3 .claude/skills/gemini/scripts/gemini_ctl.py summarize "https://example.com/long-article" --max-chars 1000

# Check Gemini CLI availability
python3 .claude/skills/gemini/scripts/gemini_ctl.py status
```

## When to Use

| Scenario | Command | Why Gemini? |
|----------|---------|-------------|
| Web search (any language) | `search` | Google Search Grounding — default search, complements Brave |
| User shares a URL to read/summarize | `web` | Keeps large page content out of CC context |
| User asks about an uploaded image | `analyze` | Gemini handles images via @file, free |
| User asks about an uploaded document | `analyze` | Document stays with Gemini, CC context clean |
| Long article/transcript for summary | `summarize` | Handles large content without CC context bloat |

**Search routing**: Use `search` as the **default** for web search. Fall back to Brave Search MCP or CC WebSearch when Gemini CLI is unavailable or when you need structured result metadata (URLs only, count-based queries).

**Do NOT use when:**
- User needs CC tool capabilities (file editing, git, bash) — use CC directly
- Simple question CC can answer from knowledge — no need to delegate
- Need precise structured data extraction — CC is better at following exact output formats

## How It Works

Gemini CLI runs as a stateless subprocess with built-in tools:
- `google_web_search` — Google Search Grounding (real-time web search)
- `web_fetch` — fetch and process web pages
- `@/path/to/file` — inject local files into context (no upload cost)

Each invocation is independent (no session persistence in pipe mode). The script constructs a targeted prompt, pipes it to `gemini` via stdin, and returns plain text output.

## Context Strategy

- **CC never sees raw content** — Gemini processes it and returns a concise result
- **File analysis**: File stays on disk, Gemini reads via @file, CC gets the summary
- **Web content**: Gemini fetches and processes, CC gets the synthesis
- This keeps CC's context window focused on the conversation

## Options

| Option | Commands | Default | Description |
|--------|----------|---------|-------------|
| `--prompt TEXT` | analyze, web, summarize | (varies) | Custom instruction |
| `--lang zh\|en\|auto` | search | auto | Output language preference |
| `--max-chars N` | summarize | 1500 | Target output length |
| `--model MODEL` | all | (default) | Override Gemini model |
| `--timeout N` | all | varies | Timeout in seconds |

## File Locations

Documents uploaded via Feishu are stored at:
```
data/files/<session_key>/<timestamp>_<filename>
```

Use file_store metadata (shown in session context) to find the file path.

## Fallback

If Gemini CLI is unavailable or fails:
- **Search**: Use CC's WebSearch tool or Brave Search MCP
- **Web**: Use CC's WebFetch tool
- **Analyze (PDF)**: Read with CC's Read tool (pages parameter, max 20 pages/request)
- **Analyze (image)**: CC has native vision via Read tool
- **Summarize**: Read the file directly into CC context (watch for length)
