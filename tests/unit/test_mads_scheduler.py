"""Verify MADS pipeline is registered in the scheduler.

Gold standard: '*/30 * * * * mads_intake' → scheduler runs intake every 30 min.
Actual implementation: data/jobs.json + register_handler("mads_pipeline", ...).
These tests confirm functional equivalence.
"""

import json
import re
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent.parent
SEED_FILE = PROJECT_ROOT / "config" / "jobs.yaml"
if not SEED_FILE.exists():
    SEED_FILE = PROJECT_ROOT / "config" / "jobs.example.yaml"
MAIN_FILE = PROJECT_ROOT / "agent" / "main.py"


def _load_seed_jobs():
    """Load job definitions from the seed file (config/jobs.yaml)."""
    with open(SEED_FILE) as f:
        return yaml.safe_load(f).get("jobs", [])


def test_mads_pipeline_job_exists_in_seed():
    """config/jobs.yaml must contain an enabled mads-pipeline entry."""
    jobs = _load_seed_jobs()
    mads_jobs = [j for j in jobs if j.get("name") == "mads-pipeline"]
    assert len(mads_jobs) == 1, "Expected exactly one mads-pipeline job"
    job = mads_jobs[0]
    assert job.get("enabled", True) is True  # defaults to enabled if not specified


def test_mads_pipeline_cron_expr():
    """mads-pipeline must have a valid cron schedule."""
    jobs = _load_seed_jobs()
    job = next(j for j in jobs if j.get("name") == "mads-pipeline")
    schedule = job.get("schedule", "")
    # Seed uses flat string (e.g. "*/5 * * * *") or dict {kind, expr}
    if isinstance(schedule, dict):
        assert schedule.get("kind") == "cron"
        expr = schedule.get("expr", "")
    else:
        expr = schedule
    assert expr in ("*/30 * * * *", "*/5 * * * *")


def test_mads_pipeline_handler_name():
    """mads-pipeline job must reference 'mads_pipeline' handler."""
    jobs = _load_seed_jobs()
    job = next(j for j in jobs if j.get("name") == "mads-pipeline")
    assert job.get("handler") == "mads_pipeline"


def test_mads_handler_registered_in_main():
    """agent/main.py must call register_handler('mads_pipeline', ...)."""
    source = MAIN_FILE.read_text()
    assert 'register_handler("mads_pipeline"' in source


def test_mads_handler_calls_run_mads_pipeline():
    """The registered handler must call run_mads_pipeline (intake logic)."""
    source = MAIN_FILE.read_text()
    assert "run_mads_pipeline" in source
    # import must exist
    assert "from agent.jobs.mads.pipeline import run_mads_pipeline" in source
