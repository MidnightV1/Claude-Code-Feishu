---
name: feishu-cal
description: Manage Feishu calendar events (日历/日程) — create, list, update, delete events, invite attendees, manage contacts. Use when the user mentions meetings (会议/开会), schedule (日程/排期), calendar (日历), events, appointments, blocking time (约时间), or inviting people.
---

<!-- ONBOARDING:START — Delete this section after first-run setup is complete -->
## First-Time Setup

> **Agent**: On first load, walk the user through these items. Once all confirmed, delete from `ONBOARDING:START` to `ONBOARDING:END`.

### Prerequisites

- [ ] **Feishu app permissions**: `calendar:calendar` (read/write calendar events)
- [ ] **Shared calendar**: User creates a calendar in Feishu and shares it with the bot app (editor access)
- [ ] **Calendar ID** in `config.yaml` → `feishu.calendar.calendar_id`
- [ ] **`feishu.calendar.enabled: true`** in `config.yaml`
- [ ] **Contacts store** initialized: either manually add contacts or run `contact sync` to learn from existing events

### How to get the Calendar ID

1. User opens Feishu Calendar → Settings → find the shared calendar
2. Calendar ID is in the URL or can be obtained via API:
   ```bash
   python3 .claude/skills/feishu-cal/scripts/cal_ctl.py calendar list
   ```
3. Copy the calendar_id to `config.yaml`

### Verify

```bash
python3 .claude/skills/feishu-cal/scripts/cal_ctl.py event list
```

Ask the user: "Do you have a shared calendar set up for the bot? I need the calendar ID to manage your events."
<!-- ONBOARDING:END -->

# Feishu Calendar

Manage the user's Feishu calendar via a shared calendar.

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

- Events are created on the shared calendar; attendees see them on their own calendar
- When creating events with attendees, contacts are auto-learned from the API response
- Use `event list` first to get event IDs for update/delete operations
- The `--attendees` flag takes comma-separated names that must exist in the contacts store
- **Shared ContactStore**: Contacts added here are also available in feishu-task and feishu-perm skills (same underlying store)
