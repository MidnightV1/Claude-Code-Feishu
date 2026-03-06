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

log = logging.getLogger("hub.feishu_api")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


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

    def _invalidate_token(self):
        """Force token refresh on next request."""
        self._token = ""
        self._token_expires = 0

    # Token-expired codes returned in 200 responses by Feishu
    _TOKEN_EXPIRED_CODES = {99991663, 99991664, 99991661, 99991668}

    def _check_token_expired(self, data: dict) -> bool:
        """If response indicates token expired, invalidate and return True."""
        if data.get("code") in self._TOKEN_EXPIRED_CODES:
            log.warning("Token expired in response (code=%s), refreshing", data["code"])
            self._invalidate_token()
            return True
        return False

    # ── generic HTTP ───────────────────────────────────────

    @staticmethod
    def _raise_for_status(r: requests.Response) -> None:
        """Like raise_for_status but includes Feishu API error details."""
        if r.ok:
            return
        detail = ""
        try:
            body = r.json()
            code = body.get("code", "?")
            msg = body.get("msg", "")
            detail = f" [feishu code={code}: {msg}]"
        except Exception:
            pass
        raise requests.HTTPError(
            f"{r.status_code} {r.reason} for {r.request.method} {r.url}{detail}",
            response=r)

    def _request(self, method: str, path: str, *,
                 body: dict | None = None,
                 params: dict | None = None) -> dict:
        fn = getattr(requests, method)
        kwargs: dict = {"headers": self._headers(), "params": params, "timeout": 15}
        if method in ("post", "patch", "put"):
            kwargs["json"] = body
        r = fn(f"{self.domain}{path}", **kwargs)
        self._raise_for_status(r)
        data = r.json()
        if self._check_token_expired(data):
            kwargs["headers"] = self._headers()
            r = fn(f"{self.domain}{path}", **kwargs)
            self._raise_for_status(r)
            data = r.json()
        return data

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("get", path, params=params)

    def post(self, path: str, body: dict | None = None,
             params: dict | None = None) -> dict:
        return self._request("post", path, body=body, params=params)

    def patch(self, path: str, body: dict | None = None,
              params: dict | None = None) -> dict:
        return self._request("patch", path, body=body, params=params)

    def delete(self, path: str, params: dict | None = None) -> dict:
        return self._request("delete", path, params=params)

    def download(self, path: str, timeout: int = 30) -> requests.Response:
        """Download raw bytes (images, files). Returns the Response object."""
        r = requests.get(f"{self.domain}{path}",
                         headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        return r


class ContactStore:
    """Local name → open_id mapping, persisted to JSON."""

    def __init__(self, store_path: str | None = None):
        self.path = Path(store_path) if store_path else _PROJECT_ROOT / "data" / "contacts.json"
        self._data: dict = {}
        self._load()

    def _load(self):
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
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
