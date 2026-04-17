"""Unit tests for LLMRouter.clear_session() — R05/R06."""
import pytest
from agent.llm.router import LLMRouter


def make_router():
    return LLMRouter.__new__(LLMRouter)


def _inject_sessions(router, sessions):
    router._sessions = sessions
    router._archived_history = {}


# ── R05 ──────────────────────────────────────────────────────────────────────

def test_clear_session_preserves_all_four_fields():
    """R05: clear_session keeps history, llm_config, last_summary, last_summarized_ts."""
    router = make_router()
    _inject_sessions(router, {
        "k": {
            "session_id": "sid-123",
            "history": [{"role": "user", "content": "hi"}],
            "llm_config": {"model": "sonnet"},
            "last_summary": "prior summary text",
            "last_summarized_ts": "2026-01-01T00:00:00",
            "updated_at": "2026-01-02T00:00:00",
        }
    })
    router.clear_session("k")
    entry = router._sessions["k"]
    assert "history" in entry
    assert "llm_config" in entry
    assert "last_summary" in entry
    assert "last_summarized_ts" in entry


def test_clear_session_removes_session_id_and_updated_at():
    """R05: clear_session strips session_id and updated_at."""
    router = make_router()
    _inject_sessions(router, {
        "k": {
            "session_id": "sid-123",
            "history": [{"role": "user", "content": "hi"}],
            "llm_config": {"model": "sonnet"},
            "last_summary": "summary",
            "last_summarized_ts": "2026-01-01T00:00:00",
            "updated_at": "2026-01-02T00:00:00",
        }
    })
    router.clear_session("k")
    entry = router._sessions["k"]
    assert "session_id" not in entry
    assert "updated_at" not in entry


def test_clear_session_preserves_correct_values():
    """R05: field values are unchanged after clear."""
    router = make_router()
    history = [{"role": "user", "content": "hello"}]
    llm_cfg = {"model": "opus"}
    summary = "compressed history"
    ts = "2026-04-17T10:00:00"
    _inject_sessions(router, {
        "k": {
            "session_id": "sid-abc",
            "history": history,
            "llm_config": llm_cfg,
            "last_summary": summary,
            "last_summarized_ts": ts,
        }
    })
    router.clear_session("k")
    entry = router._sessions["k"]
    assert entry["history"] is history
    assert entry["llm_config"] is llm_cfg
    assert entry["last_summary"] == summary
    assert entry["last_summarized_ts"] == ts


# ── R06 ──────────────────────────────────────────────────────────────────────

def test_clear_session_missing_key_no_crash():
    """R06: clear_session on unknown key does not raise."""
    router = make_router()
    router._sessions = {}
    router._archived_history = {}
    router.clear_session("nonexistent")  # must not raise


def test_clear_session_missing_optional_fields_no_crash():
    """R06: entry without last_summary/last_summarized_ts does not crash."""
    router = make_router()
    _inject_sessions(router, {
        "k": {
            "session_id": "sid-xyz",
            "history": [{"role": "user", "content": "hey"}],
        }
    })
    router.clear_session("k")
    entry = router._sessions["k"]
    assert "last_summary" not in entry
    assert "last_summarized_ts" not in entry
    assert "history" in entry
