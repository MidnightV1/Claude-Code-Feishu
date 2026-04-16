---
name: feishu-task
description: Track commitments and action items via Feishu tasks (任务/待办). Tasks answer "what needs to get done and by when" (vs calendar which answers "when/where/with whom"). Auto-trigger task creation when: (1) user makes implicit commitments (记得.../别忘了.../下周之前要...), (2) discussion concludes with action items, (3) bot discovers items needing follow-up. Integrates with heartbeat for deadline monitoring. Use when the user mentions tasks (任务), to-do (待办/todo), deadlines (截止日期/ddl), assignments (指派), reminders (提醒我), or follow-ups (跟进/盯一下).
---

# Feishu Tasks

承诺追踪 — 把对话中的待办固化为可追踪状态，配合心跳监控形成闭环。

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
- `--assignee` resolves names via shared ContactStore (contacts added in feishu-cal or feishu-perm are available here too).
- Task timestamps use milliseconds (Feishu Task v2 API format). Time parsing handles this automatically.
- `snapshot` exits silently (no output) if no open tasks, keeping heartbeat noise-free.
- The bot uses `tenant_access_token`, so the global task list API is unavailable. All queries go through the dedicated tasklist endpoint.
- The tasklist is created by the bot (owner=app). Users must be added as members (`tasklist add-member`) to see tasks and sections in their Feishu client.
