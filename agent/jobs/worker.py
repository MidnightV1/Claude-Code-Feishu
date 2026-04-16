# -*- coding: utf-8 -*-
"""Worker Process Manager — isolated phase execution for MAQS/MADS pipelines.

Wraps LLM router calls with timeout, metrics, and structured results.
Each call spawns a fresh `claude -p` subprocess with no session carryover.
Building block for the Hub 3.0 Loop Executor.
"""

import asyncio
import logging
import time

from agent.infra.models import LLMConfig, WorkerResult

log = logging.getLogger("hub.worker")


class WorkerManager:
    """Manage isolated worker processes for pipeline phase execution."""

    def __init__(self, router):
        self._router = router

    async def run_phase(
        self,
        role: str,
        model: str,
        prompt: str,
        system_prompt: str,
        workdir: str | None = None,
        timeout: int = 900,
    ) -> WorkerResult:
        """Execute a single pipeline phase as an isolated worker.

        Args:
            role: Phase identifier for logging (e.g. "diagnose", "fix", "qa")
            model: LLM model name (e.g. "opus", "sonnet")
            prompt: User prompt for the phase
            system_prompt: System prompt with phase instructions
            workdir: Optional working directory override
            timeout: Phase timeout in seconds (default 900)

        Returns:
            WorkerResult with phase output and metrics
        """
        llm_config = LLMConfig(
            provider="claude-cli",
            model=model,
            system_prompt=system_prompt,
            workspace_dir=workdir,
        )

        t0 = time.monotonic()
        log.info("Worker [%s] starting (model=%s, timeout=%ds)", role, model, timeout)

        try:
            result = await asyncio.wait_for(
                self._router.run(prompt=prompt, llm_config=llm_config),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            log.error("Worker [%s] timed out after %.1fs", role, elapsed)
            return WorkerResult(
                text=f"[TIMEOUT] Phase {role} exceeded {timeout}s limit",
                is_error=True,
                duration_s=elapsed,
            )
        except Exception as e:
            elapsed = time.monotonic() - t0
            log.error("Worker [%s] failed after %.1fs: %s", role, elapsed, e)
            return WorkerResult(
                text=f"[ERROR] {str(e)[:500]}",
                is_error=True,
                duration_s=elapsed,
            )

        elapsed = time.monotonic() - t0
        wr = WorkerResult(
            text=result.text,
            is_error=result.is_error,
            duration_s=elapsed,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
        )

        if result.is_error:
            log.warning("Worker [%s] returned error after %.1fs: %s",
                        role, elapsed, result.text[:200])
        else:
            log.info("Worker [%s] completed in %.1fs (tokens: %d/%d, $%.3f)",
                     role, elapsed, result.input_tokens, result.output_tokens, result.cost_usd)

        return wr

    # Context window sizes (conservative estimates)
    _CONTEXT_WINDOWS = {
        "opus": 200_000,
        "sonnet": 200_000,
        "haiku": 200_000,
    }

    async def run_phase_with_budget(
        self,
        role: str,
        model: str,
        prompt: str,
        system_prompt: str,
        workdir: str | None = None,
        timeout: int = 900,
        budget_pct: float = 0.8,
    ) -> WorkerResult:
        """Execute a phase with token budget enforcement.

        If total tokens (input + output) exceed budget_pct of the model's
        context window, marks the result with budget_exceeded=True and
        generates a handoff document from the partial output.

        Args:
            budget_pct: Maximum fraction of context window to use (default 0.8)
        """
        result = await self.run_phase(
            role=role, model=model, prompt=prompt,
            system_prompt=system_prompt, workdir=workdir, timeout=timeout,
        )

        if result.is_error:
            return result

        # Check budget
        window = self._CONTEXT_WINDOWS.get(model, 200_000)
        threshold = int(window * budget_pct)
        total_tokens = result.input_tokens + result.output_tokens

        if total_tokens > threshold:
            log.warning(
                "Worker [%s] exceeded token budget: %d/%d (%.0f%%)",
                role, total_tokens, threshold, total_tokens / threshold * 100,
            )
            result.budget_exceeded = True
            result.handoff_doc = (
                f"[BUDGET EXCEEDED] Phase {role} used {total_tokens} tokens "
                f"({total_tokens/window*100:.0f}% of {window} context window).\n"
                f"Partial output (last 2000 chars):\n{result.text[-2000:]}"
            )

        return result
