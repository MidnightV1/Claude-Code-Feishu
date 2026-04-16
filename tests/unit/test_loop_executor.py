# -*- coding: utf-8 -*-
"""Tests for LoopExecutor."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent.infra.models import LoopPhase, LoopState, WorkerResult
from agent.jobs.loop_executor import LoopExecutor, PHASE_TIMEOUTS, PHASE_MODELS, _SEVERITY_PRIORITY
from agent.jobs.worker import WorkerManager


class TestLoopExecutorConstants:
    def test_phase_timeouts_defined(self):
        assert LoopPhase.DIAGNOSING in PHASE_TIMEOUTS
        assert LoopPhase.FIXING in PHASE_TIMEOUTS
        assert LoopPhase.REVIEWING in PHASE_TIMEOUTS
        assert all(t > 0 for t in PHASE_TIMEOUTS.values())

    def test_phase_models_defined(self):
        assert PHASE_MODELS[LoopPhase.DIAGNOSING] == "opus"
        assert PHASE_MODELS[LoopPhase.FIXING] == "sonnet"
        assert PHASE_MODELS[LoopPhase.REVIEWING] == "opus"

    def test_severity_priority_mapping(self):
        assert _SEVERITY_PRIORITY["P0"] == 0
        assert _SEVERITY_PRIORITY["P1"] == 1
        assert _SEVERITY_PRIORITY["P2"] == 2
        assert _SEVERITY_PRIORITY["P3"] == 3
        # P0 is highest priority (lowest number)
        assert _SEVERITY_PRIORITY["P0"] < _SEVERITY_PRIORITY["P3"]


class TestLoopExecutor:
    @pytest.fixture
    def mock_worker(self):
        worker = MagicMock(spec=WorkerManager)
        worker.run_phase = AsyncMock(return_value=WorkerResult(
            text="phase output", duration_s=1.0, cost_usd=0.01
        ))
        worker.run_phase_with_budget = AsyncMock(return_value=WorkerResult(
            text="phase output", duration_s=1.0, cost_usd=0.01
        ))
        return worker

    @pytest.fixture
    def executor(self, mock_worker):
        return LoopExecutor(mock_worker)

    @pytest.mark.asyncio
    async def test_enqueue_default_severity(self, executor):
        await executor.enqueue("bug: something broken")
        assert executor._queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_enqueue_stores_priority_tuple(self, executor):
        await executor.enqueue("critical bug", severity="P0")
        priority, seq, ticket = await executor._queue.get()
        assert priority == 0  # P0 → priority 0
        assert ticket["severity"] == "P0"
        assert ticket["signal"] == "critical bug"

    @pytest.mark.asyncio
    async def test_enqueue_severity_ordering(self, executor):
        """P0 items should dequeue before P3 items regardless of insertion order."""
        await executor.enqueue("low priority task", severity="P3")
        await executor.enqueue("critical bug", severity="P0")
        await executor.enqueue("medium task", severity="P2")

        first_priority, _, first_ticket = await executor._queue.get()
        second_priority, _, second_ticket = await executor._queue.get()
        third_priority, _, third_ticket = await executor._queue.get()

        assert first_priority == 0   # P0 first
        assert second_priority == 2  # P2 second
        assert third_priority == 3   # P3 last

    @pytest.mark.asyncio
    async def test_enqueue_seq_tiebreaker(self, executor):
        """Items with same priority use seq as tiebreaker (FIFO within same priority)."""
        await executor.enqueue("first P1", severity="P1")
        await executor.enqueue("second P1", severity="P1")

        p1, seq1, t1 = await executor._queue.get()
        p2, seq2, t2 = await executor._queue.get()

        assert seq1 < seq2  # First enqueued gets lower seq → dequeues first
        assert t1["signal"] == "first P1"

    @pytest.mark.asyncio
    async def test_get_status_default_max_parallel(self, executor):
        status = executor.get_status()
        assert status["queue_size"] == 0
        assert status["active_tickets"] == {}
        assert status["max_parallel"] == 4  # default changed to 4

    @pytest.mark.asyncio
    async def test_get_status_includes_severity(self, executor):
        """Active tickets in status should include severity field."""
        state = LoopState(ticket_id="test-ticket")
        state._severity = "P1"
        executor._active["test-ticket"] = state

        status = executor.get_status()
        assert status["active_tickets"]["test-ticket"]["severity"] == "P1"

    @pytest.mark.asyncio
    async def test_get_status_unknown_severity_fallback(self, executor):
        """Active tickets without _severity attribute show 'unknown'."""
        state = LoopState(ticket_id="test-ticket")
        # Do NOT set _severity
        executor._active["test-ticket"] = state

        status = executor.get_status()
        assert status["active_tickets"]["test-ticket"]["severity"] == "unknown"

    @pytest.mark.asyncio
    async def test_run_phase_uses_run_phase_with_budget(self, executor, mock_worker):
        result = await executor._run_phase(
            LoopPhase.DIAGNOSING, role="diagnose",
            prompt="test", system_prompt="sys", ticket_id="T1"
        )
        assert result.text == "phase output"
        mock_worker.run_phase_with_budget.assert_called_once()
        call_kwargs = mock_worker.run_phase_with_budget.call_args.kwargs
        assert call_kwargs["model"] == "opus"
        assert call_kwargs["timeout"] == PHASE_TIMEOUTS[LoopPhase.DIAGNOSING]

    @pytest.mark.asyncio
    async def test_run_phase_budget_continuation(self, executor, mock_worker):
        """When budget_exceeded with handoff_doc, run_phase is called for continuation."""
        initial_result = WorkerResult(
            text="partial output",
            duration_s=1.0,
            cost_usd=0.05,
            input_tokens=100,
            output_tokens=200,
            budget_exceeded=True,
            handoff_doc="## Handoff\nContinue from step 3.",
        )
        continuation_result = WorkerResult(
            text="continuation output",
            duration_s=0.5,
            cost_usd=0.02,
            input_tokens=50,
            output_tokens=80,
        )
        mock_worker.run_phase_with_budget = AsyncMock(return_value=initial_result)
        mock_worker.run_phase = AsyncMock(return_value=continuation_result)

        result = await executor._run_phase(
            LoopPhase.DIAGNOSING, role="diagnose",
            prompt="test", system_prompt="sys", ticket_id="T1"
        )

        # Continuation was called
        mock_worker.run_phase.assert_called_once()
        cont_kwargs = mock_worker.run_phase.call_args.kwargs
        assert cont_kwargs["role"] == "diagnose_continuation"
        assert "交接文档" in cont_kwargs["prompt"]

        # Merged result uses continuation text and summed metrics
        assert result.text == "continuation output"
        assert result.duration_s == 1.5
        assert result.input_tokens == 150
        assert result.output_tokens == 280
        assert abs(result.cost_usd - 0.07) < 1e-9

    @pytest.mark.asyncio
    async def test_run_phase_budget_exceeded_continuation_fails(self, executor, mock_worker):
        """If continuation itself errors, the original budget_exceeded result is returned."""
        initial_result = WorkerResult(
            text="partial output",
            budget_exceeded=True,
            handoff_doc="## Handoff",
        )
        mock_worker.run_phase_with_budget = AsyncMock(return_value=initial_result)
        mock_worker.run_phase = AsyncMock(return_value=WorkerResult(
            text="error", is_error=True
        ))

        result = await executor._run_phase(
            LoopPhase.DIAGNOSING, role="diagnose",
            prompt="test", system_prompt="sys", ticket_id="T1"
        )
        # Falls back to original result since continuation errored
        assert result.text == "partial output"
        assert result.budget_exceeded is True

    @pytest.mark.asyncio
    async def test_run_phase_no_continuation_when_no_handoff(self, executor, mock_worker):
        """budget_exceeded without handoff_doc does not trigger continuation."""
        result_no_handoff = WorkerResult(
            text="output", budget_exceeded=True, handoff_doc=""
        )
        mock_worker.run_phase_with_budget = AsyncMock(return_value=result_no_handoff)

        await executor._run_phase(
            LoopPhase.FIXING, role="fix",
            prompt="test", system_prompt="sys", ticket_id="T1"
        )
        mock_worker.run_phase.assert_not_called()

    @pytest.mark.asyncio
    async def test_pipeline_success(self, executor, mock_worker):
        """Full pipeline: diagnose → fix → QA PASS → closed."""
        responses = [
            WorkerResult(text="diagnosis report"),
            WorkerResult(text="fix report"),
            WorkerResult(text="<qa_verdict><result>PASS</result></qa_verdict>"),
        ]
        mock_worker.run_phase_with_budget = AsyncMock(side_effect=responses)

        await executor.enqueue("test signal")
        # Run one tick manually
        priority, seq, ticket = await executor._queue.get()
        state = LoopState(ticket_id="test")
        await executor._execute_pipeline(ticket, state)

        assert state.phase == LoopPhase.CLOSED

    @pytest.mark.asyncio
    async def test_pipeline_diagnosis_failure_stalls(self, executor, mock_worker):
        mock_worker.run_phase_with_budget = AsyncMock(return_value=WorkerResult(
            text="[TIMEOUT]", is_error=True
        ))

        state = LoopState(ticket_id="test")
        await executor._execute_pipeline({"signal": "test"}, state)

        assert state.phase == LoopPhase.STALLED

    @pytest.mark.asyncio
    async def test_pipeline_qa_reject_retries_then_passes(self, executor, mock_worker):
        """QA reject → retry fix → QA pass → closed."""
        responses = [
            WorkerResult(text="diagnosis"),           # diagnose
            WorkerResult(text="fix attempt 1"),       # fix #1
            WorkerResult(text="REJECT: scope mismatch"),  # QA #1 rejects
            WorkerResult(text="fix attempt 2"),       # fix #2 (retry)
            WorkerResult(text="PASS"),                # QA #2 passes
        ]
        mock_worker.run_phase_with_budget = AsyncMock(side_effect=responses)

        state = LoopState(ticket_id="test")
        await executor._execute_pipeline({"signal": "test"}, state)

        assert state.reject_count == 1
        assert state.phase == LoopPhase.CLOSED
        assert mock_worker.run_phase_with_budget.call_count == 5

    @pytest.mark.asyncio
    async def test_pipeline_qa_max_rejects_stalls(self, executor, mock_worker):
        """Reaching max_reject stalls the ticket."""
        responses = [
            WorkerResult(text="diagnosis"),
        ]
        # 3 rounds of fix + reject (max_reject=3)
        for _ in range(3):
            responses.append(WorkerResult(text="fix"))
            responses.append(WorkerResult(text="REJECT: still wrong"))
        mock_worker.run_phase_with_budget = AsyncMock(side_effect=responses)

        state = LoopState(ticket_id="test")
        await executor._execute_pipeline({"signal": "test"}, state)

        assert state.reject_count == 3
        assert state.phase == LoopPhase.STALLED

    @pytest.mark.asyncio
    async def test_preempt_lowest_cancels_worst_severity(self, executor):
        """_preempt_lowest cancels the active ticket with highest P number."""
        mock_task_p1 = MagicMock()
        mock_task_p3 = MagicMock()

        state_p1 = LoopState(ticket_id="ticket-p1")
        state_p1._severity = "P1"
        state_p1._task = mock_task_p1

        state_p3 = LoopState(ticket_id="ticket-p3")
        state_p3._severity = "P3"
        state_p3._task = mock_task_p3

        executor._active["ticket-p1"] = state_p1
        executor._active["ticket-p3"] = state_p3

        await executor._preempt_lowest()

        # P3 (worst) should be cancelled, P1 should not
        mock_task_p3.cancel.assert_called_once()
        mock_task_p1.cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_preempt_lowest_does_not_cancel_p0(self, executor):
        """_preempt_lowest never preempts another P0 ticket."""
        mock_task = MagicMock()
        state_p0 = LoopState(ticket_id="ticket-p0")
        state_p0._severity = "P0"
        state_p0._task = mock_task
        executor._active["ticket-p0"] = state_p0

        await executor._preempt_lowest()

        # P0 should NOT be cancelled (worst_sev == 0, condition is > 0)
        mock_task.cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_preempt_lowest_noop_when_no_active(self, executor):
        """_preempt_lowest is a no-op when no active tickets."""
        # Should not raise
        await executor._preempt_lowest()

    @pytest.mark.asyncio
    async def test_p0_enqueue_triggers_preemption_when_full(self, executor):
        """Enqueuing P0 when workers are full calls _preempt_lowest."""
        # Fill active slots to max_parallel (4)
        for i in range(executor._max_parallel):
            state = LoopState(ticket_id=f"ticket-{i}")
            state._severity = "P2"
            state._task = MagicMock()
            executor._active[f"ticket-{i}"] = state

        executor._preempt_lowest = AsyncMock()
        await executor.enqueue("emergency", severity="P0")

        executor._preempt_lowest.assert_called_once()

    @pytest.mark.asyncio
    async def test_p0_enqueue_no_preemption_when_slot_available(self, executor):
        """Enqueuing P0 with free worker slots does NOT trigger preemption."""
        executor._preempt_lowest = AsyncMock()
        await executor.enqueue("emergency", severity="P0")

        executor._preempt_lowest.assert_not_called()

    @pytest.mark.asyncio
    async def test_shutdown(self, executor):
        await executor.shutdown(timeout=1.0)
        assert executor._shutdown.is_set()
