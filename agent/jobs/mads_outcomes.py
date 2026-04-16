# -*- coding: utf-8 -*-
"""MADS outcome tracking — daily cron handler.

Runs mads_outcome_tracker.py, syncs metrics to Bitable, and sends L1 notification on anomalies.
"""

import asyncio
import sys
import logging
from pathlib import Path

log = logging.getLogger("hub.mads_outcomes")

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


async def _run_script(name: str, *args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(SCRIPTS / name), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(SCRIPTS.parent),
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode()


async def run_mads_outcome_check(notifier=None) -> str:
    """Run daily MADS outcome check, sync to Bitable, notify on anomalies."""
    # 1. Run tracker
    rc, summary, err = await _run_script("mads_outcome_tracker.py")
    if rc != 0:
        log.error("Outcome tracker failed: %s", err[-500:])
        return f"Outcome tracking 失败: {err[-200:]}"

    # 2. Sync to Bitable (best-effort)
    try:
        sync_proc = await asyncio.create_subprocess_exec(
            sys.executable, str(SCRIPTS / "metrics_bitable_sync.py"),
            "--outcome", "latest",
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

    # 3. L1 notification on anomalies (revert, recurrence, test manipulation)
    has_anomaly = any(marker in summary for marker in ("REVERTED", "RECURRED", "TEST_MANIP", "REGRESSION"))
    if notifier:
        if has_anomaly:
            await notifier.send_to_delivery_target(
                f"{{{{card:header=MADS Outcome 异常,color=orange}}}}\n{summary}"
            )
        else:
            log.info("MADS outcomes normal, no notification needed")

    return summary
