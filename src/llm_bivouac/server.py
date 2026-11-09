"""HTTP surfaces: the OpenAI-compatible API, the root-only admin socket and metrics."""

from __future__ import annotations

import hmac
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from aiohttp import web

from .app_spec import Instance
from .catalog import CATALOG
from .config import Config
from .logs import InstanceLogger, instance_log
from .manager import BudgetExceeded, Conflict, Health, Manager, NotFound
from .ovh import GpuQuotaError, OvhClient, OvhError
from .services import AccessChecker, AccessUnavailable

log = logging.getLogger(__name__)

EMAIL_HEADER = "X-OpenWebUI-User-Email"
CHAT_ID_HEADER = "X-OpenWebUI-Chat-Id"
CHAT_PATH = "/v1/chat/completions"
MAX_REQUEST_BYTES = 64 * 1024 * 1024
# Not /health: OVH's gateway answers that itself with a 204, even for an app
# that does not exist, so it says nothing about the model behind it.
PROBE_PATH = "/v1/models"
HEALTH_DETAIL_CHARS = 160
ERROR_DETAIL_CHARS = 300
UPSTREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=900)
PROBE_TIMEOUT = aiohttp.ClientTimeout(total=10)
FORWARDED_RESPONSE_HEADERS = ("Content-Type", "Cache-Control")


def upstream_headers(instance: Instance, backend_api_key: str) -> dict[str, str]:
    # OVH's gateway needs the Authorization token. llama-server accepts that
    # same token if the gateway forwards it, and X-Api-Key if the gateway strips it.
    return {
        "Authorization": f"Bearer {instance.token}",
        "X-Api-Key": backend_api_key,
    }


def make_probe(
    session: aiohttp.ClientSession, config: Config
) -> Callable[[Instance], Awaitable[Health]]:
    async def probe(instance: Instance) -> Health:
        try:
            async with session.get(
                f"{instance.url}{PROBE_PATH}",
                headers=upstream_headers(instance, config.backend_api_key),
                timeout=PROBE_TIMEOUT,
            ) as response:
                if response.status == 200:
                    return Health(True, "healthy")
                text = await response.text(errors="replace")
                detail = " ".join(text[:HEALTH_DETAIL_CHARS].split())
                return Health(False, f"HTTP {response.status} {detail}".rstrip())
        except (aiohttp.ClientError, TimeoutError) as error:
            return Health(False, f"unreachable: {str(error) or type(error).__name__}")

    return probe


def _openai_error(status: int, message: str, kind: str) -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": kind, "code": status}}, status=status
    )


def _chat_notice(model: str, text: str, stream: bool) -> web.Response:
    """A status message shaped as a normal reply.

    LiteLLM rewrites upstream errors into its own exception text, so a message
    meant for the person chatting has to arrive as a successful completion.
    """
    base = {"id": f"chatcmpl-{uuid.uuid4().hex}", "created": int(time.time()), "model": model}
    if not stream:
        return web.json_response(
            {
                **base,
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        )
    chunk = {**base, "object": "chat.completion.chunk"}
    events = [
        {
            **chunk,
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}
            ],
        },
        {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    return web.Response(text=body, content_type="text/event-stream")


class CompletionObserver:
    """Reads how a response ended, without changing what is forwarded."""

    def __init__(self, event_stream: bool):
        self._event_stream = event_stream
        self._buffer = b""
        self.finish_reason: str | None = None
        self.tokens: int | None = None

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        if self._event_stream:
            *lines, self._buffer = self._buffer.split(b"\n")
            for line in lines:
                self._read(line)

    def close(self) -> str:
        """Read what is left and return the unparsed text, for error reports."""
        self._read(self._buffer)
        return self._buffer.decode(errors="replace")[:ERROR_DETAIL_CHARS]

    def _read(self, data: bytes) -> None:
        # This only observes: bad data must never interrupt the response.
        try:
            event = json.loads(data.strip().removeprefix(b"data:"))
            for choice in event.get("choices") or []:
                self.finish_reason = choice.get("finish_reason") or self.finish_reason
            usage = event.get("usage") or {}
            timings = event.get("timings") or {}
            self.tokens = usage.get("completion_tokens") or timings.get("predicted_n") or self.tokens
        except (ValueError, AttributeError, TypeError):
            pass


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
            log.warning("rejected %s from %s: invalid API key", request.path, request.remote)
            return _openai_error(401, "invalid API key", "authentication_error")
        email = request.headers.get(EMAIL_HEADER)
        if not email:
            log.warning("rejected %s: no user identity", request.path)
            return _openai_error(403, "no user identity on this request", "permission_error")
        try:
            allowed = await self._access.is_allowed(email)
        except AccessUnavailable as error:
            log.warning("rejected request from %s: Authentik is unavailable", email)
            return _openai_error(503, str(error), "service_unavailable")
        if not allowed:
            log.warning("rejected request from %s: not in the access group", email)
            return _openai_error(403, "you are not in the ai-lab group", "permission_error")

        body = await request.read()
        try:
            payload = json.loads(body)
            model = payload["model"]
            stream = bool(payload.get("stream"))
        except (ValueError, KeyError, TypeError, AttributeError):
            log.warning("rejected request from %s: no model in the body", email)
            return _openai_error(400, "the request has no model", "invalid_request_error")
        if model not in CATALOG:
            log.warning("rejected request from %s: unknown model %s", email, model)
            return _openai_error(404, f"unknown model {model}", "invalid_request_error")
        who = f"user={email} chat={request.headers.get(CHAT_ID_HEADER, '-')}"
        instance = self._manager.ready_instance(model)
        if instance is None:
            return self._not_ready(request, model, stream, who)
        messages = payload.get("messages")
        instance_log(instance.deployment_id).info(
            "incoming message: %s messages=%d stream=%s",
            who,
            len(messages) if isinstance(messages, list) else 0,
            stream,
        )
        return await self._stream(request, instance, body, who)

    def _not_ready(
        self, request: web.Request, model: str, stream: bool, who: str
    ) -> web.StreamResponse:
        starting = self._manager.find_active(model)
        if starting is None:
            log.info("%s asked for %s, which is not running", who, model)
            text = f"{model} is not running. Start it with: bivouac up {model}"
        else:
            summary = self._manager.startup_summary(starting)
            instance_log(starting.deployment_id).info(
                "message refused while starting: %s (%s)", who, summary
            )
            text = (
                f"{model} is still starting ({summary}). "
                "Send your message again once `bivouac status` shows it as ready."
            )
        if request.path != CHAT_PATH:
            return _openai_error(409, text, "not_running")
        return _chat_notice(model, text, stream)

    async def _stream(
        self, request: web.Request, instance: Instance, body: bytes, who: str
    ) -> web.StreamResponse:
        ilog = instance_log(instance.deployment_id)
        self._manager.begin_request(instance)
        started = time.monotonic()
        try:
            headers = upstream_headers(instance, self._config.backend_api_key)
            headers["Content-Type"] = "application/json"
            try:
                upstream = await self._session.post(
                    f"{instance.url}{request.path}",
                    data=body,
                    headers=headers,
                    timeout=UPSTREAM_TIMEOUT,
                )
            except (aiohttp.ClientError, TimeoutError) as error:
                ilog.warning("backend unreachable: %s %s", who, error)
                return _openai_error(502, "the model backend is unreachable", "bad_gateway")
            async with upstream:
                response = web.StreamResponse(status=upstream.status)
                for name in FORWARDED_RESPONSE_HEADERS:
                    if name in upstream.headers:
                        response.headers[name] = upstream.headers[name]
                await response.prepare(request)
                observer = CompletionObserver(
                    "text/event-stream" in upstream.headers.get("Content-Type", "")
                )
                try:
                    async for chunk in upstream.content.iter_any():
                        observer.feed(chunk)
                        await response.write(chunk)
                    await response.write_eof()
                except ConnectionResetError:
                    ilog.info(
                        "conversation ended by the client: %s after %.1f s",
                        who,
                        time.monotonic() - started,
                    )
                    raise
                except (aiohttp.ClientError, TimeoutError) as error:
                    ilog.warning(
                        "conversation lost: backend connection failed: %s after %.1f s: %s",
                        who,
                        time.monotonic() - started,
                        error or type(error).__name__,
                    )
                    raise
                self._log_outcome(ilog, who, upstream.status, observer, started)
                return response
        finally:
            self._manager.end_request(instance)

    @staticmethod
    def _log_outcome(
        ilog: InstanceLogger,
        who: str,
        status: int,
        observer: CompletionObserver,
        started: float,
    ) -> None:
        leftover = observer.close()
        duration = time.monotonic() - started
        if status >= 400:
            ilog.warning("backend error: %s HTTP %d: %s", who, status, " ".join(leftover.split()))
        elif observer.finish_reason is None:
            ilog.warning("response ended with no finish reason: %s after %.1f s", who, duration)
        else:
            ilog.info(
                "conversation ended by the model: %s finish_reason=%s tokens=%s duration=%.1fs",
                who,
                observer.finish_reason,
                observer.tokens if observer.tokens is not None else "unknown",
                duration,
            )


@web.middleware
async def _log_admin_commands(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Log every command and refusal. Successful reads are skipped: `up` polls them."""
    response = await handler(request)
    if response.status >= 400:
        detail = response.text if isinstance(response, web.Response) else ""
        log.warning("admin %s %s -> %d %s", request.method, request.path, response.status, detail)
    elif request.method != "GET":
        log.info("admin %s %s -> %d", request.method, request.path, response.status)
    return response


def make_admin_app(manager: Manager, ovh: OvhClient) -> web.Application:
    """Admin API for the CLI. Only reachable through the root-only Unix socket."""

    async def handle(call: Callable[[], Awaitable[Any]]) -> web.Response:
        try:
            return web.json_response(await call())
        except NotFound as error:
            return web.json_response({"error": str(error)}, status=404)
        except (Conflict, BudgetExceeded) as error:
            return web.json_response({"error": str(error)}, status=409)
        except GpuQuotaError as error:
            return web.json_response({"error": str(error)}, status=402)
        except OvhError as error:
            return web.json_response({"error": str(error)}, status=502)

    async def status(_: web.Request) -> web.Response:
        async def call() -> Any:
            return {"instances": [manager.view(i) for i in await ovh.list_instances()]}

        return await handle(call)

    async def instance(request: web.Request) -> web.Response:
        app_id = request.match_info["app_id"]

        async def call() -> Any:
            found = await ovh.get_instance(app_id)
            if found is None:
                reason = manager.ended_reason(app_id) or "reason unknown, see the proxy logs"
                raise NotFound(f"app {app_id} no longer exists on OVH: {reason}")
            return manager.view(found)

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
            started = await manager.up(
                request.match_info["entry"],
                max_spend_eur=payload.get("max_spend_eur"),
                keep_when_idle=bool(payload.get("keep_when_idle")),
            )
            return manager.view(started)

        return await handle(call)

    async def refresh(request: web.Request) -> web.Response:
        async def call() -> Any:
            return manager.view(await manager.refresh(request.match_info["entry"]))

        return await handle(call)

    async def down(request: web.Request) -> web.Response:
        async def call() -> Any:
            return manager.view(await manager.down(request.match_info["entry"]))

        return await handle(call)

    async def budget(_: web.Request) -> web.Response:
        return await handle(manager.budget)

    async def quota(_: web.Request) -> web.Response:
        return await handle(ovh.quota)

    app = web.Application(middlewares=[_log_admin_commands])
    app.router.add_get("/status", status)
    app.router.add_get("/instance/{app_id}", instance)
    app.router.add_get("/catalog", catalog)
    app.router.add_get("/estimate/{entry}", estimate)
    app.router.add_post("/up/{entry}", up)
    app.router.add_post("/refresh/{entry}", refresh)
    app.router.add_post("/down/{entry}", down)
    app.router.add_get("/budget", budget)
    app.router.add_get("/quota", quota)
    return app


def make_metrics_app(manager: Manager) -> web.Application:
    async def metrics(_: web.Request) -> web.Response:
        instances = manager.instances
        lines = [
            f"ailab_deployments {len(instances)}",
            f"ailab_requests_inflight {sum(manager.inflight(i.app_id) for i in instances)}",
        ]
        if manager.last_usage_eur is not None:
            lines.append(f"ailab_ovh_usage_eur {manager.last_usage_eur}")
        return web.Response(text="\n".join(lines) + "\n")

    app = web.Application()
    app.router.add_get("/metrics", metrics)
    return app
