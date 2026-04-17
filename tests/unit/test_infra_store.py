# -*- coding: utf-8 -*-
"""Unit tests for agent/infra/store.py — atomic JSON I/O, backup fallback, lock sweep."""
import asyncio
import json
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import agent.infra.store as store_mod
from agent.infra.store import (
    load_json_sync,
    save_json_sync,
    update_json_key,
    delete_json_key,
    _get_file_lock,
    _file_locks,
    LockPool,
)


# ── load_json_sync ────────────────────────────────────────────────────────────

class TestLoadJsonSync:
    def test_missing_file_returns_default_dict(self, tmp_path):
        result = load_json_sync(str(tmp_path / "nonexistent.json"))
        assert result == {}

    def test_missing_file_returns_custom_default(self, tmp_path):
        result = load_json_sync(str(tmp_path / "nonexistent.json"), default=[])
        assert result == []

    def test_loads_valid_json(self, tmp_path):
        p = tmp_path / "data.json"
        p.write_text(json.dumps({"key": "value"}))
        result = load_json_sync(str(p))
        assert result == {"key": "value"}

    def test_corrupt_falls_back_to_backup(self, tmp_path):
        p = tmp_path / "data.json"
        bak = tmp_path / "data.json.bak"
        p.write_text("not valid json {{{")
        bak.write_text(json.dumps({"restored": True}))
        result = load_json_sync(str(p))
        assert result == {"restored": True}

    def test_corrupt_no_backup_returns_default(self, tmp_path):
        p = tmp_path / "data.json"
        p.write_text("not valid json {{{")
        result = load_json_sync(str(p))
        assert result == {}

    def test_corrupt_backup_also_corrupt_returns_default(self, tmp_path):
        p = tmp_path / "data.json"
        bak = tmp_path / "data.json.bak"
        p.write_text("{{bad}}")
        bak.write_text("{{also bad}}")
        result = load_json_sync(str(p), default={"fallback": 1})
        assert result == {"fallback": 1}


# ── save_json_sync ────────────────────────────────────────────────────────────

class TestSaveJsonSync:
    def test_writes_data(self, tmp_path):
        p = tmp_path / "out.json"
        save_json_sync(str(p), {"hello": "world"})
        loaded = json.loads(p.read_text())
        assert loaded == {"hello": "world"}

    def test_creates_backup(self, tmp_path):
        p = tmp_path / "out.json"
        save_json_sync(str(p), {"v": 1})
        assert (tmp_path / "out.json.bak").exists()

    def test_atomic_no_tmp_left(self, tmp_path):
        p = tmp_path / "out.json"
        save_json_sync(str(p), {"x": 1})
        tmp_files = [f for f in tmp_path.iterdir() if ".tmp" in f.name]
        assert len(tmp_files) == 0

    def test_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "a" / "b" / "data.json"
        save_json_sync(str(p), {"nested": True})
        assert p.exists()

    def test_overwrites_existing(self, tmp_path):
        p = tmp_path / "data.json"
        save_json_sync(str(p), {"v": 1})
        save_json_sync(str(p), {"v": 2})
        assert json.loads(p.read_text()) == {"v": 2}

    def test_roundtrip_unicode(self, tmp_path):
        p = tmp_path / "data.json"
        data = {"name": "张三", "emoji": "🎉"}
        save_json_sync(str(p), data)
        assert load_json_sync(str(p)) == data


# ── _get_file_lock ────────────────────────────────────────────────────────────

class TestGetFileLock:
    def setup_method(self):
        _file_locks.clear()

    def test_returns_lock(self):
        lock = _get_file_lock("/tmp/test.json")
        import asyncio
        assert isinstance(lock, asyncio.Lock)

    def test_same_path_same_lock(self):
        l1 = _get_file_lock("/tmp/same.json")
        l2 = _get_file_lock("/tmp/same.json")
        assert l1 is l2

    def test_different_paths_different_locks(self):
        l1 = _get_file_lock("/tmp/a.json")
        l2 = _get_file_lock("/tmp/b.json")
        assert l1 is not l2

    def test_sweep_when_over_50(self):
        _file_locks.clear()
        # Fill 51 unlocked entries
        for i in range(51):
            _get_file_lock(f"/tmp/file_{i}.json")
        assert len(_file_locks) <= 51
        # One more access triggers sweep, dict should shrink
        _get_file_lock("/tmp/trigger.json")
        assert len(_file_locks) < 52


class TestLockPool:
    def test_threading_lock_factory_returns_same_lock_for_same_key(self):
        pool = LockPool(threading.Lock, max_size=2)
        l1 = pool.get("a")
        l2 = pool.get("a")
        assert isinstance(l1, type(threading.Lock()))
        assert l1 is l2
        assert len(pool) == 1

    def test_asyncio_lock_factory_returns_lock(self):
        pool = LockPool(asyncio.Lock, max_size=2)
        lock = pool.get("a")
        assert isinstance(lock, asyncio.Lock)
        assert len(pool) == 1

    def test_sweep_skips_locked_entry(self):
        pool = LockPool(threading.Lock, max_size=1)
        held = pool.get("x")
        held.acquire()
        try:
            pool.get("y")
            pool.get("z")
            assert "x" in pool._locks
        finally:
            held.release()

    def test_sweep_skips_current_key(self):
        pool = LockPool(threading.Lock, max_size=1)
        pool.get("x")
        current = pool.get("y")
        assert pool.get("y") is current
        assert "y" in pool._locks

    def test_clear_resets_pool(self):
        pool = LockPool(threading.Lock, max_size=2)
        pool.get("a")
        pool.get("b")
        pool.clear()
        assert len(pool) == 0

    def test_gold_standard_max_size_two(self):
        pool = LockPool(threading.Lock, max_size=2)
        pool.get("a")
        pool.get("b")
        pool.get("c")
        assert len(pool) <= 3
        assert pool.get("a") is not None


# ── update_json_key / delete_json_key ────────────────────────────────────────

class TestUpdateDeleteJsonKey:
    def test_update_creates_file(self, tmp_path):
        p = str(tmp_path / "kv.json")
        asyncio.get_event_loop().run_until_complete(update_json_key(p, "a", 1))
        assert load_json_sync(p) == {"a": 1}

    def test_update_preserves_other_keys(self, tmp_path):
        p = str(tmp_path / "kv.json")
        save_json_sync(p, {"x": 10, "y": 20})
        asyncio.get_event_loop().run_until_complete(update_json_key(p, "x", 99))
        data = load_json_sync(p)
        assert data["x"] == 99
        assert data["y"] == 20

    def test_delete_removes_key(self, tmp_path):
        p = str(tmp_path / "kv.json")
        save_json_sync(p, {"a": 1, "b": 2})
        asyncio.get_event_loop().run_until_complete(delete_json_key(p, "a"))
        data = load_json_sync(p)
        assert "a" not in data
        assert data["b"] == 2

    def test_delete_missing_key_noop(self, tmp_path):
        p = str(tmp_path / "kv.json")
        save_json_sync(p, {"b": 2})
        asyncio.get_event_loop().run_until_complete(delete_json_key(p, "nonexistent"))
        assert load_json_sync(p) == {"b": 2}

    def test_concurrent_updates_no_clobber(self, tmp_path):
        p = str(tmp_path / "kv.json")

        async def run():
            await asyncio.gather(
                update_json_key(p, "k1", "v1"),
                update_json_key(p, "k2", "v2"),
                update_json_key(p, "k3", "v3"),
            )

        asyncio.get_event_loop().run_until_complete(run())
        data = load_json_sync(p)
        assert data.get("k1") == "v1"
        assert data.get("k2") == "v2"
        assert data.get("k3") == "v3"
