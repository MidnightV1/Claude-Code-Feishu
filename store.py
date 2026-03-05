# -*- coding: utf-8 -*-
"""Atomic JSON persistence for claude-code-lark."""

import json
import os
import shutil
import asyncio
import logging

log = logging.getLogger("hub.store")


def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def load_json_sync(path: str, default=None):
    """Load JSON file synchronously. Returns default if file doesn't exist."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default if default is not None else {}
    except json.JSONDecodeError as e:
        log.warning("Corrupt JSON at %s: %s, trying backup", path, e)
        bak = path + ".bak"
        if os.path.exists(bak):
            with open(bak, "r", encoding="utf-8") as f:
                return json.load(f)
        return default if default is not None else {}


def save_json_sync(path: str, data):
    """Atomic write: temp file → os.replace → .bak backup."""
    _ensure_dir(path)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
    # best-effort backup
    try:
        shutil.copy2(path, path + ".bak")
    except Exception:
        pass


async def load_json(path: str, default=None):
    return await asyncio.to_thread(load_json_sync, path, default)


async def save_json(path: str, data):
    await asyncio.to_thread(save_json_sync, path, data)
