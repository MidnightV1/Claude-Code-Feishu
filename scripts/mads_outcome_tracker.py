#!/usr/bin/env python3
"""MADS Outcome Tracker — daily scan of fix outcomes.

Checks six dimensions for each recent MADS/MAQS merge:
  A. Fix survival — was the merge reverted?
  B. Bug recurrence — did the same error_type reappear after the fix?
     B2. Recurrence classification — incomplete fix vs regression
  C. Test manipulation — did the fixer remove/relax assertions?
  D. QA first-pass rate — was the fix accepted without QA rejections?
  E. Scope overflow — did the fix touch files outside the contract?

Standalone: python3 scripts/mads_outcome_tracker.py
Importable: await run_outcome_tracker() -> str
"""

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("hub.mads_outcomes")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ERROR_TRACKER_PATH = PROJECT_ROOT / "data" / "error_tracker.jsonl"
OUTCOMES_DIR = PROJECT_ROOT / "data" / "mads_outcomes"
BITABLE_SCRIPT = PROJECT_ROOT / ".claude" / "skills" / "feishu-bitable" / "scripts" / "bitable_ctl.py"
BITABLE_APP_TOKEN = "A4bLb6NXKaW5rds9J7aczRson9d"
BITABLE_TABLE_ID = "tbl86WbtxhBsNGk2"

# Patterns for MADS/MAQS merge commits
MERGE_PATTERN = re.compile(r"^([0-9a-f]+)\s+merge:\s+fix/((?:MAQS|MADS)-\S+)\s+into\s+\S+", re.IGNORECASE)

# Test file indicators
TEST_FILE_PATTERNS = (re.compile(r"test_"), re.compile(r"_test\.py$"), re.compile(r"tests/"))

# Assertion keywords
ASSERTION_RE = re.compile(r"\b(assert\b|assertEqual|assertAlmostEqual|assertRaises|expect\(|\.to\.)")


def _git(*args: str, cwd: str | None = None) -> str:
    """Run a git command, return stdout."""
    result = subprocess.run(
        ["git", *args],
        capture_output=True, text=True,
        cwd=cwd or str(PROJECT_ROOT),
    )
    return result.stdout.strip()


def _is_test_file(path: str) -> bool:
    for pat in TEST_FILE_PATTERNS:
        if pat.search(path):
            return True
    return False


# ── A. Fix Survival ──────────────────────────────────────────────

def scan_recent_merges(days: int = 7) -> list[dict]:
    """Find MADS/MAQS merge commits in the last N days."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    raw = _git("log", "--oneline", f"--since={since}", "--all")
    if not raw:
        return []

    merges = []
    for line in raw.splitlines():
        m = MERGE_PATTERN.match(line.strip())
        if m:
            commit_hash = m.group(1)
            ticket_id = m.group(2)
            # Get commit timestamp
            ts = _git("log", "-1", "--format=%aI", commit_hash)
            merges.append({
                "ticket_id": ticket_id,
                "merge_commit": commit_hash,
                "merged_at": ts[:10] if ts else "",
                "merged_ts": ts,
            })
    return merges


def check_reverted(merge: dict) -> bool:
    """Check if a merge commit was reverted."""
    commit_hash = merge["merge_commit"]
    ticket_id = merge["ticket_id"]
    # Search for revert commits mentioning this hash or ticket
    revert_log = _git("log", "--oneline", "--all", "--grep=revert")
    if not revert_log:
        return False
    for line in revert_log.splitlines():
        low = line.lower()
        if commit_hash in low or ticket_id.lower() in low:
            return True
    return False


# ── B. Bug Recurrence ────────────────────────────────────────────

def load_error_tracker(days: int = 14) -> list[dict]:
    """Load recent errors from error_tracker.jsonl."""
    if not ERROR_TRACKER_PATH.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    errors = []
    try:
        with open(ERROR_TRACKER_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("timestamp", 0) >= cutoff:
                        errors.append(entry)
                except json.JSONDecodeError:
                    continue
    except OSError:
        log.warning("Could not read error tracker: %s", ERROR_TRACKER_PATH)
    return errors


def extract_error_type_from_commit(merge: dict) -> str | None:
    """Try to extract original error_type from commit message or branch name.

    Convention: MAQS tickets often contain the error type in the commit body
    or the fix commit message.
    """
    commit_hash = merge["merge_commit"]
    # Get full commit message of the merge
    msg = _git("log", "-1", "--format=%B", commit_hash)
    # Also check the parent fix commit (first parent is target branch, second is fix branch)
    parents = _git("log", "-1", "--format=%P", commit_hash).split()
    if len(parents) > 1:
        fix_msg = _git("log", "-1", "--format=%B", parents[1])
        msg = msg + "\n" + fix_msg

    # Look for common error type patterns in messages
    # e.g., "TypeError", "KeyError", "AttributeError", etc.
    error_match = re.search(r"\b(\w+Error|\w+Exception)\b", msg)
    if error_match:
        return error_match.group(1)
    return None


def check_recurrence(merge: dict, errors: list[dict]) -> tuple[bool, int]:
    """Check if the same error_type appeared after the merge."""
    error_type = extract_error_type_from_commit(merge)
    if not error_type:
        return False, 0

    merge_ts_str = merge.get("merged_ts", "")
    if not merge_ts_str:
        return False, 0

    try:
        merge_dt = datetime.fromisoformat(merge_ts_str)
        merge_epoch = merge_dt.timestamp()
    except (ValueError, TypeError):
        return False, 0

    count = 0
    for err in errors:
        if err.get("error_type") == error_type and err.get("timestamp", 0) > merge_epoch:
            count += 1
    return count > 0, count


def _query_bitable_ticket(ticket_id: str) -> dict | None:
    """Query Bitable for a MADS/MAQS ticket record. Returns first match or None."""
    if not BITABLE_SCRIPT.exists():
        log.debug("Bitable script not found: %s", BITABLE_SCRIPT)
        return None
    try:
        filter_expr = f'CurrentValue.[ticket_id]="{ticket_id}"'
        result = subprocess.run(
            [
                sys.executable, str(BITABLE_SCRIPT),
                "record", "list", BITABLE_APP_TOKEN, BITABLE_TABLE_ID,
                "--filter", filter_expr, "--json",
            ],
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode != 0:
            log.debug("Bitable query failed for %s: %s", ticket_id, result.stderr[:200])
            return None
        data = json.loads(result.stdout)
        items = data if isinstance(data, list) else data.get("items", data.get("records", []))
        if items:
            rec = items[0]
            return rec.get("fields", rec)
        return None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        log.debug("Bitable query error for %s: %s", ticket_id, exc)
        return None


def _get_changed_py_files(commit_hash: str) -> list[str]:
    """Get list of .py files changed in a commit."""
    raw = _git("diff", "--name-only", f"{commit_hash}^..{commit_hash}")
    if not raw:
        return []
    return [f for f in raw.splitlines() if f.strip().endswith(".py")]


def _parse_affected_files(raw: str | None) -> set[str]:
    """Parse affected_files field from Bitable (comma/newline/XML formats)."""
    if not raw:
        return set()
    # Try XML block: <file>path</file>
    xml_files = re.findall(r"<file>(.*?)</file>", raw)
    if xml_files:
        return {f.strip() for f in xml_files if f.strip()}
    # Comma or newline separated
    parts = re.split(r"[,\n]+", raw)
    return {p.strip() for p in parts if p.strip()}


# ── D. QA First-Pass Rate ───────────────────────────────────────

def check_qa_first_pass(merge: dict) -> tuple[bool, int]:
    """Check if a ticket passed QA on first attempt (reject_count == 0).

    Returns (first_pass: bool, reject_count: int).
    """
    ticket_id = merge.get("ticket_id", "")
    fields = _query_bitable_ticket(ticket_id)
    if fields is None:
        # No record found — assume first pass (no data)
        return True, 0
    reject_count = 0
    raw_val = fields.get("reject_count", 0)
    if isinstance(raw_val, (int, float)):
        reject_count = int(raw_val)
    elif isinstance(raw_val, str):
        try:
            reject_count = int(raw_val)
        except ValueError:
            reject_count = 0
    return reject_count == 0, reject_count


# ── E. Scope Overflow ───────────────────────────────────────────

def check_scope_overflow(merge: dict) -> tuple[bool, list[str]]:
    """Check if fix touched files outside the contract's affected_files.

    Returns (overflow: bool, list_of_extra_files).
    Allows +1 tolerance for imports/tests.
    """
    commit_hash = merge["merge_commit"]
    actual_files = set(_get_changed_py_files(commit_hash))
    if not actual_files:
        return False, []

    ticket_id = merge.get("ticket_id", "")
    fields = _query_bitable_ticket(ticket_id)
    if fields is None:
        # No contract data — cannot determine overflow
        return False, []

    contracted = _parse_affected_files(fields.get("affected_files"))
    if not contracted:
        # No affected_files recorded — skip
        return False, []

    extra_files = sorted(actual_files - contracted)
    # Allow +1 tolerance (e.g. __init__.py import or test file)
    overflow = len(extra_files) > 1
    return overflow, extra_files


# ── B2. Recurrence Classification ───────────────────────────────

def classify_recurrence(merge: dict, errors: list[dict]) -> dict:
    """Classify bug recurrence into incomplete fix vs regression.

    Returns dict with recurred, recurrence_count, incomplete_fix, regression, details.
    """
    result = {
        "recurred": False,
        "recurrence_count": 0,
        "incomplete_fix": False,
        "regression": False,
        "details": [],
    }

    error_type = extract_error_type_from_commit(merge)
    merge_ts_str = merge.get("merged_ts", "")
    if not merge_ts_str:
        return result

    try:
        merge_dt = datetime.fromisoformat(merge_ts_str)
        merge_epoch = merge_dt.timestamp()
    except (ValueError, TypeError):
        return result

    commit_hash = merge["merge_commit"]
    changed_files = set(_get_changed_py_files(commit_hash))

    # Collect error types seen before the merge (baseline)
    pre_merge_types = set()
    for err in errors:
        if err.get("timestamp", 0) <= merge_epoch:
            et = err.get("error_type")
            if et:
                pre_merge_types.add(et)

    # Analyze post-merge errors
    same_type_count = 0
    regression_hits = []

    for err in errors:
        if err.get("timestamp", 0) <= merge_epoch:
            continue

        err_type = err.get("error_type", "")
        err_file = err.get("file", err.get("source_file", ""))

        # Same error_type as the fix target → incomplete fix
        if error_type and err_type == error_type:
            same_type_count += 1

        # New error_type from a file changed in this merge → regression
        if err_type and err_type not in pre_merge_types and err_file:
            if err_file in changed_files:
                regression_hits.append({
                    "error_type": err_type,
                    "file": err_file,
                })

    total_recurrence = same_type_count + len(regression_hits)
    result["recurred"] = total_recurrence > 0
    result["recurrence_count"] = total_recurrence
    result["incomplete_fix"] = same_type_count > 0
    result["regression"] = len(regression_hits) > 0

    if same_type_count > 0:
        result["details"].append(f"Incomplete fix: {error_type} reappeared x{same_type_count}")
    for hit in regression_hits:
        result["details"].append(f"Regression: new {hit['error_type']} in {hit['file']}")

    return result


# ── C. Test Manipulation ─────────────────────────────────────────

def check_test_manipulation(merge: dict) -> tuple[list[str], list[str]]:
    """Check if test files were modified and if assertions were removed/relaxed."""
    commit_hash = merge["merge_commit"]
    diff = _git("diff", f"{commit_hash}^..{commit_hash}", "--", "*.py")
    if not diff:
        return [], []

    test_files_touched: list[str] = []
    suspicious: list[str] = []
    current_file = ""

    for line in diff.splitlines():
        # Track current file
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 4:
                current_file = parts[3].lstrip("b/")
                if _is_test_file(current_file) and current_file not in test_files_touched:
                    test_files_touched.append(current_file)
            continue

        if not _is_test_file(current_file):
            continue

        # Removed assertion lines (starts with -)
        if line.startswith("-") and not line.startswith("---"):
            if ASSERTION_RE.search(line):
                suspicious.append(f"REMOVED in {current_file}: {line[1:].strip()[:100]}")

        # Changed numeric thresholds in assertions (rough heuristic)
        if line.startswith("+") and not line.startswith("+++"):
            if ASSERTION_RE.search(line):
                # Check if a preceding removed line had a stricter number
                # (simplified: just flag modified assertions as noteworthy)
                pass  # Adding assertions is fine, only removals are suspicious

    return test_files_touched, suspicious


# ── D. Output ─────────────────────────────────────────────────────

def run_scan(days: int = 7) -> dict:
    """Run the full outcome scan and return structured results."""
    merges = scan_recent_merges(days)
    errors = load_error_tracker(days=14)

    results = []
    for merge in merges:
        reverted = check_reverted(merge)
        recurred, recurrence_count = check_recurrence(merge, errors)
        recurrence_class = classify_recurrence(merge, errors)
        test_files, suspicious = check_test_manipulation(merge)
        qa_passed, reject_count = check_qa_first_pass(merge)
        overflow, overflow_files = check_scope_overflow(merge)

        results.append({
            "ticket_id": merge["ticket_id"],
            "merge_commit": merge["merge_commit"],
            "merged_at": merge["merged_at"],
            "fix_survived": not reverted,
            "bug_recurred": recurrence_class["recurred"],
            "recurrence_count": recurrence_class["recurrence_count"],
            "incomplete_fix": recurrence_class["incomplete_fix"],
            "regression": recurrence_class["regression"],
            "recurrence_details": recurrence_class["details"],
            "test_manipulation": len(suspicious) > 0,
            "test_files_touched": test_files,
            "suspicious_changes": suspicious,
            "qa_first_pass": qa_passed,
            "qa_reject_count": reject_count,
            "scope_overflow": overflow,
            "overflow_files": overflow_files,
            "notes": "",
        })

    total = len(results)
    survived = sum(1 for r in results if r["fix_survived"])
    recurred = sum(1 for r in results if r["bug_recurred"])
    manipulated = sum(1 for r in results if r["test_manipulation"])
    qa_passed_n = sum(1 for r in results if r["qa_first_pass"])
    overflow_n = sum(1 for r in results if r["scope_overflow"])

    report = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "scanned_merges": total,
        "scan_window_days": days,
        "results": results,
        "summary": {
            "fix_survival_rate": round(survived / total * 100, 1) if total else None,
            "bug_recurrence_rate": round(recurred / total * 100, 1) if total else None,
            "test_manipulation_count": manipulated,
            "qa_first_pass_rate": round(qa_passed_n / total * 100, 1) if total else None,
            "scope_overflow_rate": round(overflow_n / total * 100, 1) if total else None,
        },
    }

    # Save to file
    OUTCOMES_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTCOMES_DIR / f"{report['date']}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    return report


def format_summary(report: dict) -> str:
    """Format a human-readable summary."""
    s = report["summary"]
    total = report["scanned_merges"]
    date = report["date"]
    days = report.get("scan_window_days", 7)

    survived_n = sum(1 for r in report["results"] if r["fix_survived"])
    recurred_n = sum(1 for r in report["results"] if r["bug_recurred"])
    manip_n = s["test_manipulation_count"]
    qa_passed_n = sum(1 for r in report["results"] if r.get("qa_first_pass", True))
    overflow_n = sum(1 for r in report["results"] if r.get("scope_overflow", False))

    lines = [
        f"MADS Outcome Report ({date})",
        f"扫描合并: {total} | 窗口: {days} 天",
        "",
    ]
    if total == 0:
        lines.append("无最近的 MADS/MAQS 合并记录。")
    else:
        surv_rate = s["fix_survival_rate"]
        rec_rate = s["bug_recurrence_rate"]
        qa_rate = s.get("qa_first_pass_rate")
        overflow_rate = s.get("scope_overflow_rate")
        lines.append(f"Fix 存活率: {surv_rate}% ({survived_n}/{total})")
        lines.append(f"Bug 复发率: {rec_rate}% ({recurred_n}/{total})")
        lines.append(f"QA 首次通过率: {qa_rate}% ({qa_passed_n}/{total})")
        lines.append(f"Scope 溢出率: {overflow_rate}% ({overflow_n}/{total})")
        lines.append(f"Test 操纵: {manip_n} 件")

        # Flag any issues
        for r in report["results"]:
            flags = []
            if not r["fix_survived"]:
                flags.append("REVERTED")
            if r.get("incomplete_fix"):
                flags.append(f"INCOMPLETE_FIX x{r['recurrence_count']}")
            elif r.get("regression"):
                flags.append("REGRESSION")
            elif r["bug_recurred"]:
                flags.append(f"RECURRED x{r['recurrence_count']}")
            if r["test_manipulation"]:
                flags.append("TEST_MANIP")
            if not r.get("qa_first_pass", True):
                flags.append(f"QA_REJECT x{r.get('qa_reject_count', 0)}")
            if r.get("scope_overflow"):
                flags.append(f"SCOPE_OVERFLOW +{len(r.get('overflow_files', []))} files")
            if flags:
                lines.append(f"  ⚠ {r['ticket_id']}: {', '.join(flags)}")

    lines.append(f"\n详情: data/mads_outcomes/{date}.json")
    return "\n".join(lines)


async def run_outcome_tracker(days: int = 7) -> str:
    """Async entry point for cron handler."""
    report = run_scan(days)
    summary = format_summary(report)
    log.info("Outcome scan complete: %d merges scanned", report["scanned_merges"])
    return summary


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(run_outcome_tracker())
    print(result)
