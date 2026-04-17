"""Unit tests for the pre-fix gate logic in MAQS process_ticket.

The gate fires when diagnosis has BOTH:
  - no <affected_files> / <affected-files> block   → parse_affected_files returns []
  - no <workflow_steps> block                       → _parse_workflow_steps returns []

This combination means the Fixer would receive a "free execute" message while
Hardgate rejects any non-tests/ change, causing a retry death loop.
"""


DIAGNOSIS_NO_AF_NO_WF = """\
### 诊断报告

<diagnosis_meta>
<complexity>L2</complexity>
<user_impact>high</user_impact>
<atomic_split>none</atomic_split>
<recommended_approach>A</recommended_approach>
<experience_summary>Some summary.</experience_summary>
</diagnosis_meta>

调查结论：发现某函数缺少空值检查。
"""

DIAGNOSIS_WITH_AF_NO_WF = """\
### 诊断报告

<diagnosis_meta>
<affected_files>
- agent/jobs/maqs.py
</affected_files>
<complexity>L2</complexity>
<user_impact>high</user_impact>
<atomic_split>none</atomic_split>
<recommended_approach>A</recommended_approach>
</diagnosis_meta>
"""

DIAGNOSIS_NO_AF_WITH_WF = """\
### 诊断报告

<diagnosis_meta>
<complexity>L2</complexity>
<user_impact>high</user_impact>
<workflow_steps>
[
  {"content": "step 1", "affected_files": ["agent/jobs/maqs.py"], "verification": ""},
  {"content": "step 2", "affected_files": ["agent/jobs/maqs.py"], "verification": ""}
]
</workflow_steps>
<atomic_split>none</atomic_split>
</diagnosis_meta>
"""

DIAGNOSIS_WITH_AF_WITH_WF = """\
### 诊断报告

<diagnosis_meta>
<affected_files>
- agent/jobs/maqs.py
</affected_files>
<workflow_steps>
[
  {"content": "step 1", "affected_files": ["agent/jobs/maqs.py"], "verification": ""},
  {"content": "step 2", "affected_files": ["agent/jobs/maqs.py"], "verification": ""}
]
</workflow_steps>
<complexity>L2</complexity>
</diagnosis_meta>
"""


# ── parse_affected_files ─────────────────────────────────────────────────────

def test_parse_affected_files_empty_when_no_block():
    from agent.jobs.hardgate import parse_affected_files
    result = parse_affected_files(DIAGNOSIS_NO_AF_NO_WF)
    assert result == [], f"Expected [], got {result!r}"


def test_parse_affected_files_returns_files_when_block_present():
    from agent.jobs.hardgate import parse_affected_files
    result = parse_affected_files(DIAGNOSIS_WITH_AF_NO_WF)
    assert result == ["agent/jobs/maqs.py"]


# ── _parse_workflow_steps ────────────────────────────────────────────────────

def test_parse_workflow_steps_empty_when_no_block():
    from agent.jobs.maqs import _parse_workflow_steps
    result = _parse_workflow_steps(DIAGNOSIS_NO_AF_NO_WF)
    assert result == [], f"Expected [], got {result!r}"


def test_parse_workflow_steps_returns_steps_when_block_present():
    from agent.jobs.maqs import _parse_workflow_steps
    result = _parse_workflow_steps(DIAGNOSIS_NO_AF_WITH_WF)
    assert len(result) == 2
    assert result[0]["content"] == "step 1"


# ── gate condition ───────────────────────────────────────────────────────────

def test_gate_condition_fires_when_both_empty():
    """Both parsers return empty → `not _af and not workflow` is True."""
    from agent.jobs.hardgate import parse_affected_files
    from agent.jobs.maqs import _parse_workflow_steps

    _af = parse_affected_files(DIAGNOSIS_NO_AF_NO_WF)
    raw_steps = _parse_workflow_steps(DIAGNOSIS_NO_AF_NO_WF)
    # workflow would be None since raw_steps is empty (see process_ticket logic)
    workflow = None if not raw_steps else object()

    assert not _af, "affected_files should be empty"
    assert not workflow, "workflow should be None/falsy"
    # This is the exact gate condition in process_ticket
    assert not _af and not workflow, "Gate should fire for incomplete diagnosis"


def test_gate_condition_does_not_fire_when_af_present():
    """affected_files present → gate must NOT fire."""
    from agent.jobs.hardgate import parse_affected_files
    from agent.jobs.maqs import _parse_workflow_steps

    _af = parse_affected_files(DIAGNOSIS_WITH_AF_NO_WF)
    raw_steps = _parse_workflow_steps(DIAGNOSIS_WITH_AF_NO_WF)
    workflow = None if not raw_steps else object()

    assert _af, "affected_files should be non-empty"
    assert not (not _af and not workflow), "Gate must NOT fire when af is present"


def test_gate_condition_does_not_fire_when_workflow_present():
    """workflow_steps present → gate must NOT fire."""
    from agent.jobs.hardgate import parse_affected_files
    from agent.jobs.maqs import _parse_workflow_steps

    _af = parse_affected_files(DIAGNOSIS_NO_AF_WITH_WF)
    raw_steps = _parse_workflow_steps(DIAGNOSIS_NO_AF_WITH_WF)
    workflow = None if not raw_steps else object()  # non-None when steps present

    assert not _af, "affected_files should be empty"
    assert workflow, "workflow should be truthy (built from steps)"
    assert not (not _af and not workflow), "Gate must NOT fire when workflow is present"


def test_gate_condition_does_not_fire_when_both_present():
    """Both af and workflow present → gate must NOT fire."""
    from agent.jobs.hardgate import parse_affected_files
    from agent.jobs.maqs import _parse_workflow_steps

    _af = parse_affected_files(DIAGNOSIS_WITH_AF_WITH_WF)
    raw_steps = _parse_workflow_steps(DIAGNOSIS_WITH_AF_WITH_WF)
    workflow = None if not raw_steps else object()

    assert _af
    assert workflow
    assert not (not _af and not workflow), "Gate must NOT fire when both are present"
