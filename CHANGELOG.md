# Changelog

All notable changes to this project will be documented in this file.

Format: feature-oriented grouping per release, not per-commit.

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
