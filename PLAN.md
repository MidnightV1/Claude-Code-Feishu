# Architecture Plan

## Design Philosophy

Lightweight Python service — fully userland, no sudo, no Docker. Multi-model routing with cost-awareness: understanding/design tasks use Pro-tier models, execution/parsing tasks use Flash/Sonnet-tier.

---

## Model Routing Strategy

Route by task complexity: **understand/design/review → Pro tier, execute/code/parse → Flash/Sonnet tier**.

| Task | Model | Provider | Rationale |
|------|-------|----------|-----------|
| Feishu chat (understand intent, plan, design) | Claude Opus | claude-cli | Accurate intent parsing, tool use |
| Coding, bug fixes | Claude Sonnet | claude-cli | High token, lower complexity, tool use |
| Review, inspection | Claude Opus / Gemini 3.1-Pro | claude-cli / gemini-api | Deep reasoning needed |
| Search result parsing, web reading | Gemini 3-Flash | gemini-cli / gemini-api | High token, low complexity, cheap |
| Multimodal (image analysis) | Gemini 3-Flash | gemini-api | Strong multimodal |
| Large document / PDF | Gemini 3.1-Pro | gemini-api | 1M context, Files API |
| Heartbeat triage | Claude Sonnet | claude-cli | Two-layer: triage (no tools) → action (tools) |
| Cron tasks | per-job config | any | Flexible per-job model selection |

### Three Providers

| Provider | Invocation | Cost | Capability | Use Case |
|----------|-----------|------|------------|----------|
| `claude-cli` | `claude -p --output-format stream-json` | Subscription | Tools, file R/W, shell, session resume | Tasks requiring action |
| `gemini-cli` | `gemini` subprocess via stdin pipe | Free (Google account) | Tools, file management, shell, web search | Zero-cost tool tasks |
| `gemini-api` | `google-genai` Python SDK | Per-token | Multimodal, Files API, 1M context | Multimodal/large docs/lightweight calls |

> **Note**: Except heartbeat (must use claude-cli for tool access), tasks marked `gemini-api` can use `gemini-cli` as a free alternative. Migrate based on stability.

---

## Architecture

```
Feishu WebSocket ──→ FeishuBot ──→ LLMRouter ─┬→ claude -p         (claude-cli)
                      │              ↑         ├→ gemini stdin pipe  (gemini-cli)
                      │    ┌─────────┘         └→ google-genai       (gemini-api)
                      ├──→ CronScheduler (per-job model)
                      └──→ HeartbeatMonitor (two-layer Sonnet)
                                │
                         Dispatcher ──→ Feishu API
```

### Key Design Decisions

1. **Subprocess isolation** — Claude CLI and Gemini CLI run as subprocesses, not in-process. Prevents ld.so conflicts, memory leaks, and allows independent timeout control.

2. **Idle-based timeout** — Chat sessions use idle timeout (no output for N seconds) instead of absolute timeout. Long tool executions keep the session alive as long as they produce output.

3. **Session recovery** — Two-layer: primary path attempts `--resume` with full CLI context; fallback path injects Sonnet-compressed history into system prompt for new sessions.

4. **Briefing as subprocess** — `briefing_run.py` runs as a fully independent process with its own LLM clients and Dispatcher. No shared state with the hub process except file I/O.

5. **Hot-reload via SIGUSR1** — Cron job changes don't require restart. `main.py` catches SIGUSR1 and reloads `jobs.json`.

6. **Cost as design constraint** — Heartbeat uses Sonnet (subscription, no API cost). Briefing generation prefers Gemini CLI (free). Gemini API reserved for tasks that specifically need its capabilities (Files API, multimodal).

---

## Branch Strategy

```
feature/* ──→ dev ──→ master (production)
              ↑           ↑
         daily dev    smoke test gate
         Mac CC       post-receive deploys
         Win CC       cron runs here
```

- `dev`: all development happens here
- `master`: production only, merged via `scripts/promote.sh` (smoke test → merge → push)
- `opensource`: sanitized public version, synced from master to GitHub
- post-receive hook: master push → deploy → smoke test → auto-revert on failure

---

## Config Structure

```yaml
feishu:
  app_id: "cli_xxx"        # Chat bot (WebSocket)
  app_secret: "xxx"

notify:
  app_id: "cli_yyy"        # Notifier (alerts, briefings)
  app_secret: "yyy"
  delivery_chat_id: "oc_xxx"

llm:
  default:
    provider: "claude-cli"
    model: "opus"
  claude-cli:
    timeout_seconds: 600
    idle_timeout_seconds: 300
    max_timeout_seconds: 1800
  gemini-cli:
    timeout_seconds: 300
  gemini-api:
    api_key: "AIzaSy..."

scheduler:
  enabled: true
heartbeat:
  enabled: true
  interval_seconds: 1800
```

Two Feishu apps recommended: Chat Bot (WebSocket, user conversations) + Notifier (alerts, scheduled messages).
