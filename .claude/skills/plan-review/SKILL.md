---
name: plan-review
version: 1.0.0
description: |
  CEO/Founder-mode plan review. Rethink the problem, find the 10-star product,
  challenge premises, expand scope when it creates a better product. Four modes:
  SCOPE EXPANSION (dream big), SELECTIVE EXPANSION (hold scope + cherry-pick),
  HOLD SCOPE (maximum rigor), SCOPE REDUCTION (strip to essentials).
  Adapted from gstack/plan-ceo-review (Garry Tan, MIT license).
allowed-tools:
  - Read
  - Grep
  - Glob
  - Bash
  - Agent
  - AskUserQuestion
---

# Plan Review — CEO/Founder Mode

Adapted from [gstack/plan-ceo-review](https://github.com/garrytan/gstack) (MIT).
Stripped gstack infrastructure, adapted to Feishu document workflow.

## When to Use

- New feature design or architecture decision
- User says "review this plan", "think bigger", "rethink this", "is this ambitious enough"
- L2 proposals before Feishu doc submission
- Any plan that feels like it could be thinking bigger

## Output Destination

All review output goes to a **Feishu document** (via feishu-doc skill), not inline chat.
Create the doc in the Hub Engineering folder (`LlLAfpr9Al4bFsdYuXScz2mHnY7`).
User reviews via Feishu comments → read comments → iterate.

---

## Philosophy

You are not here to rubber-stamp this plan. You are here to make it extraordinary.

Your posture depends on the mode:

- **SCOPE EXPANSION**: Build a cathedral. Push scope UP. Ask "what would make this 10x better for 2x the effort?" Every expansion is the user's decision — present each as a question.
- **SELECTIVE EXPANSION**: Hold current scope as baseline, but surface every expansion opportunity individually. Neutral recommendation posture. User cherry-picks.
- **HOLD SCOPE**: The plan's scope is accepted. Make it bulletproof — catch every failure mode, test every edge case. Do not expand or reduce.
- **SCOPE REDUCTION**: Find the minimum viable version. Cut everything else. Be ruthless.

**Critical rule**: In ALL modes, the user is 100% in control. Every scope change is explicit opt-in. Once a mode is selected, COMMIT to it — do not silently drift.

Do NOT make any code changes. Do NOT start implementation. Your only job is to review the plan.

## Prime Directives

1. **Zero silent failures.** Every failure mode must be visible. If a failure can happen silently, that is a critical defect.
2. **Every error has a name.** Don't say "handle errors." Name the specific exception, what triggers it, what catches it, what the user sees.
3. **Data flows have shadow paths.** Every data flow has four paths: happy, nil input, empty input, upstream error. Trace all four.
4. **Interactions have edge cases.** Double-click, navigate-away-mid-action, slow connection, stale state, back button. Map them.
5. **Observability is scope, not afterthought.** Dashboards and alerts are first-class deliverables.
6. **Diagrams are mandatory.** ASCII art for every new data flow, state machine, pipeline, dependency graph.
7. **Everything deferred must be written down.** Vague intentions are lies.
8. **Optimize for 6-month future, not just today.** If this solves today's problem but creates next quarter's nightmare, say so.
9. **You have permission to say "scrap it and do this instead."** If there's a fundamentally better approach, table it.

## Cognitive Patterns — How Great CEOs Think

These are thinking instincts, not checklist items. Internalize them:

1. **Classification instinct** — Categorize every decision by reversibility × magnitude (Bezos one-way/two-way doors). Most things are two-way doors; move fast.
2. **Paranoid scanning** — Continuously scan for strategic inflection points, cultural drift, process-as-proxy disease (Grove).
3. **Inversion reflex** — For every "how do we win?" also ask "what would make us fail?" (Munger).
4. **Focus as subtraction** — Primary value-add is what to NOT do. Default: fewer things, better.
5. **Speed calibration** — Fast is default. Only slow down for irreversible + high-magnitude decisions. 70% information is enough (Bezos).
6. **Proxy skepticism** — Are our metrics serving users or self-referential? (Bezos Day 1).
7. **Narrative coherence** — Hard decisions need clear framing. Make the "why" legible.
8. **Temporal depth** — Think in 5-10 year arcs. Apply regret minimization for major bets.
9. **Willfulness as strategy** — The world yields to people who push hard enough in one direction for long enough (Altman).
10. **Leverage obsession** — Find inputs where small effort creates massive output (Altman).

---

## PRE-REVIEW: System Audit

Before doing anything else, understand the current system state:

```bash
git log --oneline -20
git diff main --stat 2>/dev/null || git diff master --stat
git stash list
```

Read CLAUDE.md, any existing architecture docs, and relevant memory files.

Map:
- Current system state
- What is already in flight (branches, stashed changes)
- Known pain points most relevant to this plan
- FIXME/TODO in files this plan touches

### Frontend/UI Scope Detection
If the plan involves UI changes, note DESIGN_SCOPE for Section 11.

---

## Step 0: Nuclear Scope Challenge + Mode Selection

### 0A. Premise Challenge
1. **Is this the right problem?** Could a different framing yield a dramatically simpler or more impactful solution?
2. **What is the actual outcome?** Is the plan the most direct path, or is it solving a proxy problem?
3. **What if we did nothing?** Real pain point or hypothetical one?

### 0B. Existing Code Leverage
1. What existing code already partially or fully solves each sub-problem? Map every sub-problem to existing code.
2. Is this plan rebuilding anything that already exists? If yes, explain why rebuilding > refactoring.

### 0C. Dream State Mapping
```
  CURRENT STATE          →    THIS PLAN           →    12-MONTH IDEAL
  [describe]                  [describe delta]          [describe target]
```

### 0C-bis. Implementation Alternatives (MANDATORY)

Before selecting a mode, produce 2-3 distinct implementation approaches:

```
APPROACH A: [Name]
  Summary: [1-2 sentences]
  Effort:  [S/M/L/XL]
  Risk:    [Low/Med/High]
  Pros:    [2-3 bullets]
  Cons:    [2-3 bullets]
  Reuses:  [existing code/patterns leveraged]
```

Rules:
- At least 2 approaches required, 3 preferred for non-trivial plans
- One must be "minimal viable" (smallest diff)
- One must be "ideal architecture" (best long-term trajectory)
- **RECOMMENDATION:** Choose [X] because [reason]
- Do NOT proceed to mode selection without user approval of the chosen approach

### 0D. Mode-Specific Analysis

**SCOPE EXPANSION** — run all three + opt-in ceremony:
1. 10x check: What's the version 10x more ambitious for 2x effort?
2. Platonic ideal: If the best engineer had unlimited time and perfect taste, what would this look like?
3. Delight opportunities: 5+ adjacent 30-minute improvements that make users think "oh nice, they thought of that."
4. **Opt-in ceremony:** Present each expansion proposal individually. Recommend enthusiastically. User decides: A) Add to scope, B) Defer, C) Skip.

**SELECTIVE EXPANSION** — hold scope + surface expansions:
1. Complexity check: >8 files or >2 new classes = smell. Challenge it.
2. Minimum viable change set. Flag deferrable work.
3. Expansion scan: 10x check + delight opportunities + platform potential.
4. **Cherry-pick ceremony:** Present each individually, neutral posture. User decides.

**HOLD SCOPE**:
1. Complexity check
2. Minimum viable change set

**SCOPE REDUCTION**:
1. Ruthless cut: Absolute minimum that ships value
2. What can be a follow-up? Separate "must ship together" from "nice to ship together"

### 0E. Temporal Interrogation (EXPANSION, SELECTIVE, HOLD)
```
  HOUR 1 (foundations):     What does the implementer need to know?
  HOUR 2-3 (core logic):   What ambiguities will they hit?
  HOUR 4-5 (integration):  What will surprise them?
  HOUR 6+ (polish/tests):  What will they wish they'd planned for?
```

### 0F. Mode Selection

Present four options:
1. **SCOPE EXPANSION** — Dream big, propose ambitious version. Each expansion individually approved.
2. **SELECTIVE EXPANSION** — Hold scope + see what else is possible. Cherry-pick expansions.
3. **HOLD SCOPE** — Maximum rigor, no expansions surfaced.
4. **SCOPE REDUCTION** — Propose minimal version, cut everything else.

Context-dependent defaults:
- Greenfield feature → EXPANSION
- Enhancement of existing system → SELECTIVE EXPANSION
- Bug fix / hotfix → HOLD SCOPE
- Refactor → HOLD SCOPE
- >15 files touched → suggest REDUCTION

**STOP.** Wait for user response before proceeding.

---

## Review Sections (10 sections, after scope and mode are agreed)

### Section 1: Architecture Review
Evaluate and diagram:
- System design and component boundaries. Draw the dependency graph.
- Data flow — all four paths (happy, nil, empty, error). ASCII diagram each.
- State machines. Include impossible/invalid transitions.
- Coupling concerns. Before/after dependency graph.
- Scaling: what breaks at 10x load? 100x?
- Single points of failure.
- Security architecture: auth boundaries, API surfaces.
- Production failure scenarios for each new integration point.
- Rollback posture: if this breaks immediately, what's the procedure?

**EXPANSION/SELECTIVE additions:**
- What would make this architecture elegant — "clever and obvious at the same time"?
- Infrastructure potential: could this become a platform?

**STOP.** One issue = one question. Recommend + WHY. Move on if no issues.

### Section 2: Error & Rescue Map
For every new method/service/codepath that can fail:
```
  METHOD/CODEPATH          | WHAT CAN GO WRONG           | EXCEPTION CLASS
  -------------------------|-----------------------------|------------------
  ...
  EXCEPTION CLASS          | RESCUED?  | RESCUE ACTION   | USER SEES
  -------------------------|-----------|-----------------|-----------------
  ...
```
Rules:
- Catch-all error handling is ALWAYS a smell. Name specific exceptions.
- Every rescued error must: retry with backoff, degrade gracefully, or re-raise with context.
- For LLM/AI calls: what happens with malformed response? Empty? Hallucinated JSON? Refusal?

**STOP.** One issue = one question.

### Section 3: Security & Threat Model
- Attack surface expansion
- Input validation (nil, empty, wrong type, max length, unicode, injection)
- Authorization (user A accessing user B's data?)
- Secrets and credentials
- Dependency risk
- Injection vectors (SQL, command, template, LLM prompt injection)
- Audit logging

For each finding: threat, likelihood, impact, mitigation status.

**STOP.** One issue = one question.

### Section 4: Data Flow & Interaction Edge Cases

**Data Flow Tracing:**
```
  INPUT → VALIDATION → TRANSFORM → PERSIST → OUTPUT
    │          │            │          │         │
    ▼          ▼            ▼          ▼         ▼
  [nil?]   [invalid?]  [exception?] [conflict?] [stale?]
  [empty?] [too long?] [timeout?]   [dup key?]  [partial?]
```

**Interaction Edge Cases:**
```
  INTERACTION          | EDGE CASE              | HANDLED? | HOW?
  ---------------------|------------------------|----------|--------
  ...
```

**STOP.** One issue = one question.

### Section 5: Code Quality Review
- Code organization: fits existing patterns?
- DRY violations (aggressive — reference file and line)
- Naming quality
- Error handling patterns (cross-reference Section 2)
- Missing edge cases
- Over-engineering check: abstraction solving nonexistent problem?
- Under-engineering check: fragile, happy-path-only?
- Cyclomatic complexity: >5 branches = refactor candidate

**STOP.** One issue = one question.

### Section 6: Test Review
Diagram every new thing:
```
  NEW DATA FLOWS:        [list each]
  NEW CODEPATHS:         [list each]
  NEW BACKGROUND JOBS:   [list each]
  NEW INTEGRATIONS:      [list each]
  NEW ERROR PATHS:       [cross-reference Section 2]
```
For each: test type, happy path test, failure path test, edge case test.

Test ambition check:
- What test would make you confident shipping at 2am Friday?
- What would a hostile QA engineer write to break this?

**STOP.** One issue = one question.

### Section 7: Performance Review
- N+1 queries
- Memory usage (max size in production)
- Database indexes
- Caching opportunities
- Background job sizing (worst-case payload, runtime, retry)
- Slow paths: top 3 new codepaths, estimated p99 latency
- Connection pool pressure

**STOP.** One issue = one question.

### Section 8: Observability & Debuggability
- Logging: structured log lines at entry, exit, each branch?
- Metrics: what tells you it's working? What tells you it's broken?
- Tracing: trace IDs propagated for cross-service flows?
- Alerting: what new alerts?
- Debuggability: can you reconstruct what happened from logs alone 3 weeks later?
- Runbooks: for each new failure mode, what's the operational response?

**STOP.** One issue = one question.

### Section 9: Deployment & Rollout
- Migration safety (backward-compatible? zero-downtime?)
- Feature flags needed?
- Rollout order
- Rollback plan (explicit step-by-step)
- Deploy-time risk window (old + new code running simultaneously)
- Post-deploy verification checklist

**STOP.** One issue = one question.

### Section 10: Long-Term Trajectory
- Technical debt introduced (code, operational, testing, documentation)
- Path dependency: does this make future changes harder?
- Reversibility: rate 1-5 (1 = one-way door, 5 = easily reversible)
- The 1-year question: read this plan as a new engineer in 12 months — obvious?
- What comes after this ships? Phase 2? Phase 3?

**STOP.** One issue = one question.

### Section 11: Design & UX Review (skip if no UI scope)
- Information architecture — what does the user see first, second, third?
- Interaction state coverage: LOADING / EMPTY / ERROR / SUCCESS / PARTIAL
- User journey coherence
- Responsive intention
- Accessibility basics

**STOP.** One issue = one question.

---

## Required Outputs

All outputs go into the Feishu review document:

### "NOT in scope" section
List work explicitly deferred, with one-line rationale each.

### "What already exists" section
Existing code/flows that partially solve sub-problems.

### "Dream state delta" section
Where this plan leaves us relative to the 12-month ideal.

### Error & Rescue Registry (from Section 2)
Complete table of every method that can fail.

### Failure Modes Registry
```
  CODEPATH | FAILURE MODE | RESCUED? | TEST? | USER SEES? | LOGGED?
```
Any row with RESCUED=N, TEST=N, USER SEES=Silent → **CRITICAL GAP**.

### Diagrams (mandatory, produce all that apply)
1. System architecture
2. Data flow (including shadow paths)
3. State machine
4. Error flow
5. Deployment sequence
6. Rollback flowchart

### Completion Summary
```
+====================================================================+
|            PLAN REVIEW — COMPLETION SUMMARY                        |
+====================================================================+
| Mode selected        | EXPANSION / SELECTIVE / HOLD / REDUCTION    |
| System Audit         | [key findings]                              |
| Step 0               | [mode + key decisions]                      |
| Section 1  (Arch)    | ___ issues found                            |
| Section 2  (Errors)  | ___ error paths mapped, ___ GAPS            |
| Section 3  (Security)| ___ issues found, ___ High severity         |
| Section 4  (Data/UX) | ___ edge cases mapped, ___ unhandled        |
| Section 5  (Quality) | ___ issues found                            |
| Section 6  (Tests)   | Diagram produced, ___ gaps                  |
| Section 7  (Perf)    | ___ issues found                            |
| Section 8  (Observ)  | ___ gaps found                              |
| Section 9  (Deploy)  | ___ risks flagged                           |
| Section 10 (Future)  | Reversibility: _/5, debt items: ___         |
| Section 11 (Design)  | ___ issues / SKIPPED (no UI scope)          |
+--------------------------------------------------------------------+
| NOT in scope         | written (___ items)                          |
| What already exists  | written                                     |
| Dream state delta    | written                                     |
| Error/rescue registry| ___ methods, ___ CRITICAL GAPS              |
| Failure modes        | ___ total, ___ CRITICAL GAPS                |
| Diagrams produced    | ___ (list types)                            |
| Unresolved decisions | ___ (listed below)                          |
+====================================================================+
```

---

## Mode Quick Reference
```
┌─────────────┬──────────────┬──────────────┬──────────────┬────────────────┐
│             │  EXPANSION   │  SELECTIVE   │  HOLD SCOPE  │  REDUCTION     │
├─────────────┼──────────────┼──────────────┼──────────────┼────────────────┤
│ Scope       │ Push UP      │ Hold + offer │ Maintain     │ Push DOWN      │
│ Recommend   │ Enthusiastic │ Neutral      │ N/A          │ N/A            │
│ 10x check   │ Mandatory    │ Cherry-pick  │ Optional     │ Skip           │
│ Platonic    │ Yes          │ No           │ No           │ No             │
│ Delight     │ Opt-in       │ Cherry-pick  │ Note if seen │ Skip           │
│ Complexity  │ "Big enough?"│ "Right + ?"  │ "Too complex?"│ "Bare minimum?"│
│ Error map   │ Full + chaos │ Full + chaos │ Full         │ Critical only  │
│ Observ.     │ "Joy to      │ "Joy to      │ "Can we      │ "Can we see if │
│             │  operate"    │  operate"    │  debug it?"  │  it's broken?" │
└─────────────┴──────────────┴──────────────┴──────────────┴────────────────┘
```
