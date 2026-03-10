---
name: feishu-cal
description: Manage Feishu calendar events (日历/日程) — create, list, update, delete events, invite attendees, manage contacts. Use when the user mentions meetings (会议/开会), schedule (日程/排期), calendar (日历), events, appointments, blocking time (约时间), or inviting people. Calendar answers "什么时候在哪做什么" — time-anchored events you need to "be present" for.
---

# Feishu Calendar

Bot 拥有独立日历，用户已订阅为 reader。Bot 创建的日程用户自动可见。涉及用户的事件必须拉用户为 attendee 才能同步到用户日历。

## Tool

```
python3 .claude/skills/feishu-cal/scripts/cal_ctl.py <command> [args]
```

## Event Commands

```bash
# Create event (supports flexible time formats)
python3 .claude/skills/feishu-cal/scripts/cal_ctl.py event create "周会" "2026-03-03T10:00" "2026-03-03T11:00"
python3 .claude/skills/feishu-cal/scripts/cal_ctl.py event create "1:1" "tomorrow 14:00" "tomorrow 14:30" --attendees "张三,李四"
python3 .claude/skills/feishu-cal/scripts/cal_ctl.py event create "Focus Time" "+1h" "+3h" --desc "Deep work block"

# List events (default: next 7 days)
python3 .claude/skills/feishu-cal/scripts/cal_ctl.py event list
python3 .claude/skills/feishu-cal/scripts/cal_ctl.py event list --date 2026-03-10 --days 3

# Update event
python3 .claude/skills/feishu-cal/scripts/cal_ctl.py event update <event_id> --title "New Title" --start "2026-03-03T11:00"

# Delete event
python3 .claude/skills/feishu-cal/scripts/cal_ctl.py event delete <event_id>
```

## Time Formats

| Format | Example | Meaning |
|--------|---------|---------|
| ISO | `2026-03-03T10:00` | Specific datetime |
| Date + Time | `2026-03-03 10:00` | Same as above |
| Time only | `14:00` | Today (or tomorrow if past) |
| Tomorrow | `tomorrow 10:00` | Tomorrow at 10:00 |
| Relative | `+2h` / `+30m` | From now |

All times use Asia/Shanghai timezone.

## Contact Management

Attendees are resolved by name from the local contacts store. Contacts are automatically learned from calendar attendees.

```bash
# Add a contact manually
python3 .claude/skills/feishu-cal/scripts/cal_ctl.py contact add "张三" ou_xxxxx

# List all contacts
python3 .claude/skills/feishu-cal/scripts/cal_ctl.py contact list

# Remove a contact
python3 .claude/skills/feishu-cal/scripts/cal_ctl.py contact remove "张三"

# Learn contacts from recent calendar events
python3 .claude/skills/feishu-cal/scripts/cal_ctl.py contact sync
```

## Behavior Notes

- Events are created on bot's calendar; attendees see them on their own calendar
- When creating events with attendees, contacts are auto-learned from the API response
- Use `event list` first to get event IDs for update/delete operations
- The `--attendees` flag takes comma-separated names that must exist in the contacts store
- **Shared ContactStore**: Contacts added here are also available in feishu-task and feishu-perm skills (same underlying store)
- **创建时加参会人** — `event update` 暂不支持 `--attendees`，需要在创建时通过 `--attendees` 一次性指定。后续增减参会人需单独调用 attendees API（待实现）
- **与 feishu-task 的边界** — 日历管理时间锚点（有开始/结束、需要"在场"的事件），任务管理承诺追踪（有状态流转、需要"做完"的事项）。重叠时：如"周三 14:00 面试候选人"建日历事件，如果还有准备工作（"面试前看简历"）额外建任务
