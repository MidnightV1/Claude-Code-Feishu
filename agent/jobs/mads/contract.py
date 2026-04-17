# -*- coding: utf-8 -*-
"""MADS Contract Negotiation stage.

Implementer (Sonnet) proposes an Implementation Contract; a reviewer agent
either accepts or requests revisions. They iterate via a shared artifact
file until consensus is reached or the round limit is hit.

Complexity-routed:
  L1: no contract (skipped by pipeline)
  L2: lightweight contract — 3-section proposal + isolated self-review (no loop)
  L3: standard contract — full 7-section proposal + reviewer (max 2 rounds)
  L4/L5: decomposed before reaching contract (handled by pipeline)

Inspired by Anthropic's "sprint contract" pattern — no code is written until
both agents agree on what "done" looks like and how to verify it.
"""

import re
from agent.jobs.mads.helpers import run_agent, write_artifact, read_artifact, append_artifact, log


# ══════════════════════════════════════════════════════════════════════
#  L3 Standard Prompts (full 7-section contract)
# ══════════════════════════════════════════════════════════════════════

PROPOSER_PROMPT = """\
You are the Implementer agent in a multi-agent development system (MADS).
Your role at this stage is to propose a precise Implementation Contract
BEFORE writing any code. The contract must be specific enough for a QA
agent to independently verify the work without ambiguity.

## Core principle: User-scenario-driven

The contract is a promise about USER EXPERIENCE, not a code inventory.
Start from what the user does, what they see, and what outcome they get.
Implementation details serve to explain HOW those experiences are delivered,
not the other way around.

## Your task

Read the ticket information provided and produce an Implementation Contract
in the structured format below.

## Output format

Produce a markdown document with exactly these sections:

### Implementation Contract

**Summary**: One sentence describing what the user will be able to do after
this is implemented.

**User Scenarios**
Numbered list of concrete user-facing scenarios. Each scenario follows:
- **Trigger**: What the user does (e.g., "User posts '做个 XX 功能' in Feishu")
- **Expected behavior**: What the system does in response, step by step
- **User-visible outcome**: What the user sees, receives, or experiences
- **Timing**: When each step happens (immediate / async / within Xh)

Cover the happy path AND the key failure/edge paths the user might encounter.
This is the MAIN BODY of the contract — spend the most effort here.

**Acceptance Criteria**
Numbered list of criteria written from the user's perspective. Each must be
independently verifiable by observing system behavior (not by reading code).
Good: "When user comments on the design doc, they receive an updated doc
link within the next pipeline cycle."
Bad: "function `foo(x)` returns `bar` when `x` is empty."

**Implementation Plan**
For each user scenario above, describe the technical approach:
- Files to modify/create and what changes
- Key functions and their roles
- Data flow from trigger to outcome

This section is the APPENDIX — it explains how the scenarios are delivered.

**Verification Method**
For each acceptance criterion:
- How to simulate the trigger
- What to observe (Feishu message, Bitable record, file artifact, git commit)
- What constitutes PASS vs FAIL

**Edge Cases & Boundaries**
Scenarios where the system might not behave as expected:
- What the user might do wrong (bad input, wrong timing)
- What might fail (API timeout, LLM error, data inconsistency)
- How each case is handled — and what the user sees in each case

**Assumptions**
Dependencies on external systems, configs, or behaviors that the contract
relies on.

---

Remember: a good contract reads like a product spec, not a code review.
The user reading this contract should understand exactly what they're getting
without knowing any implementation details.
"""

REVIEWER_PROMPT = """\
You are the QA agent in a multi-agent development system (MADS).
Your role at this stage is to critically review an Implementation Contract
proposed by the Implementer. You are the last line of defense before code
is written — it is much cheaper to catch gaps here than after implementation.

## Core principle: User-scenario completeness

The contract must first and foremost guarantee a complete user experience.
A contract with perfect code coverage but missing user scenarios is REJECTED.
A contract with complete user scenarios but imperfect code details can be
revised incrementally.

## Your task

Read the proposed Implementation Contract (and any prior revision history)
and evaluate it against the original ticket information.

## Review checklist

Check for ALL of the following, IN THIS PRIORITY ORDER:

1. **User scenario completeness** — Does the contract cover ALL user-facing
   scenarios implied by the ticket? Are there user journeys that are missing?
   Does each scenario describe trigger → behavior → outcome → timing?
   Can a non-technical stakeholder read the scenarios and understand what
   they're getting?

2. **Acceptance criteria quality** — Are criteria written from the user's
   perspective (what the user sees/experiences), not from the code's
   perspective (what function returns)? Is each criterion independently
   verifiable by observing behavior?

3. **Missing edge cases** — Are there user actions, system states, or
   failure modes the contract does not account for? Consider: bad user input,
   concurrent operations, API failures, timeout, partial success. For each
   edge case: does the user know what happened?

4. **Scope problems**
   - Too broad: changes that aren't required by the ticket
   - Too narrow: required changes that are missing
   - Scope creep: tangential "improvements" that should be separate tickets

5. **Implementation-scenario alignment** — Does the Implementation Plan
   actually deliver the promised user scenarios? Are there scenarios with
   no corresponding implementation? Are there implementation details that
   serve no user scenario?

6. **Assumption validity** — Are the stated assumptions correct based on
   the ticket context? Would any assumption failure break a user scenario?

## Output

**CRITICAL**: Your output MUST include a `<contract_verdict>` XML block.
Without this block the system cannot parse your judgment and the contract
will stall. This is mandatory regardless of your verdict.

Write a brief review narrative (2-5 sentences), then output the control block:

```xml
<contract_verdict>
<result>ACCEPT or REVISE</result>
<feedback>
If ACCEPT: "approved" or a brief confirmation sentence.
If REVISE: Specific, numbered list of changes required. Be precise —
tell the Implementer exactly what to add, change, or remove.
Each item should be actionable (e.g., "Add edge case for empty list input
in Success Criteria #2 and add a corresponding verification step").
</feedback>
</contract_verdict>
```

Default to REVISE if you are uncertain. An incomplete contract is worse
than a delayed one. Only ACCEPT if the contract is genuinely complete
and verifiable.
"""


# ══════════════════════════════════════════════════════════════════════
#  L2 Lightweight Prompts (3-section contract + isolated review)
# ══════════════════════════════════════════════════════════════════════

PROPOSER_PROMPT_LIGHT = """\
You are the Implementer agent in a multi-agent development system (MADS).
This is a lightweight (L2) contract for a single-file fix.

## Your task

Read the diagnosis and produce a compact Implementation Contract.
Only 3 sections needed — no User Scenarios, Implementation Plan,
Edge Cases, or Assumptions (the diagnosis already covers these).

## Output format

### Implementation Contract

**Summary**: One sentence describing the fix effect.

**Acceptance Criteria**
Numbered list. Each criterion must be:
- Written from the user's perspective (observable behavior, not code internals)
- Independently verifiable
- Covering ALL root causes identified in the diagnosis

**Verification Method**
For each acceptance criterion:
- How to trigger/simulate
- What to observe (log, Feishu message, git diff, Bitable record)
- PASS vs FAIL definition

That's it. Keep it concise — this contract is for a single-file fix.
"""

REVIEWER_PROMPT_LIGHT = """\
You are reviewing a lightweight (L2) Implementation Contract for a
single-file fix. The Proposer and you are separate agents — you have
NOT seen the Proposer's reasoning, only the final contract output.

## Review checklist

Check these 3 points:

1. **Coverage**: Do the Acceptance Criteria cover ALL root causes from
   the diagnosis? Missing any root cause = REVISE.

2. **Verifiability**: Can each Verification Method actually be executed?
   Are PASS/FAIL conditions specific enough?

3. **Scope boundary**: Does the contract stay within `<affected_files>`?
   If you notice issues OUTSIDE the affected files, note them but do NOT
   expand the contract scope — those should be separate tickets.

## Output

**CRITICAL**: Your output MUST include a `<contract_verdict>` XML block.

```xml
<contract_verdict>
<result>ACCEPT or REVISE</result>
<feedback>
If ACCEPT: "approved" or brief confirmation.
If REVISE: Specific, numbered list of required changes.
</feedback>
</contract_verdict>
```

For L2 contracts, default to ACCEPT if criteria cover all root causes
and verification is executable. Minor imperfections can be caught by
the QA phase after implementation.
"""


# ══════════════════════════════════════════════════════════════════════
#  Verdict parser
# ══════════════════════════════════════════════════════════════════════

def parse_contract_verdict(text: str, round_num: int = 1) -> tuple[str, str]:
    """Parse <contract_verdict> XML block from QA reviewer output.

    Returns (verdict, feedback) where verdict is "ACCEPT" or "REVISE".

    Fail-closed: parse failure always returns REVISE regardless of round_num.
    Unresolved rounds exhaust max_rounds → negotiate_contract returns None →
    upstream escalation path triggers normally.
    """
    m = re.search(
        r"<contract_verdict>\s*"
        r"<result>\s*(ACCEPT|REVISE)\s*</result>\s*"
        r"<feedback>\s*(.*?)\s*</feedback>\s*"
        r"</contract_verdict>",
        text, re.DOTALL | re.IGNORECASE,
    )
    if m:
        verdict = m.group(1).upper()
        feedback = m.group(2).strip()
        return verdict, feedback

    # Parse failure — fail-closed: always REVISE to prevent silent bad contracts
    log.warning(
        "Contract review missing <contract_verdict> at round %d — "
        "defaulting to REVISE (fail-closed)",
        round_num,
    )
    return "REVISE", ""


# ══════════════════════════════════════════════════════════════════════
#  L2 Lightweight contract (isolated self-review, no loop)
# ══════════════════════════════════════════════════════════════════════

async def negotiate_contract_light(
    router,
    ticket_id: str,
    ticket_info: str,
) -> str:
    """L2 lightweight contract: Propose + isolated review, no negotiation loop.

    Two independent API calls to prevent self-review bias:
      Call 1 (Sonnet): Read diagnosis → produce 3-section contract
      Call 2 (Sonnet): Read diagnosis + contract → review (fresh context)

    If reviewer says REVISE, feedback is passed to fix stage (not looped).
    Always returns a contract string (never None — L2 doesn't stall).
    """
    log.info("[contract:%s] L2 lightweight — proposing", ticket_id)

    # ── Call 1: Propose ──
    proposer_prompt = (
        f"## Ticket Information (Diagnosis)\n\n{ticket_info}\n\n"
        "Produce the lightweight Implementation Contract for this ticket."
    )
    proposal = await run_agent(
        router,
        role=f"contract-proposer-light:{ticket_id}",
        model="sonnet",
        prompt=proposer_prompt,
        system_prompt=PROPOSER_PROMPT_LIGHT,
    )

    # ── Call 2: Isolated review (fresh context — no proposer reasoning) ──
    log.info("[contract:%s] L2 lightweight — isolated review", ticket_id)
    reviewer_prompt = (
        f"## Ticket Information (Diagnosis)\n\n{ticket_info}\n\n"
        f"## Proposed Contract\n\n{proposal}\n\n"
        "Review this contract and output your verdict."
    )
    review = await run_agent(
        router,
        role=f"contract-reviewer-light:{ticket_id}",
        model="sonnet",
        prompt=reviewer_prompt,
        system_prompt=REVIEWER_PROMPT_LIGHT,
    )

    verdict, feedback = parse_contract_verdict(review, round_num=2)
    log.info("[contract:%s] L2 verdict: %s", ticket_id, verdict)

    # Build contract artifact
    content = f"--- L2 Lightweight Contract ---\n{proposal}\n\n"
    content += f"--- L2 Review ({verdict}) ---\n{review}\n\n"
    if verdict == "REVISE" and feedback:
        content += (
            f"--- Reviewer Feedback (passed to Fix stage) ---\n"
            f"{feedback}\n\n"
        )

    write_artifact(ticket_id, "contract.md", content)
    return content


# ══════════════════════════════════════════════════════════════════════
#  L3 Standard negotiation loop
# ══════════════════════════════════════════════════════════════════════

async def negotiate_contract(
    router,
    ticket_id: str,
    ticket_info: str,
    max_rounds: int = 2,
    reviewer_model: str = "sonnet",
) -> str | None:
    """Run the contract negotiation loop between Implementer and Reviewer.

    Round structure:
      1. Implementer proposes contract → written to contract.md
      2. Reviewer reviews → appended to contract.md
      3. If ACCEPT: return final contract text
         If REVISE: Implementer revises → appended, repeat from 2
      4. If max_rounds exceeded without ACCEPT: return None (caller escalates)

    Args:
        router: LLM router instance.
        ticket_id: Ticket identifier (used for artifact path).
        ticket_info: Diagnosis (bugs) or implementation_spec + test_cases (features).
        max_rounds: Maximum negotiation rounds before giving up (default 2).
        reviewer_model: Model for the reviewer agent (default "sonnet").

    Returns:
        Final agreed contract text, or None if consensus not reached.
    """
    log.info("[contract:%s] Starting negotiation (max_rounds=%d, reviewer=%s)",
             ticket_id, max_rounds, reviewer_model)

    # ── Round 1: initial proposal ──────────────────────────────────────
    proposer_prompt_r1 = (
        f"## Ticket Information\n\n{ticket_info}\n\n"
        "Produce the Implementation Contract for this ticket."
    )

    log.info("[contract:%s] Round 1 — Implementer proposing", ticket_id)
    proposal = await run_agent(
        router,
        role=f"contract-proposer:{ticket_id}",
        model="sonnet",
        prompt=proposer_prompt_r1,
        system_prompt=PROPOSER_PROMPT,
    )

    # Write initial contract artifact
    initial_content = f"--- Round 1: Proposal ---\n{proposal}\n\n"
    write_artifact(ticket_id, "contract.md", initial_content)

    for round_num in range(1, max_rounds + 1):
        # ── QA Review ─────────────────────────────────────────────────
        contract_so_far = read_artifact(ticket_id, "contract.md") or ""

        # Only pass ticket info + latest proposal to reviewer (not full history)
        # This prevents context inflation that caused QA to produce empty responses
        reviewer_prompt = (
            f"## Ticket Information\n\n{ticket_info}\n\n"
            f"## Contract (all rounds so far)\n\n{contract_so_far}\n\n"
            "Review the latest proposal (the most recent 'Proposal' section above) "
            "and output your verdict."
        )

        log.info("[contract:%s] Round %d — Reviewer (%s) reviewing",
                 ticket_id, round_num, reviewer_model)
        review = await run_agent(
            router,
            role=f"contract-reviewer:{ticket_id}",
            model=reviewer_model,
            prompt=reviewer_prompt,
            system_prompt=REVIEWER_PROMPT,
        )

        append_artifact(ticket_id, "contract.md", f"--- Round {round_num}: Review ---\n{review}\n\n")

        verdict, feedback = parse_contract_verdict(review, round_num=round_num)
        log.info("[contract:%s] Round %d verdict: %s", ticket_id, round_num, verdict)

        if verdict == "ACCEPT":
            final_contract = read_artifact(ticket_id, "contract.md") or ""
            log.info(
                "[contract:%s] Contract accepted after %d round(s)",
                ticket_id, round_num,
            )
            return final_contract

        # ── REVISE: check round budget ────────────────────────────────
        if round_num >= max_rounds:
            log.warning(
                "[contract:%s] No consensus after %d rounds — escalating",
                ticket_id, max_rounds,
            )
            return None

        # ── Implementer revises ───────────────────────────────────────
        next_round = round_num + 1
        contract_so_far = read_artifact(ticket_id, "contract.md") or ""

        revise_prompt = (
            f"## Ticket Information\n\n{ticket_info}\n\n"
            f"## Contract negotiation history\n\n{contract_so_far}\n\n"
            f"## QA Feedback requiring revision\n\n{feedback}\n\n"
            "The QA agent has requested revisions. Produce a revised Implementation "
            "Contract that addresses ALL feedback points. Keep sections that were "
            "not flagged unchanged; update only what was requested."
        )

        log.info("[contract:%s] Round %d — Implementer revising", ticket_id, next_round)
        revised_proposal = await run_agent(
            router,
            role=f"contract-proposer:{ticket_id}",
            model="sonnet",
            prompt=revise_prompt,
            system_prompt=PROPOSER_PROMPT,
        )

        append_artifact(
            ticket_id, "contract.md",
            f"--- Round {next_round}: Revised Proposal ---\n{revised_proposal}\n\n",
        )

    # Should be unreachable, but guard anyway
    log.warning("[contract:%s] Exited loop unexpectedly", ticket_id)
    return None
