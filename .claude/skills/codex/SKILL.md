# codex

OpenAI Codex CLI integration — code review, adversarial review, task handoff, and usage monitoring.

**Trigger**: when user mentions codex, code review by codex (codex审查/codex review), adversarial review (对抗审查), codex rescue, codex usage/quota (codex额度/用量/配额).

## Tool

```bash
codex <command> [options]
```

## Commands

### Review (代码审查)

```bash
codex review --uncommitted                     # review uncommitted changes
codex review --base main                       # review branch diff vs main
codex review --uncommitted "Focus on security" # custom instructions
```

### Adversarial Review (对抗审查)

```bash
codex review --uncommitted "Act as a skeptical senior engineer. Challenge every assumption. Look for: hidden state mutations, race conditions, missing error paths, security holes, performance traps, and incorrect abstractions. Do NOT just confirm the code works — find what could break."
```

### Rescue / Task Handoff (任务委派)

```bash
codex exec "Investigate and fix failing tests" --full-auto
codex exec "Refactor auth module" --full-auto --sandbox workspace-write
```

### Usage Monitoring (额度监控)

```bash
python3 .claude/skills/codex/scripts/codex_usage.py          # usage summary
python3 .claude/skills/codex/scripts/codex_usage.py --detail  # per-session breakdown
```

## Quota Model

Codex quota is tied to ChatGPT subscription:
- **5-hour rolling window**: burst limit
- **Weekly total**: aggregate cap
- **Auto-downgrade**: near 90% usage, switches to mini model

## Pipeline Integration

| Aspect | Sonnet (current) | Codex (proposed) |
|--------|-----------------|------------------|
| Cost | Per-token API billing | Subscription (fixed monthly) |
| Strength | Follows instructions precisely | Strong at code gen + tool use |
| Weakness | No local tool access | Quota-limited |
| Best for | High-volume simple fixes | Complex fixes needing exploration |

Integration path:
1. **Phase 1**: Codex for review (QA gate supplement) — zero risk
2. **Phase 2**: Codex for rescue on stalled tickets — fallback
3. **Phase 3**: Codex as Implementer for P2/P3 — cost optimization

## Notes

- Review mode is always read-only (safe for any context)
- `--full-auto` enables autonomous execution with workspace-write sandbox
- Session IDs logged for resume/review
- Codex config: `~/.codex/config.toml`
