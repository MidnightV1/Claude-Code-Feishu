# -*- coding: utf-8 -*-
"""MADS Decompose stage — Opus splits an approved design into atomic work tickets.

After a design document is approved (user reviewed via Feishu comments), this
stage calls Opus to produce structured XML sub-tickets, each independently
committable and rollbackable.  The sub-tickets are then written as Bitable
records linked to the parent ticket.

XML schema mirrors Bitable table headers — flat tags, natural language content.
"""

import json
import re
from xml.etree import ElementTree as ET

from agent.jobs.mads.helpers import (
    bitable_add,
    log,
    parse_json_response,
    run_agent,
    write_artifact,
)

_MAX_DECOMPOSE_RETRIES = 2

# ══════════════════════════════════════════════════════════════════════
#  Decomposer system prompt
# ══════════════════════════════════════════════════════════════════════

DECOMPOSER_PROMPT = """You are a senior software architect acting as a technical decomposer.

Your task: read an approved design document and split it into atomic sub-tickets.

## Decomposition rules

1. **Atomic** — each sub-ticket must be independently committable and rollbackable.
   A reviewer must be able to merge it without merging any other sub-ticket.
2. **Single-concern** — each sub-ticket must address exactly ONE logical change.
   Signs it needs further splitting:
   - Description has 3+ numbered steps that touch different functions/modules
   - target_files lists 3+ files with non-trivial changes to each
   - The ticket requires BOTH creating a new function AND integrating it elsewhere
   Split "create X" and "integrate X into Y" into separate sub-tickets when they
   can be independently verified. A fixer agent has ~5 minutes and ~100 LOC budget
   per ticket — design accordingly.
3. **Concrete** — description must list exact steps (filenames, function
   signatures, data structures).  Vague goals ("improve X") are not allowed.
4. **Testable** — test_cases must cover the normal path, at least one edge case,
   and at least one error-handling case.
5. **Scoped** — list every file that may be touched in target_files.
   No file outside that list may be changed.
6. **Ordered** — use dependency (0-based indices of other sub-tickets)
   to express prerequisite atoms.  Atoms with no dependencies can execute in parallel.

## Output format — flat semantic XML

You MUST structure your entire output using XML tags. No JSON, no markdown, no prose outside tags.

First, your analysis (archived, not passed downstream):

<decompose-analysis>
Your reasoning about how to split the design into atomic units...
</decompose-analysis>

Then, the sub-tickets. Each tag maps directly to a Bitable field.
All content is natural language — no nested XML inside fields.

<sub-ticket index="0">
  <title>imperative action phrase, max 80 chars</title>
  <target_files>comma-separated file paths; mark new files with (new)</target_files>
  <scope>what to modify and what to create, in natural language</scope>
  <description>numbered implementation steps with exact file names, function signatures, data structures</description>
  <test_cases>at least one normal path, one edge case, and one error case</test_cases>
  <golden_data>sample input and expected output, or empty if not applicable</golden_data>
  <dependency>comma-separated indices of prerequisite sub-tickets, or empty</dependency>
</sub-ticket>

<sub-ticket index="1">
  ...
</sub-ticket>

Rules:
- Each sub-ticket must have the index attribute (0-based)
- dependency contains comma-separated indices of prerequisite sub-tickets; leave empty if none
- All field content is plain natural language — do NOT use XML/HTML tags, angle brackets, or special markup inside field values. Use backticks for code references if needed. If field content must contain angle brackets or special characters, wrap the entire field value in a CDATA section: <![CDATA[content here]]>
- No hard limit on count. Each atom must be independently committable, independently testable, and necessary for the final goal. Prefer more smaller atoms over fewer large ones, as long as each atom is meaningful and verifiable on its own
- No text outside XML tags
"""

# ══════════════════════════════════════════════════════════════════════
#  XML Parser
# ══════════════════════════════════════════════════════════════════════

# Fields that map directly to Bitable columns (all plain text)
_FIELDS = ("title", "target_files", "scope", "description",
           "test_cases", "golden_data", "dependency")


def _parse_sub_ticket_xml(raw: str) -> list[dict]:
    """Parse <sub-ticket> XML blocks into list of dicts for create_sub_tickets.

    Flat schema — each field is a simple text element, no nesting.
    Returns list of sub-ticket dicts. Empty list on failure.
    """
    # Extract all <sub-ticket ...>...</sub-ticket> blocks
    blocks = re.findall(r"<sub-ticket[^>]*>.*?</sub-ticket>", raw, re.DOTALL)
    if not blocks:
        return []

    results = []
    for block in blocks:
        try:
            root = ET.fromstring(block)
            ticket = {}
            for field in _FIELDS:
                ticket[field] = (root.findtext(field) or "").strip()
        except ET.ParseError as e:
            log.warning("XML parse error in sub-ticket block: %s — falling back to regex", e)
            ticket = {}
            for field in _FIELDS:
                m = re.search(rf"<{field}>(.*?)</{field}>", block, re.DOTALL)
                ticket[field] = m.group(1).strip() if m else ""

        if not ticket.get("title"):
            continue

        results.append(ticket)

    return results


# ══════════════════════════════════════════════════════════════════════
#  Decompose
# ══════════════════════════════════════════════════════════════════════

async def decompose_design(router, design_content: str) -> list[dict]:
    """Call Opus to decompose an approved design into atomic sub-tickets.

    Uses flat semantic XML output. Retries on parse failure up to
    _MAX_DECOMPOSE_RETRIES times with a fresh session each attempt.
    """
    prompt = (
        "Here is the approved design document to decompose into atomic sub-tickets:\n\n"
        + design_content
    )

    for attempt in range(_MAX_DECOMPOSE_RETRIES + 1):
        if attempt > 0:
            log.info("Decomposer retry %d/%d (XML parse failed)", attempt, _MAX_DECOMPOSE_RETRIES)

        raw = await run_agent(
            router=router,
            role="decomposer",
            model="opus",
            prompt=prompt,
            system_prompt=DECOMPOSER_PROMPT,
        )

        if raw.startswith("[ERROR]"):
            log.warning("Decomposer agent returned error: %s", raw[:200])
            continue

        # Parse XML sub-tickets
        parsed = _parse_sub_ticket_xml(raw)
        if parsed:
            log.info("Decomposer: extracted %d sub-tickets from XML", len(parsed))
            return parsed

        # JSON fallback: dict→array compat (LLM may return JSON instead of XML)
        fallback = parse_json_response(raw)
        if isinstance(fallback, dict):
            fallback = [fallback]
        if isinstance(fallback, list) and fallback:
            log.info("Decomposer: extracted %d sub-tickets via JSON fallback", len(fallback))
            return fallback

        log.warning(
            "Decomposer output parse failed (attempt %d, preview=%s)",
            attempt + 1,
            raw[:300],
        )

    log.error("Decomposer: all %d attempts failed", _MAX_DECOMPOSE_RETRIES + 1)
    return []


# ══════════════════════════════════════════════════════════════════════
#  Create Bitable sub-ticket records
# ══════════════════════════════════════════════════════════════════════

async def create_sub_tickets(
    app_token: str,
    table_id: str,
    parent_record_id: str,
    parent_type: str,
    parent_severity: str,
    sub_tickets: list[dict],
) -> list[str]:
    """Write sub-ticket dicts as Bitable records linked to the parent ticket.

    Fields map 1:1 from XML output — no transformation needed.
    """
    created_ids: list[str] = []

    for idx, atom in enumerate(sub_tickets):
        title = atom.get("title", f"sub-ticket-{idx}")

        fields = {
            "title": title,
            "type": parent_type,
            "target_files": atom.get("target_files", ""),
            "scope": atom.get("scope", ""),
            "description": atom.get("description", ""),
            "test_cases": atom.get("test_cases", ""),
            "golden_data": atom.get("golden_data", ""),
            "severity": parent_severity,
            "status": "open",
            "complexity": "atomic",
            "parent_ticket": parent_record_id,
            "dependency": atom.get("dependency", ""),
        }

        record_id = await bitable_add(app_token, table_id, fields)
        if record_id:
            created_ids.append(record_id)
            log.info(
                "Sub-ticket %d/%d created: record_id=%s title=%s",
                idx + 1,
                len(sub_tickets),
                record_id,
                title,
            )
        else:
            log.warning(
                "Sub-ticket %d/%d failed to create: title=%s",
                idx + 1,
                len(sub_tickets),
                title,
            )

    return created_ids


# ══════════════════════════════════════════════════════════════════════
#  Orchestrator helper: decompose + persist artifacts + create records
# ══════════════════════════════════════════════════════════════════════

async def run_decompose_stage(
    router,
    app_token: str,
    table_id: str,
    ticket_id: str,
    parent_record_id: str,
    parent_type: str,
    parent_severity: str,
    design_content: str,
) -> list[str]:
    """Full Decompose stage: decompose design → write artifacts → create sub-tickets.

    Returns list of created sub-ticket record_ids.
    """
    # Persist the input design as an artifact
    write_artifact(ticket_id, "design.md", design_content)

    sub_tickets = await decompose_design(router, design_content)
    if not sub_tickets:
        log.warning("[%s] Decompose produced no sub-tickets", ticket_id)
        return []

    # Persist decomposition result
    write_artifact(ticket_id, "decompose.json", json.dumps(sub_tickets, ensure_ascii=False, indent=2))

    record_ids = await create_sub_tickets(
        app_token=app_token,
        table_id=table_id,
        parent_record_id=parent_record_id,
        parent_type=parent_type,
        parent_severity=parent_severity,
        sub_tickets=sub_tickets,
    )

    log.info(
        "[%s] Decompose stage done: %d/%d sub-tickets created",
        ticket_id,
        len(record_ids),
        len(sub_tickets),
    )
    return record_ids
