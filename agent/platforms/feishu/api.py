"""Feishu Open API client — shared by hub and skills.

Token caching, generic HTTP methods, contact store.
Credentials resolve from env vars first, then config.yaml.
"""

import json
import os
import threading
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
        self._token_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "FeishuAPI | None":
        """Create from FEISHU_APP_ID / FEISHU_APP_SECRET env vars (injected by bot process)."""
        app_id = os.environ.get("FEISHU_APP_ID", "")
        app_secret = os.environ.get("FEISHU_APP_SECRET", "")
        if app_id and app_secret:
            domain = os.environ.get("FEISHU_DOMAIN", "https://open.feishu.cn")
            return cls(app_id, app_secret, domain)
        return None

    @classmethod
    def from_config(cls, config_path: str | None = None,
                    section: str = "feishu") -> "FeishuAPI":
        # Priority: env vars (per-bot injection) > config.yaml
        instance = cls.from_env()
        if instance:
            return instance
        cfg = _load_config(config_path)
        sec = cfg[section]
        # Support both legacy (feishu.app_id) and multi-bot (feishu.bots[0]) formats
        if "app_id" in sec:
            app_id = sec["app_id"]
            app_secret = sec["app_secret"]
        elif "bots" in sec and sec["bots"]:
            app_id = sec["bots"][0]["app_id"]
            app_secret = sec["bots"][0]["app_secret"]
        else:
            raise KeyError("No app_id found in config (checked feishu.app_id and feishu.bots[0].app_id)")
        return cls(
            app_id=app_id,
            app_secret=app_secret,
            domain=sec.get("domain", "https://open.feishu.cn"),
        )

    # ── token ──────────────────────────────────────────────

    def get_token(self) -> str:
        if self._token and time.time() < self._token_expires:
            return self._token
        with self._token_lock:
            # Double-check after acquiring lock (another thread may have refreshed)
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
        if body is not None:
            kwargs["json"] = body
        url = f"{self.domain}{path}"
        backoff = 1.0
        for attempt in range(4):
            try:
                r = fn(url, **kwargs)
            except (ConnectionError, requests.ConnectionError) as e:
                if attempt >= 3:
                    raise
                log.warning("Connection error on %s %s, retrying in %.2fs (%d/3): %s",
                            method.upper(), path, backoff, attempt + 1, e)
                time.sleep(backoff)
                backoff *= 2
                continue
            if r.status_code == 429:
                if attempt >= 3:
                    self._raise_for_status(r)
                retry_after = r.headers.get("Retry-After", "")
                try:
                    delay = max(float(retry_after), backoff) if retry_after else backoff
                except ValueError:
                    delay = backoff
                log.warning("Rate limited on %s %s, retrying in %.2fs (%d/3)",
                            method.upper(), path, delay, attempt + 1)
                time.sleep(delay)
                backoff *= 2
                continue
            self._raise_for_status(r)
            data = r.json()
            if self._check_token_expired(data):
                kwargs["headers"] = self._headers()
                try:
                    r = fn(url, **kwargs)
                except (ConnectionError, requests.ConnectionError) as e:
                    if attempt >= 3:
                        raise
                    log.warning("Connection error on %s %s after token refresh, retrying in %.2fs (%d/3): %s",
                                method.upper(), path, backoff, attempt + 1, e)
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                if r.status_code == 429:
                    if attempt >= 3:
                        self._raise_for_status(r)
                    retry_after = r.headers.get("Retry-After", "")
                    try:
                        delay = max(float(retry_after), backoff) if retry_after else backoff
                    except ValueError:
                        delay = backoff
                    log.warning("Rate limited on %s %s after token refresh, retrying in %.2fs (%d/3)",
                                method.upper(), path, delay, attempt + 1)
                    time.sleep(delay)
                    backoff *= 2
                    continue
                self._raise_for_status(r)
                data = r.json()
            return data
        return {}

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("get", path, params=params)

    def post(self, path: str, body: dict | None = None,
             params: dict | None = None) -> dict:
        return self._request("post", path, body=body, params=params)

    def put(self, path: str, body: dict | None = None,
            params: dict | None = None) -> dict:
        return self._request("put", path, body=body, params=params)

    def patch(self, path: str, body: dict | None = None,
              params: dict | None = None) -> dict:
        return self._request("patch", path, body=body, params=params)

    def delete(self, path: str, body: dict | None = None,
               params: dict | None = None) -> dict:
        return self._request("delete", path, body=body, params=params)

    def download(self, path: str, timeout: int = 30) -> requests.Response:
        """Download raw bytes (images, files). Returns the Response object."""
        r = requests.get(f"{self.domain}{path}",
                         headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        return r

    def upload(self, path: str, file_path: str, form_data: dict,
               *, field_name: str = "image", timeout: int = 30) -> dict:
        """Upload a file via multipart/form-data.

        field_name: "image" for im/v1/images, "file" for im/v1/files.
        """
        headers = self._headers()
        headers.pop("Content-Type", None)  # let requests set multipart boundary
        with open(file_path, "rb") as f:
            files = {field_name: f}
            r = requests.post(
                f"{self.domain}{path}",
                headers=headers, data=form_data, files=files, timeout=timeout,
            )
        self._raise_for_status(r)
        data = r.json()
        if self._check_token_expired(data):
            headers = self._headers()
            headers.pop("Content-Type", None)
            with open(file_path, "rb") as f:
                files = {field_name: f}
                r = requests.post(
                    f"{self.domain}{path}",
                    headers=headers, data=form_data, files=files, timeout=timeout,
                )
            self._raise_for_status(r)
            data = r.json()
        return data


    # ── Chat members ─────────────────────────────────────────

    def get_chat_members(self, chat_id: str, page_size: int = 100) -> list[dict]:
        """Get all members of a group chat. Returns list of {member_id, name, member_id_type, tenant_key}.

        Requires scope: im:chat.members:read
        """
        members = []
        page_token = ""
        while True:
            params = {"member_id_type": "open_id", "page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            data = self.get(f"/open-apis/im/v1/chats/{chat_id}/members", params=params)
            if data.get("code") != 0:
                log.warning("get_chat_members failed: %s", data.get("msg"))
                break
            items = data.get("data", {}).get("items", [])
            members.extend(items)
            page_token = data.get("data", {}).get("page_token", "")
            if not data.get("data", {}).get("has_more") or not page_token:
                break
        return members

    # ── IM media convenience methods ────────────────────────

    # File type mapping for im/v1/files
    _IM_FILE_TYPE_MAP = {
        ".pdf": "pdf", ".doc": "doc", ".docx": "doc",
        ".xls": "xls", ".xlsx": "xls", ".csv": "xls",
        ".ppt": "ppt", ".pptx": "ppt",
    }

    def send_image(self, image_path: str, receive_id: str,
                   receive_id_type: str = "open_id") -> str:
        """Upload an image and send it as a message. Returns message_id."""
        import json as _json
        resp = self.upload(
            "/open-apis/im/v1/images", image_path,
            form_data={"image_type": "message"},
        )
        if resp.get("code") != 0:
            raise RuntimeError(f"Image upload failed: {resp.get('msg')}")
        image_key = resp["data"]["image_key"]

        send_resp = self.post(
            "/open-apis/im/v1/messages",
            body={
                "receive_id": receive_id,
                "msg_type": "image",
                "content": _json.dumps({"image_key": image_key}),
            },
            params={"receive_id_type": receive_id_type},
        )
        if send_resp.get("code") != 0:
            raise RuntimeError(f"Image send failed: {send_resp.get('msg')}")
        return send_resp.get("data", {}).get("message_id", "")

    def send_file(self, file_path: str, receive_id: str,
                  receive_id_type: str = "open_id") -> str:
        """Upload a file and send it as a message. Returns message_id."""
        import json as _json
        from pathlib import Path as _Path
        p = _Path(file_path)
        suffix = p.suffix.lower()
        file_type = self._IM_FILE_TYPE_MAP.get(suffix, "stream")

        resp = self.upload(
            "/open-apis/im/v1/files", file_path,
            form_data={"file_type": file_type, "file_name": p.name},
            field_name="file",
        )
        if resp.get("code") != 0:
            raise RuntimeError(f"File upload failed: {resp.get('msg')}")
        file_key = resp["data"]["file_key"]

        send_resp = self.post(
            "/open-apis/im/v1/messages",
            body={
                "receive_id": receive_id,
                "msg_type": "file",
                "content": _json.dumps({"file_key": file_key}),
            },
            params={"receive_id_type": receive_id_type},
        )
        if send_resp.get("code") != 0:
            raise RuntimeError(f"File send failed: {send_resp.get('msg')}")
        return send_resp.get("data", {}).get("message_id", "")

    def send_audio(self, file_path: str, receive_id: str,
                   receive_id_type: str = "open_id", *,
                   duration_ms: int | None = None) -> str:
        """Upload an opus audio file and send it as a voice message. Returns message_id.

        Feishu audio messages require opus format. The file is uploaded via
        /im/v1/files with file_type=opus, then sent as msg_type=audio.
        """
        import json as _json
        from pathlib import Path as _Path
        p = _Path(file_path)

        resp = self.upload(
            "/open-apis/im/v1/files", file_path,
            form_data={"file_type": "opus", "file_name": p.name},
            field_name="file",
            timeout=60,
        )
        if resp.get("code") != 0:
            raise RuntimeError(f"Audio upload failed: {resp.get('msg')}")
        file_key = resp["data"]["file_key"]

        content = {"file_key": file_key}
        if duration_ms:
            content["duration"] = duration_ms

        send_resp = self.post(
            "/open-apis/im/v1/messages",
            body={
                "receive_id": receive_id,
                "msg_type": "audio",
                "content": _json.dumps(content),
            },
            params={"receive_id_type": receive_id_type},
        )
        if send_resp.get("code") != 0:
            raise RuntimeError(f"Audio send failed: {send_resp.get('msg')}")
        return send_resp.get("data", {}).get("message_id", "")

    def send_audio_to_chat(self, file_path: str, chat_id: str, *,
                           duration_ms: int | None = None) -> str:
        """Send audio to a group chat. Convenience wrapper."""
        return self.send_audio(
            file_path, chat_id, receive_id_type="chat_id",
            duration_ms=duration_ms,
        )


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
        tmp.replace(self.path)

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
