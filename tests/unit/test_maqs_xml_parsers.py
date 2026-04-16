"""Unit tests for MAQS XML parser helpers and Hardgate parse_affected_files."""


# ── parse_affected_files (hardgate) ────────────────────────────────────────

def test_parse_affected_files_new_tag():
    """New <affected_files> (underscore) tag from diagnosis_meta."""
    from agent.jobs.hardgate import parse_affected_files

    diagnosis = """\
### 诊断报告内容...

<diagnosis_meta>
<affected_files>
- agent/jobs/maqs.py
- agent/jobs/hardgate.py
</affected_files>
<experience_summary>Hardgate scope check now works correctly.</experience_summary>
<atomic_split>none</atomic_split>
<recommended_approach>A</recommended_approach>
</diagnosis_meta>"""
    result = parse_affected_files(diagnosis)
    assert result == ["agent/jobs/maqs.py", "agent/jobs/hardgate.py"]


def test_parse_affected_files_legacy_tag_fallback():
    """Legacy <affected-files> (dash) tag still works as fallback."""
    from agent.jobs.hardgate import parse_affected_files

    diagnosis = """\
<affected-files>
- agent/jobs/maqs.py
- agent/jobs/mads/helpers.py
</affected-files>"""
    result = parse_affected_files(diagnosis)
    assert result == ["agent/jobs/maqs.py", "agent/jobs/mads/helpers.py"]


def test_parse_affected_files_new_tag_takes_priority_over_legacy():
    """When both tags present, new tag wins."""
    from agent.jobs.hardgate import parse_affected_files

    diagnosis = """\
<affected-files>
- old/file.py
</affected-files>
<affected_files>
- new/file.py
</affected_files>"""
    result = parse_affected_files(diagnosis)
    assert result == ["new/file.py"]


def test_parse_affected_files_with_line_numbers():
    """Line number suffixes are stripped."""
    from agent.jobs.hardgate import parse_affected_files

    diagnosis = """\
<affected_files>
- agent/jobs/maqs.py:122
- agent/jobs/hardgate.py:25
</affected_files>"""
    result = parse_affected_files(diagnosis)
    assert result == ["agent/jobs/maqs.py", "agent/jobs/hardgate.py"]


def test_parse_affected_files_empty():
    """No XML block returns empty list."""
    from agent.jobs.hardgate import parse_affected_files

    assert parse_affected_files("pure markdown, no xml") == []


# ── parse_modified_files (hardgate) ──────────────────────────────────────

class TestParseModifiedFiles:
    def test_normal(self):
        from agent.jobs.hardgate import parse_modified_files
        text = """<fix_meta>
<modified_files>
- agent/jobs/maqs.py
- agent/jobs/hardgate.py
</modified_files>
<commit_message>fix: something</commit_message>
</fix_meta>"""
        assert parse_modified_files(text) == ["agent/jobs/maqs.py", "agent/jobs/hardgate.py"]

    def test_empty(self):
        from agent.jobs.hardgate import parse_modified_files
        assert parse_modified_files("no xml here") == []

    def test_single_file(self):
        from agent.jobs.hardgate import parse_modified_files
        text = "<modified_files>\n- foo.py\n</modified_files>"
        assert parse_modified_files(text) == ["foo.py"]


# ── _parse_commit_message ─────────────────────────────────────────────────

def test_parse_commit_message_extracts_from_fix_meta():
    from agent.jobs.maqs import _parse_commit_message

    fix_report = """\
### 执行结果
修改了 2 个文件。

<fix_meta>
<modified_files>
- agent/jobs/maqs.py
</modified_files>
<commit_message>fix(MAQS-abc12345): align diagnosis prompt with hardgate XML schema</commit_message>
<tests_passed>true</tests_passed>
</fix_meta>"""
    result = _parse_commit_message(fix_report, "fix: fallback")
    assert result == "fix(MAQS-abc12345): align diagnosis prompt with hardgate XML schema"


def test_parse_commit_message_fallback_when_no_tag():
    from agent.jobs.maqs import _parse_commit_message

    result = _parse_commit_message("no xml here", "fix(MAQS-xxx): default")
    assert result == "fix(MAQS-xxx): default"


def test_parse_commit_message_fallback_when_empty_tag():
    from agent.jobs.maqs import _parse_commit_message

    fix_report = "<fix_meta><commit_message>   </commit_message></fix_meta>"
    result = _parse_commit_message(fix_report, "fix: fallback")
    assert result == "fix: fallback"


# ── _parse_atomic_split ────────────────────────────────────────────────────

def test_parse_atomic_split_none():
    from agent.jobs.maqs import _parse_atomic_split

    diagnosis = "<diagnosis_meta><atomic_split>none</atomic_split></diagnosis_meta>"
    assert _parse_atomic_split(diagnosis) == []


def test_parse_atomic_split_multiple():
    from agent.jobs.maqs import _parse_atomic_split

    diagnosis = """\
<diagnosis_meta>
<atomic_split>
- Fix Hardgate XML schema alignment
- Implement atomic split parser
</atomic_split>
</diagnosis_meta>"""
    result = _parse_atomic_split(diagnosis)
    assert result == ["Fix Hardgate XML schema alignment", "Implement atomic split parser"]


def test_parse_atomic_split_no_tag():
    from agent.jobs.maqs import _parse_atomic_split

    assert _parse_atomic_split("no xml here") == []


# ── _parse_experience_summary ─────────────────────────────────────────────

def test_parse_experience_summary_extracts_from_diagnosis_meta():
    from agent.jobs.maqs import _parse_experience_summary

    diagnosis = """\
<diagnosis_meta>
<affected_files>
- agent/jobs/maqs.py
</affected_files>
<experience_summary>Hardgate 能精确拦截越界修改，体验归因摘要在通知中稳定出现。</experience_summary>
<atomic_split>none</atomic_split>
<recommended_approach>A</recommended_approach>
</diagnosis_meta>"""
    result = _parse_experience_summary(diagnosis)
    assert result == "Hardgate 能精确拦截越界修改，体验归因摘要在通知中稳定出现。"


def test_parse_experience_summary_empty_when_no_tag():
    from agent.jobs.maqs import _parse_experience_summary

    assert _parse_experience_summary("no xml here") == ""


def test_parse_experience_summary_truncates_at_200():
    from agent.jobs.maqs import _parse_experience_summary

    long_summary = "x" * 300
    diagnosis = f"<diagnosis_meta><experience_summary>{long_summary}</experience_summary></diagnosis_meta>"
    result = _parse_experience_summary(diagnosis)
    assert len(result) == 200


# ── _parse_control_signal ─────────────────────────────────────────────────

from agent.jobs.maqs import _parse_control_signal


class TestParseControlSignal:
    def test_stop_signal(self):
        text = 'Some analysis...\n<control signal="STOP">问题超出范围</control>'
        result = _parse_control_signal(text)
        assert result == ("STOP", "问题超出范围")

    def test_escalate_signal(self):
        text = '<control signal="ESCALATE">置信度不足</control>'
        result = _parse_control_signal(text)
        assert result == ("ESCALATE", "置信度不足")

    def test_no_signal(self):
        assert _parse_control_signal("normal output") is None


# ── parse_qa_verdict ───────────────────────────────────────────────────────

def test_parse_qa_verdict_pass_new_schema():
    """New schema with signals_verified/signals_failed returns PASS."""
    from agent.jobs.mads.helpers import parse_qa_verdict

    report = """\
### 综合判定
所有命题通过。

```xml
<qa_verdict>
<result>PASS</result>
<signals_verified>3</signals_verified>
<signals_failed>0</signals_failed>
<reason>全部信号命题代码证据确认</reason>
</qa_verdict>
```"""
    assert parse_qa_verdict(report) == "PASS"


def test_parse_qa_verdict_reject_new_schema():
    """REJECT result is correctly parsed."""
    from agent.jobs.mads.helpers import parse_qa_verdict

    report = """\
```xml
<qa_verdict>
<result>REJECT</result>
<signals_verified>1</signals_verified>
<signals_failed>2</signals_failed>
<reason>核心变更未实现</reason>
</qa_verdict>
```"""
    assert parse_qa_verdict(report) == "REJECT"


def test_parse_qa_verdict_old_schema_backward_compat():
    """Old schema with prompt_risk field still extracts result correctly."""
    from agent.jobs.mads.helpers import parse_qa_verdict

    report = """\
<qa_verdict>
<result>PASS</result>
<prompt_risk>false</prompt_risk>
<reason>looks good</reason>
</qa_verdict>"""
    assert parse_qa_verdict(report) == "PASS"


def test_parse_qa_verdict_missing_block_defaults_to_reject():
    """Missing <qa_verdict> block defaults to REJECT."""
    from agent.jobs.mads.helpers import parse_qa_verdict

    assert parse_qa_verdict("no control block here") == "REJECT"


def test_parse_qa_verdict_returns_str_not_tuple():
    """Return type is str, not tuple — callers must not unpack."""
    from agent.jobs.mads.helpers import parse_qa_verdict

    result = parse_qa_verdict("<qa_verdict><result>PASS</result></qa_verdict>")
    assert isinstance(result, str)
    assert result == "PASS"
