---
name: briefing
description: Manage daily briefing pipelines — run briefings, check status, list domains, trigger keyword evolution. Use when the user asks about daily briefings, briefing status, adding domains, or keyword management.
---

<!-- ONBOARDING:START — Delete this section after first-run setup is complete -->
## First-Time Setup

> **Agent**: On first load, walk the user through these items. Once all confirmed, delete from `ONBOARDING:START` to `ONBOARDING:END`.

### Prerequisites

- [ ] **Gemini API key** in `config.yaml` → `gemini-api.api_key` (used for collection and generation). If missing, generation falls back to Claude (`fallback_model` in domain.yaml, default: sonnet)
- [ ] **Claude CLI** available (used for review step and generation fallback)
- [ ] **Brave Search MCP** configured (used for web collection)
- [ ] **At least one domain** created under `~/briefing/domains/<name>/` with:
  - `domain.yaml` — models, distribution, keyword_evolution config
  - `sources.yaml` — keywords and source definitions
  - `prompts/generate.md` and `prompts/review.md`
- [ ] **Email config** (optional): `~/briefing/config/email.json` with SMTP credentials for email delivery
- [ ] **Feishu delivery** (optional): `distribution.feishu.chat_id` in `domain.yaml` for IM delivery
- [ ] **Cron job registered**: use hub-ops skill to add `briefing` or `briefing:<domain>` handler

### Quick Start

```bash
# Check existing domains
python3 scripts/briefing_run.py domains

# Test a pipeline run
python3 scripts/briefing_run.py run --domain <name>

# Check run status
python3 scripts/briefing_run.py status --domain <name>
```

Ask the user: "Which topics/domains do you want daily briefings on? I'll help set up the pipeline."
<!-- ONBOARDING:END -->

# Briefing Pipeline

Multi-domain daily briefing system: collect → generate → review → email → keyword evolution.

## Tool

```
python3 scripts/briefing_run.py <command> [options]
```

## Commands

```bash
# Run full pipeline for a domain
python3 scripts/briefing_run.py run --domain ai-drama [--date 2026-03-03]

# Run keyword evolution only
python3 scripts/briefing_run.py evolve --domain ai-drama [--date 2026-03-03]

# Check last run status
python3 scripts/briefing_run.py status --domain ai-drama

# List all configured domains
python3 scripts/briefing_run.py domains

# Run collection step only
python3 scripts/briefing_run.py run --domain ai-drama --step collect
```

All commands accept `--config config.yaml` (defaults to hub config).

## File Structure

```
~/briefing/
  config/
    email.json          # Global fallback email config (only user's own email)
  engine/
    collector.py        # Domain-aware RSS/API collector
    notify.py           # Domain-aware email sender
    prompt_init.py      # Prompt initialization and evolution
  templates/
    generate_base.md    # Base template for generation prompts
    review_base.md      # Base template for review prompts
  domains/<name>/
    domain.yaml         # Core config: models, distribution, keyword_evolution, schedule
    sources.yaml        # Keywords (by supply-chain layer) + source definitions (RSS, API)
    config/
      email.json        # Domain-specific email config (overrides global)
    prompts/
      generate.md       # Gemini generation prompt
      review.md         # Claude review prompt
    data/
      today_context.json      # Latest collector output
      keyword_feedback.json   # Per-keyword hit stats (30-day rolling)
      keywords_dynamic.yaml   # Auto-evolved keywords (candidate/active/deprecated)
      keywords_meta.json      # Evolution history + idempotency
      run_status.json         # Latest pipeline run status
      output/                 # Final briefing markdown files
```

## Email Config

Email recipients are in `email.json`. **Lookup order**: domain-specific (`domains/<name>/config/email.json`) → global fallback (`~/briefing/config/email.json`).

```json
{
  "sender": "user@example.com",
  "app_password": "smtp_app_password",
  "recipients": ["a@example.com", "b@example.com"],
  "cc": ["c@example.com"],
  "smtp_host": "smtp.qq.com",
  "smtp_port": 465
}
```

To change recipients for a specific domain, edit its `domains/<name>/config/email.json`. The global file only applies to domains without their own config.

## Adding a New Domain

1. Create `~/briefing/domains/<new-name>/`
2. Add `domain.yaml` (copy from existing domain, customize)
3. Add `sources.yaml` with keywords and sources
4. Add `prompts/generate.md` and `prompts/review.md`
5. Register a cron job: use hub-ops skill to add `briefing:<new-name>` handler

## Key Config in domain.yaml

```yaml
models:
  generate: { model: "3-Flash", thinking: medium, fallback_model: sonnet }
  review: { enabled: true, model: sonnet }
distribution:
  email: { enabled: true, subject_template: "{name} | {date}" }
  feishu: { enabled: true, chat_id: "oc_xxx" }
keyword_evolution:
  enabled: true
  max_auto_additions_per_cycle: 5
schedule: "0 8 * * *"
```

## What Doesn't Need Restart

| Change | Needs restart? |
|--------|---------------|
| Pipeline logic (`scripts/briefing_run.py`) | **No** — runs as subprocess |
| Keyword evolution logic | **No** — same file |
| `domain.yaml` / `sources.yaml` / prompts | **No** — read fresh each run |
| `keywords_dynamic.yaml` | **No** — read fresh by collector |
| This SKILL.md | **No** |
| `briefing_plugin.py` (shim) | Yes — but it never changes |
| `config.yaml` (credentials) | Yes — tell user `hub.sh restart` |

## Registered Handlers

| Handler | Domain | Cron | Description |
|---------|--------|------|-------------|
| `briefing` | ai-drama (default) | `0 8 * * *` | 每天 8:00，AI 行业日报 |
| `briefing:heritage-ai` | heritage-ai | `0 20 * * *` | 每天 20:00，遗产 AI 日报 |

`briefing` handler（无后缀）= default domain = `ai-drama`。新增 domain 用 `briefing:<name>` 格式注册。

Handler jobs don't need a prompt — they spawn `briefing_run.py` as subprocess.
