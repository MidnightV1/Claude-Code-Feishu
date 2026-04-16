# -*- coding: utf-8 -*-
"""ErrorTrackerHandler — appends ERROR+ log records to data/error_tracker.jsonl."""

import json
import logging
import os
import threading

# Align with error_scan.py _NOISE_PATTERNS
_NOISE_PATTERNS = [
    "RequestsDependencyWarning",
    "Startup notification",
    "Rate limited:",
]

_lock = threading.Lock()


class ErrorTrackerHandler(logging.Handler):
    """Logging handler that appends ERROR-level records to a JSONL file."""

    def __init__(self, jsonl_path: str = "data/error_tracker.jsonl"):
        super().__init__(level=logging.ERROR)
        self._path = jsonl_path

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            for pattern in _NOISE_PATTERNS:
                if pattern in msg:
                    return

            error_type = record.levelname
            if record.exc_info and record.exc_info[0]:
                error_type = record.exc_info[0].__name__

            entry = {
                "timestamp": record.created,
                "level": record.levelname,
                "logger": record.name,
                "error_type": error_type,
                "message": msg[:500],
            }
            line = json.dumps(entry, ensure_ascii=False)
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with _lock:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass  # Never let a logging handler crash the app
