---
name: feishu-cal
description: Manage events on the bot's own Feishu calendar (日历/日程) — create, list, update, delete events, manage contacts. The bot calendar is independent from the user's; when an event involves the user (会议、约饭、面试等), always add the user as attendee so it syncs to their calendar. Use when the user mentions meetings (会议/开会), schedule (日程/排期), calendar (日历), events, appointments, blocking time (约时间), or asks to note down a scheduled event (记一下/加个日程).
---

# Feishu Calendar

Bot 拥有独立日历，通过 API 管理日程。用户已订阅该日历（reader），可在飞书日历中查看所有日程。

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

- **日历归属**：日程创建在 bot 自己的日历上，用户已订阅可直接查看
- **参会人同步**：涉及用户的日程（会议、约饭、面试等），必须通过 API 添加用户为 attendee，这样用户会收到提醒且日程出现在个人日历中
- **添加 attendee**：`event create --attendees` 支持；`event update` 不支持 `--attendees`，需直接调 API：
  ```python
  api.post(f'/open-apis/calendar/v4/calendars/{cal_id}/events/{event_id}/attendees',
           body={'attendees': [{'type': 'user', 'user_id': 'ou_xxx'}]},
           params={'user_id_type': 'open_id'})
  ```
- When creating events with attendees, contacts are auto-learned from the API response
- Use `event list` first to get event IDs for update/delete operations
- The `--attendees` flag takes comma-separated names that must exist in the contacts store
- **Shared ContactStore**: Contacts added here are also available in feishu-task and feishu-perm skills (same underlying store)

## Setup (已完成)

1. Feishu app permissions: `calendar:calendar`
2. Calendar ID 配置在 `config.yaml` → `feishu.calendar.calendar_id`
3. 用户已通过 ACL API 订阅 bot 日历（role: reader）
4. Contacts store 已初始化（支持 contact sync 从历史日程学习）
