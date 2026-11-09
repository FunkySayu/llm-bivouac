from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from llm_bivouac.ovh import OvhClient
from llm_bivouac.server import ApiServer, make_admin_app, make_probe
from llm_bivouac.services import AccessUnavailable

ENTRY = "smollm3-3b-q4-cpu"
AUTH = {"Authorization": "Bearer proxy-key"}
USER = {**AUTH, "X-OpenWebUI-User-Email": "me@example.com"}
BODY = {"model": ENTRY, "messages": [], "stream": True}


class Access:
    def __init__(self):
        self.allowed = True
        self.broken = False

    async def is_allowed(self, _email):
        if self.broken:
            raise AccessUnavailable()
        return self.allowed


def with_api(harness, scenario):
    async def main():
        seen = {}

        async def completions(request):
            seen["headers"] = request.headers
            seen["body"] = await request.json()
            seen["inflight"] = harness.manager.inflight(harness.manager.instances[0].app_id)
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            for chunk in seen.get("chunks", (b"data: one\n\n", b"data: two\n\n")):
                await response.write(chunk)
            await response.write_eof()
            return response

        backend_app = web.Application()
        backend_app.router.add_post("/v1/chat/completions", completions)
        backend = TestServer(backend_app)
        await backend.start_server()
        access = Access()
        async with aiohttp.ClientSession() as session:
            api = ApiServer(harness.config, harness.manager, access, session)
            server = TestServer(api.app())
            await server.start_server()
            try:
                await scenario(session, server, backend, access, seen)
            finally:
                await server.close()
                await backend.close()

    asyncio.run(main())


async def make_ready(harness, backend):
    instance = await harness.manager.up(ENTRY)
    harness.ovh.run(instance.app_id, str(backend.make_url("")).rstrip("/"))
    await harness.manager.tick()
    return harness.manager.instances[0]


def test_models_requires_the_key_and_lists_the_catalog(harness):
    async def scenario(session, server, _backend, _access, _seen):
        async with session.get(server.make_url("/v1/models")) as response:
            assert response.status == 401
        async with session.get(server.make_url("/v1/models"), headers=AUTH) as response:
            assert response.status == 200
            payload = await response.json()
        assert [m["id"] for m in payload["data"]] == [ENTRY]

    with_api(harness, scenario)


def test_chat_is_refused_without_identity_or_group(harness):
    async def scenario(session, server, _backend, access, _seen):
        url = server.make_url("/v1/chat/completions")
        async with session.post(url, json=BODY, headers=AUTH) as response:
            assert response.status == 403
        access.allowed = False
        async with session.post(url, json=BODY, headers=USER) as response:
            assert response.status == 403
        access.allowed = True
        access.broken = True
        async with session.post(url, json=BODY, headers=USER) as response:
            assert response.status == 503

    with_api(harness, scenario)


def stream_text(raw: str) -> str:
    assert raw.endswith("data: [DONE]\n\n")
    events = [json.loads(e[len("data: ") :]) for e in raw.split("\n\n")[:-2]]
    assert events[-1]["choices"][0]["finish_reason"] == "stop"
    return "".join(e["choices"][0]["delta"].get("content", "") for e in events)


def test_chat_on_a_stopped_model_replies_with_how_to_start_it(harness):
    async def scenario(session, server, _backend, _access, _seen):
        url = server.make_url("/v1/chat/completions")
        async with session.post(url, json=BODY, headers=USER) as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/event-stream")
            text = stream_text(await response.text())
        assert f"bivouac up {ENTRY}" in text

    with_api(harness, scenario)


def test_chat_while_starting_reports_progress_instead_of_not_running(harness):
    async def scenario(session, server, _backend, _access, _seen):
        instance = await harness.manager.up(ENTRY)
        harness.ovh.apps[instance.app_id]["status"] = {"state": "INITIALIZING"}
        await harness.manager.tick()
        url = server.make_url("/v1/chat/completions")
        body = {**BODY, "stream": False}
        async with session.post(url, json=body, headers=USER) as response:
            assert response.status == 200
            payload = await response.json()
        text = payload["choices"][0]["message"]["content"]
        assert "still starting" in text and "INITIALIZING" in text
        assert "not running" not in text

    with_api(harness, scenario)


def test_legacy_completions_keep_an_http_error_when_not_running(harness):
    async def scenario(session, server, _backend, _access, _seen):
        url = server.make_url("/v1/completions")
        async with session.post(url, json=BODY, headers=USER) as response:
            assert response.status == 409

    with_api(harness, scenario)


def test_conversation_is_logged_from_message_to_finish_reason(harness, caplog):
    finish = {"choices": [{"delta": {}, "finish_reason": "length"}], "usage": {"completion_tokens": 7}}

    async def scenario(session, server, backend, _access, seen):
        instance = await make_ready(harness, backend)
        # Split mid-event: the observer must reassemble it.
        event = f"data: {json.dumps(finish)}\n\n".encode()
        seen["chunks"] = (event[:15], event[15:], b"data: [DONE]\n\n")
        seen["instance"] = instance.deployment_id[:8]
        headers = {**USER, "X-OpenWebUI-Chat-Id": "chat-1"}
        url = server.make_url("/v1/chat/completions")
        async with session.post(url, data=json.dumps(BODY), headers=headers) as response:
            assert await response.read() == b"".join(seen["chunks"])

    with caplog.at_level(logging.INFO, logger="llm_bivouac"):
        with_api(harness, scenario)
    lines = [r.getMessage() for r in caplog.records]
    incoming = next(line for line in lines if "incoming message" in line)
    assert "user=me@example.com chat=chat-1" in incoming
    ended = next(line for line in lines if "conversation ended by the model" in line)
    assert "finish_reason=length" in ended and "tokens=7" in ended
    assert ended.startswith("[")


def test_admin_commands_are_logged_but_status_polls_are_not(harness, caplog):
    async def main():
        server = TestServer(make_admin_app(harness.manager, harness.ovh))
        await server.start_server()
        try:
            async with aiohttp.ClientSession() as session:
                await (await session.post(server.make_url(f"/up/{ENTRY}"), json={})).read()
                await (await session.get(server.make_url("/status"))).read()
                await (await session.post(server.make_url("/down/nope"), json={})).read()
        finally:
            await server.close()

    with caplog.at_level(logging.INFO, logger="llm_bivouac"):
        asyncio.run(main())
    admin = [r.getMessage() for r in caplog.records if r.getMessage().startswith("admin ")]
    assert admin[0] == f"admin POST /up/{ENTRY} -> 200"
    assert "/down/nope -> 404" in admin[1]
    assert len(admin) == 2


def test_probe_reports_why_the_backend_is_not_healthy(harness):
    async def models(_request):
        return web.json_response({"error": {"message": "Loading model"}}, status=503)

    async def gateway_health(_request):
        return web.Response(status=204)

    async def main():
        app = web.Application()
        app.router.add_get("/v1/models", models)
        app.router.add_get("/health", gateway_health)
        server = TestServer(app)
        await server.start_server()
        instance = await harness.manager.up(ENTRY)
        try:
            async with aiohttp.ClientSession() as session:
                probe = make_probe(session, harness.config)
                target = replace(instance, url=str(server.make_url("")).rstrip("/"))
                result = await probe(target)
                await server.close()
                unreachable = await probe(target)
        finally:
            await server.close()
        assert not result.ok and "HTTP 503" in result.detail and "Loading model" in result.detail
        assert not unreachable.ok and unreachable.detail.startswith("unreachable:")

    asyncio.run(main())


def test_chat_streams_through_to_the_backend_with_both_locks(harness):
    async def scenario(session, server, backend, _access, seen):
        instance = await make_ready(harness, backend)
        url = server.make_url("/v1/chat/completions")
        async with session.post(url, data=json.dumps(BODY), headers=USER) as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/event-stream")
            assert await response.read() == b"data: one\n\ndata: two\n\n"
        assert seen["headers"]["Authorization"] == f"Bearer {instance.token}"
        assert seen["headers"]["X-Api-Key"] == "backend-key"
        assert seen["inflight"] == 1
        assert harness.manager.inflight(instance.app_id) == 0

    with_api(harness, scenario)


def test_admin_status_never_exposes_the_token(harness):
    async def main():
        await harness.manager.up(ENTRY)
        server = TestServer(make_admin_app(harness.manager, harness.ovh))
        await server.start_server()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(server.make_url("/status")) as response:
                    text = await response.text()
        finally:
            await server.close()
        assert ENTRY in text
        assert "token_value" not in text and "secret-" not in text

    asyncio.run(main())


def ovh_client(harness, handler):
    """An OvhClient pointed at a local fake of OVH's API and login."""

    async def login(_request):
        return web.json_response({"access_token": "t", "expires_in": 3600})

    app = web.Application()
    app.router.add_post("/token", login)
    app.router.add_route("*", "/1.0/cloud/project/project/{tail:.*}", handler)
    return app


def test_delete_forces_and_a_404_means_the_app_is_gone(harness):
    seen = {}

    async def handler(request):
        if request.method == "DELETE":
            seen["force"] = request.query.get("force")
            return web.Response(status=200)
        return web.json_response({"message": "not found"}, status=404)

    async def main():
        server = TestServer(ovh_client(harness, handler))
        await server.start_server()
        base = str(server.make_url("")).rstrip("/")
        config = replace(
            harness.config, ovh_token_url=f"{base}/token", ovh_api_base_url=f"{base}/1.0"
        )
        try:
            async with aiohttp.ClientSession() as session:
                client = OvhClient(config, session)
                await client.delete_app("running")
                assert seen["force"] == "true"
                assert await client.get_instance("deleted") is None
        finally:
            await server.close()

    asyncio.run(main())


def test_404_no_usage_counts_as_zero_and_402_is_a_quota_error(harness):
    from llm_bivouac.ovh import GpuQuotaError

    async def handler(request):
        if request.path.endswith("/usage/current"):
            return web.json_response({"message": "no usages found"}, status=404)
        return web.json_response({"message": "quota exceeded, please contact us"}, status=402)

    async def main():
        server = TestServer(ovh_client(harness, handler))
        await server.start_server()
        base = str(server.make_url("")).rstrip("/")
        config = replace(
            harness.config, ovh_token_url=f"{base}/token", ovh_api_base_url=f"{base}/1.0"
        )
        try:
            async with aiohttp.ClientSession() as session:
                client = OvhClient(config, session)
                assert await client.current_usage_eur() == 0.0
                try:
                    await client.create_app({})
                except GpuQuotaError as error:
                    assert "CS17114051" in str(error)
                else:
                    raise AssertionError("expected a quota error")
        finally:
            await server.close()

    asyncio.run(main())
