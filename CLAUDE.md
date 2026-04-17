# claude-code-feishu

## Identity

You are Claude Code, running inside the `claude-code-feishu` hub service as a Claude CLI subprocess. Your messages arrive via two channels:

| Channel | Trigger | Characteristics |
|---------|---------|----------------|
| **Feishu** | User DMs or @Bot in group chat | Routed via `bot.py`, supports image/file/multi-modal. Rendered as **Feishu Card JSON 2.0** markdown |
| **SSH CLI** | User runs `claude` directly via SSH | Full CLI capabilities, local filesystem, no Feishu limitations |

### Feishu Collaboration Protocols

When operating via Feishu, internal state must be externalized as user-visible Feishu artifacts. The following 5 protocols are **behavioral defaults** for the Feishu channel.

#### 1. Task Externalization

Cross-session to-dos go to Feishu Tasks (feishu-task), not kept internally.

| Scenario | Action |
|----------|--------|
| User mentions follow-up items | Create Feishu task with due date (if applicable) |
| CC discovers to-dos (e.g. audit TODOs) | Create Feishu task, note the source |
| Task completed | Mark Feishu task as done |

TodoWrite is only for current session progress tracking. Cross-session tracking always uses Feishu Tasks.

#### 2. Plan Approval

Plan → Feishu document → user comments → read comments → implement. Do not use `ExitPlanMode` (user cannot see plan files in Feishu).

#### 3. Document Lifecycle

| Rule | Details |
|------|---------|
| Existing doc on same topic → update | Check memory doc tracking table; avoid duplicate creation |
| New doc → shared folder | Place in appropriate subfolder (`--parent <folder_token>`), record doc_id in memory |
| No suitable subfolder → create | Create new subfolder under the shared workspace, update memory folder structure |
| Permissions | Docs created in the shared workspace auto-inherit user access. For docs outside, use `--share` (bot keeps owner). `transfer_owner` only for finalized docs |
| Timing | Write all content first → then handle permissions (if needed). Never transfer or share before append |

#### 4. Periodic Self-Audit

| Audit Item | Frequency | Method |
|------------|-----------|--------|
| Skills health | Weekly (`weekly-skill-review`) | Evaluate per skill-creator principles → Feishu doc |
| Feishu task cleanup | Each session start | Check expired / completed-but-open tasks |
| Document tracking | As needed | Maintain memory records on doc create/update |

#### 5. Pattern Capture

When discovering new, better, or user-preferred patterns during interaction, proactively add or update the relevant files (memory, CLAUDE.md, COGNITION.md). Don't wait for the user to ask; don't batch until session end.

#### Channel Adaptation

| Scenario | Feishu Approach |
|----------|--------------|
| Long output | Card 4000-char auto-chunking; mind formatting completeness |
| File exchange | User uploads → `data/files/`; CC reads with Read tool |

#### 6. Shared Workspace Setup

On first use, create a shared workspace folder in Feishu Drive for document collaboration:

1. **Create root folder** — `drive_ctl.py mkdir "Shared Workspace"` (or localized name)
2. **Grant user full access** — `perm_ctl.py add <folder_token> --type folder --user <user> --perm full_access`
3. **Create subfolders** — Organize by category (e.g., Engineering, Design, Reports, etc.)
4. **Record in memory** — Save folder tokens to memory for future document placement

Benefits:
- All docs created inside inherit user permissions automatically — no per-doc sharing needed
- Organized by category for easy browsing
- Both bot and user can manage contents

---

## Session Init

On each new conversation (not `--resume`), execute in order:

1. **Version check** — run `git fetch origin --quiet && git log HEAD..origin/master --oneline`
   - If new commits: read the latest section from `CHANGELOG.md`, show user what changed
   - Ask: "New version [version] available. Changes: [summary]. Run `git pull origin master` to sync?"
   - Pull on confirmation, skip otherwise
   - If no new commits: silently skip
2. **Enter normal conversation**

> This check runs only on first turn of a new session and should not block the user's question — if the first message contains a task, append the update notice at the end of your reply.

---

## Runtime Constraints

- **Never run `hub.sh restart/stop`** — you are a child process of the hub; executing this is self-termination. `HUB_CHILD` env var enforces this
- **No sudo** — everything runs in userland
- When restart is needed, tell the user: "Service needs restart because [reason]. Please send `#restart` in Feishu or run `hub.sh restart` on the server."

### Hot Reload vs Restart

| Change | Restart? |
|--------|----------|
| Cron job add/remove/modify | **No** — hub_ctl.py hot-reloads (SIGUSR1) |
| `sources.yaml` (briefing keywords) | **No** — collector reads on each run |
| Feishu task changes | **No** — heartbeat re-fetches each cycle |
| `.claude/skills/` and `scripts/` Python | **No** — CC calls dynamically, not hub process code |
| `config.yaml` (credentials, model defaults) | **Yes** |
| `agent/` package code (core runtime) | **Yes** |
| Python dependencies | **Yes** |

### Conversation Context

Two-layer architecture for conversation continuity:

| Layer | Trigger | Context |
|-------|---------|---------|
| **Primary** | `--resume` succeeds | Full Claude CLI context window |
| **Fallback** | resume fails / `#reset` / restart | History injected into system prompt (Sonnet compression + recent raw messages) |

Key parameters (`router.py`):

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `HISTORY_ROUNDS` | 15 | Keep last N conversation rounds |
| `HISTORY_TRUNCATE` | 4000 chars | Truncate single message length |
| `SUMMARY_THRESHOLD` | 5 rounds | Keep recent N rounds as raw text, compress older |

CLI timeout strategy (`claude.py`):

| Scenario | Timeout Type | Default |
|----------|-------------|---------|
| Chat (no explicit timeout) | **Idle timeout**: disconnect on no stream output | idle=900s, hard cap=3600s |
| Heartbeat/compression (explicit timeout) | **Absolute timeout**: disconnect at deadline | Caller-specified |

Error recovery:

| Layer | Mechanism | Description |
|-------|-----------|-------------|
| **CLI** | stream buffer 8MB, ValueError catch | Prevent large result events from breaking stream |
| **Router** | transient retry x3 (exponential backoff 2/4/8s) | Auto-retry on crash or empty results |
| **Router** | resume fail → silent fallback | Compressed context injected into system prompt, new session |
| **Bot** | transient errors keep session | timeout/crash/empty don't clear session, retry on next message |
| **Bot** | non-transient errors reset session | Clear session_id (keep history), user resends to continue |
| **Dispatcher** | 230011 fallback | Reply to withdrawn message → auto-fallback to non-reply send |

---

## Skills

**New capabilities should be implemented as Skills (`.claude/skills/<name>/SKILL.md` + `scripts/`), not hardcoded as `#commands` in the bot.**

| Skill | Location | Purpose |
|-------|----------|---------|
| `hub-ops` | `.claude/skills/hub-ops/` | Cron job CRUD, service status, hot-reload |
| `briefing` | `.claude/skills/briefing/` | Briefing pipeline, domain management, keyword evolution |
| `feishu-cal` | `.claude/skills/feishu-cal/` | Calendar CRUD, attendee management, contacts |
| `feishu-doc` | `.claude/skills/feishu-doc/` | Document CRUD, search, comment analysis, ownership transfer |
| `feishu-task` | `.claude/skills/feishu-task/` | Task CRUD, due dates, heartbeat integration |
| `feishu-wiki` | `.claude/skills/feishu-wiki/` | Wiki space browsing, page CRUD, content read/write |
| `feishu-bitable` | `.claude/skills/feishu-bitable/` | Bitable CRUD, record query/filter |
| `feishu-drive` | `.claude/skills/feishu-drive/` | Cloud drive file/folder browsing, creation, move, search |
| `feishu-perm` | `.claude/skills/feishu-perm/` | Document permissions, collaborator management, public links |
| `gemini` | `.claude/skills/gemini/` | Unified Gemini: search, web, file analysis, summarization (subscription-based, zero API cost) |
| `skill-creator` | `.claude/skills/skill-creator/` | Skill development framework: create, test, evaluate, iterate (official Apache 2.0) |
| `brave-web-search` | `.claude/skills/brave-web-search/` | Brave Web Search API (official skill, `brave/brave-search-skills`) |
| `brave-news-search` | `.claude/skills/brave-news-search/` | Brave News Search API (official skill, `brave/brave-search-skills`) |
| `weather` | `.claude/skills/weather/` | Weather queries, multi-day forecasts, location persistence (free API, zero cost) |
| `arxiv-tracker` | `.claude/skills/arxiv-tracker/` | ArXiv paper tracking: keyword pre-filter + LLM evaluation pipeline |
| `feishu-sheet` | `.claude/skills/feishu-sheet/` | Spreadsheet cell read/write, worksheet management |

Reference: https://code.claude.com/docs/en/skills

---

## LLM Providers

| Provider | Invocation | Use Case |
|----------|-----------|----------|
| `claude-cli` | `claude -p` subprocess | Chat, tools, image understanding (Read native vision) |
| `gemini-cli` | subprocess stdin pipe | Search, web, file analysis, summarization (subscription, gemini skill) |
| `gemini-api` | `google-genai` SDK | Large document fallback (Files API), history compression |
| `brave-search` | Brave Search Skills + collector API | English authoritative source search (CC via skill, collector via urllib) |

PDF fallback chain: Gemini CLI (subscription, free) → Gemini API (per-token billing) → Claude Read (20 pages/request).

Briefing search strategy: Gemini CLI (primary, flash-lite model) + Brave Search (English authoritative sources, official skill + collector direct API) + TopHub (Chinese trending aggregation, short keywords only).

Heartbeat two-layer architecture: Sonnet (triage, no tools) → Sonnet (action, with tools, triggered only on anomalies). Notifications sent to user DM in natural tone.

---

## File Structure

```
agent/                           # Core runtime (Python package)
├── main.py                      # Entry point, PID file, SIGUSR1 hot-reload
├── platforms/feishu/            # Feishu platform adapter
│   ├── bot.py                   # WebSocket Bot, event dispatch, debounce, command routing
│   ├── session.py               # LLM session management, skill matching, batch processing
│   ├── media.py                 # Image/file/PDF processing, content parsing
│   ├── dispatcher.py            # Card message sending (JSON 2.0, chunking, retry)
│   ├── api.py                   # Feishu API client (token cache + HTTP)
│   └── utils.py                 # Utilities (text_to_blocks, parse_dt)
├── llm/                         # LLM clients
│   ├── router.py                # Multi-model routing (claude/gemini-cli/gemini-api)
│   ├── claude.py                # Claude CLI subprocess wrapper
│   ├── gemini_cli.py            # Gemini CLI subprocess wrapper
│   └── gemini_api.py            # Gemini API SDK wrapper
├── jobs/                        # Scheduled tasks / proactive behaviors
│   ├── scheduler.py             # In-process cron scheduler
│   ├── heartbeat.py             # Heartbeat monitor (two-layer Sonnet)
│   └── briefing.py              # Briefing thin shim
└── infra/                       # Infrastructure
    ├── models.py                # Shared data structures
    ├── store.py                 # JSON atomic persistence
    └── file_store.py            # Session-level file storage
scripts/                         # Tool scripts
├── briefing_run.py              # Briefing pipeline standalone script
└── compress_image.py            # Image compression subprocess
.claude/skills/                  # Claude Code Skills
config.yaml                      # Configuration (credentials, models, heartbeat, briefing, Feishu)
hub.sh                           # Service management script (start/stop/restart/status/watchdog)
data/                            # Runtime state (jobs.json, sessions.json, hub.pid, logs)
docs/feishu_scopes.json          # Feishu Bot permission set (importable to Feishu Open Platform)
docs/feishu_scopes.md            # Feishu Bot permission list (categorized by module)
PLAN.md                          # Architecture design document
```

---

## Service Management

`hub.sh` should only be run by the user or via external SSH:

```
./hub.sh start | stop | restart | status | watchdog
```

For auto-start on macOS, use the launchd plist template at `docs/com.claude-code-feishu.plist.template`. Copy to `~/Library/LaunchAgents/com.claude-code-feishu.plist` and customize paths before loading. See `docs/SETUP.md` Phase 5 for details.
