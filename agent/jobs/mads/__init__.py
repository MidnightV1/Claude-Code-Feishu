# -*- coding: utf-8 -*-
"""MADS — Multi-Agent Development System.

Generalizes MAQS (bug-fixing) into a full development pipeline covering
bug, feature, refactor, skill, and config tasks.

Architecture:
    helpers.py   — Shared infrastructure (Bitable, Git, LLM, file I/O, notify)
    design.py    — Design stage (Opus → Feishu doc)
    review.py    — Design review flow (Feishu task + comment polling)
    decompose.py — Atomic decomposition (Design → N sub-tickets)
    contract.py  — Contract negotiation (Implementer ↔ QA pre-alignment)
    pipeline.py  — Orchestrator and state machine
"""
