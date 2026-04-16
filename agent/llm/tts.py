# -*- coding: utf-8 -*-
"""Fish.audio TTS client — S2-Pro model with natural language style control.

Wraps the official fish-audio-sdk for async TTS synthesis with:
- Automatic style tag injection ([laughter], [pause], [whisper], etc.)
- Prosody control (speed, volume)
- Audio caching with configurable TTL
"""

import hashlib
import logging
import os
import time
from pathlib import Path

from fishaudio import AsyncFishAudio, TTSConfig

log = logging.getLogger("hub.tts")

_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_CACHE_DIR = os.path.join(_PROJECT_ROOT, "data", "tts_cache")
_DEFAULT_TTL_DAYS = 30


def _load_config() -> dict:
    import yaml
    cfg_path = os.path.join(_PROJECT_ROOT, "config.yaml")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _get_client(api_key: str | None = None) -> AsyncFishAudio:
    if not api_key:
        cfg = _load_config()
        api_key = cfg.get("api_keys", {}).get("fish_audio", "")
    if not api_key:
        raise ValueError("Fish.audio API key not configured (api_keys.fish_audio)")
    return AsyncFishAudio(api_key=api_key)


def _cache_key(text: str, voice_id: str, fmt: str, speed: float) -> str:
    h = hashlib.sha256(f"{text}|{voice_id}|{fmt}|{speed}".encode()).hexdigest()[:16]
    return h


def _cache_path(key: str, fmt: str) -> Path:
    d = Path(_CACHE_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.{fmt}"


def _get_cached(key: str, fmt: str) -> bytes | None:
    p = _cache_path(key, fmt)
    if p.exists():
        age_days = (time.time() - p.stat().st_mtime) / 86400
        if age_days < _DEFAULT_TTL_DAYS:
            return p.read_bytes()
        p.unlink(missing_ok=True)
    return None


def _put_cache(key: str, fmt: str, data: bytes) -> Path:
    p = _cache_path(key, fmt)
    p.write_bytes(data)
    return p


def cleanup_cache(max_age_days: int = _DEFAULT_TTL_DAYS) -> int:
    """Remove cached audio files older than max_age_days. Returns count removed."""
    d = Path(_CACHE_DIR)
    if not d.exists():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for f in d.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    if removed:
        log.info("tts cache cleanup: removed %d files", removed)
    return removed


# ── Style control tags (S2-Pro natural language control) ─────────────

STYLE_TAGS = {
    # Pauses & breathing
    "pause": "[pause]",
    "short_pause": "[short pause]",
    "inhale": "[inhale]",
    "exhale": "[exhale]",
    "clearing_throat": "[clearing throat]",
    # Vocal expressions
    "laughter": "[laughter]",
    "chuckle": "[chuckle]",
    "giggle": "[giggles]",
    "sigh": "[sigh]",
    "tsk": "[tsk]",
    # Emotion
    "happy": "[happy]",
    "excited": "[excited]",
    "sad": "[sad]",
    "angry": "[angry]",
    "surprised": "[surprised]",
    "delight": "[delight]",
    # Style
    "whisper": "[whisper]",
    "shouting": "[shouting]",
    "low_voice": "[low voice]",
    "professional": "[professional broadcast tone]",
    "singing": "[singing]",
    # Prosody
    "emphasis": "[emphasis]",
    "pitch_up": "[pitch up]",
    "volume_up": "[volume up]",
    "volume_down": "[volume down]",
}


async def synthesize(
    text: str,
    *,
    voice_id: str | None = None,
    fmt: str = "mp3",
    speed: float = 1.0,
    temperature: float = 0.7,
    top_p: float = 0.7,
    use_cache: bool = True,
    api_key: str | None = None,
) -> tuple[bytes, str]:
    """Synthesize text to audio.

    Args:
        text: Text to synthesize. May contain S2-Pro style tags like [laughter].
        voice_id: Fish.audio voice reference ID. Falls back to config default.
        fmt: Audio format (mp3, wav, opus, pcm).
        speed: Speech speed multiplier (0.5-2.0).
        temperature: Generation randomness (0-1).
        top_p: Nucleus sampling (0-1).
        use_cache: Whether to use disk cache.
        api_key: Override API key.

    Returns:
        Tuple of (audio_bytes, file_path).
    """
    if not voice_id:
        cfg = _load_config()
        voice_id = cfg.get("fish_audio", {}).get("default_voice", "")
    if not voice_id:
        raise ValueError("No voice_id provided and no default_voice configured")

    key = _cache_key(text, voice_id, fmt, speed)
    if use_cache:
        cached = _get_cached(key, fmt)
        if cached:
            log.debug("tts cache hit: %s (%d bytes)", key, len(cached))
            return cached, str(_cache_path(key, fmt))

    client = _get_client(api_key)
    cfg = _load_config()
    model = cfg.get("fish_audio", {}).get("model", "s2-pro")

    tts_config = TTSConfig(
        reference_id=voice_id,
        format=fmt,
        temperature=temperature,
        top_p=top_p,
        chunk_length=200,
        latency="balanced",
        # SDK sends opus_bitrate in kbps but API expects bps; -1000 (auto) works for both
        **({"opus_bitrate": -1000} if fmt == "opus" else {}),
    )

    log.info("tts synthesize: %d chars, voice=%s, speed=%.1f, model=%s",
             len(text), voice_id[:8], speed, model)

    audio = await client.tts.convert(
        text=text,
        speed=speed if speed != 1.0 else None,
        config=tts_config,
        model=model,
    )

    path = _put_cache(key, fmt, audio)
    log.info("tts done: %d bytes → %s", len(audio), path)
    return audio, str(path)


async def synthesize_to_file(
    text: str,
    output_path: str,
    **kwargs,
) -> str:
    """Synthesize and save to a specific file path."""
    audio, _ = await synthesize(text, use_cache=False, **kwargs)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_bytes(audio)
    return output_path
