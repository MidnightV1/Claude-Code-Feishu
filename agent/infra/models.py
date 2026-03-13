# -*- coding: utf-8 -*-
"""Shared data structures for claude-code-feishu."""

from dataclasses import dataclass, field, asdict
from typing import Optional
import time
import uuid


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
    return CronJobState(**{k: v for k, v in d.items() if k in CronJobState.__dataclass_fields__})


def cron_job_from_dict(d: dict) -> CronJob:
    d = dict(d)
    d["schedule"] = cron_schedule_from_dict(d.get("schedule"))
    d["llm"] = llm_config_from_dict(d.get("llm"))
    d["state"] = cron_job_state_from_dict(d.get("state"))
    return CronJob(**{k: v for k, v in d.items() if k in CronJob.__dataclass_fields__})


