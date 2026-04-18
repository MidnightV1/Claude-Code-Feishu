# -*- coding: utf-8 -*-
"""Gemini API wrapper via google-genai SDK."""

import asyncio
import mimetypes
import os
import time
import logging
from agent.infra.models import LLMResult

log = logging.getLogger("hub.gemini_api")

# Model ID mapping
MODELS = {
    "3.1-Pro":        "gemini-3.1-pro-preview",
    "3-Flash":        "gemini-3-flash-preview",
    "2.5-Flash":      "gemini-2.5-flash",
    "2.5-Pro":        "gemini-2.5-pro",
    "2.0-flash-lite": "gemini-2.0-flash-lite",
    "3.1-Flash-Lite": "gemini-3.1-flash-lite-preview",
}

# Pricing: USD per million tokens
PRICING = {
    "3.1-Pro":        {"input": 2.0,   "output": 12},
    "3-Flash":        {"input": 0.5,   "output": 3},
    "2.5-Flash":      {"input": 0.15,  "output": 0.6},
    "2.5-Pro":        {"input": 1.25,  "output": 10},
}

# Thinking level support per model
THINKING_LEVELS = {
    "3.1-Pro": ("low", "medium", "high"),
    "3-Flash": ("minimal", "low", "medium", "high"),
}


class GeminiAPI:
    def __init__(self, config: dict):
        self.api_key = config.get("api_key", "")
        self.default_timeout = config.get("timeout_seconds", 120)
        self.default_temperature = config.get("temperature", 1.0)
        self.file_ttl_days = config.get("file_ttl_days", 30)
        self._client = None
        self._uploaded_files: list[tuple[str, float]] = []  # (file_name, upload_time)

    def _get_client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    async def run(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str = "3-Flash",
        thinking: str | None = None,
        temperature: float | None = None,
        timeout_seconds: int | None = None,
        files: list[str] | None = None,
        image_src: str | None = None,
    ) -> LLMResult:
        timeout = timeout_seconds or self.default_timeout
        temp = temperature if temperature is not None else self.default_temperature

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self._run_sync, prompt, system_prompt, model,
                    thinking, temp, files, image_src,
                ),
                timeout=timeout,
            )
            return result
        except asyncio.TimeoutError:
            log.warning("Gemini API timed out after %ds (model=%s)", timeout, model)
            return LLMResult(
                text=f"[Timeout after {timeout}s]",
                is_error=True, duration_ms=timeout * 1000,
            )
        except Exception as e:
            log.error("Gemini API error (model=%s): %s", model, e)
            return LLMResult(text=f"[Error: {e}]", is_error=True)

    def _run_sync(
        self, prompt, system_prompt, model, thinking, temperature, files, image_src,
    ) -> LLMResult:
        from google.genai import types
        import requests as req_lib

        client = self._get_client()
        model_id = MODELS.get(model, model)

        # Thinking config - never mix thinking_level and thinking_budget
        thinking_config = None
        if model.startswith("3"):
            # 3-series: use thinking_level
            level = thinking or "high"
            valid = THINKING_LEVELS.get(model, ("low", "medium", "high"))
            if level not in valid:
                level = valid[-1]  # default to highest available
            thinking_config = types.ThinkingConfig(thinking_level=level)
        else:
            # 2.5-series: use thinking_budget
            if thinking:
                thinking_config = types.ThinkingConfig(
                    thinking_budget=-1, include_thoughts=True
                )
            else:
                thinking_config = types.ThinkingConfig(
                    thinking_budget=0, include_thoughts=False
                )

        # Build contents
        parts = []

        # File attachments — inline bytes for small files, Files API for large
        if files:
            for file_path in files:
                mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
                size = os.path.getsize(file_path)
                if size < 10 * 1024 * 1024:  # <10MB: inline bytes (faster)
                    with open(file_path, "rb") as f:
                        parts.append(types.Part.from_bytes(data=f.read(), mime_type=mime))
                else:  # >=10MB: Files API upload
                    uploaded = client.files.upload(file=file_path)
                    parts.append(types.Part.from_uri(
                        file_uri=uploaded.uri, mime_type=uploaded.mime_type,
                    ))
                    self._uploaded_files.append((uploaded.name, time.time()))

        # Image handling (inline bytes)
        if image_src:
            if image_src.startswith(("http://", "https://")):
                img_bytes = req_lib.get(image_src, timeout=30).content
            else:
                with open(image_src, "rb") as f:
                    img_bytes = f.read()
            # Detect mime type
            mime = "image/jpeg"
            if image_src.lower().endswith(".png"):
                mime = "image/png"
            elif image_src.lower().endswith(".webp"):
                mime = "image/webp"
            parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))

        parts.append(types.Part.from_text(text=prompt))

        contents = types.Content(role="user", parts=parts)

        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            thinking_config=thinking_config,
        )

        start = time.monotonic()
        response = client.models.generate_content(
            model=model_id, config=config, contents=contents
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        # Parse response
        text = response.text or ""
        usage = response.usage_metadata
        input_tokens = usage.prompt_token_count or 0 if usage else 0
        thoughts = usage.thoughts_token_count or 0 if usage else 0
        candidates = usage.candidates_token_count or 0 if usage else 0
        output_tokens = candidates + thoughts

        # Cost calculation
        pricing = PRICING.get(model, {"input": 0, "output": 0})
        cost = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000

        # Opportunistic cleanup of expired uploaded files
        self._cleanup_expired_files()

        return LLMResult(
            text=text,
            duration_ms=duration_ms,
            cost_usd=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _cleanup_expired_files(self):
        """Delete uploaded files older than file_ttl_days."""
        if not self._uploaded_files:
            return
        ttl_seconds = self.file_ttl_days * 86400
        now = time.time()
        still_valid = []
        client = self._get_client()
        for name, upload_time in self._uploaded_files:
            if now - upload_time > ttl_seconds:
                try:
                    client.files.delete(name=name)
                    log.info("Deleted expired Gemini file: %s", name)
                except Exception as e:
                    log.debug("Failed to delete Gemini file %s: %s", name, e)
            else:
                still_valid.append((name, upload_time))
        self._uploaded_files = still_valid
