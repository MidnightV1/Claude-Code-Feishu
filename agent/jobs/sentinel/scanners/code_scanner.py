# -*- coding: utf-8 -*-
"""CodeScanner — detects stale TODOs, long-lived uncommitted changes, and stale branches."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time

from agent.jobs.sentinel.base import BaseSentinelScanner
from agent.jobs.sentinel.models import EntropySignal, ScanContext

logger = logging.getLogger(__name__)

# Patterns and their severity
_TODO_PATTERNS = [
    (re.compile(r"#\s*(FIXME|HACK)\b", re.IGNORECASE), "medium"),
    (re.compile(r"#\s*TODO\b", re.IGNORECASE), "medium"),
]

_STALE_TODO_DAYS = 30
_UNCOMMITTED_STALE_HOURS = 24

# Protected branches never flagged as stale
_PROTECTED_BRANCHES = {"dev", "master", "opensource"}


class CodeScanner(BaseSentinelScanner):
    """Scans codebase for stale TODOs, long-lived uncommitted changes,
    and merged branches that were never deleted."""

    name = "code_scanner"

    async def scan(self, context: ScanContext) -> list[EntropySignal]:
        results: list[EntropySignal] = []

        sub_scans = await asyncio.gather(
            self._scan_todos(context),
            self._scan_uncommitted(context),
            self._scan_stale_branches(context),
            return_exceptions=True,
        )

        for outcome in sub_scans:
            if isinstance(outcome, BaseException):
                logger.warning("code_scanner sub-scan error: %s", outcome)
            else:
                results.extend(outcome)

        # Dedup against recent signals from context
        deduped: list[EntropySignal] = []
        for signal in results:
            if not self._is_duplicate(signal, context.recent_signals):
                deduped.append(signal)

        return deduped

    def estimate_change_rate(self) -> str:
        return "daily"

    # ------------------------------------------------------------------
    # Sub-scanners
    # ------------------------------------------------------------------

    async def _scan_todos(self, context: ScanContext) -> list[EntropySignal]:
        """Grep Python files for TODO/FIXME/HACK; flag entries older than 30 days."""
        workspace = context.workspace_dir
        signals: list[EntropySignal] = []

        # grep -Ern for all TODO/FIXME/HACK in .py files (-E for portability on macOS)
        proc = await asyncio.create_subprocess_exec(
            "grep", "-Ern", "--include=*.py",
            r"#\s*(TODO|FIXME|HACK)\b",
            workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()

        if not stdout:
            return signals

        lines = stdout.decode(errors="replace").splitlines()
        now = time.time()

        for line in lines:
            # format: /path/to/file.py:42:    # TODO: something
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            filepath, lineno_str, content = parts[0], parts[1], parts[2]

            # Determine severity from keyword
            severity = "low"
            for pattern, pat_severity in _TODO_PATTERNS:
                if pattern.search(content):
                    severity = pat_severity
                    break

            # git blame to get commit age
            age_days = await self._git_blame_age_days(workspace, filepath, lineno_str)
            if age_days is None or age_days < _STALE_TODO_DAYS:
                continue

            rel_path = os.path.relpath(filepath, workspace)
            route = "notify" if self._is_skill_path(filepath) else "maqs"
            autonomy_level = 2 if self._is_skill_path(filepath) else 1

            signal = self._create_signal(
                category="stale_todo",
                severity=severity,
                autonomy_level=autonomy_level,
                summary=f"Stale {severity.upper()} in {rel_path}:{lineno_str} ({age_days}d old)",
                route=route,
                evidence=[f"{rel_path}:{lineno_str}"],
                suggested_action=(
                    "Review and resolve or remove this annotation; "
                    f"it has been unaddressed for {age_days} days."
                ),
                context={"file": rel_path, "line": lineno_str, "age_days": age_days},
            )
            signals.append(signal)

        return signals

    async def _scan_uncommitted(self, context: ScanContext) -> list[EntropySignal]:
        """Flag files with unstaged changes whose mtime is older than 24 hours."""
        workspace = context.workspace_dir
        signals: list[EntropySignal] = []

        proc = await asyncio.create_subprocess_exec(
            "git", "-C", workspace, "status", "--porcelain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()

        if not stdout:
            return signals

        now = time.time()
        stale_files: list[str] = []

        for line in stdout.decode(errors="replace").splitlines():
            if len(line) < 4:
                continue
            xy = line[:2]
            # Unstaged changes: second char is M, D, A, ?, etc. (not space/blank)
            if xy[1] not in (" ", ""):
                filename = line[3:].strip()
                # Strip rename arrow if present (e.g. "old -> new")
                if " -> " in filename:
                    filename = filename.split(" -> ")[-1]
                full_path = os.path.join(workspace, filename)
                try:
                    mtime = os.path.getmtime(full_path)
                    age_hours = (now - mtime) / 3600
                    if age_hours > _UNCOMMITTED_STALE_HOURS:
                        stale_files.append((filename, age_hours))
                except OSError:
                    pass

        if stale_files:
            evidence = [
                f"{f} (unstaged, {h:.0f}h since last write)"
                for f, h in stale_files
            ]
            signal = self._create_signal(
                category="uncommitted_stale",
                severity="medium",
                autonomy_level=1,
                summary=f"{len(stale_files)} file(s) have unstaged changes older than {_UNCOMMITTED_STALE_HOURS}h",
                route="notify",
                evidence=evidence,
                suggested_action="Review and either commit or discard these changes.",
                context={"files": [f for f, _ in stale_files]},
            )
            signals.append(signal)

        return signals

    async def _scan_stale_branches(self, context: ScanContext) -> list[EntropySignal]:
        """Find branches that are fully merged into dev but not yet deleted."""
        workspace = context.workspace_dir
        signals: list[EntropySignal] = []

        proc = await asyncio.create_subprocess_exec(
            "git", "-C", workspace, "branch", "--merged", "dev",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()

        if not stdout:
            return signals

        for line in stdout.decode(errors="replace").splitlines():
            branch = line.strip().lstrip("* ")
            if not branch or branch in _PROTECTED_BRANCHES:
                continue

            signal = self._create_signal(
                category="stale_branch",
                severity="low",
                autonomy_level=0,
                summary=f"Branch '{branch}' is merged into dev but not deleted",
                route="silent_log",
                evidence=[f"git branch --merged dev → {branch}"],
                suggested_action=f"Run `git branch -d {branch}` to clean up.",
                context={"branch": branch},
            )
            signals.append(signal)

        return signals

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _git_blame_age_days(
        self, workspace: str, filepath: str, lineno: str
    ) -> int | None:
        """Return how many days ago the given line was last committed, or None on error."""
        try:
            lineno_int = int(lineno)
        except ValueError:
            return None

        proc = await asyncio.create_subprocess_exec(
            "git", "-C", workspace,
            "blame", "--porcelain",
            f"-L{lineno_int},{lineno_int}",
            filepath,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode != 0 or not stdout:
            return None

        for line in stdout.decode(errors="replace").splitlines():
            if line.startswith("author-time "):
                try:
                    commit_ts = int(line.split(" ", 1)[1])
                    age_seconds = time.time() - commit_ts
                    return int(age_seconds / 86400)
                except (ValueError, IndexError):
                    return None

        return None
