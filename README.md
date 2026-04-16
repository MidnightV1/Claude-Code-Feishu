# claude-code-lark

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://python.org)

English | [中文](README.zh-CN.md)

Give Claude Code a Feishu/Lark messaging interface — with autonomous development pipelines, quality assurance, 25 modular skills, and multi-LLM orchestration.

## What is this

A Python service that connects Claude Code CLI to Feishu (Lark) via WebSocket. Users chat with Claude in Feishu DMs or group chats, and Claude has full tool access (file I/O, shell, web search) through the CLI subprocess.

**Not a wrapper.** The bot runs an actual Claude Code CLI session per user, with persistent conversation context, tool use, and all Claude Code capabilities.

## Why this project

There are many Feishu/Lark AI bots out there — most wrap an LLM API for basic chat. This project takes a different approach:

- **Claude Code CLI native** — Not an API wrapper. Each user gets a real `claude -p` subprocess with full tool access: file I/O, shell, code editing, web search, sub-agents. The bot is Claude Code, not a chatbot that calls Claude.
- **Autonomous development pipeline (MADS)** — Multi-Agent Development System with diagnosis, contract negotiation, fix, QA, and merge stages. Complexity routing (L1–L5) sends simple bugs to Sonnet and architectural changes to Opus. Scope guards prevent drift; git worktree isolation enables parallel tickets.
- **Automated quality assurance (MAQS)** — Multi-Agent Quality System discovers bugs from error logs, creates tickets, and feeds them into the MADS pipeline. Stale ticket recovery and QA verdict loops close the feedback cycle.
- **Deep Feishu integration** — 25 purpose-built Skills covering calendar, documents, tasks, wiki, bitable, drive, boards, sheets, permissions, and more. Not just message forwarding — Claude can create calendar events, write Feishu docs, manage tasks, and browse your wiki directly.
- **Self-evolving daily briefings** — Automated news pipeline with keyword evolution: after each run, the LLM identifies coverage gaps and improves search keywords. The briefing gets smarter over time without manual tuning.
- **Sentinel** — Autonomous entropy control with health pulse, code quality, and doc audit scanners. Signal aggregation detects degradation before users notice.
- **Multi-LLM routing with fallback chains** — Claude CLI for chat, Gemini CLI for web search and document analysis, Gemini API for multimodal. Each capability has a degradation path (e.g. PDF: Gemini CLI → Gemini API → Claude Read).
- **Session continuity** — `--resume` preserves full CLI context; on failure, Sonnet compresses history into structured summaries for seamless recovery.
- **Self-hosted on minimal hardware** — Designed for any always-on machine (home server, mini PC, cloud VM). Single Python process, no Docker/Redis/database required.

## Architecture

```
Feishu WebSocket ──> FeishuBot ──> LLMRouter ─┬─> claude -p       (claude-cli)
                        │              │       ├─> gemini cli       (gemini-cli)
                        │              │       └─> google-genai     (gemini-api)
                        ├──> CronScheduler (per-job model routing)
                        ├──> LoopExecutor (MADS ticket lifecycle)
                        ├──> Sentinel (health / code / doc scanners)
                        └──> HeartbeatMonitor (two-layer triage → action)
                                  │
                           Dispatcher ──> Feishu Card JSON 2.0
```

Key components:

| Component | Role |
|-----------|------|
| `feishu_bot.py` | WebSocket event handler, debounce batching, multimodal input |
| `llm_router.py` | Session management, resume-or-fallback, history compression |
| `dispatcher.py` | Feishu card rendering, chunking, secret scanning, retry |
| `claude_cli.py` | Claude CLI wrapper with streaming, idle-based timeout |
| `loop_executor.py` | MADS ticket orchestration with priority queue and preemption |
| `worker.py` | Concurrent workers with git worktree isolation |
| `sentinel/` | Autonomous scanners: health pulse, code quality, doc audit |
| `scheduler.py` | In-process cron scheduler with hot-reload (SIGUSR1) |
| `heartbeat.py` | System health monitoring via LLM judgment |

## Features

### Core
- **Chat** — Full Claude Code conversations via Feishu DM or group @mention
- **Multimodal** — Image understanding (native Claude vision), PDF/file analysis (Gemini CLI → API → Claude fallback)
- **TTS** — Text-to-speech via Fish.audio S2-Pro, delivered as Feishu voice messages
- **Progress Tracking** — Real-time TodoWrite progress on thinking cards for complex tasks
- **Session Continuity** — `--resume` first, fallback to compressed history injection

### Development Pipeline (MADS)
- **Diagnosis** — Opus-driven root cause analysis with codebase exploration
- **Contract** — Scope negotiation with affected files, acceptance criteria, complexity routing
- **Fix** — Sonnet implementation with scope guards (Hardgate) preventing drift
- **QA** — Automated test verification with reject → retry loops (max 3 rounds)
- **Merge** — Merge queue with rebase-on-conflict recovery
- **Design** — Opus design documents for composite tasks (L4+)
- **Decompose** — Split complex fixes into atomic, independently testable work items

### Quality Assurance (MAQS)
- **Error Tracker** — Aggregates runtime errors for automated bug discovery
- **Auto-ticketing** — Discovers bugs from error logs, creates MADS tickets
- **Stale Recovery** — Detects and recovers abandoned tickets

### Autonomous Operations
- **Sentinel** — Health pulse, code quality, and doc audit scanners with signal aggregation
- **Heartbeat** — Periodic health checks with two-layer LLM triage and DM alerts
- **Exploration** — Discover research directions from conversations, tasks, and errors
- **Cron Jobs** — Scheduled tasks with hot-reload (no restart needed)
- **Daily Briefings** — Automated multi-domain news digest (see below)

### Feishu Integration
- **Calendar** — Create, list, update, delete events; contact management
- **Documents** — Create, read, search, comment, ownership transfer; block tree traversal
- **Tasks** — CRUD with assignees, due dates, heartbeat deadline monitoring
- **Wiki** — Browse spaces, create/move/read/write pages
- **Bitable** — Multidimensional table CRUD, record query/filter
- **Drive** — File/folder management, search, send media to chats
- **Boards** — Whiteboard creation, flowchart drawing, node reading
- **Sheets** — Spreadsheet read/write, worksheet management
- **Permissions** — Document sharing, collaborator CRUD, public links

## Daily Briefings

An automated news digest pipeline that runs on a cron schedule:

```
Brave Search → Collect articles → Gemini generates draft → Claude reviews → Deliver via email/Feishu
```

- **Multi-domain**: Configure separate briefing domains (e.g. "tech", "finance"), each with its own keywords, prompts, and delivery targets
- **Keyword evolution**: After each cycle, the LLM analyzes coverage gaps and suggests new search keywords — the keyword list improves over time
- **Review layer**: Optional Claude review pass catches hallucinations and improves quality before delivery
- **Flexible delivery**: Email (SMTP), Feishu IM card, Feishu document, or any combination

Each domain is a folder under `~/briefing/domains/<name>/` with `sources.yaml` (keywords), `domain.yaml` (models + delivery config), and prompt templates. See `.claude/skills/briefing/SKILL.md` for full setup.

## Prerequisites

- Python 3.10+ with pip
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- A Feishu (Lark) custom app with Bot capability and WebSocket enabled
- Google AI Studio API key (for Gemini — multimodal, heartbeat, briefings)
- (Optional) Gemini CLI for web search and document analysis
- (Optional) Fish.audio API key for TTS voice messages

## Quick Start

```bash
# Clone
git clone https://github.com/MidnightV1/claude-code-lark.git && cd claude-code-lark

# Install dependencies
pip install -r requirements.txt

# Configure
cp config.yaml.example config.yaml
# Edit config.yaml — fill in Feishu app credentials, Gemini API key, etc.

# Start
./hub.sh start

# Check status
./hub.sh status

# View logs
tail -f data/hub.log
```

For detailed setup with an AI agent guiding you through each step, see [SETUP.md](docs/SETUP.md).

## Feishu App Setup

We recommend creating **two** Feishu apps — one for interactive chat, one for notifications/alerts:

| App | Purpose | Why separate |
|-----|---------|-------------|
| **Chat Bot** | User conversations, tool use | Interactive sessions, per-user context |
| **Notifier** | Heartbeat alerts, briefing delivery, scheduled messages | Background tasks, no user session needed |

This separation keeps notification delivery independent of chat sessions. A single app works too — just skip the `notify` section in config.

### For each app:

1. Go to [Feishu Open Platform](https://open.feishu.cn/app) → Create Custom App
2. Enable **Bot** capability
3. Enable **WebSocket** mode (not HTTP callback) — *chat app only*
4. Subscribe to events (chat app only):
   - `im.message.receive_v1` — receive messages
   - `im.message.recalled_v1` — handle message recall
5. Grant permissions — import [`docs/feishu_scopes.json`](docs/feishu_scopes.json) for a complete set, or grant individually per skill
6. Publish a version to activate the bot
7. Copy App ID and App Secret to `config.yaml`

## Skills

Skills are modular capabilities in `.claude/skills/`. Each has a `SKILL.md` with usage docs and a setup guide. 25 skills included:

### Feishu Platform
| Skill | Purpose |
|-------|---------|
| `feishu-cal` | Calendar event CRUD, contacts |
| `feishu-doc` | Document CRUD, search, comments, block tree read |
| `feishu-task` | Task management, deadline monitoring |
| `feishu-wiki` | Wiki space and page management |
| `feishu-bitable` | Multidimensional table CRUD, record query/filter |
| `feishu-drive` | Cloud file/folder management, search, media send |
| `feishu-board` | Board/whiteboard creation, flowchart drawing |
| `feishu-sheet` | Spreadsheet read/write, worksheet management |
| `feishu-perm` | Document permission management, sharing |

### Development & Quality
| Skill | Purpose |
|-------|---------|
| `dev-pipeline` | Unified MADS/MAQS entry point: ticket creation, status, stage control |
| `sentinel` | Autonomous health/code/doc scanners, signal aggregation |
| `visual-qa` | CDP screenshot, accessibility tree, five-dimension scoring |
| `codex` | OpenAI Codex CLI integration for code review and task handoff |
| `skill-creator` | Skill development framework: create, test, evaluate, iterate |

### Search & Intelligence
| Skill | Purpose |
|-------|---------|
| `gemini` | Web search, URL reading, file analysis, summarization (zero API cost) |
| `brave-web-search` | Brave Web Search for English-language sources |
| `brave-news-search` | Brave News Search with freshness filtering |
| `arxiv-tracker` | ArXiv paper tracking with keyword pre-filter + LLM evaluation |

### Operations & Utilities
| Skill | Purpose |
|-------|---------|
| `hub-ops` | Cron jobs, service status, hot-reload |
| `briefing` | Daily news briefing pipeline management |
| `weather` | Weather queries with location persistence |
| `plan-review` | CEO/Founder-mode plan review (4 scope modes) |

### Social Media
| Skill | Purpose |
|-------|---------|
| `twitter-cli` | Twitter/X read, search, post, bookmarks |
| `xiaohongshu-cli` | Xiaohongshu/RED notes, trending, posting |
| `bilibili-cli` | Bilibili videos, trending, subtitles, AI summary |

Each skill is opt-in — activate only what you need.

## Configuration

See `config.yaml.example` for all options. Key sections:

| Section | Purpose |
|---------|---------|
| `feishu` | App credentials, calendar, docs, tasks, contacts |
| `llm` | Default provider/model, CLI paths, timeouts |
| `gemini-api` | Google AI Studio API key |
| `briefing` | Briefing pipeline model config |
| `scheduler` | Enable/disable cron, store path |
| `heartbeat` | Interval, active hours, LLM model |
| `notify` | Optional second Feishu app for alerts |

## Service Management

```bash
./hub.sh start       # Start in background
./hub.sh stop        # Stop gracefully
./hub.sh restart     # Stop + start
./hub.sh status      # Check if running
./hub.sh watchdog    # Start if not running (for cron)
```

## For AI Agents

If you're a Claude Code instance setting up this service for the first time, read [`SETUP.md`](docs/SETUP.md) — it's a step-by-step bootstrap guide designed for you to walk the user through the entire setup interactively.

## License

[MIT](LICENSE)
