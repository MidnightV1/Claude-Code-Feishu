# -*- coding: utf-8 -*-
"""User identity store — tracks users, roles, and preferences.

Storage: JSONL-friendly single JSON file (data/users.json), per-key atomic updates.
Fields are flat / JSON-string for future SQLite migration.
"""

import time
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

from agent.infra.store import load_json, update_json_key

log = logging.getLogger("hub.user_store")


# ═══ User Model ═══

@dataclass
class User:
    open_id: str = ""
    name: str = ""                          # display name (from Feishu profile)
    role: str = "user"                      # "admin" | "user"
    preferences: dict = field(default_factory=dict)   # model prefs, language, etc.
    workspace: dict = field(default_factory=dict)      # recent docs, tasks, context
    created_at: float = 0.0
    updated_at: float = 0.0

    def is_admin(self) -> bool:
        return self.role == "admin"


def user_from_dict(d: dict) -> User:
    if d is None:
        return User()
    return User(**{k: v for k, v in d.items() if k in User.__dataclass_fields__})


def user_to_dict(u: User) -> dict:
    return asdict(u)


# ═══ UserStore ═══

class UserStore:
    def __init__(self, path: str = "data/users.json", feishu_api=None):
        self.path = path
        self.feishu_api = feishu_api        # optional: for fetching user profiles
        self._users: dict[str, User] = {}   # open_id -> User

    async def load(self):
        raw = await load_json(self.path, {})
        self._users = {k: user_from_dict(v) for k, v in raw.items()}
        log.info("UserStore loaded: %d users", len(self._users))

    def get(self, open_id: str) -> Optional[User]:
        return self._users.get(open_id)

    async def get_or_create(self, open_id: str, name: str = "") -> User:
        """Get existing user or create new one. Fetches name from Feishu if needed."""
        user = self._users.get(open_id)
        if user:
            # Backfill name if it was stored as open_id prefix
            if user.name.startswith("ou_") and self.feishu_api:
                fetched = await self._fetch_name(open_id)
                if fetched:
                    user.name = fetched
                    await self.update(user)
                    log.info("Backfilled name: %s → %s", open_id[:10], fetched)
            return user

        # New user — try to fetch display name
        display_name = name
        if not display_name and self.feishu_api:
            display_name = await self._fetch_name(open_id)

        now = time.time()
        user = User(
            open_id=open_id,
            name=display_name or open_id[:10],
            role="user",
            created_at=now,
            updated_at=now,
        )
        self._users[open_id] = user
        await self._save_user(user)
        log.info("New user created: %s (%s)", user.name, open_id[:10])
        return user

    async def update(self, user: User):
        """Save user changes."""
        user.updated_at = time.time()
        self._users[user.open_id] = user
        await self._save_user(user)

    async def set_role(self, open_id: str, role: str):
        """Set user role (admin/user)."""
        user = self._users.get(open_id)
        if user:
            user.role = role
            await self.update(user)

    def get_admin_ids(self) -> set[str]:
        """Return set of admin open_ids."""
        return {uid for uid, u in self._users.items() if u.is_admin()}

    def list_users(self) -> list[User]:
        return list(self._users.values())

    async def _save_user(self, user: User):
        await update_json_key(self.path, user.open_id, user_to_dict(user))

    async def _fetch_name(self, open_id: str) -> str:
        """Fetch user display name from Feishu API."""
        try:
            import asyncio
            resp = await asyncio.to_thread(
                self.feishu_api.get,
                f"/open-apis/contact/v3/users/{open_id}",
                params={"user_id_type": "open_id"},
            )
            data = resp.get("data", {}).get("user", {})
            return data.get("name", "")
        except Exception as e:
            log.warning("Failed to fetch user name for %s: %s", open_id[:10], e)
            return ""
