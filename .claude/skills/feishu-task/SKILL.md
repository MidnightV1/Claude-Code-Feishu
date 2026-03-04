---
name: feishu-task
description: Manage Feishu tasks — create, list, update, complete, delete tasks with assignees and due dates. Integrates with heartbeat for deadline monitoring. Use when the user mentions tasks, to-do items, deadlines, or task assignments.
---

<!-- ONBOARDING:START — Delete this section after first-run setup is complete -->
## First-Time Setup

> **Agent**: On first load, walk the user through these items. Once all confirmed, delete from `ONBOARDING:START` to `ONBOARDING:END`.

### Prerequisites

- [ ] **Feishu app permissions**: `task:task:read`, `task:task:write`, `task:tasklist:read`, `task:tasklist:write`

### Setup Steps

1. **Create a shared tasklist** (bot manages all tasks through this list):
   ```bash
   python3 .claude/skills/feishu-task/scripts/task_ctl.py tasklist create "Hub Tasks"
   ```
2. **Copy returned GUID** to `config.yaml`:
   ```yaml
   feishu:
     tasks:
       tasklist_guid: "<guid from above>"
   ```
3. **Add the user as tasklist member** (so they can see tasks/sections in Feishu client):
   ```bash
   python3 .claude/skills/feishu-task/scripts/task_ctl.py tasklist add-member "用户名" --role editor
   ```
4. **Enable heartbeat integration** (optional):
   ```yaml
   heartbeat:
     tasks:
       enabled: true
       alert_window_hours: 2
   ```

### Why a shared tasklist?

The global task list API requires user OAuth token (not available to the bot). By using a dedicated tasklist, the bot can list, monitor, and manage tasks with its app-level token. Adding the user as editor ensures they can view and manage tasks directly in the Feishu client, including sections (groups) and task details.

### Verify

```bash
python3 .claude/skills/feishu-task/scripts/task_ctl.py tasklist list
```

Ask the user: "I need to set up task management. Can you add `task:task:read/write` and `task:tasklist:read/write` permissions to the Feishu app?"
<!-- ONBOARDING:END -->

# Feishu Tasks

Create, manage, and track tasks with assignees and deadlines. Integrates with heartbeat for automatic deadline alerts.

## Tool

```
python3 .claude/skills/feishu-task/scripts/task_ctl.py <command> [args]
```

## Commands

```bash
# Create a task
task_ctl.py create "提交周报" --due "tomorrow 17:00" --assignee "张三" --desc "Q1 summary"
task_ctl.py create "Review PR" --due "+2h"
task_ctl.py create "团队周会准备"

# List open tasks
task_ctl.py list
task_ctl.py list --assignee "张三"
task_ctl.py list --completed

# Get task details
task_ctl.py get <task_guid>

# Update a task
task_ctl.py update <task_guid> --title "New title"
task_ctl.py update <task_guid> --due "2026-03-10 18:00" --desc "Updated description"

# Mark task as done
task_ctl.py complete <task_guid>

# Assign / unassign members
task_ctl.py assign <task_guid> "张三,李四"
task_ctl.py unassign <task_guid> "张三"

# Delete a task
task_ctl.py delete <task_guid>

# Section (group) management
task_ctl.py section create "紧急"
task_ctl.py section list
task_ctl.py section delete <section_guid>

# Create task in a specific section
task_ctl.py create "紧急修复" --section "紧急" --due "+2h"

# Tasklist management
task_ctl.py tasklist create "My List"
task_ctl.py tasklist list
task_ctl.py tasklist add-member "张三,李四" --role editor
task_ctl.py tasklist remove-member "张三"

# Task snapshot (used by heartbeat)
task_ctl.py snapshot
task_ctl.py snapshot --window-hours 4
```

## Time Formats

The `--due` parameter accepts flexible time formats:
- ISO: `2026-03-10T15:00`, `2026-03-10 15:00`, `2026-03-10`
- Time only: `15:00` (today, or tomorrow if past)
- Relative: `+2h`, `+30m`
- Natural: `tomorrow 15:00`

## Heartbeat Integration

The heartbeat uses a two-layer architecture driven entirely by the task snapshot:

1. **Triage (Sonnet)**: Reads snapshot, judges OK vs anomaly with quantified rules
2. **Action (Sonnet)**: Only triggered on anomaly — has full tool access to update tasks, create follow-ups, compose natural-tone DM notifications

Config:
```yaml
heartbeat:
  triage:
    provider: claude-cli
    model: sonnet
  action:
    provider: claude-cli
    model: sonnet
  notify_open_id: ou_xxx  # DM target
  alert_window_hours: 2  # alert for tasks due within N hours
```

Snapshot output includes current time, assignee info, and open tasks without due dates:
```
[任务快照] 2026-03-04 15:30 Tuesday
逾期 (1):
- "提交周报" | 逾期 2h | 负责人: 张三
即将到期 (1):
- "Review PR" | 45min后到期 | 负责人: 未指派
进行中 (1):
- "调研竞品" | 负责人: 李四
```

## Behavior Notes

- All tasks created by the bot are automatically added to the configured tasklist.
- `list` shows open tasks by default. Use `--completed` for done tasks.
- `--assignee` resolves names via ContactStore (same as calendar skill).
- Task timestamps use milliseconds (Feishu Task v2 API format). Time parsing handles this automatically.
- `snapshot` exits silently (no output) if no open tasks, keeping heartbeat noise-free.
- The bot uses `tenant_access_token`, so the global task list API is unavailable. All queries go through the dedicated tasklist endpoint.
- The tasklist is created by the bot (owner=app). Users must be added as members (`tasklist add-member`) to see tasks and sections in their Feishu client.
