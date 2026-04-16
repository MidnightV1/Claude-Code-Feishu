#!/usr/bin/env python3
"""Send deploy notification to Feishu delivery chat.

Usage: deploy_notify.py "message text"

Supports {{card:header=...,color=...}} directives.
"""

import asyncio
import sys
import yaml
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.platforms.feishu.dispatcher import Dispatcher


async def main():
    if len(sys.argv) < 2:
        print("Usage: deploy_notify.py <message>", file=sys.stderr)
        sys.exit(1)

    msg = sys.argv[1].replace("\\n", "\n")
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    d = Dispatcher(cfg.get("notify", {}))
    await d.start()
    await d.send_card_to_delivery(msg)
    await d.stop()


if __name__ == "__main__":
    asyncio.run(main())
