# -*- coding: utf-8 -*-
"""Gold-standard test: FeishuBot.stop() flushes dirty reply cache to disk."""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent.platforms.feishu.bot import FeishuBot


def _make_bot(tmp_path):
    """Minimal FeishuBot bypassing __init__ for test isolation."""
    bot = object.__new__(FeishuBot)
    bot._reply_cache_path = str(tmp_path / "reply_cache.json")
    bot._reply_cache = {"msg123": "hello reply"}
    bot._reply_cache_dirty = True
    bot._pending = {}
    bot._thinking_cards = {}
    bot._queued_cards = {}
    bot._feishu_api = MagicMock()
    return bot


@pytest.mark.asyncio
async def test_stop_flushes_dirty_reply_cache(tmp_path):
    """stop() must persist dirty reply cache; _reply_cache_dirty → False; _shutting_down → True."""
    bot = _make_bot(tmp_path)
    cache_path = Path(bot._reply_cache_path)

    await bot.stop()

    assert cache_path.exists(), "reply_cache.json should exist after stop()"
    assert json.loads(cache_path.read_text()) == {"msg123": "hello reply"}
    assert bot._reply_cache_dirty is False
    assert bot._shutting_down is True


@pytest.mark.asyncio
async def test_stop_skips_flush_when_cache_not_dirty(tmp_path):
    """stop() should not write to disk when dirty flag is False."""
    bot = _make_bot(tmp_path)
    bot._reply_cache_dirty = False
    cache_path = Path(bot._reply_cache_path)

    await bot.stop()

    assert not cache_path.exists(), "no write expected for clean cache"
    assert bot._shutting_down is True


@pytest.mark.asyncio
async def test_stop_sets_shutting_down_even_if_flush_errors(tmp_path):
    """_shutting_down must be True even if flush raises an exception."""
    bot = _make_bot(tmp_path)
    bot._reply_cache_path = "/nonexistent_dir/reply_cache.json"  # force IOError

    await bot.stop()

    assert bot._shutting_down is True
