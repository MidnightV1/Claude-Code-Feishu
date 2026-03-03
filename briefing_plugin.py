# -*- coding: utf-8 -*-
"""Thin briefing plugin — subprocess launcher for the pipeline.

All pipeline logic lives in scripts/briefing_run.py.
This module only registers commands and handlers; it never needs to change.
"""

import asyncio
import json
import logging
from pathlib import Path

log = logging.getLogger("hub.briefing")

PYTHON = Path.home() / "python313/python/bin/python3"
SCRIPT = Path(__file__).resolve().parent / "scripts" / "briefing_run.py"
DOMAINS_DIR = Path.home() / "briefing" / "domains"


class BriefingPlugin:
    """Subprocess-based briefing plugin — zero pipeline logic in hub process."""

    def __init__(self, notify_config: dict, default_domain: str = "ai-drama"):
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
            "commands": [{
                "prefix": "#briefing",
                "handler": self.handle_command,
                "help": (
                    "| `#briefing run [date] [--domain X]` | Run briefing pipeline |\n"
                    "| `#briefing status [--domain X]` | Last run info |\n"
                    "| `#briefing domains` | List available domains |"
                ),
            }],
            "handlers": handlers,
        }

    def _make_handler(self, domain: str):
        async def handler():
            return await self.run(domain)
        return handler

    async def _handler_default(self):
        return await self.run(self.default_domain)

    async def handle_command(self, cmd: str, args: str) -> str:
        after_prefix = cmd.replace("#briefing", "").strip()
        if after_prefix:
            subcmd = after_prefix.split()[0]
            rest = args
        elif args:
            parts = args.split(None, 1)
            subcmd = parts[0]
            rest = parts[1] if len(parts) > 1 else ""
        else:
            subcmd = "run"
            rest = ""

        domain = self.default_domain
        date_str = None
        parts = rest.strip().split()
        for i, p in enumerate(parts):
            if p == "--domain" and i + 1 < len(parts):
                domain = parts[i + 1]
            elif not p.startswith("--"):
                date_str = p

        if subcmd == "status":
            result = await self._spawn("status", domain)
            return self._format_status(result)

        if subcmd == "domains":
            result = await self._spawn("domains", domain)
            return self._format_domains(result)

        if subcmd in ("run", "#briefing"):
            asyncio.ensure_future(self._run_safe(domain, date_str))
            return f"日报 pipeline 已启动：{domain}" + (f" ({date_str})" if date_str else "")

        if subcmd == "evolve":
            asyncio.ensure_future(self._run_safe(domain, date_str, step="evolve"))
            return f"关键词进化已启动：{domain}"

        return f"Unknown: `{subcmd}`. Try `run`, `status`, `domains`, `evolve`."

    async def run(self, domain: str = None, date_str: str = None) -> str:
        """Entry point for cron handlers."""
        domain = domain or self.default_domain
        result = await self._spawn("run", domain, date_str=date_str)
        try:
            data = json.loads(result)
            return f"[{data.get('domain')}] {data.get('date')} | {data.get('status')}"
        except (json.JSONDecodeError, TypeError):
            return result or "Pipeline completed"

    async def _run_safe(self, domain: str, date_str: str = None, step: str = None):
        try:
            command = step or "run"
            await self._spawn(command, domain, date_str=date_str)
        except Exception:
            log.exception("Briefing pipeline error [%s]", domain)

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

    @staticmethod
    def _format_status(raw: str) -> str:
        try:
            s = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw or "No data"
        if s.get("status") == "no data":
            return "No briefing has run yet."
        from datetime import datetime
        started = datetime.fromtimestamp(s["started_at"]).strftime("%H:%M:%S") if s.get("started_at") else "?"
        return (
            f"**Last briefing run**\n"
            f"- Domain: {s.get('domain', '?')}\n"
            f"- Date: {s.get('date', '?')}\n"
            f"- Status: {s.get('status', '?')}\n"
            f"- Started: {started}\n"
            f"- Elapsed: {s.get('elapsed_s', '?')}s\n"
            f"- Generate: {s.get('model', '?')}\n"
            f"- Review: {s.get('review_model', 'off')}\n"
            f"- Cost: ${s.get('cost_usd', 0):.4f}\n"
            + (f"- Errors: {', '.join(s.get('errors', []))}" if s.get("errors") else "")
        )

    @staticmethod
    def _format_domains(raw: str) -> str:
        try:
            domains = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw or "No domains"
        if not domains:
            return "No domains configured."
        lines = []
        for d in domains:
            evo = " 🔄" if d.get("evolution") else ""
            lines.append(f"- **{d['name']}**: {d['display_name']} `{d.get('schedule', '')}`{evo}")
        return "**Available domains:**\n" + "\n".join(lines)
