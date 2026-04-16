"""Response quality periodic check — runs proxy metrics script as subprocess."""

import asyncio
import logging
import sys
from pathlib import Path

log = logging.getLogger("hub.response_quality")


async def run_response_quality_check() -> str:
    """Execute response_quality_metrics.py and return summary text."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "response_quality_metrics.py"
    if not script.exists():
        log.error("Script not found: %s", script)
        return "响应质量检查失败: 脚本不存在"

    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(script),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=str(script.parent.parent),
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        log.error("Response quality check failed: %s", stderr.decode()[-500:])
        return f"响应质量检查失败: {stderr.decode()[-200:]}"

    return stdout.decode().strip()
