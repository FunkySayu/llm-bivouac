"""Clients for the two small outside services: ntfy and Authentik."""

from __future__ import annotations

import logging
import time

import aiohttp

from .config import Config

log = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=10)


class Notifier:
    def __init__(self, config: Config, session: aiohttp.ClientSession):
        self._url = f"{config.ntfy_url}/{config.ntfy_topic}"
        self._token = config.ntfy_publish_token
        self._session = session

    async def send(self, title: str, message: str, *, urgent: bool = False) -> None:
        """Send a notification. A failure is logged: it must never block a teardown."""
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Title": title.encode("ascii", "replace").decode(),
            "Priority": "urgent" if urgent else "default",
        }
        try:
            async with self._session.post(
                self._url, data=message.encode(), headers=headers, timeout=TIMEOUT
            ) as response:
                if response.status >= 400:
                    log.warning("ntfy returned %s for %r", response.status, title)
        except (aiohttp.ClientError, TimeoutError) as error:
            log.warning("could not send ntfy notification %r: %s", title, error)


class AccessUnavailable(Exception):
    def __str__(self) -> str:
        return "Could not check access with Authentik; try again shortly"


class AccessChecker:
    """Checks ai-lab membership by email, caching each answer briefly."""

    def __init__(self, config: Config, session: aiohttp.ClientSession):
        self._url = f"{config.authentik_url}/api/v3/core/users/"
        self._token = config.authentik_api_token
        self._group = config.authentik_group
        self._ttl = config.authentik_cache_seconds
        self._session = session
        self._cache: dict[str, tuple[float, bool]] = {}

    async def is_allowed(self, email: str) -> bool:
        key = email.casefold()
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and now - cached[0] < self._ttl:
            return cached[1]
        allowed = await self._lookup(email, key)
        self._cache[key] = (now, allowed)
        return allowed

    async def _lookup(self, email: str, key: str) -> bool:
        try:
            async with self._session.get(
                self._url,
                params={"email": email},
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=TIMEOUT,
            ) as response:
                if response.status != 200:
                    raise AccessUnavailable()
                payload = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            raise AccessUnavailable() from error
        for user in payload.get("results", []):
            if user.get("email", "").casefold() != key or not user.get("is_active", False):
                continue
            if any(g.get("name") == self._group for g in user.get("groups_obj") or []):
                return True
        return False
