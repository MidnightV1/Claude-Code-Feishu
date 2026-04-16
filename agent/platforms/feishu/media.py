# -*- coding: utf-8 -*-
"""Media processing mixin — image, file, PDF handling + content parsing.

Extracted from FeishuBot to reduce monolith size.
Used as a mixin: class FeishuBot(MediaMixin, SessionMixin, ...)
"""

import json
import os
import asyncio
import logging

from agent.infra.models import LLMConfig

log = logging.getLogger("hub.feishu_bot")


class MediaMixin:
    """Image download/compression, file handling, PDF parsing, content parsing.

    Expects self to have: _feishu_api, file_store, router, dispatcher.
    """

    # ═══ Image Processing ═══

    async def _process_image(
        self, message_id: str, content_str: str, session_key: str,
        *, image_key: str | None = None,
    ) -> str | None:
        """Download, compress, and store image. Returns absolute stored path.

        PIL compression runs in a subprocess to keep native libraries
        (libwebp, libjpeg) out of the main process — avoids ld.so dlopen
        race conditions when forking Claude CLI.

        If *image_key* is provided directly (e.g. from post message), skip
        parsing content_str.
        """
        if not image_key:
            try:
                content = json.loads(content_str) if isinstance(content_str, str) else {}
            except Exception:
                content = {}
            image_key = content.get("image_key", "") if isinstance(content, dict) else ""
        if not image_key:
            return None

        raw_path = None
        compressed_path = None
        try:
            # Step 1: Download raw image (no PIL, just HTTP)
            raw_path = await asyncio.to_thread(
                self._download_feishu_image_raw, message_id, image_key
            )
            if not raw_path:
                return None

            # Step 2: Compress in isolated subprocess (PIL stays out of main process)
            compressed_path = os.path.expanduser(
                f"~/tmp/feishu_img_{image_key[:16]}.webp"
            )
            import sys
            script = os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts", "compress_image.py")
            proc = await asyncio.create_subprocess_exec(
                sys.executable, script, raw_path, compressed_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            except (asyncio.TimeoutError, Exception):
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                raise
            if proc.returncode != 0:
                log.error("Image compress subprocess failed: %s",
                          stderr.decode(errors="replace")[:300])
                return None

            # Parse compression stats
            try:
                stats = json.loads(stdout.decode())
                log.info("Image compressed: %dKB -> %dKB (webp, max 1024px)",
                         stats.get("orig_kb", 0), stats.get("final_kb", 0))
            except Exception:
                log.info("Image compressed (stats unavailable)")

            # Step 3: Store permanently
            stored_path = self.file_store.save_from_path(
                session_key, compressed_path,
                original_name=f"{image_key[:16]}.webp",
                file_type="image",
            )
            return stored_path
        except Exception as e:
            log.error("Image processing error: %s", e)
            return None
        finally:
            for p in (raw_path, compressed_path):
                if p:
                    try:
                        os.unlink(p)
                    except Exception:
                        pass

    def _download_feishu_image_raw(
        self, message_id: str, image_key: str,
    ) -> str | None:
        """Download raw image from Feishu. No PIL, no native libraries."""
        try:
            resp = self._feishu_api.download(
                f"/open-apis/im/v1/messages/{message_id}/resources/{image_key}?type=image"
            )
            raw_path = os.path.expanduser(f"~/tmp/feishu_raw_{image_key[:16]}.dat")
            os.makedirs(os.path.dirname(raw_path), exist_ok=True)
            with open(raw_path, "wb") as f:
                f.write(resp.content)
            return raw_path
        except Exception as e:
            log.error("Image download error: %s", e)
            return None

    # ═══ File Processing ═══

    # Text-readable file extensions
    _TEXT_EXTS = {
        ".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml",
        ".toml", ".ini", ".cfg", ".conf", ".sh", ".bash", ".zsh",
        ".html", ".css", ".xml", ".csv", ".log", ".sql", ".go",
        ".rs", ".java", ".kt", ".c", ".cpp", ".h", ".hpp", ".rb",
        ".swift", ".r", ".lua", ".pl", ".php", ".env", ".gitignore",
        ".dockerfile", ".makefile",
    }

    async def _process_file(
        self, message_id: str, content_str: str, session_key: str,
    ) -> tuple[str | None, str]:
        """Download file, save to FileStore, parse content. Returns (prompt_text, footer)."""
        try:
            content = json.loads(content_str) if isinstance(content_str, str) else {}
        except Exception:
            content = {}

        file_key = content.get("file_key", "")
        file_name = content.get("file_name", "unknown")
        if not file_key:
            return None, ""

        tmp_path = None
        try:
            tmp_path = await asyncio.to_thread(
                self._download_feishu_file_sync, message_id, file_key, file_name
            )
            if not tmp_path:
                return None, ""

            ext = os.path.splitext(file_name)[1].lower()
            file_type = (
                "pdf" if ext == ".pdf"
                else "text" if ext in self._TEXT_EXTS or ext == ""
                else "other"
            )

            # Save to FileStore permanently
            stored_path = self.file_store.save_from_path(
                session_key, tmp_path,
                original_name=file_name,
                file_type=file_type,
            )

            # PDF → Gemini CLI (subscription) → Gemini API (fallback) → Claude Read
            if ext == ".pdf":
                summary, footer, method = await self._parse_pdf(
                    stored_path, file_name, session_key,
                )
                if summary:
                    skill_hint = (
                        "如需查看文档具体内容，可使用 gemini-doc skill 查询：\n"
                        "`python3 .claude/skills/gemini-doc/scripts/"
                        f'gemini_doc_ctl.py query {stored_path} "问题"`'
                    )
                    return (
                        f"[用户发送了文件: {file_name}]\n"
                        f"文件路径: {stored_path}\n"
                        f"文档摘要:\n{summary}\n\n{skill_hint}",
                        footer,
                    )
                # All parsers failed — CC reads directly
                self.file_store.update_analysis(
                    session_key, os.path.basename(stored_path),
                    "PDF（解析失败，需 Read 工具读取）",
                )
                return (
                    f"[用户发送了文件: {file_name}]\n"
                    f"文件路径: {stored_path}\n"
                    f"PDF 自动解析失败。请用 Read 工具直接读取此文件"
                    f"（每次最多 20 页，用 pages 参数指定页码范围）。",
                    "`parse: fallback to Claude Read`",
                )

            # Text/code → read directly
            elif ext in self._TEXT_EXTS or ext == "":
                with open(stored_path, "r", encoding="utf-8", errors="replace") as f:
                    file_content = f.read()
                if len(file_content) > 10000:
                    file_content = (
                        file_content[:10000]
                        + f"\n\n... [truncated, total {len(file_content)} chars]"
                    )
                return (
                    f"[用户发送了文件: {file_name}]\n```\n{file_content}\n```",
                    "",
                )

            else:
                return (
                    f"[用户发送了文件: {file_name}] "
                    f"(不支持的格式: {ext}，支持 PDF 和文本/代码文件)",
                    "",
                )

        except Exception as e:
            log.error("File processing error: %s", e)
            return None, ""
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def _download_feishu_file_sync(
        self, message_id: str, file_key: str, file_name: str,
    ) -> str | None:
        """Download file from Feishu API. Returns temp file path."""
        try:
            resp = self._feishu_api.download(
                f"/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=file",
                timeout=60,
            )

            safe_name = os.path.basename(file_name)  # prevent path traversal
            tmp_path = os.path.expanduser(
                f"~/tmp/feishu_file_{file_key[:16]}_{safe_name}"
            )
            os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
            with open(tmp_path, "wb") as f:
                f.write(resp.content)
            log.info("File downloaded: %s (%.1fKB)", file_name, len(resp.content) / 1024)
            return tmp_path
        except Exception as e:
            log.error("File download error: %s", e)
            return None

    # ═══ PDF Parsing (Gemini CLI → Gemini API → Claude Read fallback) ═══

    _PDF_SUMMARY_PROMPT = (
        "Analyze this document. Output a structured summary in Chinese:\n"
        "1. 文档类型和主题（一句话）\n"
        "2. 核心内容摘要（3-5 要点）\n"
        "3. 关键数据/结论（如有）\n"
        "4. 文档结构（章节列表）\n"
        "Be concise. Total output under 800 chars."
    )

    async def _parse_pdf(
        self, file_path: str, file_name: str, session_key: str,
    ) -> tuple[str | None, str, str]:
        """Parse PDF via Gemini CLI → Gemini API fallback.

        Returns (summary_text, footer, method).
        summary_text is None if all methods fail.
        """
        prompt = f"File: '{file_name}'. {self._PDF_SUMMARY_PROMPT}"

        # Tier 1: Gemini CLI (subscription, no per-token cost)
        if self.router.gemini_cli.available:
            result = await self.router.gemini_cli.run_with_file(
                prompt, file_path, timeout_seconds=600,
            )
            if not result.is_error and result.text.strip():
                summary = result.text.strip()
                self.file_store.update_analysis(
                    session_key, os.path.basename(file_path), summary[:500],
                )
                footer = f"\n\n`parse: gemini-cli | {result.duration_ms}ms`"
                log.info("PDF parsed via Gemini CLI: %s", file_name)
                return summary, footer, "gemini-cli"
            log.warning("Gemini CLI parse failed for %s: %s",
                        file_name, (result.text or "empty")[:200])

        # Tier 2: Gemini API (paid, but still cheaper than full-text injection)
        if self.router.gemini_api.api_key:
            vision_config = LLMConfig(provider="gemini-api", model="3-Flash")
            result = await self.router.run(
                prompt=prompt, llm_config=vision_config, files=[file_path],
            )
            if not result.is_error and result.text.strip():
                summary = result.text.strip()
                self.file_store.update_analysis(
                    session_key, os.path.basename(file_path), summary[:500],
                )
                footer = ""
                if result.cost_usd > 0:
                    footer = (
                        f"\n\n`parse: gemini-api/3-Flash"
                        f" | ${result.cost_usd:.4f} | {result.duration_ms}ms`"
                    )
                log.info("PDF parsed via Gemini API: %s", file_name)
                return summary, footer, "gemini-api"
            log.warning("Gemini API parse failed for %s: %s",
                        file_name, (result.text or "empty")[:200])

        # Tier 3: All failed
        return None, "", "none"

    # ═══ Content Parsing ═══

    def _parse_content(self, content_str: str, msg_type: str) -> str:
        try:
            content = (
                json.loads(content_str) if isinstance(content_str, str) else content_str
            )
        except json.JSONDecodeError:
            return content_str if isinstance(content_str, str) else ""

        if not isinstance(content, dict):
            return str(content) if content else ""

        if msg_type == "text":
            return content.get("text", "")
        elif msg_type == "post":
            text, _image_keys = self._parse_post_content(content)
            return text
        elif msg_type == "markdown":
            return content.get("text", "")
        elif msg_type == "interactive":
            return self._parse_card_content(content)
        return ""

    def _parse_post_content(self, content: dict) -> tuple[str, list[str]]:
        """Parse post message content, handling both flat and multi-language structures.

        Returns (text, image_keys) tuple.
        """
        # Detect structure: flat {title, content: [[...]]} vs multi-lang {zh_cn: {title, content}}
        if "content" in content and isinstance(content["content"], list):
            # Flat structure (most common from Feishu client)
            return self._extract_post_body(content)
        # Multi-language structure — use first available language
        for lang_content in content.values():
            if isinstance(lang_content, dict) and "content" in lang_content:
                return self._extract_post_body(lang_content)
        return "", []

    def _extract_post_body(self, post: dict) -> tuple[str, list[str]]:
        """Extract text and image_keys from a single post body {title, content: [[elements]]}."""
        lines = []
        image_keys = []
        title = post.get("title")
        if title:
            lines.append(title)
        for para in post.get("content", []):
            if not isinstance(para, list):
                continue
            parts = []
            for elem in para:
                tag = elem.get("tag", "")
                if tag in ("text", "md"):
                    parts.append(elem.get("text", ""))
                elif tag == "a":
                    text = elem.get("text", "")
                    href = elem.get("href", "")
                    parts.append(f"[{text}]({href})" if href else text)
                elif tag == "at":
                    parts.append(elem.get("name", elem.get("key", "")))
                elif tag == "code_block":
                    lang = elem.get("language", "")
                    parts.append(f"```{lang}\n{elem.get('text', '')}\n```")
                elif tag == "emotion":
                    parts.append(f":{elem.get('emoji_type', '')}:")
                elif tag == "img":
                    ik = elem.get("image_key", "")
                    if ik:
                        image_keys.append(ik)
                # media/hr — skip, no text content
            if parts:
                lines.append("".join(parts))
        return "\n".join(lines), image_keys

    @staticmethod
    def _parse_card_content(content: dict) -> str:
        """Extract text from an interactive card (JSON 2.0 schema).

        Our cards wrap markdown in: body.elements[].tag=="markdown" → .content
        API may return a degraded format with nested lists (like post content).
        """
        parts = []
        # JSON 2.0: body.elements[].tag=="markdown"
        for el in content.get("body", {}).get("elements", []):
            if isinstance(el, dict) and el.get("tag") == "markdown":
                parts.append(el.get("content", ""))
        if parts:
            return "\n".join(parts)
        # Fallback: JSON 1.0 legacy cards or degraded API format
        for el in content.get("elements", []):
            if isinstance(el, list):
                # Degraded format: nested paragraphs [[{tag,text},...]]
                for inline in el:
                    if isinstance(inline, dict) and inline.get("tag") == "text":
                        t = inline.get("text", "")
                        if t:
                            parts.append(t)
            elif isinstance(el, dict):
                if el.get("tag") == "markdown":
                    parts.append(el.get("content", ""))
                elif el.get("tag") == "div":
                    text_obj = el.get("text", {})
                    if text_obj.get("tag") == "lark_md":
                        parts.append(text_obj.get("content", ""))
        return "\n".join(parts)

    # ═══ Audio Processing (Voice Messages) ═══

    _VOICE_TRANSCRIBE_PROMPT = (
        "Transcribe this voice message. Output in the speaker's language.\n\n"
        "Rules:\n"
        "1. Remove filler words (嗯、啊、那个、就是说、you know, like, um)\n"
        "2. Fix grammar, organize into clear sentences\n"
        "3. Keep the original meaning — don't add or infer content\n"
        "4. Output ONLY the transcription, no meta-commentary\n\n"
        "If the message is long (>30s) or covers multiple topics:\n"
        "- Separate topics with bullet points\n"
        "- If it contains tasks/instructions, format as:\n"
        "  **指令**: <what to do>\n"
        "  **细节**: <any specifics mentioned>\n"
        "- For rambling or disorganized speech, reorganize by topic while preserving all points"
    )

    async def _process_audio(
        self, message_id: str, content_str: str, session_key: str,
    ) -> str | None:
        """Download voice message and transcribe via Gemini Flash.

        Returns structured transcription text, or None on failure.
        """
        try:
            content = json.loads(content_str) if isinstance(content_str, str) else {}
        except Exception:
            content = {}

        file_key = content.get("file_key", "")
        duration = content.get("duration", 0)  # milliseconds
        if not file_key:
            return None

        tmp_path = None
        try:
            # Step 1: Download audio from Feishu
            tmp_path = await asyncio.to_thread(
                self._download_feishu_audio_sync, message_id, file_key
            )
            if not tmp_path:
                return None

            duration_s = round(duration / 1000) if duration else "?"
            log.info("Audio downloaded: %s (duration=%ss)", file_key[:16], duration_s)

            # Step 2: Gemini API transcription + structuring
            # Gemini CLI @file doesn't support audio; use Gemini API Files upload
            if not self.router.gemini_api.api_key:
                log.warning("Gemini API not configured for audio transcription")
                return None

            voice_config = LLMConfig(
                provider="gemini-api", model="3.1-Flash-Lite",
                thinking="low", timeout_seconds=60,
            )
            result = await self.router.run(
                prompt=self._VOICE_TRANSCRIBE_PROMPT,
                llm_config=voice_config,
                files=[tmp_path],
            )

            if result.is_error or not result.text.strip():
                log.warning("Audio transcription failed: %s",
                            (result.text or "empty")[:200])
                return None

            transcription = result.text.strip()
            log.info("Audio transcribed (%dms): %s", result.duration_ms,
                     transcription[:100])
            return transcription

        except Exception as e:
            log.error("Audio processing error: %s", e)
            return None
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def _download_feishu_audio_sync(
        self, message_id: str, file_key: str,
    ) -> str | None:
        """Download audio file from Feishu API. Returns temp file path."""
        try:
            resp = self._feishu_api.download(
                f"/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=file",
                timeout=30,
            )
            # Must be within project dir for Gemini CLI file sandbox
            _project_root = os.path.normpath(
                os.path.join(os.path.dirname(__file__), "..", "..", "..")
            )
            tmp_path = os.path.join(_project_root, "data", "tmp", f"feishu_audio_{file_key[:16]}.opus")
            os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
            with open(tmp_path, "wb") as f:
                f.write(resp.content)
            log.info("Audio file saved: %.1fKB", len(resp.content) / 1024)
            return tmp_path
        except Exception as e:
            log.error("Audio download error: %s", e)
            return None

    # Feishu API returns this placeholder for interactive cards
    _DEGRADED_PLACEHOLDER = "请升级至最新版本客户端，以查看内容"
