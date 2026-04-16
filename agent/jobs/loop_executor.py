# -*- coding: utf-8 -*-
"""Loop Executor — async execution loop for MAQS pipeline.

Hub 3.0 component: decouples ticket execution from conversation.
Uses WorkerManager for isolated phase execution with timeouts.
Provides enqueue() for dev signal hook and run_forever() for main process.
"""

import asyncio
import logging
import time

from agent.infra.models import LoopPhase, LoopState, WorkerResult
from agent.jobs.worker import WorkerManager

log = logging.getLogger("hub.loop_executor")

# Severity → priority mapping (lower number = higher priority)
_SEVERITY_PRIORITY = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# Per-phase timeout limits (seconds)
PHASE_TIMEOUTS = {
    LoopPhase.DIAGNOSING: 900,   # Opus diagnosis: up to 15 min
    LoopPhase.FIXING: 900,       # Sonnet/Codex fix: up to 15 min
    LoopPhase.REVIEWING: 900,    # Opus QA: up to 15 min
    LoopPhase.VISUAL_QA: 300,    # Visual QA: 5 min
}

# Phase → model mapping
PHASE_MODELS = {
    LoopPhase.DIAGNOSING: "opus",
    LoopPhase.FIXING: "sonnet",
    LoopPhase.REVIEWING: "opus",
}


class LoopExecutor:
    """Async execution loop for MAQS ticket pipeline.

    Maintains an internal queue of tickets to process.
    Each ticket goes through: diagnose → fix → hardgate → QA → merge.
    """

    def __init__(self, worker: WorkerManager, config: dict | None = None):
        self._worker = worker
        self._config = config or {}
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = 0  # tie-breaker for same priority
        self._active: dict[str, LoopState] = {}  # ticket_id → state
        self._max_parallel = self._config.get("max_parallel", 4)
        self._shutdown = asyncio.Event()

    async def enqueue(self, signal_text: str, severity: str = "P2"):
        """Add a development signal to the processing queue."""
        priority = _SEVERITY_PRIORITY.get(severity, 2)
        self._seq += 1
        ticket = {
            "signal": signal_text,
            "enqueued_at": time.time(),
            "source": "dev_signal",
            "severity": severity,
        }
        await self._queue.put((priority, self._seq, ticket))
        log.info("LoopExecutor: enqueued signal (severity=%s, queue size: %d)",
                 severity, self._queue.qsize())

        # P0 preemption: if all workers busy, cancel lowest-priority active ticket
        if severity == "P0" and len(self._active) >= self._max_parallel:
            await self._preempt_lowest()

    async def run_forever(self):
        """Main execution loop. Runs until shutdown is signaled."""
        log.info("LoopExecutor starting (max_parallel=%d)", self._max_parallel)

        workers = []
        for i in range(self._max_parallel):
            task = asyncio.create_task(self._worker_loop(i))
            workers.append(task)

        try:
            await self._shutdown.wait()
        finally:
            log.info("LoopExecutor shutting down, cancelling %d workers", len(workers))
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            log.info("LoopExecutor stopped")

    async def _worker_loop(self, worker_id: int):
        """Single worker consuming from the queue."""
        while not self._shutdown.is_set():
            try:
                priority, seq, ticket = await asyncio.wait_for(
                    self._queue.get(), timeout=30.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            ticket_id = ticket.get("signal", "unknown")[:40]
            log.info("Worker %d processing: %s", worker_id, ticket_id)

            state = LoopState(ticket_id=ticket_id)
            state._severity = ticket.get("severity", "P2")
            self._active[ticket_id] = state

            try:
                task = asyncio.current_task()
                state._task = task
                await self._execute_pipeline(ticket, state)
            except asyncio.CancelledError:
                log.warning("Worker %d preempted for %s", worker_id, ticket_id)
                state.phase = LoopPhase.STALLED
                # Re-enqueue the preempted ticket for later processing
                await self._queue.put((_SEVERITY_PRIORITY.get(state._severity, 3), self._seq, ticket))
                self._seq += 1
            except Exception as e:
                log.error("Pipeline failed for %s: %s", ticket_id, e)
                state.phase = LoopPhase.STALLED
            finally:
                self._active.pop(ticket_id, None)
                self._queue.task_done()

    async def _preempt_lowest(self):
        """Cancel the lowest-priority (highest P number) active ticket to make room for P0."""
        if not self._active:
            return
        # Find the ticket with worst (highest) severity number
        worst_tid = None
        worst_sev = -1
        for tid, state in self._active.items():
            sev = _SEVERITY_PRIORITY.get(getattr(state, '_severity', 'P3'), 3)
            if sev > worst_sev:
                worst_sev = sev
                worst_tid = tid
        if worst_tid and worst_sev > 0:  # Don't preempt other P0s
            state = self._active.get(worst_tid)
            if state and hasattr(state, '_task') and state._task:
                log.warning("P0 preemption: cancelling %s (severity P%d)", worst_tid, worst_sev)
                state._task.cancel()

    async def _execute_pipeline(self, ticket: dict, state: LoopState):
        """Execute the full pipeline for a single ticket."""

        # Phase 1: Diagnosis
        state.phase = LoopPhase.DIAGNOSING
        state.updated_at = time.time()

        diagnosis = await self._run_phase(
            LoopPhase.DIAGNOSING,
            role="diagnose",
            prompt=f"请诊断以下信号：\n\n{ticket.get('signal', '')}",
            system_prompt="你是诊断 agent。分析问题根因。",
            ticket_id=state.ticket_id,
        )

        if diagnosis.is_error:
            state.phase = LoopPhase.STALLED
            log.error("Diagnosis failed for %s: %s", state.ticket_id, diagnosis.text[:200])
            return

        state.phase = LoopPhase.DIAGNOSED
        state.updated_at = time.time()

        # Phase 2+3: Fix → QA loop (retries on REJECT up to max_reject)
        while state.reject_count < state.max_reject:
            # Fix phase
            state.phase = LoopPhase.FIXING
            state.updated_at = time.time()

            _fix_context = diagnosis.text
            if state.reject_count > 0:
                _fix_context += f"\n\n上次 QA 反馈（第 {state.reject_count} 次驳回）：\n{qa_result.text}"

            fix_result = await self._run_phase(
                LoopPhase.FIXING,
                role="fix",
                prompt=f"请基于以下诊断实施修复：\n\n{_fix_context}",
                system_prompt="你是修复 agent。执行最小化修复。",
                ticket_id=state.ticket_id,
            )

            if fix_result.is_error:
                state.phase = LoopPhase.STALLED
                log.error("Fix failed for %s: %s", state.ticket_id, fix_result.text[:200])
                return

            # QA Review phase
            state.phase = LoopPhase.REVIEWING
            state.updated_at = time.time()

            qa_result = await self._run_phase(
                LoopPhase.REVIEWING,
                role="qa",
                prompt="请对当前分支最新提交进行质量审查。",
                system_prompt=f"你是 QA agent。诊断报告：\n{diagnosis.text}",
                ticket_id=state.ticket_id,
            )

            if qa_result.is_error:
                state.phase = LoopPhase.STALLED
                return

            # Check QA verdict
            if "REJECT" not in qa_result.text:
                break  # QA passed

            state.reject_count += 1
            if state.reject_count >= state.max_reject:
                state.phase = LoopPhase.STALLED
                log.warning("Max rejects reached for %s", state.ticket_id)
                return
            log.info("QA rejected %s (attempt %d/%d), retrying fix",
                     state.ticket_id, state.reject_count, state.max_reject)

        state.phase = LoopPhase.CLOSED
        state.updated_at = time.time()
        log.info("Pipeline completed for %s", state.ticket_id)

    async def _run_phase(
        self,
        phase: LoopPhase,
        role: str,
        prompt: str,
        system_prompt: str,
        ticket_id: str = "",
        workdir: str | None = None,
    ) -> WorkerResult:
        """Execute a phase with per-phase timeout and budget via WorkerManager."""
        timeout = PHASE_TIMEOUTS.get(phase, 900)
        model = PHASE_MODELS.get(phase, "sonnet")

        log.info("Phase %s starting for %s (model=%s, timeout=%ds)",
                 phase.name, ticket_id, model, timeout)

        result = await self._worker.run_phase_with_budget(
            role=role,
            model=model,
            prompt=prompt,
            system_prompt=system_prompt,
            workdir=workdir,
            timeout=timeout,
        )

        # Budget exceeded: attempt continuation with handoff
        if result.budget_exceeded and result.handoff_doc:
            log.info("Phase %s budget exceeded for %s, continuing with handoff",
                     phase.name, ticket_id)
            continuation = await self._worker.run_phase(
                role=f"{role}_continuation",
                model=model,
                prompt=f"继续上一阶段的工作。交接文档：\n\n{result.handoff_doc}",
                system_prompt=system_prompt,
                workdir=workdir,
                timeout=timeout,
            )
            if not continuation.is_error:
                # Merge: use continuation text, sum metrics
                result = WorkerResult(
                    text=continuation.text,
                    is_error=False,
                    duration_s=result.duration_s + continuation.duration_s,
                    input_tokens=result.input_tokens + continuation.input_tokens,
                    output_tokens=result.output_tokens + continuation.output_tokens,
                    cost_usd=result.cost_usd + continuation.cost_usd,
                )

        if result.is_error:
            log.warning("Phase %s failed for %s: %s",
                         phase.name, ticket_id, result.text[:200])
        else:
            log.info("Phase %s completed for %s in %.1fs ($%.3f)",
                     phase.name, ticket_id, result.duration_s, result.cost_usd)

        return result

    def get_status(self) -> dict:
        """Return current executor status for monitoring."""
        return {
            "queue_size": self._queue.qsize(),
            "active_tickets": {
                tid: {"phase": s.phase.name, "reject_count": s.reject_count,
                      "severity": getattr(s, '_severity', 'unknown')}
                for tid, s in self._active.items()
            },
            "max_parallel": self._max_parallel,
            "shutdown_requested": self._shutdown.is_set(),
        }

    async def shutdown(self, timeout: float = 30.0):
        """Signal graceful shutdown and wait for active work to complete."""
        log.info("Shutdown requested, waiting up to %.0fs", timeout)
        self._shutdown.set()
