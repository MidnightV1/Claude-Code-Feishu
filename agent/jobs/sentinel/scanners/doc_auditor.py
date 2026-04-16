# -*- coding: utf-8 -*-
"""DocAuditor — audits Feishu docs for staleness and duplicates.

Detection strategy:
  - Staleness  : cross-reference git-changed Python modules with doc update dates
  - Duplicates : normalize doc titles and find near-matches in the listing
  - Recall     : after each search, log query+result count; flag zero-result queries
                 that should logically have matches
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path

from agent.jobs.sentinel.base import BaseSentinelScanner
from agent.jobs.sentinel.models import EntropySignal, ScanContext

log = logging.getLogger("hub.sentinel.doc_auditor")

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DOC_CTL_PATH = str(PROJECT_ROOT / ".claude/skills/feishu-doc/scripts/doc_ctl.py")

# Days without update before a module's doc is considered stale
STALENESS_DAYS = 30

# git log look-back window for "recently changed modules"
GIT_LOOKBACK = "30d"

# Module-name → doc title keyword mappings used for staleness correlation.
# Each entry: module stem → list of title keywords (any match = related doc found).
MODULE_DOC_KEYWORDS: dict[str, list[str]] = {
    "briefing": ["日报", "briefing", "简报"],
    "heartbeat": ["心跳", "heartbeat", "监控"],
    "sentinel": ["sentinel", "哨兵", "熵"],
    "scheduler": ["scheduler", "定时", "cron"],
    "session": ["session", "会话", "上下文"],
    "router": ["router", "路由", "llm"],
    "claude": ["claude", "cli"],
    "bot": ["bot", "飞书", "feishu"],
    "dispatcher": ["dispatcher", "卡片", "card"],
    "media": ["media", "图片", "文件"],
    "mads": ["mads", "合同", "contract", "设计"],
    "maqs": ["maqs", "qa", "质检", "工单"],
}

# Queries that should always return results if the search index is healthy.
# Format: (query_string, human_readable_reason)
CANARY_QUERIES: list[tuple[str, str]] = [
    ("PLAN", "architecture design doc should always be indexed"),
    ("CLAUDE", "CLAUDE.md / system notes should appear"),
    ("日报", "briefing docs should always be indexed"),
]


async def _doc_ctl(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run doc_ctl.py with the given arguments, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, DOC_CTL_PATH, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(PROJECT_ROOT),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1, "", f"doc_ctl timeout after {timeout}s"
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def _git_changed_modules(lookback: str = GIT_LOOKBACK) -> list[str]:
    """Return a deduplicated list of Python module *stems* changed in the last `lookback` period."""
    proc = await asyncio.create_subprocess_exec(
        "git", "log", f"--since={lookback}", "--name-only", "--pretty=format:",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(PROJECT_ROOT),
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        log.warning("DocAuditor: git log timed out")
        return []

    stems: set[str] = set()
    for line in stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if line.endswith(".py"):
            stems.add(Path(line).stem)
    return list(stems)


def _parse_doc_list(output: str) -> list[dict]:
    """Parse `doc_ctl list` stdout into a list of doc dicts.

    Expected format (one line per doc):
        <doc_id>  <title>  [<updated_at_epoch_or_iso>]

    If the format does not match, we fall back to storing only the raw line as
    ``title`` so duplicate detection still works.
    """
    docs: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        # Try tab-separated: id\ttitle\tupdated_at
        parts = line.split("\t")
        if len(parts) >= 2:
            docs.append({
                "doc_id": parts[0].strip(),
                "title": parts[1].strip(),
                "updated_at": parts[2].strip() if len(parts) >= 3 else "",
                "raw": line,
            })
        else:
            # Fall back: treat whole line as title
            docs.append({"doc_id": "", "title": line, "updated_at": "", "raw": line})
    return docs


def _normalize_title(title: str) -> str:
    """Lowercase, strip common prefixes/tags, collapse whitespace."""
    t = title.lower()
    # Strip common decoration patterns: [MADS], 【日报】, (v2), #tag, etc.
    t = re.sub(r"[\[\(【（][^\]\)】）]{0,20}[\]\)】）]", "", t)
    t = re.sub(r"#\S+", "", t)
    # Collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _titles_similar(a: str, b: str, threshold: float = 0.75) -> bool:
    """Simple token-overlap similarity — avoids heavy deps (difflib/fuzz)."""
    if a == b:
        return True
    tokens_a = set(re.split(r"[\s\-_/·:：,，.。]+", a)) - {"", "the", "a", "an"}
    tokens_b = set(re.split(r"[\s\-_/·:：,，.。]+", b)) - {"", "the", "a", "an"}
    if not tokens_a or not tokens_b:
        return False
    overlap = len(tokens_a & tokens_b)
    shorter = min(len(tokens_a), len(tokens_b))
    return (overlap / shorter) >= threshold


class DocAuditor(BaseSentinelScanner):
    """Audits Feishu docs for staleness and duplicates using native Feishu search."""

    name = "doc_auditor"

    def estimate_change_rate(self) -> str:
        return "weekly"

    # ── Public entry point ────────────────────────────────────────────────────

    async def scan(self, context: ScanContext) -> list[EntropySignal]:
        signals: list[EntropySignal] = []

        # 1. Staleness check via native full-text search (no doc listing needed)
        stale_signals = await self._check_staleness(context)
        signals.extend(stale_signals)

        # 2. Fetch full doc listing for duplicate detection
        rc, stdout, stderr = await _doc_ctl("list", timeout=60)
        if rc != 0:
            log.warning("DocAuditor: doc_ctl list failed (rc=%d): %s", rc, stderr[:200])
            docs: list[dict] = []
        else:
            docs = _parse_doc_list(stdout)
            log.info("DocAuditor: listed %d docs", len(docs))

        # 3. Duplicate detection (requires doc list)
        if docs:
            dup_signals = self._check_duplicates(docs, context)
            signals.extend(dup_signals)

        # 4. Search recall monitoring via native search
        recall_signals = await self._monitor_recall(context)
        signals.extend(recall_signals)

        return signals

    # ── Staleness ─────────────────────────────────────────────────────────────

    async def _check_staleness(self, context: ScanContext) -> list[EntropySignal]:
        """Flag docs related to recently-changed modules using native full-text search."""
        import time as _time

        signals: list[EntropySignal] = []

        changed_stems = await _git_changed_modules()
        if not changed_stems:
            log.debug("DocAuditor: no changed Python modules in last %s", GIT_LOOKBACK)
            return signals

        for stem in changed_stems:
            keywords = MODULE_DOC_KEYWORDS.get(stem, [stem])
            seen_tokens: set[str] = set()

            for kw in keywords:
                rc, stdout, stderr = await _doc_ctl("native-search", kw, timeout=30)
                if rc != 0:
                    log.warning(
                        "DocAuditor: native-search failed for %r: %s", kw, stderr[:200]
                    )
                    continue

                for doc in _parse_doc_list(stdout):
                    token = doc.get("doc_id", "")
                    if not token or token in seen_tokens:
                        continue
                    seen_tokens.add(token)

                    updated_at = doc.get("updated_at", "")
                    if not updated_at:
                        continue

                    try:
                        updated_epoch = float(updated_at)
                    except (ValueError, TypeError):
                        continue

                    age_days = (_time.time() - updated_epoch) / 86400
                    if age_days >= STALENESS_DAYS:
                        summary = (
                            f"Doc '{doc['title']}' may be stale "
                            f"({int(age_days)}d since update) — "
                            f"related module `{stem}.py` changed in last {GIT_LOOKBACK}"
                        )
                        signal = self._create_signal(
                            category="stale_doc",
                            severity="low",
                            autonomy_level=2,
                            summary=summary,
                            route="notify",
                            evidence=[
                                f"doc_id: {token}",
                                f"title: {doc['title']}",
                                f"last_updated_epoch: {updated_at}",
                                f"age_days: {int(age_days)}",
                                f"triggered_by_module: {stem}.py",
                                f"search_keyword: {kw}",
                            ],
                            suggested_action=(
                                f"Review and update '{doc['title']}' to reflect "
                                f"recent changes in `{stem}.py`."
                            ),
                            context={"doc_id": token, "module": stem},
                        )
                        if not self._is_duplicate(signal, context.recent_signals):
                            signals.append(signal)

        return signals

    # ── Duplicate detection ───────────────────────────────────────────────────

    def _check_duplicates(
        self, docs: list[dict], context: ScanContext
    ) -> list[EntropySignal]:
        """Detect docs with similar normalized titles."""
        signals: list[EntropySignal] = []

        normalized: list[tuple[str, dict]] = [
            (_normalize_title(d["title"]), d) for d in docs if d.get("title")
        ]

        reported_pairs: set[frozenset] = set()

        for i, (norm_a, doc_a) in enumerate(normalized):
            for norm_b, doc_b in normalized[i + 1:]:
                if not norm_a or not norm_b:
                    continue
                # Skip self-comparison (same doc listed twice by API)
                if norm_a == norm_b:
                    continue
                pair_key = frozenset({doc_a.get("doc_id", norm_a), doc_b.get("doc_id", norm_b)})
                if pair_key in reported_pairs:
                    continue
                if _titles_similar(norm_a, norm_b):
                    reported_pairs.add(pair_key)
                    summary = (
                        f"Possible duplicate docs: '{doc_a['title']}' ≈ '{doc_b['title']}'"
                    )
                    signal = self._create_signal(
                        category="doc_duplicate",
                        severity="low",
                        autonomy_level=2,
                        summary=summary,
                        route="notify",
                        evidence=[
                            f"doc_a: {doc_a.get('doc_id', 'unknown')} — {doc_a['title']}",
                            f"doc_b: {doc_b.get('doc_id', 'unknown')} — {doc_b['title']}",
                            f"normalized_a: {norm_a}",
                            f"normalized_b: {norm_b}",
                        ],
                        suggested_action=(
                            "Review both docs and merge or clearly differentiate them."
                        ),
                        context={
                            "doc_id_a": doc_a.get("doc_id", ""),
                            "doc_id_b": doc_b.get("doc_id", ""),
                        },
                    )
                    if not self._is_duplicate(signal, context.recent_signals):
                        signals.append(signal)

        return signals

    # ── Recall monitoring ─────────────────────────────────────────────────────

    async def _monitor_recall(self, context: ScanContext) -> list[EntropySignal]:
        """Run canary queries via native search and flag zero-result responses."""
        signals: list[EntropySignal] = []

        for query, reason in CANARY_QUERIES:
            rc, stdout, stderr = await _doc_ctl("native-search", query, timeout=30)

            result_count = 0
            if rc == 0 and stdout.strip():
                result_count = len([l for l in stdout.splitlines() if l.strip()])

            # Log to context for external monitoring consumers
            recall_log = context.user_config.setdefault("doc_auditor_recall_log", [])
            recall_log.append({
                "query": query,
                "result_count": result_count,
                "rc": rc,
                "error": stderr[:200] if rc != 0 else "",
            })

            log.debug(
                "DocAuditor recall: query=%r results=%d rc=%d", query, result_count, rc
            )

            if rc != 0:
                log.warning(
                    "DocAuditor: search failed for canary query %r: %s", query, stderr[:200]
                )
                continue

            if result_count == 0:
                summary = (
                    f"Search recall degradation: query '{query}' returned 0 results "
                    f"({reason})"
                )
                signal = self._create_signal(
                    category="search_recall_degradation",
                    severity="medium",
                    autonomy_level=2,
                    summary=summary,
                    route="notify",
                    evidence=[
                        f"query: {query}",
                        f"expected: ≥1 result",
                        f"actual: 0 results",
                        f"reason: {reason}",
                    ],
                    suggested_action=(
                        "Check Feishu search index health. "
                        "Confirm docs are in shared folders accessible to the bot."
                    ),
                    context={"query": query, "result_count": 0},
                )
                if not self._is_duplicate(signal, context.recent_signals):
                    signals.append(signal)

        return signals
