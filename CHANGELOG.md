# Changelog

All notable changes to this project will be documented in this file.

Format: feature-oriented grouping per release, not per-commit.

---

## [0.12.1] — 2026-03-09

### Fixed
- **Lark SDK event loop isolation** — Replaced module-global loop variable with a thread-local proxy (`_ThreadLocalLoop`). Fixes two issues: (1) Python 3.13 strict loop-affinity check on `asyncio.Lock` created in the main thread but used in executor threads, and (2) multi-bot race where bot2 overwrites the global loop, breaking bot1's WebSocket reconnection. Each bot thread now transparently gets its own event loop.

---

## [0.12.0] — 2026-03-08

### Fixed
- **Per-bot home_dir auth fix** — `home_dir` was overriding `HOME` env var, breaking Claude CLI OAuth (tokens live in macOS Keychain tied to the original HOME). Now reads `CLAUDE.md`/`COGNITION.md` from `home_dir/.claude/` and injects into `system_prompt` instead. Preserves persona isolation without breaking authentication.
- **Python 3.13 event loop compatibility** — `asyncio.set_event_loop()` now called in WebSocket executor thread. Fixes `RuntimeError: no current event loop` on Python 3.13+ where implicit event loop creation was removed.
- **Debounce tuning** — First-text debounce window reduced from 1.0s to 0.5s for faster response.

---

## [0.11.0] — 2026-03-08

### Added
- **Feishu Sheet skill** — Read/write Feishu Spreadsheets (电子表格): metadata, worksheet listing, cell range I/O. Supports wiki-embedded sheets (auto-resolves `obj_token` via wiki API).
- **SQLite session persistence** — Replace JSON read-modify-write with WAL-mode SQLite. Auto-migrates from `sessions.json` on first run.
- **Stream event capture** — Claude CLI `--include-partial-messages` flag enables early `content_block_start` detection for tool use visibility.
- **Daily error scanner** — Parses hub log, groups errors by type, Sonnet analysis, writes to Feishu Bitable, alerts on ERROR count. Cron at noon.
- **Per-user rate limiting** — 10 requests/minute sliding window per user.
- **FeishuAPI `put()` method** — For Sheets v2 write API.

### Changed
- **Gemini 3 series default** — Models updated to `gemini-3-flash-preview` and `gemini-3.1-pro-preview`. Legacy aliases (`flash`, `pro`, `flash-lite`) map to Gemini 3.
- **Debounce merge improvement** — Text messages get 1s window (was 0s instant flush) to catch near-simultaneous media; pending media blocks flush until processing completes.
- **Reply cache coalesced writes** — Dirty flag + `call_later(30s)` instead of write-on-every-cache.
- **Startup temp cleanup** — Stale `~/tmp/feishu_*` files removed on boot.
- **Content hash length** — 16 → 32 hex chars for lower collision probability.
- **Async quote reply** — `asyncio.to_thread` wraps sync HTTP call.
- **Bitable skill** — Added `app create` and `table create` commands.

### Fixed
- Smart debounce for text+media merge: prevents text-first flush splitting paired messages.

---

## [0.10.0] — 2026-03-07

### Added
- **Message state machine (MessageStore)** — SQLite-backed persistent message tracking with three-layer dedup: L0 in-memory message_id, L1 SQLite message_id, L2 content_hash + time window per msg_type. Fixes Feishu WebSocket re-delivery bug where messages were re-processed with new message_ids.
- **Stale message guard** — Messages older than 2 minutes (by `create_time`) are dropped at the entry point, catching cross-time WebSocket re-deliveries that exceed the content hash window.
- **IO latency logging** — Tracks `recv → thinking card` (ms) and `recv → reply ready` (s) for performance monitoring.

### Fixed
- **WebSocket re-delivery** — Feishu WebSocket re-delivers messages with new message_ids on reconnect, bypassing the previous in-memory dedup. Now caught by persistent SQLite content_hash matching.
- **Command dedup window** — Changed from infinite (blocking legitimate re-execution) to 60 seconds.
- **FeishuAPI config compatibility** — `from_config()` now supports both legacy `feishu.app_id` and multi-bot `feishu.bots[0].app_id` formats.

---

## [0.9.0] — 2026-03-07

### Added
- **Multi-bot instance support** — Run multiple Feishu bots from a single service. Each bot has independent WebSocket connection, dispatcher, session namespace, reply cache, and optional `system_prompt` / `default_model` override. Legacy single-bot config remains compatible (zero-migration).
- **Per-bot persona isolation** — New `home_dir` config field per bot. Reads CLAUDE.md and COGNITION.md from `home_dir/.claude/` and injects into the bot's system prompt, enabling team-facing bots to use separate identity/cognition from the admin's personal config.

### Changed
- `validate_config()` refactored to support both legacy and multi-bot config validation.
- Primary bot's dispatcher is reused (not duplicated) when it's also the first bot in the list.
- Admin IDs aggregated from all bot configs for role seeding.

---

## [0.8.3] — 2026-03-07

### Fixed
- **doc_ctl _resolve_content** — Path-like strings without newlines were silently written as document content when temp file was deleted. Now errors on missing file paths instead of writing path string.
- **text_to_blocks relative links** — Relative path links (e.g. `README.zh-CN.md`, `LICENSE`) caused Feishu API 400 schema mismatch. Non-http(s) URLs now render as plain text instead of link elements.

---

## [0.8.2] — 2026-03-07

### Fixed
- **P0: Process management** — `_kill_tree` now does SIGTERM → wait 2s → SIGKILL (was instant double-signal causing zombies). Timeout path cancels stderr pipe and awaits process exit to prevent deadlocks.
- **P0: Event loop blocking** — Token refresh uses `threading.Lock` (double-check pattern). `#usage` command uses async subprocess instead of blocking `subprocess.run`.
- **P1: Memory leaks** — TTL sweep for 5 unbounded dicts (`_session_locks`, `_file_locks`, `_meta_locks`, etc.) that grew indefinitely per-user.
- **P1: Gemini file cleanup** — Uploaded files via Files API now tracked and deleted after configurable TTL (`file_ttl_days`, default 30 days).
- **P1: Subprocess leak** — Image compression subprocess now has 60s timeout + kill-on-exception guard.
- **P2: Retry logic** — Dispatcher distinguishes non-retryable errors (TypeError, ValueError) from transient failures.
- **P2: Recovery context** — Fixed early return bug that skipped recent history when compression failed.
- **P2: Scheduler** — Timer re-entry guard prevents overlapping tick execution.
- **P2: Orchestrator** — Unconfirmed plans auto-expire after 10 minutes (was unbounded).

### Improved
- Module-level constants for thinking pools, transient markers, cache limits (were recreated per-call)
- `_PROJECT_ROOT` uses `Path` instead of 4× chained `os.path.dirname`
- Fixed `store.py` sweep that could accidentally delete the lock being requested

---

## [0.8.1] — 2026-03-07

### Added
- **Personality-driven status words** — Thinking card now shows fun Chinese verbs instead of mechanical labels. 49 tool-specific words across 10 categories + 43 idle thinking words (regular + long thinking pools). Inspired by Claude Code CLI's 239 hidden spinner states.

### Fixed
- **doc_ctl batch insert** — `_insert_blocks` now splits into batches of 30 to avoid Feishu API 400 errors on large documents
- **doc_ctl file path support** — `update` and `replace` commands now accept file paths as content argument (auto-detected, reads file content)

---

## [0.6.1] — 2026-03-06

### Fixed
- **Path resolution after package restructure** — All `__file__`-relative paths (`briefing.py`, `api.py`, `bot.py`, `media.py`) now correctly traverse from their new `agent/` subdirectory back to project root for `scripts/`, `config.yaml`, and `data/`
- **Recall cancel** — Router now checks `result.cancelled` before attempting history save or retry, preventing new subprocess spawn after message recall
- **Keychain credential loading** — `check_quota.py` reads OAuth token from macOS Keychain (Claude Code 2.x), falls back to legacy `~/.claude/.credentials.json`

---

## [0.6.0] — 2026-03-06

### Added
- **Package architecture** — Restructured from 16 flat root files into a layered `agent/` package with clear separation of concerns:
  - `agent/platforms/feishu/` — Feishu platform adapter (bot, session, media, dispatcher, api, utils)
  - `agent/llm/` — LLM clients (router, claude, gemini-cli, gemini-api)
  - `agent/jobs/` — Scheduled tasks (scheduler, heartbeat, briefing)
  - `agent/infra/` — Infrastructure (models, store, file_store)
- **Multi-platform extensibility** — Package structure designed for future `platforms/wecom/`, `platforms/dingtalk/` adapters
- **Full markdown support in `text_to_blocks()`** — Headings, dividers, bullet/ordered lists, blockquotes, inline bold/code/italic/strikethrough/links all render correctly in Feishu docx blocks

### Changed
- **Bot decomposition** — `feishu_bot.py` (1384 lines) split into `bot.py` (679) + `session.py` (340) + `media.py` (419) using mixin pattern
- Entry point changed from `python3 main.py` to `python3 -m agent.main`
- All skill scripts updated to import from `agent.platforms.feishu.api` / `agent.platforms.feishu.utils`

### Fixed
- `text_to_blocks()` — Feishu docx block API silently rejects block_type 16 (bullet) and 17 (ordered list) on creation; now renders as text blocks with visual prefixes
- Callout blocks (block_type 19) lose inline elements silently; blockquotes now render as text blocks with visual prefix

---

## [0.5.0] — 2026-03-06

### Added
- **Brave Search skills** — Official `brave-web-search` and `brave-news-search` skills (`brave/brave-search-skills`, Apache 2.0). English authoritative source search for both CC interactive use and briefing collector
- **Unified API key management** — `api_keys` section in `config.yaml` for external service credentials (Brave Search, etc.), shared between skills and collector
- **Briefing multi-source search** — Gemini CLI (primary) + Brave Search (English sources) + TopHub (Chinese trending), with per-source language affinity

### Changed
- **Context management optimization** — Resolved information conflicts between runtime prompt, CLAUDE.md, and Feishu system prompt by establishing clear document responsibilities:
  - **CLAUDE.md** owns: identity, protocols, skills list, capabilities, session init, architecture
  - **Feishu system prompt** owns: rendering syntax, multimodal input handling, reply formatting
  - Eliminated duplicate content (skills list, session commands, capability declarations) that was injected from multiple sources simultaneously
- **Compression prompt upgrade** — Summary now preserves decision **rationale and excluded alternatives** (not just decisions), user corrections, and file change context. Removed rigid 500-char limit in favor of completeness-first approach
- **Context window expansion** — `HISTORY_ROUNDS` 8→15, `HISTORY_TRUNCATE` 2000→4000, `SUMMARY_THRESHOLD` 4→5 (more raw context preserved before compression)
- Removed MCP-based Brave Search in favor of official Skills (simpler, no server process)

---

## [0.4.0] — 2026-03-06

### Added
- **Gemini CLI as briefing generator** — Primary generation via Gemini CLI (zero API cost), Claude as fallback
- **Gemini CLI system prompt support** — Prepend system_prompt to user prompt (CLI has no separate system prompt channel)
- **Briefing progress card PID tracking** — All progress cards and run_status.json include process PID for debugging concurrent instances

### Changed
- Briefing generation timeout 180s → 300s (accommodates larger context from new Gemini Search sources)
- Added `feedparser` to requirements.txt

---

## [0.3.0] — 2026-03-05

### Added
- **Unified Gemini skill** — `search`, `web`, `analyze`, `summarize` subcommands, replacing single-purpose `gemini-doc` skill. Default web search via Google Search Grounding (zero API cost, subscription-based)
- **skill-creator framework** — official Apache 2.0 skill for creating, testing, evaluating, and iterating skills
- **Feishu collaboration protocols** — 5 behavioral protocols for Feishu channel: task externalization, plan approval via Feishu docs, document lifecycle management, periodic self-audit, proactive pattern capture

### Fixed
- **Recall cancel robustness** — SIGKILL entire process group (not just main process) to prevent orphan node workers; explicit `llm_task.cancel()` + fire-and-forget card deletion to avoid double CancelledError in Python 3.13
- **Scheduler double execution** — save `next_run_at` before execution, add `last_run_at` guard to prevent re-run on crash/restart
- **Briefing dedup race** — write "running" status immediately after dedup check, prevent TOCTOU race between concurrent processes
- **Process orphaning** — CLI timeout now kills entire process group (`start_new_session` + `os.killpg`), not just the parent
- **`transfer_owner` 400 error** — remove invalid `member_type: appid` (Feishu API rejects it); bot retains access via `tenant_access_token`
- **Daily briefing `domain=None` crash** — add None guard + bind handler to explicit domain

### Changed
- Refined skill descriptions across 10 skills — Chinese trigger keywords, structural consistency
- CLI idle timeout 180s → 300s to cover long Bash tool executions
- Transient errors (timeout, ld.so, empty result) keep session alive instead of resetting
- LLM retry 2× → 3× with exponential backoff (2/4/8s)
- Stream buffer 1MB → 8MB + ValueError catch for large result events
- Dispatcher: 230011 (withdrawn message) auto-fallback to non-reply send

---

## [0.2.0] — 2026-03-04

### Added
- **New skills**: `feishu-bitable` (multidimensional table CRUD, record query/filter, field schema, URL parsing), `feishu-drive` (cloud file/folder management, search), `feishu-perm` (document permission management, collaborator CRUD, public sharing)
- **Document ownership transfer** — `create --owner` + `transfer_owner` command in feishu-doc skill
- **Briefing Gemini→Claude fallback** — configurable fallback model in domain.yaml
- **Briefing Feishu document delivery** — push briefings to Feishu docs + email
- **Cold-start bootstrap guide** (SETUP doc)
- **TodoWrite streaming** — replace ~550-line long-task orchestrator with ~50-line native CC stream-json interception
- **README** in English and Chinese + "Why this project" section

### Fixed
- Chat timeout strategy + ld.so crash retry + open-source security cleanup
- Briefing notification consolidation + error resilience
- CLI fallback model always explicitly passed

---

## [0.1.0] — 2026-03-03

### Added
- **Core chat**: Feishu WebSocket bot with debounce, multi-modal (image/file/text), card markdown rendering
- **Session recovery**: `--resume` with fallback to Sonnet-compressed context injection
- **Thinking card**: live progress via stream-json, idle pulse + elapsed timer
- **Message recall**: track thinking cards, gap-safe cancel, history purge
- **Heartbeat**: two-layer Sonnet (triage → action), DM notification, task monitoring
- **Briefing pipeline**: collect → generate → review → deliver → keyword evolution, as skill + subprocess
- **Skills**: `hub-ops`, `briefing`, `feishu-cal`, `feishu-doc` (with comment analysis), `feishu-task`, `feishu-wiki`, `gemini-doc`
- **Commands**: `#reset`, `#opus`, `#sonnet`, `#think`, `#usage`, `#help`
- **Scheduler**: in-process cron (croniter + asyncio), hot-reload via SIGUSR1
- **Context**: hybrid injection (summary for older rounds + raw for recent), Sonnet compression with Gemini API fallback
- **Quote reply**: reply cache, degraded format fallback, interactive card content parsing
- **Native Claude vision**: images via Read tool (replaced Gemini image pipeline)
- **Image compression**: isolated PIL subprocess to prevent ld.so conflicts
- **Open-source readiness**: security hardening, DRY refactor across 13 files

---

## [0.0.1] — 2026-03-02

### Added
- Initial commit: hub service scaffold, Feishu bot, Claude CLI subprocess wrapper
- Git collaboration setup (bare repo + post-receive hook)

---

## Acknowledgments

- **[feishu-skills](https://github.com/autogame-17/feishu-skills)** by autogame-17 — 36 modular Feishu skill modules for AI agents. Our `feishu-bitable`, `feishu-drive`, and `feishu-perm` skills drew significant inspiration from this project's architecture and API integration patterns. MIT licensed.
