---
name: briefing
description: Manage daily briefing pipelines — run briefings, check status, list domains, trigger keyword evolution. Use when the user asks about daily briefings, briefing status, adding domains, or keyword management.
---

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

## Domain Structure

Each domain lives in `~/briefing/domains/<name>/`:

```
domains/<name>/
  domain.yaml       # Core config: models, distribution, keyword_evolution, schedule
  sources.yaml      # Keywords (by supply-chain layer) + source definitions (RSS, API)
  prompts/
    generate.md     # Gemini generation prompt
    review.md       # Claude review prompt
  data/
    today_context.json      # Latest collector output
    keyword_feedback.json   # Per-keyword hit stats (30-day rolling)
    keywords_dynamic.yaml   # Auto-evolved keywords (candidate/active/deprecated)
    keywords_meta.json      # Evolution history + idempotency
    run_status.json         # Latest pipeline run status
    output/                 # Final briefing markdown files
```

## Adding a New Domain

1. Create `~/briefing/domains/<new-name>/`
2. Add `domain.yaml` (copy from existing domain, customize)
3. Add `sources.yaml` with keywords and sources
4. Add `prompts/generate.md` and `prompts/review.md`
5. Register a cron job: use hub-ops skill to add `briefing:<new-name>` handler

## Key Config in domain.yaml

```yaml
models:
  generate: { model: "3-Flash", thinking: medium }
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

| Handler | Domain | Description |
|---------|--------|-------------|
| `briefing` | default (ai-drama) | Backward-compatible, runs default domain |
| `briefing:<name>` | specific | Per-domain handler, auto-discovered |

Handler jobs don't need a prompt — they spawn `briefing_run.py` as subprocess.
