# -*- coding: utf-8 -*-
"""Tests for WorkerManager."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.infra.models import LLMResult, WorkerResult
from agent.jobs.worker import WorkerManager


class TestWorkerResult:
    def test_defaults(self):
        wr = WorkerResult()
        assert wr.text == ""
        assert wr.is_error is False
        assert wr.duration_s == 0.0
        assert wr.input_tokens == 0

    def test_fields(self):
        wr = WorkerResult(text="ok", duration_s=1.5, cost_usd=0.01)
        assert wr.text == "ok"
        assert wr.duration_s == 1.5


class TestWorkerManager:
    @pytest.fixture
    def mock_router(self):
        router = MagicMock()
        router.run = AsyncMock(return_value=LLMResult(
            text="diagnosis complete",
            input_tokens=100,
            output_tokens=200,
            cost_usd=0.05,
        ))
        return router

    @pytest.mark.asyncio
    async def test_run_phase_success(self, mock_router):
        wm = WorkerManager(mock_router)
        result = await wm.run_phase(
            role="diagnose", model="opus",
            prompt="Analyze this", system_prompt="You are...",
        )
        assert result.text == "diagnosis complete"
        assert result.is_error is False
        assert result.input_tokens == 100
        assert result.output_tokens == 200
        assert result.cost_usd == 0.05
        assert result.duration_s > 0

    @pytest.mark.asyncio
    async def test_run_phase_error(self, mock_router):
        mock_router.run = AsyncMock(return_value=LLMResult(
            text="something went wrong", is_error=True,
        ))
        wm = WorkerManager(mock_router)
        result = await wm.run_phase(
            role="fix", model="sonnet",
            prompt="Fix this", system_prompt="You are...",
        )
        assert result.is_error is True
        assert "something went wrong" in result.text

    @pytest.mark.asyncio
    async def test_run_phase_timeout(self, mock_router):
        async def slow(*a, **kw):
            await asyncio.sleep(10)
            return LLMResult(text="too late")
        mock_router.run = slow
        wm = WorkerManager(mock_router)
        result = await wm.run_phase(
            role="diagnose", model="opus",
            prompt="Analyze", system_prompt="You are...",
            timeout=1,
        )
        assert result.is_error is True
        assert "TIMEOUT" in result.text

    @pytest.mark.asyncio
    async def test_run_phase_exception(self, mock_router):
        mock_router.run = AsyncMock(side_effect=RuntimeError("connection lost"))
        wm = WorkerManager(mock_router)
        result = await wm.run_phase(
            role="qa", model="opus",
            prompt="Review", system_prompt="You are...",
        )
        assert result.is_error is True
        assert "connection lost" in result.text

    @pytest.mark.asyncio
    async def test_run_phase_with_budget_within_limit(self, mock_router):
        mock_router.run = AsyncMock(return_value=LLMResult(
            text="ok", input_tokens=1000, output_tokens=500,
        ))
        wm = WorkerManager(mock_router)
        result = await wm.run_phase_with_budget(
            role="diagnose", model="opus",
            prompt="test", system_prompt="sys",
        )
        assert result.budget_exceeded is False
        assert result.handoff_doc == ""

    @pytest.mark.asyncio
    async def test_run_phase_with_budget_exceeded(self, mock_router):
        mock_router.run = AsyncMock(return_value=LLMResult(
            text="very long output",
            input_tokens=100_000, output_tokens=80_000,
        ))
        wm = WorkerManager(mock_router)
        result = await wm.run_phase_with_budget(
            role="diagnose", model="opus",
            prompt="test", system_prompt="sys",
            budget_pct=0.8,
        )
        assert result.budget_exceeded is True
        assert "BUDGET EXCEEDED" in result.handoff_doc
        assert result.text == "very long output"  # original text preserved

    @pytest.mark.asyncio
    async def test_run_phase_with_budget_error_passthrough(self, mock_router):
        mock_router.run = AsyncMock(return_value=LLMResult(
            text="error", is_error=True,
        ))
        wm = WorkerManager(mock_router)
        result = await wm.run_phase_with_budget(
            role="fix", model="sonnet",
            prompt="test", system_prompt="sys",
        )
        assert result.is_error is True
        assert result.budget_exceeded is False  # no budget check on errors
