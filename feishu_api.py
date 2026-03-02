"""Feishu Open API client — shared by hub and skills.

Token caching, generic HTTP methods, contact store.
Credentials resolve from env vars first, then config.yaml.
"""

import json
import os
import time
import logging
from pathlib import Path

import requests

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent


def _resolve(value: str) -> str:
    """Resolve ${ENV_VAR} patterns, fallback to literal."""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value


def _load_config(config_path: str | None = None) -> dict:
    import yaml

    path = Path(config_path) if config_path else _PROJECT_ROOT / "config.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


class FeishuAPI:
    """Lightweight Feishu REST client with token caching."""

    def __init__(self, app_id: str, app_secret: str,
                 domain: str = "https://open.feishu.cn"):
        self.app_id = _resolve(app_id)
        self.app_secret = _resolve(app_secret)
        self.domain = domain.rstrip("/")
        self._token: str = ""
        self._token_expires: float = 0

    @classmethod
    def from_config(cls, config_path: str | None = None,
                    section: str = "feishu") -> "FeishuAPI":
        cfg = _load_config(config_path)
        sec = cfg[section]
        return cls(
            app_id=sec["app_id"],
            app_secret=sec["app_secret"],
            domain=sec.get("domain", "https://open.feishu.cn"),
        )

    # ── token ──────────────────────────────────────────────

    def get_token(self) -> str:
        if self._token and time.time() < self._token_expires:
            return self._token
        r = requests.post(
            f"{self.domain}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        self._token = data["tenant_access_token"]
        self._token_expires = time.time() + data.get("expire", 7200) - 60
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.get_token()}",
            "Content-Type": "application/json",
        }

    # ── generic HTTP ───────────────────────────────────────

    def get(self, path: str, params: dict | None = None) -> dict:
        r = requests.get(f"{self.domain}{path}",
                         headers=self._headers(), params=params, timeout=15)
        return r.json()

    def post(self, path: str, body: dict | None = None,
             params: dict | None = None) -> dict:
        r = requests.post(f"{self.domain}{path}",
                          headers=self._headers(), json=body, params=params,
                          timeout=15)
        return r.json()

    def patch(self, path: str, body: dict | None = None,
              params: dict | None = None) -> dict:
        r = requests.patch(f"{self.domain}{path}",
                           headers=self._headers(), json=body, params=params,
                           timeout=15)
        return r.json()

    def delete(self, path: str, params: dict | None = None) -> dict:
        r = requests.delete(f"{self.domain}{path}",
                            headers=self._headers(), params=params, timeout=15)
        return r.json()


class ContactStore:
    """Local name → open_id mapping, persisted to JSON."""

    def __init__(self, store_path: str | None = None):
        self.path = Path(store_path) if store_path else _PROJECT_ROOT / "data" / "contacts.json"
        self._data: dict = {}
        self._load()

    def _load(self):
        if self.path.exists():
            self._data = json.loads(self.path.read_text())
        else:
            self._data = {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2))
        tmp.rename(self.path)

    def lookup(self, name: str) -> str | None:
        """Find open_id by name (exact or substring match)."""
        # exact
        if name in self._data:
            return self._data[name]["open_id"]
        # substring
        for k, v in self._data.items():
            if name in k:
                return v["open_id"]
        return None

    def lookup_name(self, open_id: str) -> str | None:
        for k, v in self._data.items():
            if v["open_id"] == open_id:
                return k
        return None

    def add(self, name: str, open_id: str, source: str = "manual"):
        self._data[name] = {"open_id": open_id, "source": source}
        self._save()

    def remove(self, name: str) -> bool:
        if name in self._data:
            del self._data[name]
            self._save()
            return True
        return False

    def list_all(self) -> dict:
        return dict(self._data)
