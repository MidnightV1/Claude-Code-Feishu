# -*- coding: utf-8 -*-
"""Session-scoped file storage with date-based organization.

Files are stored in date subdirectories: data/files/{session_key}/{YYYY-MM-DD}/
Context injection is filtered by recent conversation history to avoid stale files.
"""

import hashlib
import os
import json
import shutil
import logging
import threading
from datetime import datetime

log = logging.getLogger("hub.file_store")


class FileStore:
    def __init__(self, base_dir: str = "data/files"):
        self.base_dir = os.path.expanduser(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)
        self._meta_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _get_meta_lock(self, session_key: str) -> threading.Lock:
        with self._locks_guard:
            if session_key not in self._meta_locks:
                self._meta_locks[session_key] = threading.Lock()
            return self._meta_locks[session_key]

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
        from agent.infra.store import save_json_sync
        save_json_sync(self._meta_path(session_key), meta)

    @staticmethod
    def _file_md5(path: str) -> str:
        """Compute MD5 hex digest of a file."""
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def _resolve_path(self, session_key: str, entry: dict) -> str:
        """Resolve the absolute path for a file entry.

        Handles both old flat layout and new date-subdir layout.
        """
        session_dir = self._session_dir(session_key)
        # New layout: subdir field present
        if entry.get("subdir"):
            return os.path.join(session_dir, entry["subdir"], entry["filename"])
        # Legacy flat layout
        return os.path.join(session_dir, entry["filename"])

    def save_from_path(
        self,
        session_key: str,
        src_path: str,
        original_name: str | None = None,
        file_type: str = "other",
        analysis: str | None = None,
    ) -> str:
        """Copy file to session storage with date subdirectory. Returns stored path.

        Layout: data/files/{session_key}/{YYYY-MM-DD}/{HHMMSS}_{original_name}
        Deduplicates by content hash — returns existing path if identical file
        was already stored in this session.
        """
        if not original_name:
            original_name = os.path.basename(src_path)

        src_hash = self._file_md5(src_path)
        session_dir = self._session_dir(session_key)

        with self._get_meta_lock(session_key):
            meta = self._load_meta(session_key)
            # Content-hash dedup
            for entry in meta:
                if entry.get("content_hash") == src_hash:
                    existing = self._resolve_path(session_key, entry)
                    if os.path.exists(existing):
                        log.info("File dedup: %s already stored as %s",
                                 original_name, entry["filename"])
                        return existing

            now = datetime.now()
            date_dir = now.strftime("%Y-%m-%d")
            time_prefix = now.strftime("%H%M%S")
            dest_name = f"{time_prefix}_{original_name}"

            # Create date subdirectory
            full_dir = os.path.join(session_dir, date_dir)
            os.makedirs(full_dir, exist_ok=True)
            dest = os.path.join(full_dir, dest_name)

            shutil.copy2(src_path, dest)

            meta.append({
                "filename": dest_name,
                "subdir": date_dir,
                "original_name": original_name,
                "type": file_type,
                "timestamp": now.isoformat(timespec="seconds"),
                "analysis": analysis,
                "size_bytes": os.path.getsize(dest),
                "content_hash": src_hash,
            })
            self._save_meta(session_key, meta)

        log.info("File stored: %s → %s/%s (%s)",
                 original_name, date_dir, dest_name, session_key[:20])
        return dest

    def update_analysis(self, session_key: str, filename: str, analysis: str):
        """Update analysis result for a stored file."""
        with self._get_meta_lock(session_key):
            meta = self._load_meta(session_key)
            for entry in meta:
                if entry["filename"] == filename:
                    entry["analysis"] = analysis
                    self._save_meta(session_key, meta)
                    return

    def list_files(self, session_key: str) -> list[dict]:
        return self._load_meta(session_key)

    def get_context_prompt(
        self,
        session_key: str,
        recent_history: list[dict] | None = None,
        summary_text: str | None = None,
    ) -> str | None:
        """Generate file context for LLM system prompt injection.

        Filtering strategy:
        - Files referenced in recent_history (raw text) → full injection with path
        - Files referenced in summary_text → reference link only
        - Other files → not injected (avoids stale context pollution)
        - Fallback (no history info): inject files from last 3 days
        """
        files = self._load_meta(session_key)
        if not files:
            return None

        # Build lookup: path → entry
        path_map = {}
        for f in files:
            path = self._resolve_path(session_key, f)
            path_map[f["filename"]] = (f, path)

        # Classify files by reference source
        recent_files = set()  # filenames referenced in recent raw history
        summary_files = set()  # filenames referenced in compressed summary

        if recent_history:
            history_text = " ".join(m.get("text", "") for m in recent_history)
            for fname, (entry, path) in path_map.items():
                if path in history_text or fname in history_text:
                    recent_files.add(fname)

        if summary_text:
            for fname, (entry, path) in path_map.items():
                if fname not in recent_files:
                    if path in summary_text or fname in summary_text:
                        summary_files.add(fname)

        # Fallback: no history info — use time-based filter (last 3 days)
        has_history = recent_history is not None
        if not has_history:
            from datetime import timedelta
            cutoff = datetime.now() - timedelta(days=3)
            for fname, (entry, path) in path_map.items():
                try:
                    ts = datetime.fromisoformat(entry["timestamp"])
                    if ts >= cutoff:
                        recent_files.add(fname)
                except (ValueError, KeyError):
                    pass

        # Build output
        lines = []
        for fname in list(recent_files) + list(summary_files):
            entry, path = path_map[fname]
            is_ref = fname in summary_files

            if entry['type'] == 'image':
                if is_ref:
                    line = f"- [图片·历史] {path} ({entry['timestamp'][:10]})"
                else:
                    line = f"- [图片] {path} ({entry['timestamp'][:16]})"
                    if entry.get("analysis"):
                        preview = entry["analysis"][:80].replace("\n", " ")
                        if len(entry["analysis"]) > 80:
                            preview += "..."
                        line += f" — {preview}"
            else:
                if is_ref:
                    line = f"- [历史文件] {entry['original_name']} → {path}"
                else:
                    line = f"- {entry['original_name']} ({entry['type']}, {entry['timestamp'][:16]})"
                    if entry.get("analysis"):
                        preview = entry["analysis"][:100].replace("\n", " ")
                        if len(entry["analysis"]) > 100:
                            preview += "..."
                        line += f" — {preview}"
            lines.append(line)

        if not lines:
            return None
        return "会话文件：\n" + "\n".join(lines)
