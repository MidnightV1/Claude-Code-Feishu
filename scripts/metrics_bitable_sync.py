#!/usr/bin/env python3
"""Sync tracker outputs to Bitable metrics dashboard.

Reads MADS outcome reports, signal digests, and quality metrics,
then updates the corresponding Bitable records with current values,
trend indicators, and timestamps.

Standalone usage:
    python3 scripts/metrics_bitable_sync.py --outcome data/mads_outcomes/2026-04-05.json
    python3 scripts/metrics_bitable_sync.py --digest data/signal_digests/2026-W14.json
    python3 scripts/metrics_bitable_sync.py --all   # sync from latest available files
"""

import argparse
import asyncio
import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("hub.metrics_sync")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BITABLE_CTL = (
    PROJECT_ROOT / ".claude" / "skills" / "feishu-bitable"
    / "scripts" / "bitable_ctl.py"
)

BITABLE_APP = "MuMvby70IaIaVxsOoSpclgmkn4L"
BITABLE_TABLE = "tbluAZ24v66AeFqS"

# Map metric display names to Bitable record IDs.
# Keys are stored without spaces; matching strips spaces from input too.
METRIC_RECORDS = {
    "周级纠正率": "recvfPGjgSebY0",
    "Fix存活率": "recvfPGkuT5nvK",
    "Bug复发率": "recvfPGlZa03hG",
    "QA首次通过率": "recvfPGna4kQh7",
    "主动Reset率": "recvfPGougXKaz",
    "已知Pattern覆盖率": "recvfPGpQEsfMX",
    "Feedback升级采纳率": "recvfPGqZkDYus",
    "Dream更新采纳率": "recvfPGs88uYtb",
    "目标驱动探索占比": "recvfPGtgeC9B6",
    "推荐Engagement率": "recvfPGuMF7JHR",
}


def _normalize_name(name: str) -> str:
    """Strip spaces for flexible matching."""
    return name.replace(" ", "")


def _resolve_record_id(metric_name: str) -> str | None:
    """Look up record_id, tolerating spaces in the input name."""
    norm = _normalize_name(metric_name)
    return METRIC_RECORDS.get(norm)


def _run_bitable_ctl(*args: str) -> subprocess.CompletedProcess:
    """Run bitable_ctl.py and return the result."""
    cmd = [sys.executable, str(BITABLE_CTL), *args]
    log.debug("Running: %s", " ".join(cmd))
    return subprocess.run(
        cmd, capture_output=True, text=True,
        cwd=str(PROJECT_ROOT), timeout=30,
    )


def _parse_record_get_output(stdout: str) -> dict:
    """Parse text output from `bitable_ctl.py record get`.

    Output format:
        Record: recXXXX
          字段名: JSON_VALUE
          ...

    Returns dict of field_name -> parsed_value.
    """
    fields = {}
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("Record:"):
            continue
        m = re.match(r"^(.+?):\s+(.+)$", line)
        if m:
            key = m.group(1).strip()
            raw = m.group(2).strip()
            try:
                fields[key] = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                fields[key] = raw
    return fields


def _read_record(record_id: str) -> dict | None:
    """Read a single Bitable record, return its fields or None on error."""
    result = _run_bitable_ctl(
        "record", "get", BITABLE_APP, BITABLE_TABLE, record_id,
    )
    if result.returncode != 0:
        log.warning(
            "Failed to read record %s: %s",
            record_id, result.stderr.strip()[:200],
        )
        return None
    return _parse_record_get_output(result.stdout)


def _update_record(record_id: str, fields: dict) -> bool:
    """Update a Bitable record. Returns True on success."""
    fields_json = json.dumps(fields, ensure_ascii=False)
    result = _run_bitable_ctl(
        "record", "update", BITABLE_APP, BITABLE_TABLE, record_id,
        "--fields", fields_json,
    )
    if result.returncode != 0:
        log.warning(
            "Failed to update record %s: %s",
            record_id, result.stderr.strip()[:200],
        )
        return False
    return True


def _compute_trend(old_value: float | None, new_value: float) -> str:
    """Compute trend arrow: ↑ / → / ↓."""
    if old_value is None:
        return "→"
    if new_value > old_value:
        return "↑"
    elif new_value < old_value:
        return "↓"
    return "→"


def _now_ms() -> int:
    """Current timestamp in milliseconds (Bitable date field format)."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


# ── Core sync ───────────────────────────────────────────


async def sync_metrics(metrics: dict[str, float]) -> str:
    """Write metric values to Bitable.

    Args:
        metrics: dict mapping metric display names to current values
                 e.g., {"Fix存活率": 100.0, "Bug复发率": 0.0}

    Returns:
        Summary of updates performed.
    """
    if not metrics:
        return "No metrics to sync."

    updated = []
    skipped = []
    errors = []

    for name, new_value in metrics.items():
        record_id = _resolve_record_id(name)
        if not record_id:
            log.warning("Unknown metric: %s (skipped)", name)
            skipped.append(name)
            continue

        # Read current record
        current = _read_record(record_id)
        if current is None:
            errors.append(name)
            continue

        # Extract previous 当前值 for trend
        old_value = current.get("当前值")
        if isinstance(old_value, (int, float)):
            old_value = float(old_value)
        else:
            old_value = None

        # Compute trend
        trend = _compute_trend(old_value, new_value)

        # Build update payload
        update_fields: dict = {
            "当前值": new_value,
            "趋势": trend,
            "上次更新": _now_ms(),
        }

        # If 基线 is null and this is first value, set it
        baseline = current.get("基线")
        if baseline is None or (isinstance(baseline, str) and not baseline.strip()):
            update_fields["基线"] = new_value
            log.info("Setting baseline for %s: %s", name, new_value)

        if _update_record(record_id, update_fields):
            norm = _normalize_name(name)
            arrow = trend
            log.info("%s: %s → %s %s", norm, old_value, new_value, arrow)
            updated.append(f"{norm}: {old_value}→{new_value} {arrow}")
        else:
            errors.append(name)

    parts = []
    if updated:
        parts.append(f"Updated {len(updated)}: {'; '.join(updated)}")
    if skipped:
        parts.append(f"Skipped {len(skipped)}: {', '.join(skipped)}")
    if errors:
        parts.append(f"Errors {len(errors)}: {', '.join(errors)}")
    return " | ".join(parts) if parts else "Nothing to do."


# ── Source adapters ─────────────────────────────────────


async def sync_from_outcome_report(report_path: str) -> str:
    """Read mads_outcomes JSON and sync relevant metrics.

    Extracts: Fix存活率, Bug复发率, QA首次通过率 from the summary.
    """
    path = Path(report_path)
    if not path.exists():
        return f"Report not found: {report_path}"

    report = json.loads(path.read_text(encoding="utf-8"))
    summary = report.get("summary", {})

    metrics: dict[str, float] = {}

    if summary.get("fix_survival_rate") is not None:
        metrics["Fix存活率"] = float(summary["fix_survival_rate"])

    if summary.get("bug_recurrence_rate") is not None:
        metrics["Bug复发率"] = float(summary["bug_recurrence_rate"])

    if summary.get("qa_first_pass_rate") is not None:
        metrics["QA首次通过率"] = float(summary["qa_first_pass_rate"])

    if not metrics:
        return f"No extractable metrics in {report_path}"

    return await sync_metrics(metrics)


async def sync_from_signal_digest(digest_path: str) -> str:
    """Read signal digest JSON and sync relevant metrics.

    Extracts: 周级纠正率 (correction tag count / total signals * 100).
    """
    path = Path(digest_path)
    if not path.exists():
        return f"Digest not found: {digest_path}"

    data = json.loads(path.read_text(encoding="utf-8"))

    # Weekly signal digest structure: {"tag_counts": {"correction": N, ...}, "sessions_analyzed": M}
    tag_counts = data.get("tag_counts", {})
    total_signals = sum(tag_counts.values()) if tag_counts else 0
    sessions = data.get("sessions_analyzed", 0)

    if total_signals <= 0:
        return f"No signals found in {digest_path}"

    correction_count = tag_counts.get("correction", 0)
    correction_rate = round(correction_count / total_signals * 100, 1)

    metrics = {"周级纠正率": correction_rate}
    return await sync_metrics(metrics)


async def sync_from_quality_metrics(quality_data: dict) -> str:
    """Sync response quality proxy metrics.

    Extracts: 主动Reset率 from quality_data.
    Expected keys: reset_count, total_sessions.
    """
    reset_count = quality_data.get("reset_count", 0)
    total = quality_data.get("total_sessions", 0)

    if total <= 0:
        return "No session data to compute reset rate."

    reset_rate = round(reset_count / total * 100, 1)
    return await sync_metrics({"主动Reset率": reset_rate})


# ── Auto-discovery for --all ────────────────────────────


def _find_latest_outcome() -> Path | None:
    """Find the most recent mads_outcomes JSON."""
    outcomes_dir = PROJECT_ROOT / "data" / "mads_outcomes"
    if not outcomes_dir.exists():
        return None
    files = sorted(outcomes_dir.glob("*.json"), reverse=True)
    return files[0] if files else None


def _find_latest_digest() -> Path | None:
    """Find the most recent signal digest JSON."""
    digest_dir = PROJECT_ROOT / "data" / "signal_digests"
    if not digest_dir.exists():
        return None
    files = sorted(digest_dir.glob("*.json"), reverse=True)
    return files[0] if files else None


# ── CLI entry point ─────────────────────────────────────


async def _main():
    parser = argparse.ArgumentParser(
        description="Sync tracker outputs to Bitable metrics dashboard",
    )
    parser.add_argument(
        "--outcome", metavar="PATH",
        help="Path to mads_outcomes JSON report",
    )
    parser.add_argument(
        "--digest", metavar="PATH",
        help="Path to signal digest JSON",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Auto-discover and sync from latest available files",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(name)s %(levelname)s: %(message)s",
    )

    results = []

    if args.all:
        outcome_path = _find_latest_outcome()
        if outcome_path:
            log.info("Auto-discovered outcome: %s", outcome_path)
            results.append(await sync_from_outcome_report(str(outcome_path)))
        else:
            log.info("No outcome reports found.")

        digest_path = _find_latest_digest()
        if digest_path:
            log.info("Auto-discovered digest: %s", digest_path)
            results.append(await sync_from_signal_digest(str(digest_path)))
        else:
            log.info("No signal digests found.")

    if args.outcome:
        if args.outcome == "latest":
            p = _find_latest_outcome()
            if p:
                results.append(await sync_from_outcome_report(str(p)))
        else:
            results.append(await sync_from_outcome_report(args.outcome))

    if args.digest:
        if args.digest == "latest":
            p = _find_latest_digest()
            if p:
                results.append(await sync_from_signal_digest(str(p)))
        else:
            results.append(await sync_from_signal_digest(args.digest))

    if not results:
        parser.print_help()
        sys.exit(1)

    for r in results:
        print(r)


if __name__ == "__main__":
    asyncio.run(_main())
