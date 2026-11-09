from __future__ import annotations

import pytest

from llm_bivouac.config import load_config
from llm_bivouac.ledger import BudgetExceeded, BudgetSummary, LedgerUnavailable
from llm_bivouac.manager import Manager
from llm_bivouac.ovh import GpuQuotaError, OvhError
from llm_bivouac.state import StateStore


class FakeLedger:
    def __init__(self):
        self.available = True
        self.refuse = False
        self.reservations: dict[str, float] = {}
        self.deadlines: dict[str, float] = {}
        self.ended: dict[str, str] = {}
        self.spent = 0.0

    def _check(self):
        if not self.available:
            raise LedgerUnavailable()

    async def reserve(self, *, deployment_id, price_eur_per_hour, started_at, deadline, **_):
        self._check()
        if self.refuse:
            raise BudgetExceeded("over budget")
        self.reservations[deployment_id] = price_eur_per_hour * (deadline - started_at) / 3600
        self.deadlines[deployment_id] = deadline

    async def extend(self, deployment_id, new_deadline, extra_eur):
        self._check()
        if self.refuse:
            raise BudgetExceeded("over budget")
        self.deadlines[deployment_id] = new_deadline

    async def set_deadline(self, deployment_id, deadline):
        self.deadlines[deployment_id] = deadline

    async def end(self, deployment_id, ended_at, reason):
        self._check()
        self.ended[deployment_id] = reason

    async def summary(self):
        self._check()
        return BudgetSummary(1, 100.0, self.spent, 0.0)

    async def spent_this_month(self):
        self._check()
        return self.spent


class FakeOvh:
    def __init__(self):
        self.apps: dict[str, dict] = {}
        self.tokens: dict[str, str] = {}
        self.fail_create: Exception | None = None
        self.fail_delete = False
        self.fail_label = False
        self.usage = 0.0
        self.counter = 0

    def _next(self) -> str:
        self.counter += 1
        return f"id-{self.counter}"

    async def create_operator_token(self, name, selector):
        token_id = self._next()
        self.tokens[token_id] = selector
        return token_id, f"secret-{token_id}"

    async def delete_token(self, token_id):
        self.tokens.pop(token_id, None)

    async def create_app(self, spec):
        if self.fail_create:
            raise self.fail_create
        app_id = self._next()
        self.apps[app_id] = {
            "id": app_id,
            "spec": dict(spec),
            "status": {"state": "QUEUED"},
        }
        return self.apps[app_id]

    async def get_app(self, app_id):
        return self.apps[app_id]

    async def list_apps(self, selector):
        name, value = selector.split("=")
        return [a for a in self.apps.values() if a["spec"]["labels"].get(name) == value]

    async def delete_app(self, app_id):
        if self.fail_delete:
            raise OvhError(500, "boom")
        self.apps.pop(app_id, None)

    async def set_app_label(self, app_id, name, value):
        if self.fail_label:
            raise OvhError(500, "boom")
        self.apps[app_id]["spec"]["labels"][name] = value

    async def current_usage_eur(self):
        return self.usage

    def run(self, app_id, url="https://app.example"):
        self.apps[app_id]["status"] = {"state": "RUNNING", "url": url}


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, bool]] = []

    async def send(self, title, message, *, urgent=False):
        self.sent.append((title, urgent))

    def titles(self):
        return [title for title, _ in self.sent]


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


ENV = {
    "AILAB_BUDGET_POOL_EUR": "100",
    "AILAB_BUDGET_REFUSE_AT_EUR": "90",
    "AILAB_BUDGET_NOTIFY_PERCENT": "50,80,90",
    "AILAB_MAX_GPUS": "4",
    "AILAB_MAX_SPEND_TTL_MULTIPLIER": "2",
    "AILAB_CLI_CONFIRM_EUR_PER_HOUR": "3",
    "AILAB_OWUI_MAX_EUR_PER_HOUR": "6",
    "AILAB_TTL_SECONDS": "3600",
    "AILAB_TTL_WARNING_SECONDS": "600",
    "AILAB_IDLE_TEARDOWN_SECONDS": "1200",
    "AILAB_TEARDOWN_DRAIN_SECONDS": "300",
    "AILAB_LISTEN_PORT": "8000",
    "AILAB_METRICS_PORT": "9100",
    "AILAB_SOCKET_PATH": "/tmp/test.sock",
    "AILAB_PROXY_API_KEY": "proxy-key",
    "VLLM_API_KEY": "backend-key",
    "DATABASE_URL": "postgresql://unused",
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
    def __init__(self, directory):
        self.directory = str(directory)
        self.config = load_config({**ENV, "AILAB_STATE_DIR": self.directory})
        self.ledger = FakeLedger()
        self.ovh = FakeOvh()
        self.notifier = FakeNotifier()
        self.clock = Clock()
        self.healthy = True
        self.store = StateStore(self.directory)
        self.manager = self._manager()

    def _manager(self):
        async def probe(_deployment):
            return self.healthy

        return Manager(
            self.config, self.store, self.ledger, self.ovh, self.notifier, probe, self.clock
        )

    def restart(self):
        self.store = StateStore(self.directory)
        self.manager = self._manager()


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


__all__ = ["GpuQuotaError", "Harness"]
