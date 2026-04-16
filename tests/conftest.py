# -*- coding: utf-8 -*-
"""Core pytest fixtures for nas-claude-code-feishu tests.

Markers (register in pytest.ini or pyproject.toml):
    unit        - fast, no I/O, no external services
    integration - may touch filesystem, no network
    e2e         - requires live Feishu credentials
    llm         - calls a real LLM (slow, billed)
    golden      - compares output against golden files (use --update-golden to refresh)
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Path bootstrap ────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

FIXTURES_DIR = Path(__file__).parent / "fixtures"
REAL_MSG_DIR = FIXTURES_DIR / "real_messages"
GOLDEN_DIR = Path(__file__).parent / "golden"


# ── CLI options ───────────────────────────────────────────────────────────────

def pytest_addoption(parser):
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Update golden files with current output instead of asserting equality",
    )
    parser.addoption(
        "--llm-runs",
        default=1,
        type=int,
        help="Number of times to repeat each LLM test (for variance detection)",
    )


# ── Option fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def update_golden(request):
    """True when --update-golden is passed; golden test should write instead of compare."""
    return request.config.getoption("--update-golden")


@pytest.fixture
def llm_runs(request):
    """Repetition count for LLM variance tests."""
    return request.config.getoption("--llm-runs")


# ── Config ────────────────────────────────────────────────────────────────────

@pytest.fixture
def config():
    """Minimal hub config dict sufficient for unit tests (no real credentials)."""
    return {
        "llm": {
            "default_model": "opus",
            "fallback_model": "sonnet",
        },
        "notify": {
            "dispatcher_id": "test_dispatcher",
            "notifier_id": "test_notifier",
        },
        "heartbeat": {
            "enabled": False,
        },
    }


# ── Fixture data ──────────────────────────────────────────────────────────────

@pytest.fixture
def real_messages():
    """Load all test data from tests/fixtures/real_messages/.

    Returns a dict keyed by filename stem. JSON files are parsed; text files
    are returned as raw strings.
    """
    messages: dict = {}
    if not REAL_MSG_DIR.exists():
        return messages
    for f in sorted(REAL_MSG_DIR.glob("*.json")):
        try:
            messages[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            pytest.fail(f"Invalid JSON in fixture {f}: {exc}")
    for f in sorted(REAL_MSG_DIR.glob("*.txt")):
        messages[f.stem] = f.read_text(encoding="utf-8")
    return messages


# ── Dispatcher ────────────────────────────────────────────────────────────────

@pytest.fixture
def dispatcher_config():
    """Minimal Dispatcher config (no real Feishu credentials)."""
    return {
        "app_id": "cli_test00000000",
        "app_secret": "test_secret_placeholder",
        "domain": "https://open.feishu.cn",
        "delivery_chat_id": "oc_test_chat_id",
    }


@pytest.fixture
def dispatcher(dispatcher_config):
    """Dispatcher instance with mocked Feishu client (no network calls)."""
    from agent.platforms.feishu.dispatcher import Dispatcher

    d = Dispatcher(dispatcher_config)
    # Inject a mock client so _ensure_client() passes without start()
    d._client = MagicMock()
    return d


# ── LLMRouter ─────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_claude_cli():
    """Mocked ClaudeCli that returns a successful LLMResult by default."""
    from agent.infra.models import LLMResult

    cli = MagicMock()
    cli.run = AsyncMock(return_value=LLMResult(
        text="mock response",
        session_id="mock-session-id",
        duration_ms=100,
    ))
    return cli


@pytest.fixture
def mock_gemini_cli():
    """Mocked GeminiCli."""
    from agent.infra.models import LLMResult

    cli = MagicMock()
    cli.run = AsyncMock(return_value=LLMResult(
        text="mock gemini response",
        duration_ms=80,
    ))
    return cli


@pytest.fixture
def mock_gemini_api():
    """Mocked GeminiAPI."""
    from agent.infra.models import LLMResult

    api = MagicMock()
    api.run = AsyncMock(return_value=LLMResult(
        text="mock gemini api response",
        duration_ms=120,
    ))
    return api


@pytest.fixture
def router(mock_claude_cli, mock_gemini_cli, mock_gemini_api, tmp_path):
    """LLMRouter wired with mock LLM clients and isolated session storage."""
    from agent.llm.router import LLMRouter

    sessions_db = str(tmp_path / "sessions.db")
    r = LLMRouter(
        claude=mock_claude_cli,
        gemini_cli=mock_gemini_cli,
        gemini_api=mock_gemini_api,
        sessions_path=str(tmp_path / "sessions.json"),
    )
    # Replace the SQLite store with an in-memory mock so tests are isolated
    r._store = MagicMock()
    r._store.load_all.return_value = {}
    r._store.save = MagicMock()
    r._store.delete = MagicMock()
    r._store.save_all = MagicMock()
    return r


# ── Model dataclasses ─────────────────────────────────────────────────────────

@pytest.fixture
def llm_result_ok():
    """A successful LLMResult fixture."""
    from agent.infra.models import LLMResult
    return LLMResult(
        text="Hello, world!",
        session_id="sess-abc123",
        duration_ms=250,
        is_error=False,
    )


@pytest.fixture
def llm_result_error():
    """An error LLMResult fixture."""
    from agent.infra.models import LLMResult
    return LLMResult(
        text="Error: something went wrong",
        session_id=None,
        duration_ms=50,
        is_error=True,
    )


@pytest.fixture
def llm_config_default():
    """Default LLMConfig fixture."""
    from agent.infra.models import LLMConfig
    return LLMConfig(provider="claude-cli", model="opus")


# ── Autonomy log isolation ────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_autonomy_log(tmp_path, monkeypatch):
    """Redirect autonomy log writes to a temp dir so tests never touch the
    production data/autonomy_log.jsonl."""
    import agent.infra.autonomy as autonomy_mod
    monkeypatch.setattr(autonomy_mod, "ACTION_LOG_PATH", str(tmp_path / "autonomy_log.jsonl"))
