# claude-code-feishu

## Identity

You are Claude Code, running inside the `claude-code-feishu` hub service as a Claude CLI subprocess. Your messages arrive via two channels:

| Channel | Trigger | Characteristics |
|---------|---------|----------------|
| **Feishu** | User DMs or @Bot in group chat | Routed via `feishu_bot.py`, supports image/file/multi-modal. Rendered as **Feishu Card JSON 2.0** markdown |
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
| New doc → record | Write doc_id + URL to memory |
| Ownership transfer | Transfer to user after creation |

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
| `config.yaml` (credentials, model defaults) | **Yes** |
| Hub Python code (main.py etc.) | **Yes** |
| Python dependencies | **Yes** |

### Conversation Context

Two-layer architecture for conversation continuity:

| Layer | Trigger | Context |
|-------|---------|---------|
| **Primary** | `--resume` succeeds | Full Claude CLI context window |
| **Fallback** | resume fails / `#reset` / restart | History injected into system prompt (Sonnet compression + recent raw messages) |

Key parameters (`llm_router.py`):

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `HISTORY_ROUNDS` | 8 | Keep last N conversation rounds |
| `HISTORY_TRUNCATE` | 2000 chars | Truncate single message length |
| `SUMMARY_THRESHOLD` | 4 rounds | Trigger summary compression above this |

CLI timeout strategy (`claude_cli.py`):

| Scenario | Timeout Type | Default |
|----------|-------------|---------|
| Chat (no explicit timeout) | **Idle timeout**: disconnect on no stream output | idle=300s, hard cap=1800s |
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

**New capabilities should be implemented as Skills (`.claude/skills/<name>/SKILL.md` + `scripts/`), not hardcoded as `#commands` in `feishu_bot.py`.**

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

Reference: https://code.claude.com/docs/en/skills

---

## LLM Providers

| Provider | Invocation | Use Case |
|----------|-----------|----------|
| `claude-cli` | `claude -p` subprocess | Chat, tools, image understanding (Read native vision) |
| `gemini-cli` | subprocess stdin pipe | Search, web, file analysis, summarization (subscription, gemini skill) |
| `gemini-api` | `google-genai` SDK | Large document fallback (Files API), history compression |

PDF fallback chain: Gemini CLI (subscription, free) → Gemini API (per-token billing) → Claude Read (20 pages/request).

Heartbeat two-layer architecture: Sonnet (triage, no tools) → Sonnet (action, with tools, triggered only on anomalies). Notifications sent to user DM in natural tone.

---

## File Structure

| File/Directory | Purpose |
|----------------|---------|
| `main.py` | Entry point, PID file, SIGUSR1 hot-reload signal |
| `feishu_bot.py` | Feishu WebSocket Bot, message routing, debounce, multi-modal |
| `briefing_plugin.py` | Briefing thin shim (subprocess launcher, no logic) |
| `scripts/briefing_run.py` | Briefing pipeline script (collect → generate → review → deliver → keyword evolution) |
| `scripts/compress_image.py` | Image compression subprocess (isolate PIL to prevent ld.so conflicts) |
| `scheduler.py` | In-process cron scheduler (croniter + asyncio timer) |
| `heartbeat.py` | Heartbeat monitor (two-layer Sonnet: triage → action, DM notification) |
| `gemini_cli.py` | Gemini CLI subprocess wrapper (stdin pipe, @file syntax) |
| `llm_router.py` | Multi-model router (claude-cli / gemini-cli / gemini-api) |
| `dispatcher.py` | Feishu message sender (card JSON 2.0 markdown, chunking, retry, card update) |
| `file_store.py` | Session file store (image/file persistence + context injection) |
| `claude_cli.py` | Claude CLI subprocess wrapper (stream-json + TodoWrite progress streaming) |
| `gemini_api.py` | Gemini API SDK wrapper (multi-modal + Files API) |
| `feishu_api.py` | Feishu API client (token cache + HTTP + contact mapping) |
| `models.py` | Shared data structures |
| `store.py` | JSON atomic persistence |
| `feishu_utils.py` | Shared utilities (`text_to_blocks`, `parse_dt`) |
| `config.yaml` | Configuration (credentials, models, heartbeat, briefing, Feishu) |
| `hub.sh` | Service management script (start/stop/restart/status/watchdog) |
| `.claude/skills/` | Claude Code Skills |
| `data/` | Runtime state (jobs.json, sessions.json, hub.pid, logs) |
| `docs/feishu_scopes.json` | Feishu Bot permission set (importable to Feishu Open Platform) |
| `docs/feishu_scopes.md` | Feishu Bot permission list (categorized by module) |
| `PLAN.md` | Architecture design document |

---

## Service Management

`hub.sh` should only be run by the user or via external SSH:

```
./hub.sh start | stop | restart | status | watchdog
```
