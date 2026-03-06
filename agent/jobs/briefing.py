# -*- coding: utf-8 -*-
"""Thin briefing plugin — subprocess launcher for the pipeline.

All pipeline logic lives in scripts/briefing_run.py.
This module only registers commands and handlers; it never needs to change.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("hub.briefing")

PYTHON = Path(os.environ.get("BRIEFING_PYTHON", sys.executable))
SCRIPT = Path(__file__).resolve().parent / "scripts" / "briefing_run.py"
DOMAINS_DIR = Path.home() / "briefing" / "domains"


class BriefingPlugin:
    """Subprocess-based briefing plugin — zero pipeline logic in hub process."""

    def __init__(self, notify_config: dict, default_domain: str = None):
        self.default_domain = default_domain
        self.notify_config = notify_config
        self._config_path = str(Path(__file__).resolve().parent / "config.yaml")

    def descriptor(self) -> dict:
        handlers = [{"name": "briefing", "fn": self._handler_default}]
        # Auto-discover domains → register per-domain handlers
        if DOMAINS_DIR.exists():
            for d in sorted(DOMAINS_DIR.iterdir()):
                if (d / "domain.yaml").exists():
                    name = d.name
                    handlers.append({
                        "name": f"briefing:{name}",
                        "fn": self._make_handler(name),
                    })
        return {
            "handlers": handlers,
        }

    def _make_handler(self, domain: str):
        async def handler():
            return await self.run(domain)
        return handler

    async def _handler_default(self):
        return await self.run(self.default_domain)

    async def run(self, domain: str = None, date_str: str = None) -> str:
        """Entry point for cron handlers."""
        domain = domain or self.default_domain
        if not domain:
            raise ValueError("No domain specified and no default_domain configured")
        result = await self._spawn("run", domain, date_str=date_str)
        try:
            data = json.loads(result)
            return f"[{data.get('domain')}] {data.get('date')} | {data.get('status')}"
        except (json.JSONDecodeError, TypeError):
            return result or "Pipeline completed"

    async def _spawn(self, command: str, domain: str,
                     date_str: str = None, timeout: int = 900) -> str:
        """Spawn briefing_run.py as subprocess."""
        cmd = [str(PYTHON), str(SCRIPT), command,
               "--domain", domain, "--config", self._config_path]
        if date_str:
            cmd.extend(["--date", date_str])

        log.info("Spawning: %s %s --domain %s", SCRIPT.name, command, domain)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(SCRIPT.parent.parent),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            log.error("briefing_run.py timed out (%ds)", timeout)
            return json.dumps({"status": "error", "error": f"Timeout ({timeout}s)"})

        if stderr:
            for line in stderr.decode("utf-8", errors="replace").strip().splitlines()[-5:]:
                log.info("[briefing_run] %s", line)

        return stdout.decode("utf-8", errors="replace").strip()
