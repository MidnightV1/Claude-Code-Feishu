# Test Specifications

Auditable test documentation for nas-claude-code-feishu. Each spec defines:
- **What** is being tested and **why**
- **Acceptance criteria** (pass/fail conditions)
- **Test data sources** (fixture files, golden files)
- **Known risks** (what bugs this catches, what the tweet about Opus faking compiler output teaches us)

## Anti-Hallucination Protocol

When Sonnet executes tests, it MUST follow the XML state protocol (see test_runner.py):
1. Process monitor verifies pytest actually ran (PID tracking + exit code)
2. XML state output is MANDATORY before any result reporting
3. Golden file diffs are computed by the runner (not by Sonnet) — Sonnet cannot fabricate pass/fail
4. pytest-json-report generates machine-readable results — no LLM interpretation of text output

## Spec Index

| Module | Spec | Test File | Golden Files | Fixtures |
|--------|------|-----------|-------------|----------|
| utils.py | [utils_spec.md](utils_spec.md) | unit/test_utils.py | golden/rendering/blocks_*.json | fixtures/real_messages/mixed_content.txt, complex_table.txt |
| dispatcher.py | [dispatcher_spec.md](dispatcher_spec.md) | unit/test_dispatcher.py | golden/rendering/card_*.json | — |
| router.py | [router_spec.md](router_spec.md) | unit/test_router.py | golden/context/*.json | fixtures/real_messages/long_conversation.json |

## Coverage Target

Phase 1: utils.py + dispatcher.py + router.py pure functions → 70%+ line coverage
