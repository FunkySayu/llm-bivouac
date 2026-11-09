from __future__ import annotations

from datetime import UTC, datetime

import pytest

from llm_bivouac.app_spec import MANAGED_LABEL, parse_instance
from llm_bivouac.config import load_config
from llm_bivouac.manager import Health, Manager
from llm_bivouac.ovh import OvhError


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeOvh:
    """OVH as the proxy sees it: the only place where app state lives."""

    def __init__(self, clock):
        self.clock = clock
        self.apps: dict[str, dict] = {}
        self.tokens: dict[str, dict] = {}
        self.fail_create: Exception | None = None
        self.fail_delete = False
        self.ignore_delete = False  # OVH accepts the delete but the app stays
        self.delete_calls: list[str] = []
        self.fail_label = False
        self.fail_list = False
        self.fail_usage = False
        self.usage = 0.0
        self.counter = 0

    def _next(self) -> str:
        self.counter += 1
        return f"id-{self.counter}"

    async def create_operator_token(self, name, selector):
        token_id = self._next()
        self.tokens[token_id] = {"id": token_id, "spec": {"name": name, "labelSelector": selector}}
        return f"secret-{token_id}"

    async def list_tokens(self):
        return list(self.tokens.values())

    async def delete_token(self, token_id):
        self.tokens.pop(token_id, None)

    async def create_app(self, spec):
        if self.fail_create:
            raise self.fail_create
        app_id = self._next()
        self.apps[app_id] = {
            "id": app_id,
            "createdAt": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
            "spec": dict(spec, labels=dict(spec["labels"])),
            "status": {"state": "QUEUED"},
        }
        return self.apps[app_id]

    async def get_instance(self, app_id):
        return parse_instance(self.apps[app_id]) if app_id in self.apps else None

    async def list_apps(self, selector):
        if self.fail_list:
            raise OvhError(500, "boom")
        name, value = selector.split("=")
        return [a for a in self.apps.values() if a["spec"]["labels"].get(name) == value]

    async def list_instances(self):
        return [parse_instance(a) for a in await self.list_apps(f"{MANAGED_LABEL}=true")]

    async def delete_app(self, app_id):
        if self.fail_delete:
            raise OvhError(500, "boom")
        self.delete_calls.append(app_id)
        if not self.ignore_delete:
            self.apps.pop(app_id, None)

    async def set_app_label(self, app_id, name, value):
        if self.fail_label:
            raise OvhError(500, "boom")
        self.apps[app_id]["spec"]["labels"][name] = value

    async def current_usage_eur(self):
        if self.fail_usage:
            raise OvhError(500, "boom")
        return self.usage

    async def quota(self):
        return {}

    def run(self, app_id, url="https://app.example"):
        self.apps[app_id]["status"] = {"state": "RUNNING", "url": url}


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, bool]] = []

    async def send(self, title, message, *, urgent=False):
        self.sent.append((title, urgent))

    def titles(self):
        return [title for title, _ in self.sent]


ENV = {
    "AILAB_BUDGET_POOL_EUR": "100",
    "AILAB_BUDGET_REFUSE_AT_EUR": "90",
    "AILAB_BUDGET_NOTIFY_PERCENT": "50,80,90",
    "AILAB_MAX_GPUS": "4",
    "AILAB_MAX_SPEND_TTL_MULTIPLIER": "2",
    "AILAB_CLI_CONFIRM_EUR_PER_HOUR": "3",
    "AILAB_TTL_SECONDS": "3600",
    "AILAB_TTL_WARNING_SECONDS": "600",
    "AILAB_IDLE_TEARDOWN_SECONDS": "1200",
    "AILAB_TEARDOWN_DRAIN_SECONDS": "300",
    "AILAB_LISTEN_PORT": "8000",
    "AILAB_METRICS_PORT": "9100",
    "AILAB_SOCKET_PATH": "/tmp/test.sock",
    "AILAB_PROXY_API_KEY": "proxy-key",
    "VLLM_API_KEY": "backend-key",
    "OVH_API_BASE_URL": "https://ovh.example/1.0",
    "OVH_REGION": "GRA",
    "OVH_PROJECT_ID": "project",
    "OVH_CLIENT_ID": "client",
    "OVH_CLIENT_SECRET": "secret",
    "AUTHENTIK_URL": "https://auth.example",
    "AUTHENTIK_API_TOKEN": "token",
    "AUTHENTIK_GROUP": "ai-lab",
    "AUTHENTIK_CACHE_SECONDS": "60",
    "NTFY_URL": "http://ntfy",
    "NTFY_TOPIC": "ailab",
    "NTFY_PUBLISH_TOKEN": "token",
}


class Harness:
    def __init__(self):
        self.config = load_config(ENV)
        self.clock = Clock()
        self.ovh = FakeOvh(self.clock)
        self.notifier = FakeNotifier()
        self.healthy = True
        self.manager = self._manager()

    def _manager(self):
        async def probe(_instance):
            return Health(self.healthy, "healthy" if self.healthy else "HTTP 503 Loading model")

        return Manager(self.config, self.ovh, self.notifier, probe, self.clock)

    def restart(self):
        """A new process: nothing survives except what OVH holds."""
        self.manager = self._manager()


@pytest.fixture
def harness():
    return Harness()
