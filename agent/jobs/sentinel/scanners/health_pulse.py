# -*- coding: utf-8 -*-
"""HealthPulse — monitors error rates, skill usage, branch hygiene, and disk usage."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import Counter, defaultdict
from pathlib import Path

from agent.jobs.sentinel.base import BaseSentinelScanner
from agent.jobs.sentinel.models import EntropySignal, ScanContext

log = logging.getLogger("hub.sentinel.health_pulse")

# Thresholds
ERROR_SPIKE_MULTIPLIER = 2.0       # current hour rate > N× 7-day average
SKILL_IDLE_DAYS = 14               # skills with no invocations in this window
DISK_WARN_MB = 100                 # data/ dir size warning threshold
BRANCH_STALE_DAYS = 30             # branches with no activity in this window
PROTECTED_BRANCHES = {"dev", "master", "opensource"}


class HealthPulse(BaseSentinelScanner):
    """Scanner for operational health: errors, skills, git hygiene, disk."""

    name = "health_pulse"

    async def scan(self, context: ScanContext) -> list[EntropySignal]:
        workspace = Path(context.workspace_dir)
        signals: list[EntropySignal] = []

        results = await asyncio.gather(
            self._check_error_rate(workspace, context),
            self._check_skill_usage(workspace, context),
            self._check_branch_hygiene(workspace, context),
            self._check_disk_usage(workspace, context),
            return_exceptions=True,
        )

        for i, result in enumerate(results):
            check_name = ["error_rate", "skill_usage", "branch_hygiene", "disk_usage"][i]
            if isinstance(result, Exception):
                log.warning("HealthPulse: %s check failed: %s", check_name, str(result)[:200])
            elif isinstance(result, list):
                signals.extend(result)

        return signals

    def estimate_change_rate(self) -> str:
        return "hourly"

    # ── Individual checks ──

    async def _check_error_rate(
        self, workspace: Path, context: ScanContext
    ) -> list[EntropySignal]:
        """Detect error rate spikes by comparing current hour to 7-day baseline."""
        error_file = workspace / "data" / "error_tracker.jsonl"
        if not error_file.exists():
            log.debug("HealthPulse: error_tracker.jsonl not found, skipping")
            return []

        now = time.time()
        cutoff_24h = now - 86400
        cutoff_7d = now - 7 * 86400
        current_hour_start = now - 3600

        hourly_buckets: defaultdict[int, list[dict]] = defaultdict(list)
        current_hour_entries: list[dict] = []
        recent_entries: list[dict] = []

        try:
            with error_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ts = entry.get("timestamp")
                    if ts is None:
                        continue

                    # Normalise — accept float epoch or ISO-8601 string
                    if isinstance(ts, str):
                        try:
                            import datetime
                            ts = datetime.datetime.fromisoformat(ts).timestamp()
                        except ValueError:
                            continue

                    ts = float(ts)

                    if ts >= cutoff_7d:
                        # Bucket by hour index (relative to cutoff_7d)
                        bucket = int((ts - cutoff_7d) // 3600)
                        hourly_buckets[bucket].append(entry)

                    if ts >= cutoff_24h:
                        recent_entries.append(entry)

                    if ts >= current_hour_start:
                        current_hour_entries.append(entry)

        except OSError as e:
            log.warning("HealthPulse: cannot read error_tracker.jsonl: %s", e)
            return []

        if not hourly_buckets:
            return []

        total_hours = len(hourly_buckets)
        if total_hours == 0:
            return []

        total_errors_7d = sum(len(v) for v in hourly_buckets.values())
        avg_hourly = total_errors_7d / total_hours if total_hours else 0
        current_count = len(current_hour_entries)

        if avg_hourly == 0 and current_count == 0:
            return []

        # Require at least a meaningful baseline before firing
        if avg_hourly < 0.5 and current_count < 5:
            return []

        if avg_hourly > 0 and current_count <= ERROR_SPIKE_MULTIPLIER * avg_hourly:
            return []

        # Gather error patterns from current hour
        patterns: Counter[str] = Counter()
        for entry in current_hour_entries:
            key = (
                entry.get("error_type")
                or entry.get("type")
                or entry.get("level")
                or entry.get("message", "")[:60]
                or "unknown"
            )
            patterns[key] += 1

        evidence = [
            f"Current hour: {current_count} errors  |  7-day avg: {avg_hourly:.1f}/hr  "
            f"(ratio: {current_count / avg_hourly:.1f}x)" if avg_hourly > 0
            else f"Current hour: {current_count} errors  |  7-day avg: ~0/hr",
        ]
        for pattern, count in patterns.most_common(5):
            evidence.append(f"  {count}× {pattern}")

        correlated_commit = await self._correlate_commits(workspace, hours=1)

        signal = self._create_signal(
            category="error_spike",
            severity="high",
            autonomy_level=2,
            summary=(
                f"Error spike: {current_count} errors in current hour "
                f"({current_count / avg_hourly:.1f}x 7-day avg)" if avg_hourly > 0
                else f"Error spike: {current_count} errors in current hour (no prior baseline)"
            ),
            route="maqs",
            evidence=evidence,
            suggested_action=(
                "Review error_tracker.jsonl for root cause. "
                "Check recent deployments or config changes."
            ),
            context={
                "current_hour_count": current_count,
                "avg_hourly_7d": round(avg_hourly, 2),
                "top_patterns": dict(patterns.most_common(5)),
                "correlated_commit": correlated_commit,
            },
        )

        if self._is_duplicate(signal, context.recent_signals):
            return []
        return [signal]

    async def _correlate_commits(self, workspace: Path, hours: int = 1) -> str:
        """Return the most recent commit hash within the given hour window, or empty string."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "log",
                f"--since={hours} hours ago",
                "--format=%H",
                "--max-count=1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                commit = stdout.decode().strip()
                return commit if commit else ""
        except Exception as e:
            log.debug("HealthPulse: git log for commit correlation failed: %s", e)
        return ""

    async def _check_skill_usage(
        self, workspace: Path, context: ScanContext
    ) -> list[EntropySignal]:
        """Find skills with zero invocations in the last SKILL_IDLE_DAYS days."""
        usage_file = workspace / "data" / "skill_usage.jsonl"
        if not usage_file.exists():
            log.debug("HealthPulse: skill_usage.jsonl not found, skipping")
            return []

        cutoff = time.time() - SKILL_IDLE_DAYS * 86400
        active_skills: set[str] = set()

        try:
            with usage_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ts = entry.get("timestamp")
                    if ts is None:
                        continue

                    if isinstance(ts, str):
                        try:
                            import datetime
                            ts = datetime.datetime.fromisoformat(ts).timestamp()
                        except ValueError:
                            continue

                    if float(ts) >= cutoff:
                        skill = entry.get("skill")
                        if skill:
                            active_skills.add(skill)

        except OSError as e:
            log.warning("HealthPulse: cannot read skill_usage.jsonl: %s", e)
            return []

        # Discover all installed skills
        skills_dir = workspace / ".claude" / "skills"
        if not skills_dir.exists():
            return []

        installed_skills = {
            d.name for d in skills_dir.iterdir()
            if d.is_dir() and (d / "SKILL.md").exists()
        }

        idle_skills = sorted(installed_skills - active_skills)
        if not idle_skills:
            return []

        signals = []
        for skill_name in idle_skills:
            signal = self._create_signal(
                category="skill_unused",
                severity="low",
                autonomy_level=0,
                summary=f"Skill '{skill_name}' has no recorded usage in last {SKILL_IDLE_DAYS} days",
                route="explore",
                evidence=[
                    f"Skill path: .claude/skills/{skill_name}/",
                    f"No entries in skill_usage.jsonl since {SKILL_IDLE_DAYS}d ago",
                    "This is informational — review whether the skill is still relevant.",
                ],
                suggested_action=(
                    f"Investigate if '{skill_name}' is still needed or can be retired/consolidated."
                ),
                context={"skill": skill_name, "idle_days": SKILL_IDLE_DAYS},
            )
            if not self._is_duplicate(signal, context.recent_signals):
                signals.append(signal)

        return signals

    async def _check_branch_hygiene(
        self, workspace: Path, context: ScanContext
    ) -> list[EntropySignal]:
        """Find branches with no activity in last BRANCH_STALE_DAYS days."""
        try:
            # List all branches (local + remote)
            proc = await asyncio.create_subprocess_exec(
                "git", "branch", "-a",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                log.warning("HealthPulse: git branch -a failed: %s", stderr.decode()[:200])
                return []

            all_branches: set[str] = set()
            for line in stdout.decode().splitlines():
                branch = line.strip().lstrip("* ").strip()
                # Skip HEAD pointer lines
                if " -> " in branch:
                    continue
                # Strip remote prefix (e.g. "remotes/origin/dev" → "dev")
                for prefix in ("remotes/origin/", "origin/"):
                    if branch.startswith(prefix):
                        branch = branch[len(prefix):]
                        break
                if branch:
                    all_branches.add(branch)

        except Exception as e:
            log.warning("HealthPulse: git branch command failed: %s", e)
            return []

        try:
            # Get branches that had commits in the last BRANCH_STALE_DAYS days
            proc = await asyncio.create_subprocess_exec(
                "git", "log",
                f"--since={BRANCH_STALE_DAYS} days ago",
                "--all",
                "--format=%D",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                log.warning("HealthPulse: git log failed: %s", stderr.decode()[:200])
                return []

            recently_active: set[str] = set()
            for line in stdout.decode().splitlines():
                for ref in line.split(","):
                    ref = ref.strip()
                    if not ref or ref.startswith("HEAD"):
                        continue
                    for prefix in ("origin/", "refs/remotes/origin/", "refs/heads/"):
                        if ref.startswith(prefix):
                            ref = ref[len(prefix):]
                            break
                    if ref:
                        recently_active.add(ref)

        except Exception as e:
            log.warning("HealthPulse: git log command failed: %s", e)
            return []

        stale = sorted(
            (all_branches - recently_active - PROTECTED_BRANCHES)
        )
        if not stale:
            return []

        signals = []
        for branch in stale:
            signal = self._create_signal(
                category="branch_stale",
                severity="low",
                autonomy_level=0,
                summary=f"Branch '{branch}' has no activity in last {BRANCH_STALE_DAYS} days",
                route="silent_log",
                evidence=[
                    f"Branch: {branch}",
                    f"Not in git log --since={BRANCH_STALE_DAYS}d --all",
                    f"Protected branches (excluded): {', '.join(sorted(PROTECTED_BRANCHES))}",
                ],
                suggested_action=(
                    f"Consider deleting '{branch}' if the work is merged or abandoned: "
                    f"`git branch -d {branch}`"
                ),
                context={"branch": branch, "stale_days": BRANCH_STALE_DAYS},
            )
            if not self._is_duplicate(signal, context.recent_signals):
                signals.append(signal)

        return signals

    async def _check_disk_usage(
        self, workspace: Path, context: ScanContext
    ) -> list[EntropySignal]:
        """Warn if data/ directory exceeds DISK_WARN_MB."""
        data_dir = workspace / "data"
        if not data_dir.exists():
            return []

        try:
            total_bytes = sum(
                f.stat().st_size
                for f in data_dir.rglob("*")
                if f.is_file()
            )
        except OSError as e:
            log.warning("HealthPulse: disk usage scan failed: %s", e)
            return []

        total_mb = total_bytes / (1024 * 1024)
        if total_mb <= DISK_WARN_MB:
            return []

        # Find the largest files for context
        try:
            file_sizes = sorted(
                ((f.stat().st_size, f) for f in data_dir.rglob("*") if f.is_file()),
                reverse=True,
            )
            top_files = [
                f"{size / (1024 * 1024):.1f}MB  {path.relative_to(data_dir)}"
                for size, path in file_sizes[:5]
            ]
        except OSError:
            top_files = []

        evidence = [f"data/ total: {total_mb:.1f}MB (threshold: {DISK_WARN_MB}MB)"]
        if top_files:
            evidence.append("Largest files:")
            evidence.extend(f"  {entry}" for entry in top_files)

        signal = self._create_signal(
            category="disk_usage_high",
            severity="medium",
            autonomy_level=1,
            summary=f"data/ directory is {total_mb:.0f}MB (>{DISK_WARN_MB}MB threshold)",
            route="notify",
            evidence=evidence,
            suggested_action=(
                "Review large files in data/. Consider rotating logs, "
                "archiving old JSONL files, or cleaning up temp data."
            ),
            context={
                "total_mb": round(total_mb, 1),
                "threshold_mb": DISK_WARN_MB,
            },
        )

        if self._is_duplicate(signal, context.recent_signals):
            return []
        return [signal]
