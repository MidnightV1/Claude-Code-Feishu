"""Behavior signal extraction — weekly cron handler.

Runs weekly_signal_digest.py, syncs metrics to Bitable, and sends L1 notification.
"""

import asyncio
import sys
import logging
from pathlib import Path

log = logging.getLogger("hub.behavior_signals")

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


async def _run_script(name: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(SCRIPTS / name),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(SCRIPTS.parent),
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode()


async def run_behavior_signal_digest(notifier=None) -> str:
    """Run weekly behavior signal digest, sync to Bitable, notify."""
    # 1. Run digest
    rc, summary, err = await _run_script("weekly_signal_digest.py")
    if rc != 0:
        log.error("Signal digest failed: %s", err[-500:])
        return f"行为信号提取失败: {err[-200:]}"

    # 2. Sync to Bitable (best-effort)
    try:
        sync_proc = await asyncio.create_subprocess_exec(
            sys.executable, str(SCRIPTS / "metrics_bitable_sync.py"),
            "--digest", "latest",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(SCRIPTS.parent),
        )
        sync_out, sync_err = await asyncio.wait_for(sync_proc.communicate(), timeout=60)
        if sync_proc.returncode == 0:
            log.info("Bitable sync: %s", sync_out.decode().strip()[:200])
        else:
            log.warning("Bitable sync failed: %s", sync_err.decode()[-200:])
    except Exception as e:
        log.warning("Bitable sync error: %s", e)

    # 3. L1 notification
    if notifier and summary:
        try:
            await notifier.send_to_delivery_target(
                f"{{{{card:header=行为信号周报,color=blue}}}}\n{summary}"
            )
        except Exception as e:
            log.warning("Notification failed: %s", e)

    return summary
