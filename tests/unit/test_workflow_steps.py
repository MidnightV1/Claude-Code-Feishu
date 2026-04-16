"""Unit tests for TodoWrite-driven workflow step integration."""

import pytest


# ── Data model tests ──────────────────────────────────────────────────

def test_step_status_enum():
    from agent.infra.models import StepStatus
    assert StepStatus.PENDING == 0
    assert StepStatus.LOCKED == 4


def test_ticket_step_from_dict():
    from agent.infra.models import ticket_step_from_dict, StepStatus
    step = ticket_step_from_dict({
        "id": "step_001",
        "content": "Fix router.py",
        "status": 2,
        "affected_files": ["agent/router.py"],
        "verification": "pytest passes",
    })
    assert step.id == "step_001"
    assert step.status == StepStatus.COMPLETED
    assert step.affected_files == ["agent/router.py"]


def test_ticket_workflow_progress():
    from agent.infra.models import TicketWorkflow, TicketStep, StepStatus
    wf = TicketWorkflow(steps=[
        TicketStep(id="step_001", content="A", status=StepStatus.COMPLETED),
        TicketStep(id="step_002", content="B", status=StepStatus.FAILED),
        TicketStep(id="step_003", content="C", status=StepStatus.PENDING),
    ])
    assert wf.progress == "1/3"
    assert len(wf.failed_steps) == 1


def test_ticket_workflow_lock_passed():
    from agent.infra.models import TicketWorkflow, TicketStep, StepStatus
    wf = TicketWorkflow(steps=[
        TicketStep(id="step_001", content="A", status=StepStatus.COMPLETED,
                   qa_verdict="pass", affected_files=["a.py"]),
        TicketStep(id="step_002", content="B", status=StepStatus.COMPLETED,
                   qa_verdict="fail", affected_files=["b.py"]),
        TicketStep(id="step_003", content="C", status=StepStatus.FAILED,
                   qa_verdict="fail", affected_files=["c.py"]),
    ])
    wf.lock_passed()
    assert wf.steps[0].status == StepStatus.LOCKED
    assert wf.steps[1].status == StepStatus.COMPLETED  # fail verdict → not locked
    assert wf.steps[2].status == StepStatus.FAILED
    assert wf.locked_files == {"a.py"}
    assert wf.retry_files == {"c.py"}


def test_ticket_workflow_render_progress():
    from agent.infra.models import TicketWorkflow, TicketStep, StepStatus
    wf = TicketWorkflow(steps=[
        TicketStep(id="s1", content="Fix bug", status=StepStatus.LOCKED),
        TicketStep(id="s2", content="Add test", status=StepStatus.FAILED, qa_reason="Missing edge case"),
        TicketStep(id="s3", content="Check scope", status=StepStatus.PENDING),
    ])
    rendered = wf.render_progress()
    assert "🔒" in rendered
    assert "❌" in rendered
    assert "⬜" in rendered
    assert "已锁定" in rendered
    assert "Missing edge case" in rendered


def test_ticket_workflow_serialization_roundtrip():
    from agent.infra.models import (
        TicketWorkflow, TicketStep, StepStatus,
        ticket_workflow_from_dict,
    )
    from dataclasses import asdict
    wf = TicketWorkflow(steps=[
        TicketStep(id="step_001", content="Fix", status=StepStatus.LOCKED,
                   affected_files=["a.py"], qa_verdict="pass"),
        TicketStep(id="step_002", content="Test", status=StepStatus.FAILED,
                   affected_files=["b.py"], qa_verdict="fail", qa_reason="oops"),
    ])
    d = asdict(wf)
    restored = ticket_workflow_from_dict(d)
    assert len(restored.steps) == 2
    assert restored.steps[0].status == StepStatus.LOCKED
    assert restored.steps[1].qa_reason == "oops"


# ── Parser tests ──────────────────────────────────────────────────────

def test_parse_workflow_steps_valid():
    from agent.jobs.maqs import _parse_workflow_steps
    diagnosis = '''\
<diagnosis_meta>
<affected_files>
- agent/router.py
- tests/test_router.py
</affected_files>
<complexity>L2</complexity>
<workflow_steps>
[
  {"content": "Fix null check", "affected_files": ["agent/router.py"], "verification": "grep confirms"},
  {"content": "Add test", "affected_files": ["tests/test_router.py"], "verification": "pytest passes"}
]
</workflow_steps>
</diagnosis_meta>'''
    steps = _parse_workflow_steps(diagnosis)
    assert len(steps) == 2
    assert steps[0]["content"] == "Fix null check"
    assert steps[1]["affected_files"] == ["tests/test_router.py"]


def test_parse_workflow_steps_missing():
    from agent.jobs.maqs import _parse_workflow_steps
    diagnosis = "<diagnosis_meta><complexity>L1</complexity></diagnosis_meta>"
    assert _parse_workflow_steps(diagnosis) == []


def test_parse_workflow_steps_invalid_json():
    from agent.jobs.maqs import _parse_workflow_steps
    diagnosis = "<workflow_steps>not valid json</workflow_steps>"
    assert _parse_workflow_steps(diagnosis) == []


def test_parse_workflow_steps_too_few():
    from agent.jobs.maqs import _parse_workflow_steps
    diagnosis = '<workflow_steps>[{"content": "only one"}]</workflow_steps>'
    assert _parse_workflow_steps(diagnosis) == []


def test_parse_workflow_steps_truncates_over_7():
    from agent.jobs.maqs import _parse_workflow_steps
    steps = [{"content": f"step {i}", "affected_files": []} for i in range(10)]
    import json
    diagnosis = f"<workflow_steps>{json.dumps(steps)}</workflow_steps>"
    result = _parse_workflow_steps(diagnosis)
    assert len(result) == 7


def test_build_workflow():
    from agent.jobs.maqs import _build_workflow
    from agent.infra.models import StepStatus
    raw = [
        {"content": "Fix A", "affected_files": ["a.py"], "verification": "test"},
        {"content": "Fix B", "affected_files": ["b.py"], "verification": "grep"},
    ]
    wf = _build_workflow(raw)
    assert len(wf.steps) == 2
    assert wf.steps[0].id == "step_001"
    assert wf.steps[1].id == "step_002"
    assert all(s.status == StepStatus.PENDING for s in wf.steps)


def test_parse_step_results():
    from agent.jobs.maqs import _parse_step_results
    report = '''\
Fix done.

<step_results>
[
  {"step_id": "step_001", "status": "completed", "result": "Added null check"},
  {"step_id": "step_002", "status": "failed", "result": "Test file not found"}
]
</step_results>
'''
    results = _parse_step_results(report)
    assert len(results) == 2
    assert results[0]["status"] == "completed"
    assert results[1]["status"] == "failed"


def test_parse_step_verdicts():
    from agent.jobs.maqs import _parse_step_verdicts
    qa_report = '''\
Review complete.

<step_verdicts>
[
  {"step_id": "step_001", "verdict": "pass"},
  {"step_id": "step_002", "verdict": "fail", "reason": "Missing edge case"}
]
</step_verdicts>
'''
    verdicts = _parse_step_verdicts(qa_report)
    assert len(verdicts) == 2
    assert verdicts[0]["verdict"] == "pass"
    assert verdicts[1]["reason"] == "Missing edge case"


def test_apply_step_results():
    from agent.jobs.maqs import _build_workflow, _apply_step_results
    from agent.infra.models import StepStatus
    wf = _build_workflow([
        {"content": "A", "affected_files": [], "verification": ""},
        {"content": "B", "affected_files": [], "verification": ""},
    ])
    _apply_step_results(wf, [
        {"step_id": "step_001", "status": "completed", "result": "Done A"},
        {"step_id": "step_002", "status": "failed", "result": "Failed B"},
    ])
    assert wf.steps[0].status == StepStatus.COMPLETED
    assert wf.steps[0].result == "Done A"
    assert wf.steps[1].status == StepStatus.FAILED


def test_apply_step_verdicts():
    from agent.jobs.maqs import _build_workflow, _apply_step_results, _apply_step_verdicts
    from agent.infra.models import StepStatus
    wf = _build_workflow([
        {"content": "A", "affected_files": ["a.py"], "verification": ""},
        {"content": "B", "affected_files": ["b.py"], "verification": ""},
    ])
    _apply_step_results(wf, [
        {"step_id": "step_001", "status": "completed"},
        {"step_id": "step_002", "status": "completed"},
    ])
    _apply_step_verdicts(wf, [
        {"step_id": "step_001", "verdict": "pass"},
        {"step_id": "step_002", "verdict": "fail", "reason": "Wrong impl"},
    ])
    assert wf.steps[0].qa_verdict == "pass"
    assert wf.steps[1].qa_verdict == "fail"
    assert wf.steps[1].qa_reason == "Wrong impl"

    # Lock passed → verify
    wf.lock_passed()
    assert wf.steps[0].status == StepStatus.LOCKED
    assert wf.steps[1].status == StepStatus.COMPLETED  # not locked (fail verdict)
    assert wf.locked_files == {"a.py"}


def test_format_workflow_for_prompt():
    from agent.jobs.maqs import _build_workflow, _format_workflow_for_prompt
    from agent.infra.models import StepStatus
    wf = _build_workflow([
        {"content": "Fix A", "affected_files": ["a.py"], "verification": "test"},
        {"content": "Fix B", "affected_files": ["b.py"], "verification": "grep"},
    ])
    wf.steps[0].status = StepStatus.LOCKED
    wf.steps[1].status = StepStatus.FAILED
    text = _format_workflow_for_prompt(wf)
    assert "🔒 已锁定" in text
    assert "❌ 上轮失败" in text
    assert "step_001" in text
