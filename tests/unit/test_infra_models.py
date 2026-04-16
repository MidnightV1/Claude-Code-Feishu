# -*- coding: utf-8 -*-
"""Unit tests for agent/infra/models.py — dataclass serialization and auto-init."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent.infra.models import (
    AutonomyLevel,
    LLMConfig,
    LLMResult,
    CronSchedule,
    CronJobState,
    CronJob,
    LoopPhase,
    LoopState,
    to_dict,
    llm_config_from_dict,
    cron_schedule_from_dict,
    cron_job_state_from_dict,
    cron_job_from_dict,
    loop_state_from_dict,
)


# ── AutonomyLevel ─────────────────────────────────────────────────────────────

class TestAutonomyLevel:
    def test_values(self):
        assert AutonomyLevel.L0_SILENT == 0
        assert AutonomyLevel.L1_NOTIFY == 1
        assert AutonomyLevel.L2_APPROVE == 2
        assert AutonomyLevel.L3_DISCUSS == 3

    def test_comparison(self):
        assert AutonomyLevel.L0_SILENT < AutonomyLevel.L3_DISCUSS


# ── LLMConfig ─────────────────────────────────────────────────────────────────

class TestLLMConfig:
    def test_defaults(self):
        c = LLMConfig()
        assert c.provider == "claude-cli"
        assert c.model == "opus"
        assert c.timeout_seconds is None

    def test_roundtrip(self):
        c = LLMConfig(provider="gemini-cli", model="flash", timeout_seconds=30)
        d = to_dict(c)
        c2 = llm_config_from_dict(d)
        assert c2.provider == "gemini-cli"
        assert c2.model == "flash"
        assert c2.timeout_seconds == 30

    def test_extra_keys_ignored(self):
        d = {"provider": "gemini-api", "model": "pro", "unknown_key": "ignored"}
        c = llm_config_from_dict(d)
        assert c.provider == "gemini-api"
        assert not hasattr(c, "unknown_key")

    def test_none_returns_default(self):
        c = llm_config_from_dict(None)
        assert c.provider == "claude-cli"


# ── CronSchedule ──────────────────────────────────────────────────────────────

class TestCronSchedule:
    def test_defaults(self):
        s = CronSchedule()
        assert s.kind == "cron"
        assert s.expr is None
        assert s.tz == "Asia/Shanghai"

    def test_roundtrip(self):
        s = CronSchedule(kind="every", every_seconds=60, tz="UTC")
        d = to_dict(s)
        s2 = cron_schedule_from_dict(d)
        assert s2.kind == "every"
        assert s2.every_seconds == 60
        assert s2.tz == "UTC"

    def test_extra_keys_ignored(self):
        d = {"kind": "cron", "expr": "0 * * * *", "garbage": 123}
        s = cron_schedule_from_dict(d)
        assert s.expr == "0 * * * *"
        assert not hasattr(s, "garbage")

    def test_none_returns_default(self):
        s = cron_schedule_from_dict(None)
        assert s.kind == "cron"


# ── CronJobState ──────────────────────────────────────────────────────────────

class TestCronJobState:
    def test_defaults(self):
        st = CronJobState()
        assert st.next_run_at is None
        assert st.consecutive_errors == 0

    def test_roundtrip(self):
        st = CronJobState(last_status="ok", consecutive_errors=2)
        d = to_dict(st)
        st2 = cron_job_state_from_dict(d)
        assert st2.last_status == "ok"
        assert st2.consecutive_errors == 2

    def test_none_returns_default(self):
        st = cron_job_state_from_dict(None)
        assert st.consecutive_errors == 0


# ── CronJob ───────────────────────────────────────────────────────────────────

class TestCronJob:
    def test_auto_id(self):
        j = CronJob(name="test")
        assert len(j.id) == 12

    def test_auto_created_at(self):
        before = time.time()
        j = CronJob(name="test")
        after = time.time()
        assert before <= j.created_at <= after
        assert j.updated_at == j.created_at

    def test_explicit_id_not_overridden(self):
        j = CronJob(id="myid", name="test")
        assert j.id == "myid"

    def test_roundtrip(self):
        j = CronJob(name="daily", prompt="run daily", enabled=True)
        d = to_dict(j)
        j2 = cron_job_from_dict(d)
        assert j2.id == j.id
        assert j2.name == "daily"
        assert j2.prompt == "run daily"
        assert j2.enabled is True

    def test_nested_schedule_roundtrip(self):
        j = CronJob(name="x", schedule=CronSchedule(kind="every", every_seconds=3600))
        d = to_dict(j)
        j2 = cron_job_from_dict(d)
        assert j2.schedule.kind == "every"
        assert j2.schedule.every_seconds == 3600

    def test_nested_llm_roundtrip(self):
        j = CronJob(name="x", llm=LLMConfig(model="haiku", timeout_seconds=60))
        d = to_dict(j)
        j2 = cron_job_from_dict(d)
        assert j2.llm.model == "haiku"
        assert j2.llm.timeout_seconds == 60

    def test_extra_keys_ignored(self):
        j = CronJob(name="x")
        d = to_dict(j)
        d["future_field"] = "ignored"
        j2 = cron_job_from_dict(d)
        assert not hasattr(j2, "future_field")


# ── LoopPhase ─────────────────────────────────────────────────────────────────

class TestLoopPhase:
    def test_values(self):
        assert LoopPhase.DIAGNOSING == 0
        assert LoopPhase.DIAGNOSED  == 1
        assert LoopPhase.FIXING     == 2
        assert LoopPhase.REVIEWING  == 3
        assert LoopPhase.VISUAL_QA  == 4
        assert LoopPhase.CLOSED     == 5
        assert LoopPhase.STALLED    == 6

    def test_name(self):
        assert LoopPhase(2).name == "FIXING"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            LoopPhase(99)


# ── LoopState ─────────────────────────────────────────────────────────────────

class TestLoopState:
    def test_gold_standard(self):
        state = loop_state_from_dict({"ticket_id": "abc123", "phase": 2, "reject_count": 1})
        assert state.ticket_id == "abc123"
        assert state.phase == LoopPhase.FIXING
        assert state.phase.name == "FIXING"
        assert state.reject_count == 1

    def test_defaults(self):
        s = LoopState()
        assert s.phase == LoopPhase.DIAGNOSING
        assert s.reject_count == 0
        assert s.max_reject == 3
        assert s.updated_at == s.created_at

    def test_int_phase_coerced(self):
        s = LoopState(phase=3)
        assert s.phase == LoopPhase.REVIEWING

    def test_roundtrip(self):
        s = LoopState(ticket_id="t1", phase=LoopPhase.FIXING, reject_count=1)
        d = to_dict(s)
        s2 = loop_state_from_dict(d)
        assert s2.phase == LoopPhase.FIXING
        assert s2.ticket_id == "t1"
        assert s2.reject_count == 1

    def test_extra_keys_ignored(self):
        d = {"ticket_id": "x", "phase": 0, "unknown": "ignored"}
        s = loop_state_from_dict(d)
        assert s.ticket_id == "x"
        assert not hasattr(s, "unknown")

    def test_none_returns_default(self):
        s = loop_state_from_dict(None)
        assert s.phase == LoopPhase.DIAGNOSING
