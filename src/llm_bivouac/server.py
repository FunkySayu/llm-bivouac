"""HTTP surfaces: the OpenAI-compatible API, the root-only admin socket and metrics."""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

import aiohttp
from aiohttp import web

from .catalog import CATALOG
from .config import Config
from .ledger import BudgetExceeded, LedgerUnavailable
from .manager import Conflict, Manager, NotFound
from .ovh import GpuQuotaError, OvhError
from .services import AccessChecker, AccessUnavailable
from .state import Deployment

log = logging.getLogger(__name__)

EMAIL_HEADER = "X-OpenWebUI-User-Email"
MAX_REQUEST_BYTES = 64 * 1024 * 1024
UPSTREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=900)
PROBE_TIMEOUT = aiohttp.ClientTimeout(total=10)
FORWARDED_RESPONSE_HEADERS = ("Content-Type", "Cache-Control")


def upstream_headers(deployment: Deployment, backend_api_key: str) -> dict[str, str]:
    # OVH's gateway needs the Authorization token. llama-server accepts that
    # same token if the gateway forwards it, and X-Api-Key if the gateway strips it.
    return {
        "Authorization": f"Bearer {deployment.token_value}",
        "X-Api-Key": backend_api_key,
    }


def make_probe(
    session: aiohttp.ClientSession, config: Config
) -> Callable[[Deployment], Awaitable[bool]]:
    async def probe(deployment: Deployment) -> bool:
        try:
            async with session.get(
                f"{deployment.url}/health",
                headers=upstream_headers(deployment, config.backend_api_key),
                timeout=PROBE_TIMEOUT,
            ) as response:
                return response.status == 200
        except (aiohttp.ClientError, TimeoutError):
            return False

    return probe


def _openai_error(status: int, message: str, kind: str) -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": kind, "code": status}}, status=status
    )


class ApiServer:
    def __init__(
        self,
        config: Config,
        manager: Manager,
        access: AccessChecker,
        session: aiohttp.ClientSession,
    ):
        self._config = config
        self._manager = manager
        self._access = access
        self._session = session

    def app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_REQUEST_BYTES)
        app.router.add_get("/v1/models", self._models)
        for path in ("/v1/chat/completions", "/v1/completions"):
            app.router.add_post(path, self._forward)
        return app

    def _authorized(self, request: web.Request) -> bool:
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        return hmac.compare_digest(supplied.encode(), self._config.proxy_api_key.encode())

    async def _models(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return _openai_error(401, "invalid API key", "authentication_error")
        data = [
            {"id": e.id, "object": "model", "owned_by": "llm-bivouac", "name": e.display_name}
            for e in CATALOG.values()
        ]
        return web.json_response({"object": "list", "data": data})

    async def _forward(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            return _openai_error(401, "invalid API key", "authentication_error")
        email = request.headers.get(EMAIL_HEADER)
        if not email:
            return _openai_error(403, "no user identity on this request", "permission_error")
        try:
            allowed = await self._access.is_allowed(email)
        except AccessUnavailable as error:
            return _openai_error(503, str(error), "service_unavailable")
        if not allowed:
            return _openai_error(403, "you are not in the ai-lab group", "permission_error")

        body = await request.read()
        try:
            model = json.loads(body)["model"]
        except (ValueError, KeyError, TypeError):
            return _openai_error(400, "the request has no model", "invalid_request_error")
        if model not in CATALOG:
            return _openai_error(404, f"unknown model {model}", "invalid_request_error")
        deployment = self._manager.ready_deployment(model)
        if deployment is None:
            return _openai_error(
                409, f"{model} is not running. Start it with: bivouac up {model}", "not_running"
            )
        return await self._stream(request, deployment, body)

    async def _stream(
        self, request: web.Request, deployment: Deployment, body: bytes
    ) -> web.StreamResponse:
        self._manager.begin_request(deployment)
        try:
            headers = upstream_headers(deployment, self._config.backend_api_key)
            headers["Content-Type"] = "application/json"
            try:
                upstream = await self._session.post(
                    f"{deployment.url}{request.path}",
                    data=body,
                    headers=headers,
                    timeout=UPSTREAM_TIMEOUT,
                )
            except (aiohttp.ClientError, TimeoutError) as error:
                log.warning("could not reach deployment %s: %s", deployment.id, error)
                return _openai_error(502, "the model backend is unreachable", "bad_gateway")
            async with upstream:
                response = web.StreamResponse(status=upstream.status)
                for name in FORWARDED_RESPONSE_HEADERS:
                    if name in upstream.headers:
                        response.headers[name] = upstream.headers[name]
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
        finally:
            self._manager.end_request(deployment)


def _describe(manager: Manager, deployment: Deployment) -> dict[str, Any]:
    fields = asdict(deployment)
    del fields["token_value"]
    fields["inflight"] = manager.inflight(deployment.id)
    return fields


def make_admin_app(manager: Manager, ledger: Any, ovh: Any) -> web.Application:
    """Admin API for the CLI. Only reachable through the root-only Unix socket."""

    async def handle(call: Callable[[], Awaitable[Any]]) -> web.Response:
        try:
            return web.json_response(await call())
        except NotFound as error:
            return web.json_response({"error": str(error)}, status=404)
        except (Conflict, BudgetExceeded) as error:
            return web.json_response({"error": str(error)}, status=409)
        except LedgerUnavailable as error:
            return web.json_response({"error": str(error)}, status=503)
        except GpuQuotaError as error:
            return web.json_response({"error": str(error)}, status=402)
        except OvhError as error:
            return web.json_response({"error": str(error)}, status=502)

    async def status(_: web.Request) -> web.Response:
        async def call() -> Any:
            return {"deployments": [_describe(manager, d) for d in manager.deployments]}

        return await handle(call)

    async def catalog(_: web.Request) -> web.Response:
        async def call() -> Any:
            return {"entries": [manager.estimate(entry_id) for entry_id in CATALOG]}

        return await handle(call)

    async def estimate(request: web.Request) -> web.Response:
        async def call() -> Any:
            return manager.estimate(request.match_info["entry"])

        return await handle(call)

    async def up(request: web.Request) -> web.Response:
        payload = await request.json()

        async def call() -> Any:
            deployment = await manager.up(
                request.match_info["entry"],
                "cli",
                max_spend_eur=payload.get("max_spend_eur"),
                keep_when_idle=bool(payload.get("keep_when_idle")),
            )
            return _describe(manager, deployment)

        return await handle(call)

    async def refresh(request: web.Request) -> web.Response:
        async def call() -> Any:
            return _describe(manager, await manager.refresh(request.match_info["entry"]))

        return await handle(call)

    async def down(request: web.Request) -> web.Response:
        async def call() -> Any:
            return _describe(manager, await manager.down(request.match_info["entry"]))

        return await handle(call)

    async def budget(_: web.Request) -> web.Response:
        async def call() -> Any:
            return asdict(await ledger.summary())

        return await handle(call)

    async def history(_: web.Request) -> web.Response:
        return await handle(ledger.history)

    async def renew(_: web.Request) -> web.Response:
        async def call() -> Any:
            return asdict(await ledger.renew())

        return await handle(call)

    async def quota(_: web.Request) -> web.Response:
        return await handle(ovh.quota)

    app = web.Application()
    app.router.add_get("/status", status)
    app.router.add_get("/catalog", catalog)
    app.router.add_get("/estimate/{entry}", estimate)
    app.router.add_post("/up/{entry}", up)
    app.router.add_post("/refresh/{entry}", refresh)
    app.router.add_post("/down/{entry}", down)
    app.router.add_get("/budget", budget)
    app.router.add_get("/budget/history", history)
    app.router.add_post("/budget/renew", renew)
    app.router.add_get("/quota", quota)
    return app


def make_metrics_app(manager: Manager, ledger: Any) -> web.Application:
    async def metrics(_: web.Request) -> web.Response:
        deployments = manager.deployments
        lines = [
            f"ailab_deployments {len(deployments)}",
            f"ailab_requests_inflight {sum(manager.inflight(d.id) for d in deployments)}",
            f"ailab_ledger_available {int(ledger.available)}",
        ]
        try:
            summary = await ledger.summary()
        except LedgerUnavailable:
            pass
        else:
            lines += [
                f"ailab_budget_pool_eur {summary.pool_eur}",
                f"ailab_budget_spent_eur {summary.spent_eur}",
                f"ailab_budget_reserved_eur {summary.reserved_eur}",
            ]
        return web.Response(text="\n".join(lines) + "\n")

    app = web.Application()
    app.router.add_get("/metrics", metrics)
    return app
