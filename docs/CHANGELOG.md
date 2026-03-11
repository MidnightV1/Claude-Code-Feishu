# Changelog

All notable changes to this project will be documented in this file.

Format: feature-oriented grouping per release, not per-commit.

---

## [0.13.0] — 2026-03-11

### Added
- **Native table rendering in Feishu docs** — Markdown tables now render as native Feishu table blocks (two-step API: create empty table → fill cells) instead of plain text.
- **Long content auto-redirect** — Replies exceeding 3500 chars are automatically written to a Feishu document with a summary card + link, preventing chunked message loss.
- **Weather skill** — Standalone weather queries with location persistence, multi-day forecasts, and morning briefing integration.
- **Orchestrator prompt improvements** — Worker self-validation (import tests before reporting), structured result summaries, interface contracts for cross-task consistency.
- **User prompt enhancements** — Sender identity injection and message timestamp in user prompts for better context awareness.

### Changed
- CLI idle timeout increased from 600s to 900s, hard cap from 1800s to 3600s — supports longer multi-agent tasks.
- `append_markdown_to_doc()` is now the unified function for writing content to Feishu docs (handles both regular blocks and tables).
- `doc_ctl.py` create/append commands use the new unified append function.
- `briefing_run.py` uses `append_markdown_to_doc` for doc content writes.

### Fixed
- Skill-creator description optimization and eval loop refinements.

---

## [0.9.0] — 2026-03-07

### Added
- **Multi-bot instance support** — Run multiple Feishu bots from a single service. Each bot has independent WebSocket connection, dispatcher, session namespace, reply cache, and optional `system_prompt` / `default_model` override. Legacy single-bot config remains compatible (zero-migration).
- **Per-bot HOME isolation** — New `home_dir` config field per bot. Overrides `HOME` env var for the bot's Claude CLI subprocess, isolating global CLAUDE.md and COGNITION.md. Enables team-facing bots to use separate identity/cognition from the admin's personal config.
- **LLMConfig.env field** — Generic env override mechanism for Claude CLI subprocess. Currently used for HOME isolation; extensible for future per-bot environment needs.

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

## [0.4.0] — 2026-03-06

### Added
- **Dev/master branch workflow** — `dev` for daily development, `master` for production only. Merge via `scripts/promote.sh` (smoke test gate → merge → push → auto-deploy)
- **`scripts/smoke_test.py`** — Pre-deploy validation: import checks, config.yaml structure, domain configs, collector imports, third-party dependencies
- **`scripts/promote.sh`** — One-command dev→master promotion with smoke test gate
- **Post-receive hook enhancement** — Master-only deployment + smoke test + auto-revert on failure + Feishu notification
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
- **飞书协作协议** — 5 behavioral protocols for Feishu channel: task externalization, plan approval via Feishu docs, document lifecycle management, periodic self-audit, proactive pattern capture

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
- **Image compression**: isolated PIL subprocess to prevent ld.so crash on certain platforms
- **Open-source readiness**: security hardening, DRY refactor across 13 files

---

## [0.0.1] — 2026-03-02

### Added
- Initial commit: hub service scaffold, Feishu bot, Claude CLI subprocess wrapper
- Git collaboration setup (bare repo + post-receive hook)

---

## Acknowledgments

- **[feishu-skills](https://github.com/autogame-17/feishu-skills)** by autogame-17 — 36 modular Feishu skill modules for AI agents. Our `feishu-bitable`, `feishu-drive`, and `feishu-perm` skills drew significant inspiration from this project's architecture and API integration patterns. MIT licensed.
