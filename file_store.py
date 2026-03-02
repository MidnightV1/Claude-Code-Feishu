# -*- coding: utf-8 -*-
"""Session-scoped file storage. Files persist permanently for user and agent reference."""

import os
import json
import shutil
import logging
from datetime import datetime

log = logging.getLogger("hub.file_store")


class FileStore:
    def __init__(self, base_dir: str = "data/files"):
        self.base_dir = os.path.expanduser(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)

    def _session_dir(self, session_key: str) -> str:
        safe_key = session_key.replace(":", "__").replace("/", "_")
        d = os.path.join(self.base_dir, safe_key)
        os.makedirs(d, exist_ok=True)
        return d

    def _meta_path(self, session_key: str) -> str:
        return os.path.join(self._session_dir(session_key), "_meta.json")

    def _load_meta(self, session_key: str) -> list[dict]:
        try:
            with open(self._meta_path(session_key), "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save_meta(self, session_key: str, meta: list[dict]):
        with open(self._meta_path(session_key), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

    def save_from_path(
        self,
        session_key: str,
        src_path: str,
        original_name: str | None = None,
        file_type: str = "other",
        analysis: str | None = None,
    ) -> str:
        """Copy file to session storage. Returns stored path."""
        if not original_name:
            original_name = os.path.basename(src_path)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_name = f"{ts}_{original_name}"
        dest = os.path.join(self._session_dir(session_key), dest_name)

        shutil.copy2(src_path, dest)

        meta = self._load_meta(session_key)
        meta.append({
            "filename": dest_name,
            "original_name": original_name,
            "type": file_type,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "analysis": analysis,
            "size_bytes": os.path.getsize(dest),
        })
        self._save_meta(session_key, meta)
        log.info("File stored: %s → %s (%s)", original_name, dest_name, session_key[:20])
        return dest

    def update_analysis(self, session_key: str, filename: str, analysis: str):
        """Update analysis result for a stored file."""
        meta = self._load_meta(session_key)
        for entry in meta:
            if entry["filename"] == filename:
                entry["analysis"] = analysis
                self._save_meta(session_key, meta)
                return

    def list_files(self, session_key: str) -> list[dict]:
        return self._load_meta(session_key)

    def get_context_prompt(self, session_key: str) -> str | None:
        """Generate file context for LLM system prompt injection."""
        files = self._load_meta(session_key)
        if not files:
            return None

        session_dir = self._session_dir(session_key)
        lines = [f"会话文件（路径: {session_dir}/）："]
        for f in files:
            line = f"- {f['original_name']} ({f['type']}, {f['timestamp'][:16]})"
            if f.get("analysis"):
                preview = f["analysis"][:100].replace("\n", " ")
                if len(f["analysis"]) > 100:
                    preview += "..."
                line += f" — {preview}"
            lines.append(line)
        return "\n".join(lines)
