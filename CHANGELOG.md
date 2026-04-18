# Changelog

All notable changes to this project will be documented in this file.

Format: feature-oriented grouping per release, not per-commit.

---

## [0.22.2] — 2026-04-19

### Fixed
- `#restart` command is now config-driven via `hub.restart_command` in `config.yaml`. Previously hardcoded to one deployment's macOS launchctl + plist paths, causing silent restart failures on other hosts. Default `./hub.sh restart` works cross-platform (screen-based). See `config.yaml.example` for launchd/systemd/docker examples. HUB_CHILD env is cleared in the subprocess so hub.sh's self-kill guard doesn't fire.

### Hardened
- MAQS `workflow_steps` parser tolerates empty/nested JSON tags with single-retry on empty output.
- MADS contract reviewer prompts now include rationalization tables to resist LLM "skip-the-step" drift.
- Autonomy L2 task due-date uses `+168h` instead of unsupported `+7d` unit.

### Added
- `agent/infra/skill_usage.py` — telemetry producer for HealthPulse idle-skill scanner.

---

## [0.22.1] — 2026-04-18

- Isolate personal cron config from open-source sync (config/jobs.yaml excluded)
- Provide config/jobs.example.yaml template for open-source deployments
- Fix merge_forward expansion via single GET (previous two-step approach failed)
- Pull-driven gates for explore/reflect (signal-triggered, reduces token waste)

---

## [0.22.0] — 2026-04-16

MADS pipeline hardgate fix (100% QA false-reject resolved), session save retry with exponential backoff, cross-app notification routing, MAQS discovery field fix, TTS integration (Fish.audio S2-Pro), MADS workflow steps with TodoWrite, feishu-sheet CLI Phase 1, 72h reentry gate for L1 auto-fixes, exploration adoption tracking, Sentinel cross-app routing fix, briefing pipeline status stuck fix, Garmin migration to python-garminconnect, 4 MADS composite design documents, 20+ L1-auto bug fixes

---

## [0.21.0] — 2026-04-16

### Added
- **MADS (Multi-Agent Development System)** — Full development pipeline: diagnosis, contract, fix, QA, merge. Complexity routing (L1-L5), scope guards, decomposed fixing, Opus design stage, concurrent sub-ticket execution.
- **MAQS (Multi-Agent Quality System)** — Automated bug discovery from error tracker, stale ticket recovery, QA verdict via XML control blocks, golden data pipeline.
- **Sentinel** — Autonomous entropy control with health pulse, code quality, and doc audit scanners. Signal aggregation and notification integration.
- **Visual QA skill** — CDP screenshot, accessibility tree, five-dimension scoring, MADS post-QA integration.
- **Dev Pipeline skill** — Unified MADS/MAQS entry point: ticket creation, status queries, manual stage advancement.
- **Codex skill** — OpenAI Codex CLI integration for code review, adversarial review, task handoff.
- **Feishu Board skill** — Board/whiteboard creation, flowchart drawing, node content reading.
- **TTS module (Fish.audio)** — S2-Pro text-to-speech with Feishu voice message delivery.
- **LoopExecutor** — Async orchestration for MADS ticket lifecycle with priority queue and preemption.
- **WorkerPool** — Concurrent workers with git worktree isolation for parallel tickets.
- **Merge Queue** — Git conflict prevention with rebase-on-conflict recovery.
- **Hardgate** — 72-hour reentry gate escalates to L2 when same file auto-modified repeatedly.
- **Error Tracker** — Error aggregation for MAQS automated bug discovery.
- **Exploration Scoring** — Four-dimension quality assessment (rule + LLM dual-write).
- **Document read** — Block tree traversal with image download for feishu-doc and feishu-wiki.
- **Feishu Sheet CLI** — Phase 1 spreadsheet CLI entry point.
- **516 unit tests** — Comprehensive coverage for all new subsystems.

### Changed
- **Autonomy framework** — Expanded L0-L3 with exploration loop, behavior signals, adoption tracking.
- **Router** — Transient retry, resume graceful degradation, Sonnet/Gemini history compression.
- **Scheduler** — Hot-reload (SIGUSR1), sentinel/explorer cron handlers, dynamic job config.
- **Claude CLI** — Idle-based timeout (900s), 8MB stream buffer, unclosed tag auto-repair.
- **Dispatcher** — Secret scanning, card header color parsing, 230011 fallback.
- **Session** — Reflect hint capture, skill matching, batch processing.
- **Explorer v2** — Finding type classification, commit detection, notification routing.

### Fixed
- Briefing pipeline status stuck after API errors.
- Semantic break false positives for timeout/error replies.
- Doc credential routing (notify config vs feishu).
- Hardcoded personal paths replaced with dynamic resolution.
- MAQS pipeline fixes: XML resilience, crash recovery, scope drift.

---

## [0.20.0] — 2026-03-23

### Added
- **Superintendent framework** — Autonomous operation with L0-L3 autonomy matrix.
  Built-in autonomous tasks: daily briefing, error scan with auto-fix, daily review,
  exploration engine (discover research directions from conversations/tasks/errors),
  strategic planner (goal tree + priority ranking), heartbeat monitoring,
  weekly skill review. Agent guides users through initial setup on first launch.
- **Voice message support** — Gemini-powered transcription with adaptive prompting
  (short→verbatim, long→structured). Requires Gemini API key (google-genai SDK);
  Gemini CLI (2.5 Flash) can substitute but with reduced accuracy.
- **Interactive card actions** — Button callbacks, abort buttons on thinking cards,
  feedback buttons on exploration outputs.
  Requires enabling `card.action.trigger` event in Feishu bot configuration.
- **Multi-org bot support** — Per-bot workspace isolation with independent app credentials,
  threads, and working directories. See config.yaml.example for multi-bot setup.
- **plan-review skill** — CEO/Founder-mode plan review with four modes
  (scope expansion, selective expansion, hold scope, scope reduction).
  Adapted from gstack (MIT license).
- **Award screening script** — Automated award opportunity evaluation.
- **Gemini search benchmark** — Systematic evaluation of search quality across models.

### Changed
- **brave-web-search** slimmed from 322→74 lines (agent-operational guide).
- **brave-news-search** slimmed from 183→49 lines.
- **Frontmatter standardization** — all skills now have proper name/description fields.
- **Error scanner** enhanced with auto-remediation capabilities.
- **Heartbeat** two-layer architecture improvements.
- **arxiv-tracker** engine refactored for trend analysis.
- **Briefing pipeline** abstracted with generic domain support.

### Fixed
- Card rendering edge cases (table limits, column sets, action buttons).
- WebSocket stability for multi-bot scenarios.
- Session recovery resilience — context compression, resume fallback.
- Reply splitting for long content instead of truncation.
- Cross-org message filtering by tenant_key boundary.

### Setup Notes
- **Gemini API**: Voice messages require `gemini_api_key` in config.yaml
- **Card callbacks**: Enable `card.action.trigger` in Feishu bot event settings
- **Exploration**: Agent will guide folder creation on first exploration task
- **Cron tasks**: Agent guides users through `hub_ctl.py cron create` on first launch
- **Multi-bot**: Configure per-bot credentials and workspace in config.yaml

---

## [0.17.0] — 2026-03-14

### Added
- **Voice message support** — Transcribe voice messages via Gemini 3.1 Flash Lite API. Short messages get verbatim transcription; long/multi-topic messages are auto-structured with bullet points and task extraction. Requires Gemini API key.
- **Auto-remediation error scanner** — Noon error scan now auto-evaluates fixes: Sonnet generates patches, Opus reviews diffs (approve/revise/reject). Auto-fixable issues are silently resolved; complex changes require user confirmation. Bitable status updated automatically.
- **Social media CLI skills** — Added SKILL.md for twitter-cli, xiaohongshu-cli, and bilibili-cli (jackwener CLIs). Enables reading, searching, and posting on Twitter/X, Xiaohongshu/RED, and Bilibili.
- **Voice UX** — Thinking card shows transcription progress (🎙️ → 💭), no extra cards created/deleted. Transcription cached in reply_cache for quoted message lookup.

### Fixed
- **Gemini API file handling** — Small files (<10MB) use inline bytes via `Part.from_bytes()` instead of Files API upload, fixing 400 INVALID_ARGUMENT errors and improving latency.
- **Task event handler spam** — Registered correct Lark SDK event handlers (`task_updated_v1`, `task_comment_updated_v1`) to suppress ERROR logs.
- **ArXiv stderr noise** — Filter RequestsDependencyWarning lines from subprocess stderr before logging.
- **Warning suppression** — Suppress urllib3 `RequestsDependencyWarning` in main process and subprocesses.

---

## [0.16.1] — 2026-03-13

### Added
- GitHub issue scanner — daily automated scan for new issues with Feishu notifications
- Error scan module for runtime error tracking
- ArXiv tracker skill — keyword pre-filter + LLM evaluation pipeline with trend radar
- Hub-ops skill updates — improved cron management and service operations

### Fixed
- opensource-sync CHANGELOG path handling between dev/master and opensource branches
- Complete CHANGELOG backfill for missing versions (0.5.0–0.15.0)
- WebSocket stability improvements — ping interval cap, health monitor, reconnection
- Various code quality fixes across 13+ files

### Changed
- Session management improvements — SQLite dedup, state tracking, context compression
- Feishu dispatcher resilience — retry logic, card chunking, error recovery
- README and setup documentation updates

---

## [0.16.0] — 2026-03-13

feat: address GitHub issues #2-#5 (orphan cleanup, #haiku command, quoted attachments, merged-forward expansion) + daily GitHub issue scanner cron

---

## [0.15.0] — 2026-03-13

### Added
- **Attention boundary** — User input wrapped in `<user-input>` tags; FEISHU_SYSTEM_PROMPT now includes attention rules to prevent CC from responding to system-level injections as if they were user messages.
- **Shared workspace workflow** — CLAUDE.md updated with document lifecycle guidance: shared folder creation, auto-inherited permissions, subfolder organization.

### Fixed
- **Table creation resilience** — `_create_table_in_doc` now catches HTTP exceptions and returns False instead of crashing. `append_markdown_to_doc` tracks created blocks and performs best-effort rollback on mid-way failures. Table creation failures degrade gracefully to plain-text pipe-delimited rows.

---

## [0.14.0] — 2026-03-12

### Added
- **Code block rendering in Feishu docs** — Fenced code blocks (` ```lang `) now render as native Feishu code blocks with language-specific syntax highlighting (50+ language mappings).
- **IM media API** — `FeishuAPI.upload()`, `send_image()`, `send_file()` methods for uploading and sending images/files via Feishu IM, with token retry.
- **Drive send commands** — `drive_ctl.py send-image` and `send-file` commands to upload and send media directly to chats.

### Fixed
- **Bitable record update** — `PUT` → `PATCH` for record update API (was returning 400 on partial field updates).
- **Document update block counting** — `cmd_update` now uses `_list_blocks()` instead of a separate `_count_direct_children()` call, fixing incorrect block deletion count that could leave stale content.

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

## [0.12.0] — 2026-03-09

Full sync from internal master branch. Major stability and feature improvements.

### Added
- **Feishu Sheet skill** — Read/write Feishu Spreadsheets: metadata, worksheet listing, cell range I/O. Supports wiki-embedded sheets.
- **SQLite session persistence** — Replace JSON read-modify-write with WAL-mode SQLite. Auto-migrates from `sessions.json` on first run.
- **Message state machine (MessageStore)** — SQLite-backed persistent message tracking with three-layer dedup: L0 in-memory message_id, L1 SQLite message_id, L2 content_hash + time window. Fixes WebSocket re-delivery bug.
- **Daily error scanner** — Parses hub log, groups errors by type, Sonnet analysis, writes to Feishu Bitable, alerts on ERROR count.
- **Stream event capture** — `--include-partial-messages` flag enables early `content_block_start` detection for tool use visibility.
- **Per-user rate limiting** — 10 requests/minute sliding window per user.
- **IO latency logging** — Tracks `recv → thinking card` (ms) and `recv → reply ready` (s).

### Changed
- **Gemini 3 series default** — Models updated to `gemini-3-flash-preview` and `gemini-3.1-pro-preview`.
- **Debounce tuned** — First-text window reduced to 0.5s for faster response.
- **Idle timeout** — 300s → 600s to cover long Bash tool executions.

### Fixed
- **WebSocket stability** — Process-isolated WebSocket with zombie connection detection, SDK reconnect patches, websockets ping conflict fix.
- **Python 3.13 event loop** — `asyncio.set_event_loop()` in executor thread + Lock re-creation for strict loop affinity.
- **Per-bot home_dir auth** — No longer overrides `HOME` (broke OAuth). Now injects `CLAUDE.md`/`COGNITION.md` via `system_prompt`.
- **WebSocket re-delivery** — Persistent SQLite content_hash catches messages re-delivered with new IDs on reconnect.
- **Stale message guard** — Messages older than 2 minutes dropped at entry point.
- **Command dedup window** — Changed from infinite to 60 seconds.
- **Comprehensive code quality audit** — 15+ fixes across 13 files (process management, memory leaks, retry logic, etc).

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

## [0.8.0] — 2026-03-07

### Added
- **Opus orchestrator + Sonnet worker pool** — Parallel task execution via `<task_plan>` tag. Opus designs, Sonnet workers execute independently.
- **User identity layer** — UserStore + sender context injection. Auto-backfills user name from Feishu API.
- **feishu-doc update/replace** — Document content update and full replacement commands.

---

## [0.7.0] — 2026-03-07

### Added
- **Package restructure** — Reorganized into `agent/` package with platform extensibility (`agent/platforms/feishu/`, `agent/llm/`, `agent/jobs/`, `agent/infra/`).

### Fixed
- Bot path resolution, API path, media script path, briefing plugin path after restructure.
- `check_quota` reads credentials from macOS Keychain (Claude Code 2.x).
- Recall cancel prevents router from spawning new subprocess after cancel.

---

## [0.6.0] — 2026-03-06

### Added
- **text_to_blocks** — Full markdown support for Feishu docx API.
- **WebSocket reconnection** — Auto-rebuild client on disconnect; health monitor detects SDK silent failures.

### Fixed
- Path resolution + recall cancel + keychain credentials.

---

## [0.5.0] — 2026-03-06

### Added
- **Brave Search skills** — Official Brave Web Search + News Search skills for English source discovery.
- **Per-key atomic persistence** — Eliminate concurrent write races in JSON store.
- **Gemini-doc skill** — Gemini CLI document co-pilot with PDF fallback chain.
- **Native Claude vision** — Images passed via Read tool instead of Gemini.
- **Message queue serialization** — Heartbeat context injection.

### Changed
- Context optimization — compression prompt dedup, doc responsibility split.
- Image compression isolated to subprocess to prevent ld.so crash.

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
