"""Client for the OVH Public Cloud API. Only this proxy talks to OVH."""

from __future__ import annotations

import time
from typing import Any

import aiohttp

from .app_spec import MANAGED_LABEL, Instance, parse_instance
from .config import Config

OPERATOR_ROLE = "ai_training_operator"
TOKEN_REFRESH_MARGIN_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 30


class OvhError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"OVH API returned {status}: {message}")
        self.status = status
        self.message = message


class GpuQuotaError(OvhError):
    """OVH's 402, which means the project's GPU quota is too low."""

    def __str__(self) -> str:
        return (
            "OVH refused the app because the project's GPU quota is exhausted "
            "(ticket CS17114051 asks for 4 GPUs in GRA). CPU catalog entries still work."
        )


class OvhClient:
    def __init__(self, config: Config, session: aiohttp.ClientSession):
        self._config = config
        self._session = session
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._base = f"{config.ovh_api_base_url}/cloud/project/{config.ovh_project_id}"

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        async with self._session.post(
            self._config.ovh_token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._config.ovh_client_id,
                "client_secret": self._config.ovh_client_secret,
                "scope": "all",
            },
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
        ) as response:
            if response.status != 200:
                # The body is not logged: an OAuth error can echo the request.
                raise OvhError(response.status, "OAuth2 login failed")
            payload = await response.json()
        self._token = payload["access_token"]
        self._token_expires_at = (
            time.monotonic() + int(payload["expires_in"]) - TOKEN_REFRESH_MARGIN_SECONDS
        )
        return self._token

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(2):
            token = await self._access_token()
            async with self._session.request(
                method,
                self._base + path,
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
                **kwargs,
            ) as response:
                if response.status == 401 and attempt == 0:
                    self._token = None
                    continue
                if response.status >= 400:
                    message = await _error_message(response)
                    if response.status == 402:
                        raise GpuQuotaError(402, message)
                    raise OvhError(response.status, message)
                if response.status == 204:
                    return None
                return await response.json(content_type=None)
        raise AssertionError("unreachable")

    async def create_app(self, spec: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/ai/app", json=spec)

    async def get_instance(self, app_id: str) -> Instance | None:
        """A managed app as OVH sees it now, or None once it no longer exists."""
        try:
            app = await self._request("GET", f"/ai/app/{app_id}")
        except OvhError as error:
            if error.status == 404:
                return None
            raise
        return None if app["status"].get("state") == "DELETED" else parse_instance(app)

    async def list_apps(self, label_selector: str) -> list[dict[str, Any]]:
        return await self._request("GET", "/ai/app", params={"labelSelector": label_selector})

    async def list_instances(self) -> list[Instance]:
        apps = await self.list_apps(f"{MANAGED_LABEL}=true")
        return [parse_instance(a) for a in apps if a["status"].get("state") != "DELETED"]

    async def delete_app(self, app_id: str) -> None:
        """Delete an app, killing it if it runs. An app already gone counts as deleted.

        Without force, OVH does not kill a running app. A successful answer only
        means the request was accepted: the app is gone once OVH stops reporting it.
        """
        try:
            await self._request("DELETE", f"/ai/app/{app_id}", params={"force": "true"})
        except OvhError as error:
            if error.status != 404:
                raise

    async def set_app_label(self, app_id: str, name: str, value: str) -> None:
        await self._request("PUT", f"/ai/app/{app_id}/label", json={"name": name, "value": value})

    async def list_tokens(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/ai/token")

    async def create_operator_token(self, name: str, label_selector: str) -> str:
        """Create a token limited to the matching apps and return its secret value."""
        token = await self._request(
            "POST",
            "/ai/token",
            json={
                "name": name,
                "region": self._config.ovh_region,
                "role": OPERATOR_ROLE,
                "labelSelector": label_selector,
            },
        )
        return token["status"]["value"]

    async def delete_token(self, token_id: str) -> None:
        try:
            await self._request("DELETE", f"/ai/token/{token_id}")
        except OvhError as error:
            if error.status != 404:
                raise

    async def quota(self) -> dict[str, Any]:
        return await self._request("GET", "/ai/capabilities/quota")

    async def current_usage_eur(self) -> float:
        """Month-to-date project cost. A project with no billing yet counts as zero."""
        try:
            usage = await self._request("GET", "/usage/current")
        except OvhError as error:
            if error.status == 404 and "no usages found" in error.message.lower():
                return 0.0
            raise
        total = usage.get("totalPrice") or {}
        return float(total.get("value") or 0.0)


async def _error_message(response: aiohttp.ClientResponse) -> str:
    try:
        payload = await response.json(content_type=None)
        return str(payload.get("message", ""))[:300]
    except (ValueError, AttributeError, aiohttp.ClientError):
        return f"HTTP {response.status}"
