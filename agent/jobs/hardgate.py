# -*- coding: utf-8 -*-
"""Hardgate — deterministic pre-QA gate.

Runs mechanical checks (smoke, pytest, diff scope, typecheck) after Fix
and before QA Agent. No LLM involved — all checks are deterministic.
"""

import asyncio
import logging
import re
import sys
from dataclasses import dataclass, field

from agent.jobs.mads.helpers import PROJECT_ROOT, git as _git, git_in as _git_in

log = logging.getLogger("hub.hardgate")


@dataclass
class HardgateResult:
    passed: bool
    details: dict = field(default_factory=dict)


def parse_affected_files(diagnosis: str) -> list[str]:
    """Extract file paths from <affected_files> XML block in diagnosis_meta.

    Tries <affected_files> (new schema) first, falls back to legacy <affected-files>.
    """
    m = re.search(r"<affected_files>(.*?)</affected_files>", diagnosis, re.DOTALL)
    if not m:
        m = re.search(r"<affected-files>(.*?)</affected-files>", diagnosis, re.DOTALL)
    if not m:
        return []
    files = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if line.startswith("- "):
            # Strip "- " and any trailing :line_number suffix
            path = line[2:].strip().split(":")[0]
            # Strip trailing parenthetical annotations like (新建), (修改), (新增)
            path = re.sub(r"\s*\(.*?\)\s*$", "", path)
            if path:
                files.append(path)
    return files


def parse_modified_files(fix_report: str) -> list[str]:
    """Extract file paths from <modified_files> XML block in fix_meta."""
    m = re.search(r"<modified_files>(.*?)</modified_files>", fix_report, re.DOTALL)
    if not m:
        return []
    files = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if line.startswith("- "):
            path = line[2:].strip()
            if path:
                files.append(path)
    return files


def _find_related_files(out_of_scope: list[str], allowed: list[str],
                        workdir: str | None = None) -> list[str]:
    """Check if out-of-scope files have import relationships with allowed files.

    A file is "related" if any allowed file imports from it or it imports from
    an allowed file. This suggests the diagnosis underestimated the change scope
    rather than the fixer wandering off-target.

    Returns list of related out-of-scope files.
    """
    import os
    base = workdir or PROJECT_ROOT

    # Build set of module names from allowed files (e.g. "agent.jobs.maqs")
    def _to_module(path: str) -> str:
        return path.replace("/", ".").replace(".py", "")

    allowed_modules = {_to_module(f) for f in allowed if f.endswith(".py")}
    oos_modules = {_to_module(f): f for f in out_of_scope if f.endswith(".py")}

    related = []
    for oos_path in out_of_scope:
        if not oos_path.endswith(".py"):
            continue
        oos_mod = _to_module(oos_path)
        full_path = os.path.join(base, oos_path)

        # Check if any allowed file references this out-of-scope module
        for af in allowed:
            if not af.endswith(".py"):
                continue
            af_full = os.path.join(base, af)
            try:
                with open(af_full, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
                # Check for import of the out-of-scope module
                oos_parts = oos_mod.split(".")
                if any(part in content for part in [
                    f"from {oos_mod}",
                    f"import {oos_mod}",
                    f"from {'.'.join(oos_parts[:-1])} import {oos_parts[-1]}",
                ]):
                    related.append(oos_path)
                    break
            except (OSError, IndexError):
                continue

        if oos_path not in related:
            # Check reverse: does out-of-scope file import from allowed files?
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
                for af_mod in allowed_modules:
                    af_parts = af_mod.split(".")
                    if any(part in content for part in [
                        f"from {af_mod}",
                        f"import {af_mod}",
                        f"from {'.'.join(af_parts[:-1])} import {af_parts[-1]}",
                    ]):
                        related.append(oos_path)
                        break
            except (OSError, IndexError):
                continue

    return related


class Hardgate:
    """External verification — no LLM, deterministic checks only."""

    async def run(self, fix_branch: str, allowed_files: list[str],
                  workdir: str | None = None,
                  locked_files: set | None = None) -> HardgateResult:
        """Run all hardgate checks.

        Args:
            locked_files: Files from locked (QA-passed) steps that must not be
                modified during retry. If provided, any changes to these files
                trigger a scope violation.
        """
        smoke = await self._run_smoke_test(workdir)
        pytest = await self._run_pytest(workdir)
        diff_scope = await self._check_diff_scope(fix_branch, allowed_files, workdir,
                                                    locked_files=locked_files)
        prompt_changes = await self._check_prompt_changes(fix_branch, workdir)
        typecheck = {"ok": True, "output": "skip (no mypy configured)"}

        details = {
            "smoke": smoke,
            "pytest": pytest,
            "diff_scope": diff_scope,
            "prompt_changes": prompt_changes,
            "typecheck": typecheck,
        }
        passed = all(details[k]["ok"] for k in details)
        return HardgateResult(passed=passed, details=details)

    async def _run_smoke_test(self, workdir: str | None = None) -> dict:
        """Run scripts/smoke_test.py."""
        import os
        script = os.path.join(PROJECT_ROOT, "scripts", "smoke_test.py")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir or PROJECT_ROOT,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            ok = proc.returncode == 0
            return {"ok": ok, "output": (stdout.decode() + stderr.decode())[:500]}
        except Exception as e:
            log.warning("smoke_test failed: %s", e)
            return {"ok": False, "output": str(e)}

    async def _run_pytest(self, workdir: str | None = None) -> dict:
        """Run pytest on unit tests (tests/unit/)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pytest", "tests/unit/",
                "-x", "--tb=short", "-q",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir or PROJECT_ROOT,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            ok = proc.returncode == 0
            return {"ok": ok, "output": (stdout.decode() + stderr.decode())[:500]}
        except Exception as e:
            log.warning("pytest runner failed: %s", e)
            return {"ok": False, "output": str(e)}

    async def _check_prompt_changes(self, fix_branch: str,
                                      workdir: str | None = None) -> dict:
        """Detect prompt/system_prompt assignments that actually changed value.

        Pairs added (+) and removed (-) PROMPT/system_prompt lines as multisets:
        a pure reshuffle (identical content on both sides) is not flagged. Only
        unmatched additions signal a real prompt change.
        """
        _run = (lambda *a: _git_in(workdir, *a)) if workdir else _git
        rc, out, err = await _run("diff", f"dev...{fix_branch}")
        if rc != 0:
            return {"ok": True, "prompt_changes": False, "output": err}
        pat = re.compile(r".*(?:PROMPT|system_prompt)\s*=")
        added: list[str] = []
        removed: list[str] = []
        for line in out.splitlines():
            if not line or line[:3] in ("+++", "---"):
                continue
            if line[0] == "+":
                content = line[1:]
                if pat.match(content):
                    added.append(content)
            elif line[0] == "-":
                content = line[1:]
                if pat.match(content):
                    removed.append(content)
        changed = sorted(added) != sorted(removed)
        return {"ok": not changed, "prompt_changes": changed, "output": ""}

    async def _check_diff_scope(self, fix_branch: str, allowed_files: list[str],
                                  workdir: str | None = None,
                                  locked_files: set | None = None) -> dict:
        """Check that fix_branch only modified files within allowed_files.

        If locked_files is provided (retry with step locking), also checks that
        no locked files were modified — prevents regression of QA-passed steps.
        """
        _run = (lambda *a: _git_in(workdir, *a)) if workdir else _git
        rc, out, err = await _run("diff", f"dev...{fix_branch}", "--name-only")
        if rc != 0:
            log.warning("git diff failed for scope check: %s", err)
            return {"ok": False, "allowed": allowed_files, "actual": [], "output": err}

        actual = [f for f in out.splitlines() if f.strip()]

        # Check locked files violation (retry mode)
        if locked_files:
            violated = [f for f in actual if f in locked_files]
            if violated:
                log.warning("diff_scope: %d locked files modified during retry: %s",
                            len(violated), violated)
                return {
                    "ok": False,
                    "allowed": allowed_files,
                    "actual": actual,
                    "locked_violation": violated,
                    "output": f"Retry modified {len(violated)} locked (QA-passed) file(s): {violated}",
                }

        if not allowed_files:
            # No affected_files in diagnosis — cannot verify scope.
            # Allow only test-file-only changes; anything else is suspicious.
            non_test = [f for f in actual if not f.startswith("tests/")]
            if non_test:
                log.warning("diff_scope: no allowed_files constraint, "
                            "but %d non-test files modified: %s", len(non_test), non_test)
                return {"ok": False, "allowed": allowed_files, "actual": actual,
                        "output": "affected_files missing in diagnosis; "
                                  f"non-test files modified: {non_test}"}
            return {"ok": True, "allowed": allowed_files, "actual": actual}

        dir_prefixes = [f for f in allowed_files if f.endswith("/")]
        exact_files = set(f for f in allowed_files if not f.endswith("/"))

        def _in_scope(f: str) -> bool:
            if f in exact_files:
                return True
            return any(f.startswith(d) for d in dir_prefixes)

        out_of_scope = [f for f in actual if not _in_scope(f)]
        if not out_of_scope:
            return {"ok": True, "allowed": allowed_files, "actual": actual}

        # Distinguish true scope violation from scope underestimate:
        # If out-of-scope files are imported by or import allowed files,
        # the diagnosis likely underestimated complexity.
        related = _find_related_files(out_of_scope, allowed_files, workdir)
        if related:
            log.info("diff_scope: %d out-of-scope files appear related to allowed files "
                     "(possible complexity underestimate): %s", len(related), related)
            return {
                "ok": False,
                "allowed": allowed_files,
                "actual": actual,
                "out_of_scope": out_of_scope,
                "scope_underestimate": True,
                "related_files": related,
            }
        return {
            "ok": False,
            "allowed": allowed_files,
            "actual": actual,
            "out_of_scope": out_of_scope,
            "scope_underestimate": False,
        }
