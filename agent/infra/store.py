# -*- coding: utf-8 -*-
"""Atomic JSON persistence for claude-code-feishu."""

import json
import os
import shutil
import asyncio
import logging
import threading
from typing import Callable, Generic, TypeVar

log = logging.getLogger("hub.store")
L = TypeVar("L")


class LockPool(Generic[L]):
    def __init__(self, lock_factory: Callable[[], L], max_size: int = 50) -> None:
        self._factory = lock_factory
        self._max_size = max_size
        self._locks: dict[str, L] = {}
        self._guard = threading.Lock()

    def get(self, key: str) -> L:
        with self._guard:
            if key not in self._locks:
                self._locks[key] = self._factory()
            if len(self._locks) > self._max_size:
                for lock_key in list(self._locks):
                    if lock_key != key and not self._locks[lock_key].locked():
                        del self._locks[lock_key]
            return self._locks[key]

    def clear(self) -> None:
        with self._guard:
            self._locks.clear()

    def __len__(self) -> int:
        return len(self._locks)


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
            try:
                with open(bak, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e2:
                log.warning("Backup also corrupt at %s: %s", bak, e2)
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


# ═══ Per-Key Atomic Updates ═══

# One lock per file path — serialises read-modify-write cycles on the same file.
_file_locks: dict[str, asyncio.Lock] = {}


def _get_file_lock(path: str) -> asyncio.Lock:
    if path not in _file_locks:
        _file_locks[path] = asyncio.Lock()
    # Sweep unlocked entries when dict grows large (exclude current path)
    if len(_file_locks) > 50:
        for k in list(_file_locks):
            if k != path and not _file_locks[k].locked():
                del _file_locks[k]
    return _file_locks[path]


async def update_json_key(path: str, key: str, value):
    """Atomically update a single key in a JSON dict file.

    read → modify → write under asyncio.Lock, so concurrent updates
    to *different* keys in the same file don't clobber each other.
    """
    lock = _get_file_lock(path)
    async with lock:
        data = await load_json(path, {})
        data[key] = value
        await save_json(path, data)


async def delete_json_key(path: str, key: str):
    """Atomically remove a key from a JSON dict file."""
    lock = _get_file_lock(path)
    async with lock:
        data = await load_json(path, {})
        data.pop(key, None)
        await save_json(path, data)
