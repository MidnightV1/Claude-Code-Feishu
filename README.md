# claude-code-feishu

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://python.org)

English | [中文](README.zh-CN.md)

Give Claude Code a Feishu/Lark messaging interface — with calendar, documents, tasks, wiki, daily briefings, and autonomous health monitoring.

## What is this

A lightweight Python service that connects Claude Code CLI to Feishu (Lark) via WebSocket. Users chat with Claude in Feishu DMs or group chats, and Claude has full tool access (file I/O, shell, web search) through the CLI subprocess.

**Not a wrapper.** The bot runs an actual Claude Code CLI session per user, with persistent conversation context, tool use, and all Claude Code capabilities.

## Why this project

There are many Feishu/Lark AI bots out there — most wrap an LLM API for basic chat. This project takes a different approach:

- **Claude Code CLI native** — Not an API wrapper. Each user gets a real `claude -p` subprocess with full tool access: file I/O, shell, code editing, web search, sub-agents. The bot is Claude Code, not a chatbot that calls Claude.
- **Deep Feishu integration** — 7 purpose-built Skills (calendar, documents, tasks, wiki, daily briefings, heartbeat monitoring, document co-pilot). Not just message forwarding — Claude can create calendar events, write Feishu docs, manage tasks, and browse your wiki directly.
- **Self-evolving daily briefings** — Automated news pipeline with keyword evolution: after each run, the LLM identifies coverage gaps and improves the search keywords. The briefing gets smarter over time without manual tuning.
- **Multi-LLM routing with fallback chains** — Claude CLI for chat, Gemini CLI for document analysis, Gemini API for multimodal. Each capability has a degradation path (e.g. PDF: Gemini CLI → Gemini API → Claude Read).
- **Session continuity** — `--resume` preserves full CLI context; on failure, Sonnet compresses history into structured summaries for seamless recovery.
- **Self-hosted on minimal hardware** — Designed for NAS/low-power servers. Single Python process, no Docker/Redis/database required.

## Architecture

```
Feishu WebSocket ──> FeishuBot ──> LLMRouter ─┬─> claude -p       (claude-cli)
                        │              │       ├─> gemini cli       (gemini-cli)
                        │              │       └─> google-genai     (gemini-api)
                        ├──> CronScheduler (per-job model routing)
                        └──> HeartbeatMonitor (two-layer triage → action)
                                  │
                           Dispatcher ──> Feishu Card JSON 2.0
```

Key components:

| Package | Role |
|---------|------|
| `agent/platforms/feishu/` | WebSocket bot, session management, media processing, card dispatch |
| `agent/llm/` | Multi-model routing, resume-or-fallback, history compression |
| `agent/jobs/` | Cron scheduler, heartbeat monitor, briefing launcher |
| `agent/infra/` | Shared models, atomic JSON store, session file management |

## Features

- **Chat** — Full Claude Code conversations via Feishu DM or group @mention
- **Multimodal** — Image understanding (native Claude vision), PDF/file analysis (Gemini CLI → API → Claude fallback)
- **Calendar** — Create, list, update, delete Feishu calendar events; contact management
- **Documents** — Create, read, search, comment on Feishu documents; ownership transfer
- **Tasks** — Feishu task CRUD with assignees, due dates, and heartbeat deadline monitoring
- **Wiki** — Browse wiki spaces, create/move/read/write wiki pages
- **Daily Briefings** — Automated multi-domain news digest (see below)
- **Document Co-pilot** — Deep document analysis via Gemini CLI without polluting chat context
- **Progress Tracking** — Real-time TodoWrite progress on thinking cards for complex tasks
- **Cron Jobs** — Scheduled tasks with hot-reload (no restart needed)
- **Heartbeat** — Periodic health checks with two-layer LLM triage and DM alerts
- **Session Continuity** — `--resume` first, fallback to compressed history injection

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
- (Optional) Gemini CLI for document analysis co-pilot

## Quick Start

```bash
# Clone
git clone <repo-url> && cd claude-code-feishu

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

For detailed setup with an AI agent guiding you through each step, see [SETUP.md](SETUP.md).

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

Skills are modular capabilities in `.claude/skills/`. Each has a `SKILL.md` with usage docs and a setup guide.

| Skill | Purpose | Key Config |
|-------|---------|------------|
| `hub-ops` | Cron jobs, service status, hot-reload | (built-in) |
| `briefing` | Daily news briefing pipeline | Gemini API key, domain configs |
| `feishu-cal` | Calendar event CRUD, contacts | `feishu.calendar.calendar_id` |
| `feishu-doc` | Document CRUD, search, comments | `feishu.docs.shared_folders` |
| `feishu-task` | Task management, deadline monitoring | `feishu.tasks.tasklist_guid` |
| `feishu-wiki` | Wiki space and page management | (add bot to wiki space) |
| `gemini-doc` | Document analysis co-pilot | Gemini CLI installed |

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

If you're a Claude Code instance setting up this service for the first time, read [`SETUP.md`](SETUP.md) — it's a step-by-step bootstrap guide designed for you to walk the user through the entire setup interactively.

## License

[MIT](LICENSE)
