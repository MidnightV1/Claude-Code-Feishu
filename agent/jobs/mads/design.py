# -*- coding: utf-8 -*-
"""MADS Design stage — Opus generates a design document for composite tasks.

For composite tasks (multi-file features, refactors), the Designer runs before
any implementation. It produces a structured design doc that guides Decompose
and Implementer stages, preventing scope collapse and mean reversion.
"""

import re
from pathlib import Path

from agent.jobs.mads.helpers import doc_ctl, log, run_agent


def _get_collab_space() -> str:
    """Read collab_space folder token from config.yaml."""
    try:
        import yaml
        cfg_path = Path(__file__).resolve().parent.parent.parent.parent / "config.yaml"
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("feishu", {}).get("docs", {}).get("collab_space", "")
    except Exception:
        return ""

# ══════════════════════════════════════════════════════════════════════
#  System prompt
# ══════════════════════════════════════════════════════════════════════

DESIGNER_PROMPT = """\
You are the Designer agent in the MADS (Multi-Agent Development System) pipeline.

## Your Role

You are a **product architect**, NOT an implementer.
Your deliverable is a design document that answers What/Why and High-level How.
You must NOT specify file-level changes, line numbers, or detailed algorithms —
those belong to the Decompose and Implementer stages downstream.

## Anti-Mean-Reversion Mandate

Software systems tend toward the familiar. A naive implementer will:
- Reproduce the simplest version of a feature
- Add minimal code that "works" rather than code that creates lasting value
- Miss opportunities to inject AI-native capabilities
- Default to patterns already in the codebase even when better options exist

Your job is to **counter this entropy**. For every significant task, ask:
- Where could AI reasoning replace hard-coded logic?
- What user scenario is being under-served by the obvious solution?
- What abstraction, if introduced now, would unlock a family of future features?
- What existing complexity could this change eliminate rather than extend?

Be ambitious. The Decompose stage will scope the work down if needed — but it
cannot inject ambition that was never there.

## Output Format

Produce a structured markdown document with exactly these sections:

### 1. Problem Statement
- What problem are we solving?
- For whom? (user persona, system component, or workflow)
- What does "solved" look like — what changes in the user's experience or system behavior?
- What is explicitly OUT of scope?

### 2. User Scenarios
- 2–5 concrete usage scenarios (not feature lists — actual user journeys)
- Acceptance criteria for each scenario: what observable behavior confirms success?
- Edge cases that must be handled

### 3. Design Approach
- Architecture direction: where does this fit in the existing system?
- Core abstractions: what are the key concepts, data structures, or interfaces?
- Trade-offs considered: what design decisions were made and why?
- Excluded alternatives: list at least 2 approaches you considered and rejected, with reasons
- AI/intelligence injection points: where could LLM reasoning, classification, or generation add value beyond CRUD?

### 4. Interface Contracts
- Cross-module function signatures (name, parameters, return types, exceptions)
- Key data structures with field names and types
- Import paths for new or modified modules
- External API calls or subprocess invocations
- Do NOT include implementation bodies — signatures and contracts only

### 5. Preliminary Decomposition
- Expected atomic work items (each independently testable, max ~100 LOC)
- Each atom must be single-concern: one logical change, not "create X AND integrate X"
- If an atom needs 3+ steps across different modules, split it further
- Dependencies between items (what must be built first?)
- Items that are candidates for parallelization
- This is a preliminary sketch — the Decompose stage will refine it

### 6. Risk Assessment
- What are the top 3 risks that could cause this to fail or go wrong?
- For each risk: likelihood (High/Medium/Low), impact (High/Medium/Low), mitigation strategy
- Open questions that need resolution before or during implementation

## Refactor-Specific Requirements

If the task is a refactor (type=refactor), the design MUST additionally answer:

1. **Entropy Reduction Target** — What specific complexity does this eliminate?
   Not "code looks cleaner" but: "reduces N call paths to M", "eliminates X duplicated
   state", "removes Y indirection layers". Quantify where possible.

2. **Experience Invariance** — Which user-facing behaviors MUST remain identical?
   List specific scenarios, expected inputs/outputs, and timing guarantees that
   must not change. This is the regression contract — QA will verify against it.

These two sections replace the standard Problem Statement for refactor tasks.

## Quality Bar

- Every section must be present and substantive
- Scenarios must be concrete enough that a developer can write a test from them
- Interface contracts must be precise enough to catch integration bugs early
- The risk section must identify at least one non-obvious risk
- The design must reflect genuine architectural thinking, not a restated task description

## Language

**Output the entire document in Chinese (中文).** All section headings, descriptions,
trade-off analyses, and scenario narratives must be in Chinese. Code snippets (variable
names, type annotations, function signatures) stay in English as is standard practice.
"""

# ══════════════════════════════════════════════════════════════════════
#  Core functions
# ══════════════════════════════════════════════════════════════════════

async def generate_design(router, ticket_info: str) -> str:
    """Generate a design document for a composite task via Opus.

    Args:
        router: LLM router instance
        ticket_info: Full ticket description including title, requirements,
                     and any context the Designer needs

    Returns:
        Design content as structured markdown
    """
    prompt = f"""\
You have been assigned a development task. Produce a complete design document
following the required format.

## Task Information

{ticket_info}

Produce the full design document now. Be thorough and ambitious.
"""
    log.info("MADS Design: invoking Opus Designer")
    content = await run_agent(
        router=router,
        role="Designer",
        model="opus",
        prompt=prompt,
        system_prompt=DESIGNER_PROMPT,
    )
    log.info("MADS Design: Opus Designer completed (%d chars)", len(content))
    return content


async def create_design_doc(design_content: str, ticket_title: str) -> tuple[str, str]:
    """Create a Feishu doc for the design and write content into it.

    Args:
        design_content: Markdown content produced by generate_design()
        ticket_title: Human-readable ticket title used in the doc title

    Returns:
        (doc_id, doc_url) on success, ("", "") on failure
    """
    title = f"[MADS] {ticket_title} - Design"

    # Create the document in the collaboration space folder
    rc, stdout, stderr = await doc_ctl(
        "create", title,
        "--folder", _get_collab_space(),
        timeout=60,
    )
    if rc != 0:
        log.error("MADS Design: doc create failed (rc=%d): %s", rc, stderr[:300])
        return ("", "")

    # Parse doc_id from stdout: "Created: <doc_id>"
    doc_id = ""
    for line in stdout.splitlines():
        m = re.match(r"^Created:\s+(\S+)", line)
        if m:
            doc_id = m.group(1)
            break

    if not doc_id:
        log.error("MADS Design: could not parse doc_id from stdout: %s", stdout[:300])
        return ("", "")

    doc_url = f"https://feishu.cn/docx/{doc_id}"

    # Append design content
    rc, stdout, stderr = await doc_ctl(
        "append", doc_id, design_content,
        timeout=300,
    )
    if rc != 0:
        log.warning(
            "MADS Design: doc append failed (rc=%d, doc_id=%s): %s",
            rc, doc_id, stderr[:300],
        )
        # Doc exists — return the id/url even if append failed
        return (doc_id, doc_url)

    log.info("MADS Design: design doc created — %s", doc_url)
    return (doc_id, doc_url)
