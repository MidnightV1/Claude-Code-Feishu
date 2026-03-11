# Claude Code Feishu

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://python.org)

English | [中文](README.md)

Claude Code Feishu — let AI truly collaborate with you.

## From Tool to Colleague

Claude Code is a powerful programming tool in the terminal. But coding is only part of the job — you also need to schedule meetings, write proposals, track tasks, do research, and monitor progress.

These things happen in Feishu/Lark, not in the IDE.

This project brings Claude Code into Feishu, but it's not just "message forwarding." It gives Claude full Feishu collaboration capabilities: calendar, documents, tasks, wiki, bitable, drive. Combined with Claude Code's native file I/O, code editing, shell execution, and sub-agent coordination — you get an AI collaborator whose capabilities far exceed the CLI.

## Three Scenarios

**Scenario 1: Opening Feishu in the morning**

You haven't sent a message yet, but AI is already working. Daily briefings arrive automatically — keywords self-evolve, coverage gets more precise over time. Overdue task reminders appear in your DM. This isn't passive response — it's proactive collaboration.

**Scenario 2: One sentence becomes three people's work**

You say "fix the briefing link bug, and add a replace feature to feishu-doc skill." Opus researches both issues, designs solutions, splits them into independent subtasks for Sonnet to execute in parallel. You keep chatting with Opus about other things — when execution finishes, it validates and reports back.

**Scenario 3: Proposal discussion stays in Feishu**

No need to jump between CLI and Feishu. Claude creates a Feishu document to write the proposal, you add comments in the document, Claude reads and responds to each comment, then updates the same document. Tasks, calendar, wiki — all extensions of the same conversation.

## CLI vs Feishu: More Than a Different Interface

| Dimension | CLI / IDE | Feishu Collaboration |
|-----------|-----------|---------------------|
| Interaction | You initiate commands | It also reaches out (briefings, deadline reminders, alerts) |
| Capabilities | Code + Files + Shell | + Calendar + Docs + Tasks + Wiki + Bitable |
| Collaboration | 1:1 synchronous | Async — you can leave, it keeps working, notifies when done |
| Identity | Anonymous session | Recognizes each user, maintains independent context |
| Task Orchestration | You split and assign | Opus designs → Sonnet executes in parallel → Opus validates |
| Multi-user | Single user | Multiple users with isolated sessions |
| Context Recovery | Close and it's gone | Resume preserves full context, compressed fallback on failure |
| Multi-Bot | Single instance | Multiple bot instances with independent personas, models, sessions, and HOME isolation |

## Architecture

Single Python process — no Docker, Redis, or database required.

```
Feishu WebSocket → FeishuBot → LLMRouter ─┬→ claude -p    (chat/tools)
                      │            │       ├→ gemini cli   (search/docs)
                      │            │       └→ gemini api   (large docs/fallback)
                      ├→ Orchestrator (Opus planning + Sonnet worker pool)
                      ├→ CronScheduler (scheduled jobs)
                      ├→ HeartbeatMonitor (health checks)
                      └→ Dispatcher → Feishu Card JSON 2.0
```

Core design:

- **Session isolation**: Independent CLI session per user with per-user atomic persistence
- **Context resilience**: `--resume` first for full context; on failure, Sonnet compresses history into structured summaries injected into new sessions — seamless degradation
- **Multi-model collaboration**: Claude CLI for chat + Gemini CLI for search and document analysis, each playing to their strengths
- **Token efficiency**: Gemini CLI (subscription-based, zero cost) handles large file reading and document analysis, keeping Claude's context for deep reasoning work
- **Task orchestration**: Opus researches and plans → user confirms → Sonnet executes in parallel → Opus validates

## Capabilities

**Chat & Understanding**

- Full Claude Code conversations via Feishu DM
- Image understanding (Claude native vision)
- PDF / file analysis (multi-model fallback chain)
- Real-time progress cards (TodoWrite streaming updates)

**Deep Feishu Integration**

| Skill | Capability |
|-------|-----------|
| `feishu-cal` | Calendar event CRUD, attendee management, contacts |
| `feishu-doc` | Document create / read / update / section replace / comment analysis |
| `feishu-task` | Task management + heartbeat deadline monitoring |
| `feishu-wiki` | Wiki space browsing, page CRUD |
| `feishu-bitable` | Bitable record query / filter / CRUD |
| `feishu-drive` | Drive file and folder management |
| `feishu-perm` | Document permission management, collaborator CRUD |
| `hub-ops` | Cron job CRUD, service status, hot-reload |
| `briefing` | Daily briefing pipeline, custom data sources, multi-domain, keyword self-evolution |
| `gemini` | Search / web / file analysis / summarization (subscription, zero cost) |

Each Skill is opt-in — activate only what you need.

**Autonomous Behaviors**

- **Daily briefings**: Multi-source collection → LLM generation → review → delivery, with automatic keyword evolution
- **Heartbeat monitoring**: Two-layer Sonnet triage (triage → action), automatic DM alerts on anomalies
- **Task deadline reminders**: Heartbeat reads task snapshots, proactively alerts on overdue / upcoming deadlines

**Task Orchestration**

Opus thinks, Sonnet executes:

1. User describes a complex task
2. Opus researches, designs the solution, splits into independent subtasks
3. User confirms the plan in Feishu
4. Sonnet worker processes execute in parallel (Opus is freed for other conversations)
5. Opus validates results, resolves conflicts, reports back

## Quick Start

**Prerequisites**

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- Feishu/Lark custom app (Bot capability + WebSocket mode)
- (Recommended) Claude Code on subscription; install [Gemini CLI](https://github.com/google-gemini/gemini-cli) with Gemini basic subscription to maximize token cost savings

```bash
git clone https://github.com/MidnightV1/Claude-Code-Feishu.git
cd Claude-Code-Feishu
pip install -r requirements.txt
cp config.yaml.example config.yaml
# Edit config.yaml — fill in Feishu app credentials
./hub.sh start
```

For detailed setup, see [SETUP.md](docs/SETUP.md).

---

*The following is for AI Agents (Claude Code or others) — if you're an agent setting up this service for the first time, read on.*

## For AI Agents

If you're a Claude Code or other agent reading this project for the first time:

- `SETUP.md` is a step-by-step deployment guide — follow it directly
- Each `SKILL.md` under `.claude/skills/` has an Onboarding section for first-time setup
- `config.yaml.example` has comments explaining all configuration options
- User says "scheduled task" → `hub-ops`, "briefing" → `briefing`, "document" → `feishu-doc`

## License

[MIT](LICENSE)
