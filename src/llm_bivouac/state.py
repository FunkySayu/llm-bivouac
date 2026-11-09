"""Local mirror of live deployments.

Teardown must not depend on Postgres, so this file is what the controller acts
on. It is rewritten atomically after every change and holds per-deployment OVH
tokens, so it is created readable by the service user only.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

STARTING = "starting"
READY = "ready"
TEARING_DOWN = "tearing_down"


@dataclass
class Deployment:
    id: str
    entry_id: str
    price_eur_per_hour: float
    started_at: float
    deadline: float
    last_activity: float
    max_spend_eur: float
    requested_by: str
    token_id: str
    token_value: str
    app_id: str | None = None
    url: str | None = None
    status: str = STARTING
    keep_when_idle: bool = False
    ttl_warned: bool = False
    teardown_reason: str | None = None
    teardown_requested_at: float | None = None
    teardown_failures: int = 0
    last_teardown_attempt: float | None = None

    def cost_so_far(self, now: float) -> float:
        return self.price_eur_per_hour * max(0.0, now - self.started_at) / 3600


@dataclass
class EndedDeployment:
    """A finished deployment whose end Postgres has not recorded yet."""

    id: str
    ended_at: float
    reason: str


@dataclass
class State:
    deployments: dict[str, Deployment] = field(default_factory=dict)
    pending_ledger_ends: list[EndedDeployment] = field(default_factory=list)
    # Pool percentages already announced, and the budget period they belong to.
    notified_percent: list[int] = field(default_factory=list)
    notified_period: int | None = None


class StateStore:
    def __init__(self, directory: str):
        self._path = Path(directory) / "deadlines.json"
        self.state = self._load()

    def _load(self) -> State:
        if not self._path.exists():
            return State()
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return State(
            deployments={key: Deployment(**value) for key, value in raw["deployments"].items()},
            pending_ledger_ends=[EndedDeployment(**e) for e in raw["pending_ledger_ends"]],
            notified_percent=raw["notified_percent"],
            notified_period=raw["notified_period"],
        )

    def save(self) -> None:
        temporary = self._path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(asdict(self.state), handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._path)
