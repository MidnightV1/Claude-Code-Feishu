---
name: hub-ops
description: Manage claude-code-feishu service operations — cron jobs (定时任务/计划任务), scheduler reload, service status. Use when the user asks about scheduled tasks, periodic jobs, adding/removing/enabling cron jobs, checking service status, reloading configuration, or mentions 定时任务、计划任务、cron、调度、服务状态.
---

# Hub Operations

You are running inside claude-code-feishu as a Claude CLI subprocess. Use the `hub_ctl.py` script to manage hub operations. **Never run `hub.sh restart` or `hub.sh stop` directly** — that would kill your own parent process.

## Tool

```
python3 .claude/skills/hub-ops/scripts/hub_ctl.py <command> [args]
```

## Available Commands

### Cron Job Management

```bash
# List all jobs
python3 .claude/skills/hub-ops/scripts/hub_ctl.py cron list

# Add a handler job (e.g. briefing pipeline)
python3 .claude/skills/hub-ops/scripts/hub_ctl.py cron add "daily-briefing" "0 8 * * *" --handler briefing

# Add a prompt job (LLM-routed)
python3 .claude/skills/hub-ops/scripts/hub_ctl.py cron add "weekly-summary" "0 9 * * 1" --prompt "Summarize this week" --model claude-cli/sonnet

# Remove a job (prefix match on ID)
python3 .claude/skills/hub-ops/scripts/hub_ctl.py cron remove abc123

# Enable/disable a job
python3 .claude/skills/hub-ops/scripts/hub_ctl.py cron enable abc123
python3 .claude/skills/hub-ops/scripts/hub_ctl.py cron disable abc123

# Show job details
python3 .claude/skills/hub-ops/scripts/hub_ctl.py cron show abc123
```

### Schedule Formats

| Format | Example | Meaning |
|--------|---------|---------|
| Cron | `"0 8 * * *"` | Daily at 08:00 |
| Cron | `"*/30 * * * *"` | Every 30 minutes |
| Interval | `30m` | Every 30 minutes |
| One-shot | `2026-03-02T15:00` | Once at specific time |

All times use Asia/Shanghai timezone.

### Deploy (Promote dev→master)

```bash
# Preview — generates gate card (commits, files, test results)
python3 .claude/skills/hub-ops/scripts/hub_ctl.py promote --preview

# Execute — merge + deploy + NAS sync (run after user confirms preview)
python3 .claude/skills/hub-ops/scripts/hub_ctl.py promote --execute
```

**Workflow**: User says "部署" → CC runs `--preview` → formats result as gate card → user confirms → CC runs `--execute` → formats result as deploy notification card.

`--preview` output: JSON with commits, classified files (core/skills/config/docs), smoke+unit test results, needs_restart flag.

`--execute` output: JSON with deploy status, commit summary, NAS sync result. On smoke test failure, auto-rolls back master and reports error.

### Service Status & Reload

```bash
# Check if hub is running
python3 .claude/skills/hub-ops/scripts/hub_ctl.py status

# Trigger hot-reload (after external changes to jobs.json)
python3 .claude/skills/hub-ops/scripts/hub_ctl.py reload
```

## Registered Handlers

| Handler | Description | Managed by |
|---------|-------------|------------|
| `briefing` | Daily briefing pipeline — default domain (subprocess) | BriefingPlugin |
| `briefing:<domain>` | Per-domain briefing pipeline (auto-discovered) | BriefingPlugin |

Handler jobs don't need a prompt — they spawn `scripts/briefing_run.py` as subprocess.

## What Requires Restart vs Not

| Change | Needs restart? |
|--------|---------------|
| Cron jobs (add/remove/enable/disable) | **No** — hub_ctl.py auto-reloads |
| `sources.yaml` (briefing search terms) | **No** — collector reads fresh each run |
| `scripts/briefing_run.py` (pipeline logic) | **No** — runs as subprocess |
| `HEARTBEAT.md` | **No** — read fresh each heartbeat cycle |
| `config.yaml` (credentials, LLM defaults) | **Yes** — tell user to run `hub.sh restart` |
| Hub Python code (`main.py`, `feishu_bot.py`) | **Yes** — tell user to run `hub.sh restart` |
| Python dependencies | **Yes** — tell user to run `hub.sh restart` |

When restart IS needed, tell the user: "Service restart needed because [reason]. Please run: `hub.sh restart`"
