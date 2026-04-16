# -*- coding: utf-8 -*-
"""Shared data structures for claude-code-feishu."""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import IntEnum
from typing import List, Optional
import time
import uuid


# ═══ Autonomy ═══

class AutonomyLevel(IntEnum):
    """Superintendent autonomy levels (L0–L3).

    L0: Silent — reversible, low-risk, precedented. Log only.
    L1: Notify — reversible changes. Execute then notify user.
    L2: Approve — irreversible or directional. Doc → user confirms → execute.
    L3: Discuss — requires user judgment. Real-time conversation.
    """
    L0_SILENT = 0
    L1_NOTIFY = 1
    L2_APPROVE = 2
    L3_DISCUSS = 3


# ═══ LLM ═══

@dataclass
class LLMConfig:
    provider: str = "claude-cli"       # "claude-cli" | "gemini-cli" | "gemini-api"
    model: str = "opus"
    timeout_seconds: Optional[int] = None  # None → idle-based timeout in claude-cli
    system_prompt: Optional[str] = None
    temperature: float = 1.0           # gemini default
    thinking: Optional[str] = None     # gemini thinking level: minimal/low/medium/high
    effort: Optional[str] = None        # claude-cli effort: low/medium/high (None = CLI decides)
    env: dict = field(default_factory=dict)  # extra env vars for subprocess (e.g. HOME override)
    workspace_dir: Optional[str] = None      # per-bot working directory override (None → use global)
    setting_sources: Optional[str] = None    # claude-cli --setting-sources override (e.g. "local" to skip global config)


@dataclass
class LLMResult:
    text: str = ""
    session_id: Optional[str] = None
    duration_ms: int = 0
    is_error: bool = False
    cancelled: bool = False
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    explore_hints: str = ""  # raw <next-explore> content from assistant events
    reflect_hints: str = ""  # raw <next-reflect> content from assistant events


@dataclass
class WorkerResult:
    """Structured result from a pipeline phase execution."""
    text: str = ""
    is_error: bool = False
    duration_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    budget_exceeded: bool = False    # True if token budget was exceeded
    handoff_doc: str = ""            # structured handoff for budget overflow


# ═══ Cron ═══

@dataclass
class CronSchedule:
    kind: str = "cron"                 # "cron" | "every" | "at"
    expr: Optional[str] = None         # cron expression (kind=cron)
    every_seconds: Optional[int] = None  # interval (kind=every)
    at_time: Optional[str] = None      # ISO timestamp (kind=at)
    tz: str = "Asia/Shanghai"


@dataclass
class CronJobState:
    next_run_at: Optional[float] = None
    last_run_at: Optional[float] = None
    last_status: Optional[str] = None  # "ok" | "error" | "skipped"
    last_error: Optional[str] = None
    consecutive_errors: int = 0


@dataclass
class CronJob:
    id: str = ""
    name: str = ""
    enabled: bool = True
    schedule: CronSchedule = field(default_factory=CronSchedule)
    prompt: str = ""
    handler: str = ""              # registered handler name, takes precedence over prompt
    llm: LLMConfig = field(default_factory=LLMConfig)
    deliver_to_feishu: bool = True
    notify_bot: str = ""           # route notification via named bot dispatcher (empty = default notifier)
    silent_token: str = ""         # e.g. "SILENT_OK" — suppress delivery when output contains this token
    one_shot: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    state: CronJobState = field(default_factory=CronJobState)

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = time.time()
            self.updated_at = self.created_at


# ═══ Serialization ═══

def to_dict(obj) -> dict:
    """Convert dataclass to dict."""
    return asdict(obj)


def llm_config_from_dict(d: dict) -> LLMConfig:
    if d is None:
        return LLMConfig()
    return LLMConfig(**{k: v for k, v in d.items() if k in LLMConfig.__dataclass_fields__})


def cron_schedule_from_dict(d: dict) -> CronSchedule:
    if d is None:
        return CronSchedule()
    return CronSchedule(**{k: v for k, v in d.items() if k in CronSchedule.__dataclass_fields__})


def cron_job_state_from_dict(d: dict) -> CronJobState:
    if d is None:
        return CronJobState()
    d = dict(d)
    if "next_run_at" in d and not isinstance(d["next_run_at"], (int, float)):
        try:
            d["next_run_at"] = float(d["next_run_at"])
        except (TypeError, ValueError):
            d["next_run_at"] = None
    return CronJobState(**{k: v for k, v in d.items() if k in CronJobState.__dataclass_fields__})


def cron_job_from_dict(d: dict) -> CronJob:
    d = dict(d)
    d["schedule"] = cron_schedule_from_dict(d.get("schedule"))
    d["llm"] = llm_config_from_dict(d.get("llm"))
    d["state"] = cron_job_state_from_dict(d.get("state"))
    return CronJob(**{k: v for k, v in d.items() if k in CronJob.__dataclass_fields__})


# ═══ Workflow Steps (TodoWrite-driven pipeline) ═══

class StepStatus(IntEnum):
    PENDING = 0
    IN_PROGRESS = 1
    COMPLETED = 2
    FAILED = 3
    LOCKED = 4  # QA passed → immutable during retry


@dataclass
class TicketStep:
    id: str = ""
    content: str = ""
    active_form: str = ""
    status: StepStatus = StepStatus.PENDING
    affected_files: List[str] = field(default_factory=list)
    verification: str = ""
    result: str = ""
    qa_verdict: str = ""   # "pass" | "fail" | ""
    qa_reason: str = ""

    def __post_init__(self):
        if isinstance(self.status, int):
            self.status = StepStatus(self.status)


@dataclass
class TicketWorkflow:
    steps: List[TicketStep] = field(default_factory=list)

    @property
    def progress(self) -> str:
        done = sum(1 for s in self.steps if s.status in (StepStatus.COMPLETED, StepStatus.LOCKED))
        return f"{done}/{len(self.steps)}"

    @property
    def failed_steps(self) -> List[TicketStep]:
        return [s for s in self.steps if s.status == StepStatus.FAILED]

    @property
    def all_files(self) -> set:
        return {f for s in self.steps for f in s.affected_files}

    @property
    def locked_files(self) -> set:
        return {f for s in self.steps if s.status == StepStatus.LOCKED for f in s.affected_files}

    @property
    def retry_files(self) -> set:
        return {f for s in self.steps if s.status == StepStatus.FAILED for f in s.affected_files}

    def lock_passed(self):
        """Lock steps that passed QA — immutable during retry."""
        for s in self.steps:
            if s.status == StepStatus.COMPLETED and s.qa_verdict == "pass":
                s.status = StepStatus.LOCKED

    def render_progress(self) -> str:
        icons = {
            StepStatus.PENDING: "⬜",
            StepStatus.IN_PROGRESS: "🔄",
            StepStatus.COMPLETED: "✅",
            StepStatus.FAILED: "❌",
            StepStatus.LOCKED: "🔒",
        }
        lines = []
        for i, s in enumerate(self.steps, 1):
            icon = icons.get(s.status, "⬜")
            suffix = ""
            if s.status == StepStatus.LOCKED:
                suffix = " — 已锁定"
            elif s.status == StepStatus.FAILED and s.qa_reason:
                suffix = f" — {s.qa_reason}"
            lines.append(f"{icon} **{i}.** {s.content}{suffix}")
        done = sum(1 for s in self.steps if s.status in (StepStatus.COMPLETED, StepStatus.LOCKED))
        lines.append(f"\n{done}/{len(self.steps)} 完成")
        return "\n".join(lines)


def ticket_step_from_dict(d: dict) -> TicketStep:
    if d is None:
        return TicketStep()
    d = dict(d)
    if "status" in d and isinstance(d["status"], int):
        d["status"] = StepStatus(d["status"])
    return TicketStep(**{k: v for k, v in d.items() if k in TicketStep.__dataclass_fields__})


def ticket_workflow_from_dict(d: dict) -> TicketWorkflow:
    if d is None:
        return TicketWorkflow()
    d = dict(d)
    if "steps" in d and isinstance(d["steps"], list):
        d["steps"] = [ticket_step_from_dict(s) if isinstance(s, dict) else s for s in d["steps"]]
    return TicketWorkflow(**{k: v for k, v in d.items() if k in TicketWorkflow.__dataclass_fields__})


# ═══ Loop Execution ═══

class LoopPhase(IntEnum):
    DIAGNOSING = 0
    DIAGNOSED  = 1
    FIXING     = 2
    REVIEWING  = 3
    VISUAL_QA  = 4
    CLOSED     = 5
    STALLED    = 6


@dataclass
class LoopState:
    ticket_id: str = ""
    phase: LoopPhase = LoopPhase.DIAGNOSING
    reject_count: int = 0
    max_reject: int = 3
    created_at: float = field(default_factory=time.time)
    updated_at: float = 0.0

    def __post_init__(self):
        if isinstance(self.phase, int):
            self.phase = LoopPhase(self.phase)
        if not self.updated_at:
            self.updated_at = self.created_at


def loop_state_from_dict(d: dict) -> LoopState:
    if d is None:
        return LoopState()
    d = dict(d)
    if "phase" in d and isinstance(d["phase"], int):
        d["phase"] = LoopPhase(d["phase"])
    return LoopState(**{k: v for k, v in d.items() if k in LoopState.__dataclass_fields__})


# ═══ Merge Queue ═══

class ConflictStatus(IntEnum):
    NONE     = 0
    DETECTED = 1
    RESOLVED = 2
    FAILED   = 3


class MergeStrategy(IntEnum):
    AUTO   = 0
    MANUAL = 1
    REBASE = 2
    ABORT  = 3


@dataclass
class MergeRequest:
    wt_path: str = ""
    branch: str = ""
    ticket_id: str = ""
    priority: int = 0
    modified_files: List[str] = field(default_factory=list)
    intent: str = ""
    enqueued_at: datetime = field(default_factory=datetime.now)


def merge_request_from_dict(d: dict) -> MergeRequest:
    if d is None:
        return MergeRequest()
    d = dict(d)
    if "enqueued_at" in d and isinstance(d["enqueued_at"], str):
        d["enqueued_at"] = datetime.fromisoformat(d["enqueued_at"])
    return MergeRequest(**{k: v for k, v in d.items() if k in MergeRequest.__dataclass_fields__})


