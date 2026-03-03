# claude-code-lark

Give Claude Code a Feishu/Lark messaging interface — with calendar, documents, daily briefings, and long-running task orchestration.

## What is this

A lightweight Python service that connects Claude Code CLI to Feishu (Lark) via WebSocket. Users chat with Claude in Feishu DMs or group chats, and Claude has full tool access (file I/O, shell, web search) through the CLI subprocess.

**Not a wrapper.** The bot runs an actual Claude Code CLI session per user, with persistent conversation context, tool use, and all Claude Code capabilities.

## Architecture

```
Feishu WebSocket ──> FeishuBot ──> LLMRouter ─┬─> claude -p       (claude-cli)
                        │              │       └─> google-genai     (gemini-api)
                        ├──> CronScheduler (per-job model routing)
                        └──> HeartbeatMonitor (gemini-api, low-cost)
                                  │
                           Dispatcher ──> Feishu Card JSON 2.0
```

Key components:

| Component | Role |
|-----------|------|
| `feishu_bot.py` | WebSocket event handler, debounce batching, multimodal input |
| `llm_router.py` | Session management, resume-or-fallback, history compression |
| `dispatcher.py` | Feishu card rendering, chunking, retry, real-time updates |
| `task_runner.py` | Long-running task orchestration with progress cards |
| `scheduler.py` | In-process cron scheduler (croniter + asyncio) |
| `heartbeat.py` | System health monitoring via LLM judgment |

## Features

- **Chat** — Full Claude Code conversations via Feishu DM or group @mention
- **Multimodal** — Image analysis (via Gemini Flash), PDF/file parsing, all injected as context
- **Calendar** — Create, list, update Feishu calendar events; manage contacts
- **Documents** — Create, read, search, comment on Feishu documents
- **Daily Briefings** — Multi-domain news pipeline: collect → generate → review → email/Feishu
- **Long Tasks** — Multi-step task execution with real-time progress cards
- **Cron Jobs** — Scheduled tasks with hot-reload (no restart needed)
- **Heartbeat** — Periodic system health checks with anomaly alerts
- **Session Continuity** — `--resume` first, fallback to compressed history injection

## Prerequisites

- Python 3.10+ with pip
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- A Feishu (Lark) custom app with Bot capability and WebSocket enabled
- Google AI Studio API key (for Gemini — multimodal, heartbeat, briefings)

## Quick Start

```bash
# Clone
git clone <repo-url> && cd claude-code-lark

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
tail -f data/nas-claude-hub.log
```

## Feishu App Setup

1. Go to [Feishu Open Platform](https://open.feishu.cn/app) → Create Custom App
2. Enable **Bot** capability
3. Enable **WebSocket** mode (not HTTP callback)
4. Subscribe to events:
   - `im.message.receive_v1` — receive messages
   - `im.message.recalled_v1` — handle message recall
   - `im.message.message_read_v1` — read receipts (optional)
5. Grant permissions:
   - `im:message` — send/receive messages
   - `im:message:send_as_bot` — send messages as bot
   - Additional per-skill (see each skill's setup guide)
6. Copy App ID and App Secret to `config.yaml`

## Skills

Skills are modular capabilities in `.claude/skills/`. Each has a `SKILL.md` with usage docs and a **First-Time Setup** section that guides the agent through configuration.

| Skill | Purpose | Key Config |
|-------|---------|------------|
| `hub-ops` | Cron jobs, service status, hot-reload | (built-in) |
| `briefing` | Daily news briefing pipeline | Gemini API key, domain configs |
| `feishu-cal` | Calendar event management | `feishu.calendar.calendar_id` |
| `feishu-doc` | Document CRUD, search, comments | `feishu.docs.shared_folders` |
| `long-task` | Multi-step task orchestration | (built-in) |

Each skill's `SKILL.md` contains an `ONBOARDING` section with a prerequisites checklist. The agent walks through this on first use, then removes the section once setup is confirmed.

## Configuration

See `config.yaml.example` for all options. Key sections:

| Section | Purpose |
|---------|---------|
| `feishu` | App credentials, calendar, docs, contacts |
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

If you're an AI agent setting up this service for the first time:

1. Read `CLAUDE.md` for project-level context and constraints
2. Read `config.yaml.example` and help the user create `config.yaml`
3. Check each skill's `SKILL.md` — the `ONBOARDING` sections tell you what to ask the user
4. After confirming each skill's setup, delete its `ONBOARDING` section
5. Run `./hub.sh start` and verify with `./hub.sh status`

The onboarding sections are designed as prompts for you. Walk the user through prerequisites, help them configure, then clean up.

## License

MIT
