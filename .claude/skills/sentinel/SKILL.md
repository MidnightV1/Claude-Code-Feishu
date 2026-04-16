# Sentinel — 自主巡检与控熵

系统健康自主巡检 — 手动触发扫描、查看信号、查看统计。

## Tool

```
python3 scripts/sentinel_ctl.py <command> [args]
```

## Commands

```bash
# Run a full scan cycle
python3 scripts/sentinel_ctl.py scan
python3 scripts/sentinel_ctl.py scan --scanner code_scanner  # run specific scanner only

# View recent signals
python3 scripts/sentinel_ctl.py list
python3 scripts/sentinel_ctl.py list --hours 48
python3 scripts/sentinel_ctl.py list --source health_pulse
python3 scripts/sentinel_ctl.py list --unresolved

# View statistics
python3 scripts/sentinel_ctl.py stats
python3 scripts/sentinel_ctl.py stats --hours 48

# Resolve a signal
python3 scripts/sentinel_ctl.py resolve <signal_id>
```

## Behavior Notes

- `scan` runs all due scanners by default, or a specific scanner with `--scanner`
- `list` shows signals from the last 24h by default
- All data persisted in `data/sentinel.jsonl`
- Signals with route="maqs" auto-create MAQS tickets
- Signals with route="notify" send Feishu notifications
