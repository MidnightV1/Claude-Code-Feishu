# -*- coding: utf-8 -*-
"""ArXiv tracker plugin — thin wrapper for scheduler integration.

All tracking logic lives in .claude/skills/arxiv-tracker/scripts/arxiv_engine.py.
This module only registers handlers; it never needs to change.
"""

import logging
import sys
from pathlib import Path

log = logging.getLogger("hub.arxiv")

SKILL_DIR = Path(__file__).resolve().parent.parent.parent / ".claude" / "skills" / "arxiv-tracker"
CONFIG_PATH = SKILL_DIR / "config" / "topics.yaml"
DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "arxiv"


class ArxivPlugin:
    """Scheduler plugin for ArXiv paper tracking."""

    def descriptor(self) -> dict:
        return {
            "handlers": [
                {"name": "arxiv:daily", "fn": self._handler_daily},
            ],
        }

    async def _handler_daily(self) -> str:
        """Daily paper tracking handler."""
        return await self.run()

    async def run(self, date_str: str = None) -> str:
        """Run paper tracking. Called by scheduler or directly."""
        scripts_dir = str(SKILL_DIR / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)

        from arxiv_engine import ArxivEngine

        engine = ArxivEngine(config_path=CONFIG_PATH, data_dir=DATA_DIR)
        try:
            result = await engine.run(date_str=date_str)
            status = result.get("status", "unknown")
            date = result.get("date", "?")
            selected = result.get("llm_selected", 0)
            doc_url = result.get("doc_url", "")
            personal_url = result.get("personal_url", "")

            if status != "ok" or selected == 0:
                return f"[arxiv] {date} | {status} | {selected} papers"

            # Build notification message
            fetched = result.get("total_fetched", 0)
            kw_matched = result.get("keyword_matched", 0)
            lines = [
                f"**ArXiv 论文日报 — {date}**",
                f"扫描 {fetched} 篇 → 预筛 {kw_matched} 篇 → 精选 {selected} 篇",
            ]
            if doc_url:
                lines.append(f"\n📄 [查看日报]({doc_url})")
            if personal_url:
                lines.append(f"📌 [你可能感兴趣的]({personal_url})")
            return "\n".join(lines)
        except Exception as e:
            log.exception("ArXiv tracker failed")
            return f"[arxiv] error: {e}"
