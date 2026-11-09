"""Starts, refreshes and stops OVH apps, and enforces their limits.

OVH is the source of truth. Every decision comes from its list of managed apps
and the labels on them, so a proxy restart loses nothing that matters. Memory
only holds what OVH cannot know: requests in flight, the last activity, health
results and why a stop was requested. The chat path reads the snapshot of the
last tick to avoid a call to OVH per message.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

from .app_spec import (
    DEADLINE_LABEL,
    DEPLOYMENT_LABEL,
    WATCHDOG_MARGIN_SECONDS,
    Instance,
    build_app_spec,
    parse_instance,
)
from .catalog import CATALOG, CatalogEntry
from .config import Config
from .logs import instance_log, short_id

log = logging.getLogger(__name__)

TICK_SECONDS = 15
TEARDOWN_RETRY_SECONDS = 60
TEARDOWN_CONFIRM_SECONDS = 180
TOKEN_CLEANUP_SECONDS = 60
HEALTH_CHECK_SECONDS = 60
USAGE_CHECK_SECONDS = 900
RUNNING_STATE = "RUNNING"
DELETING_STATE = "DELETING"
FAILED_APP_STATES = {"FAILED", "ERROR", "STOPPED", "STOPPING", DELETING_STATE}
ENDED_KEPT = 20


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class BudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class Health:
    ok: bool
    detail: str


Probe = Callable[[Instance], Awaitable[Health]]


class Notifications(Protocol):
    async def send(self, title: str, message: str, *, urgent: bool = False) -> None: ...


@dataclass
class Runtime:
    """What OVH cannot tell about an app. Rebuilt from scratch after a restart."""

    last_activity: float
    inflight: int = 0
    ready: bool = False
    health: str | None = None
    health_checked_at: float = 0.0
    ttl_warned: bool = False
    stop_reason: str | None = None
    stop_requested_at: float = 0.0
    first_delete_at: float | None = None
    last_delete_at: float | None = None
    delete_failures: int = 0
    stop_detail: str | None = None


class Manager:
    def __init__(
        self,
        config: Config,
        ovh: Any,
        notifier: Notifications,
        probe: Probe,
        clock: Callable[[], float] = time.time,
    ):
        self._config = config
        self._ovh = ovh
        self._notifier = notifier
        self._probe = probe
        self._clock = clock
        self._lock = asyncio.Lock()
        self._instances: dict[str, Instance] = {}
        self._runtimes: dict[str, Runtime] = {}
        self._ended: deque[tuple[str, str]] = deque(maxlen=ENDED_KEPT)
        self._last_token_cleanup = 0.0
        self._last_usage_check = 0.0
        self._notified_budget: set[tuple[str, int]] = set()
        self.last_usage_eur: float | None = None

    # --- Reading ---

    @property
    def instances(self) -> list[Instance]:
        return list(self._instances.values())

    def _runtime(self, app_id: str) -> Runtime:
        return self._runtimes.setdefault(app_id, Runtime(last_activity=self._clock()))

    def inflight(self, app_id: str) -> int:
        runtime = self._runtimes.get(app_id)
        return runtime.inflight if runtime else 0

    def _stopping(self, instance: Instance, now: float) -> bool:
        runtime = self._runtimes.get(instance.app_id)
        return bool(runtime and runtime.stop_reason) or now >= instance.deadline

    def find_active(self, entry_id: str) -> Instance | None:
        now = self._clock()
        for instance in self._instances.values():
            if instance.entry_id == entry_id and not self._stopping(instance, now):
                return instance
        return None

    def ready_instance(self, entry_id: str) -> Instance | None:
        instance = self.find_active(entry_id)
        return instance if instance and self._runtime(instance.app_id).ready else None

    def startup_summary(self, instance: Instance) -> str:
        parts = [f"{max(0, int(self._clock() - instance.created_at))} s so far"]
        if instance.state:
            parts.append(f"OVH state {instance.state}")
        health = self._runtime(instance.app_id).health
        if health:
            parts.append(f"health: {health}")
        return ", ".join(parts)

    def view(self, instance: Instance) -> dict[str, Any]:
        """An instance as shown to the CLI. Never includes its token."""
        runtime = self._runtime(instance.app_id)
        entry = CATALOG.get(instance.entry_id)
        return {
            "app_id": instance.app_id,
            "instance": short_id(instance.deployment_id),
            "entry_id": instance.entry_id,
            "state": instance.state,
            "info": instance.info,
            "ready": runtime.ready,
            "health": runtime.health,
            "deadline": instance.deadline,
            "eur_per_hour": entry.price_eur_per_hour if entry else 0.0,
            "inflight": runtime.inflight,
            "stop_reason": runtime.stop_reason,
            "stop_detail": runtime.stop_detail,
        }

    def ended_reason(self, app_id: str) -> str | None:
        return next((reason for ended, reason in self._ended if ended == app_id), None)

    def estimate(self, entry_id: str) -> dict[str, Any]:
        entry = self._entry(entry_id)
        return {
            "entry_id": entry.id,
            "display_name": entry.display_name,
            "eur_per_hour": entry.price_eur_per_hour,
            "ttl_cost_eur": entry.price_eur_per_hour * self._config.ttl_seconds / 3600,
            "confirm_required": entry.price_eur_per_hour >= self._config.cli_confirm_eur_per_hour,
        }

    async def budget(self) -> dict[str, float]:
        instances = await self._ovh.list_instances()
        return {
            "pool_eur": self._config.budget_pool_eur,
            "refuse_at_eur": self._config.budget_refuse_at_eur,
            "used_eur": await self._ovh.current_usage_eur(),
            "reserved_eur": self._reserved(instances, self._clock()),
        }

    # --- Commands ---

    async def up(
        self,
        entry_id: str,
        *,
        max_spend_eur: float | None = None,
        keep_when_idle: bool = False,
    ) -> Instance:
        entry = self._entry(entry_id)
        async with self._lock:
            now = self._clock()
            instances = await self._ovh.list_instances()
            if any(i.entry_id == entry_id and not self._stopping(i, now) for i in instances):
                raise Conflict(f"{entry_id} is already running")
            self._check_gpu_capacity(entry, instances)
            ttl_cost = entry.price_eur_per_hour * self._config.ttl_seconds / 3600
            await self._check_budget(instances, ttl_cost, now)

            deployment_id = str(uuid.uuid4())
            ilog = instance_log(deployment_id)
            ilog.info("up requested: %s at %.3f EUR/h", entry.id, entry.price_eur_per_hour)
            selector = f"{DEPLOYMENT_LABEL}={deployment_id}"
            token = await self._ovh.create_operator_token(f"ailab-{deployment_id}", selector)
            spec = build_app_spec(
                deployment_id=deployment_id,
                entry=entry,
                region=self._config.ovh_region,
                deadline=int(now + self._config.ttl_seconds),
                max_spend_eur=max_spend_eur or self._default_max_spend(entry),
                keep_when_idle=keep_when_idle,
                self_destruct_grace=self._config.teardown_drain_seconds + WATCHDOG_MARGIN_SECONDS,
                operator_token=token,
                backend_api_key=self._config.backend_api_key,
                ai_api_url=self._config.ovh_ai_api_url,
            )
            try:
                app = await self._ovh.create_app(spec)
            except Exception as error:
                ilog.error("app creation failed: %s", error)
                await self._delete_after_failed_create(selector, ilog)
                raise
            instance = parse_instance(app)
            self._instances[instance.app_id] = instance
            self._runtime(instance.app_id)
            ilog.info("OVH app %s created", instance.app_id)
            return instance

    async def _delete_after_failed_create(self, selector: str, ilog: Any) -> None:
        """A timeout may still have created the app."""
        try:
            for app in await self._ovh.list_apps(selector):
                await self._ovh.delete_app(app["id"])
        except Exception:
            ilog.exception("could not check for an app left by the failed start")

    def _entry(self, entry_id: str) -> CatalogEntry:
        try:
            return CATALOG[entry_id]
        except KeyError:
            raise NotFound(f"unknown catalog entry {entry_id}") from None

    def _default_max_spend(self, entry: CatalogEntry) -> float:
        if entry.max_spend_eur is not None:
            return entry.max_spend_eur
        return entry.price_eur_per_hour * self._config.max_spend_ttl_multiplier

    def _check_gpu_capacity(self, entry: CatalogEntry, instances: list[Instance]) -> None:
        if entry.gpu_count == 0:
            return
        running = [CATALOG[i.entry_id] for i in instances if i.entry_id in CATALOG]
        used = sum(e.gpu_count for e in running)
        if used + entry.gpu_count > self._config.max_gpus:
            listing = ", ".join(f"{e.id} ({e.gpu_count} GPU)" for e in running) or "nothing"
            raise Conflict(
                f"{entry.id} needs {entry.gpu_count} GPU and {used}/{self._config.max_gpus} "
                f"are in use by: {listing}"
            )

    @staticmethod
    def _reserved(instances: list[Instance], now: float) -> float:
        return sum(
            CATALOG[i.entry_id].price_eur_per_hour * max(0.0, i.deadline - now) / 3600
            for i in instances
            if i.entry_id in CATALOG
        )

    async def _check_budget(self, instances: list[Instance], extra: float, now: float) -> None:
        used = await self._ovh.current_usage_eur()
        reserved = self._reserved(instances, now)
        limit = self._config.budget_refuse_at_eur
        if used + reserved + extra > limit:
            raise BudgetExceeded(
                f"Refused: OVH reports {used:.2f} € this month, {reserved:.2f} € is reserved by "
                f"running apps and this needs {extra:.2f} € more, over the {limit:.2f} € limit."
            )

    async def _current(self, entry_id: str) -> tuple[Instance | None, list[Instance], float]:
        instances = await self._ovh.list_instances()
        now = self._clock()
        matching = [i for i in instances if i.entry_id == entry_id]
        active = next((i for i in matching if not self._stopping(i, now)), None)
        return active or next(iter(matching), None), instances, now

    async def refresh(self, entry_id: str) -> Instance:
        instance, instances, now = await self._current(entry_id)
        if instance is None or self._stopping(instance, now):
            raise NotFound(f"{entry_id} is not running")
        new_deadline = now + self._config.ttl_seconds
        price = CATALOG[entry_id].price_eur_per_hour
        extra = max(0.0, price * (new_deadline - instance.deadline) / 3600)
        await self._check_budget(instances, extra, now)
        # The in-app self-destruct reads this label: if it fails, the refresh failed.
        await self._ovh.set_app_label(instance.app_id, DEADLINE_LABEL, str(int(new_deadline)))
        self._runtime(instance.app_id).ttl_warned = False
        instance_log(instance.deployment_id).info(
            "refreshed: deadline moved by %d s", int(new_deadline - instance.deadline)
        )
        return replace(instance, deadline=new_deadline)

    async def down(self, entry_id: str) -> Instance:
        """Stop an app, or return the one already stopping so it can be followed."""
        instance, _, now = await self._current(entry_id)
        if instance is None:
            raise NotFound(f"{entry_id} is not running")
        runtime = self._runtime(instance.app_id)
        if runtime.stop_reason is None and now < instance.deadline:
            try:
                # Written on the app so that the stop survives a proxy restart.
                await self._ovh.set_app_label(instance.app_id, DEADLINE_LABEL, str(int(now)))
            except Exception:
                instance_log(instance.deployment_id).exception("could not set the deadline label")
        await self._stop(instance, runtime, "stopped manually", now)
        return instance

    def begin_request(self, instance: Instance) -> None:
        runtime = self._runtime(instance.app_id)
        runtime.inflight += 1
        runtime.last_activity = self._clock()

    def end_request(self, instance: Instance) -> None:
        runtime = self._runtimes.get(instance.app_id)
        if runtime:
            runtime.inflight = max(0, runtime.inflight - 1)
            runtime.last_activity = self._clock()

    # --- Controller loop ---

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("controller tick failed")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self) -> None:
        now = self._clock()
        async with self._lock:
            try:
                instances = await self._ovh.list_instances()
            except Exception:
                # The in-app self-destruct still covers every deadline.
                log.exception("could not list apps from OVH, limits are not enforced this tick")
                return
            await self._sync(instances, now)
            if now - self._last_token_cleanup >= TOKEN_CLEANUP_SECONDS:
                self._last_token_cleanup = now
                await self._guarded(self._delete_unused_tokens)
        for instance in instances:
            try:
                await self._enforce(instance, now)
            except Exception:
                instance_log(instance.deployment_id).exception("could not process this app")
        if now - self._last_usage_check >= USAGE_CHECK_SECONDS:
            self._last_usage_check = now
            await self._guarded(self._notify_budget)

    async def _guarded(self, step: Callable[[], Awaitable[None]]) -> None:
        """Run an optional step; its failure must not stop deadline enforcement."""
        try:
            await step()
        except Exception:
            log.exception("controller step %s failed", step.__name__)

    async def _sync(self, instances: list[Instance], now: float) -> None:
        current = {i.app_id: i for i in instances}
        for app_id, previous in list(self._instances.items()):
            if app_id not in current:
                await self._finish(previous, now)
        for instance in instances:
            previous = self._instances.get(instance.app_id)
            if previous is None or (previous.state, previous.info) != (instance.state, instance.info):
                instance_log(instance.deployment_id).info(
                    "OVH app state: %s (%s)", instance.state, instance.info or "no details"
                )
            self._runtime(instance.app_id)
        self._instances = current

    async def _finish(self, instance: Instance, now: float) -> None:
        """OVH no longer reports this app, so it has really ended."""
        runtime = self._runtimes.pop(instance.app_id, None)
        reason = (runtime and runtime.stop_reason) or "deleted outside the proxy"
        self._ended.append((instance.app_id, reason))
        instance_log(instance.deployment_id).info(
            "ended after %d s, OVH no longer reports app %s: %s",
            int(now - instance.created_at),
            instance.app_id,
            reason,
        )
        await self._notifier.send("Deployment stopped", f"{instance.entry_id}: {reason}.")

    async def _enforce(self, instance: Instance, now: float) -> None:
        runtime = self._runtime(instance.app_id)
        reason = self._stop_reason(instance, runtime, now)
        if reason:
            await self._stop(instance, runtime, reason, now)
            return
        if instance.state == RUNNING_STATE and instance.url:
            await self._check_health(instance, runtime, now)
        if not runtime.ttl_warned and instance.deadline - now <= self._config.ttl_warning_seconds:
            runtime.ttl_warned = True
            minutes = max(0, round((instance.deadline - now) / 60))
            instance_log(instance.deployment_id).info("TTL warning: ends in %d min", minutes)
            await self._notifier.send(
                "Deployment ending soon",
                f"{instance.entry_id} ends in {minutes} min unless refreshed.",
            )

    def _stop_reason(self, instance: Instance, runtime: Runtime, now: float) -> str | None:
        if runtime.stop_reason:
            return runtime.stop_reason
        entry = CATALOG.get(instance.entry_id)
        if entry is None:
            return f"unknown catalog entry {instance.entry_id!r}"
        if now >= instance.deadline:
            return "TTL expired"
        if instance.state in FAILED_APP_STATES:
            detail = f": {instance.info}" if instance.info else ""
            return f"app entered state {instance.state}{detail}"
        cost = entry.price_eur_per_hour * max(0.0, now - instance.created_at) / 3600
        if cost >= instance.max_spend_eur:
            return f"spending cap of {instance.max_spend_eur:.2f} € reached"
        if not runtime.ready:
            if now - instance.created_at >= self._config.start_timeout_seconds:
                return "did not become ready in time"
            return None
        if (
            not instance.keep_when_idle
            and runtime.inflight == 0
            and now - runtime.last_activity >= self._config.idle_teardown_seconds
        ):
            return "idle"
        return None

    async def _check_health(self, instance: Instance, runtime: Runtime, now: float) -> None:
        if runtime.ready and now - runtime.health_checked_at < HEALTH_CHECK_SECONDS:
            return
        runtime.health_checked_at = now
        health = await self._probe(instance)
        ilog = instance_log(instance.deployment_id)
        if health.detail != runtime.health:
            level = logging.INFO if health.ok or not runtime.ready else logging.WARNING
            ilog.log(level, "health: %s (was: %s)", health.detail, runtime.health)
            runtime.health = health.detail
        if health.ok and not runtime.ready:
            runtime.ready = True
            runtime.last_activity = now
            ilog.info("ready after %d s at %s", int(now - instance.created_at), instance.url)

    async def _stop(self, instance: Instance, runtime: Runtime, reason: str, now: float) -> None:
        ilog = instance_log(instance.deployment_id)
        if runtime.stop_reason is None:
            runtime.stop_reason = reason
            runtime.stop_requested_at = now
            ilog.info("stop requested: %s (%d requests in flight)", reason, runtime.inflight)
        drain_limit = runtime.stop_requested_at + self._config.teardown_drain_seconds
        if runtime.inflight > 0 and now < drain_limit:
            return
        if (
            runtime.last_delete_at is not None
            and now - runtime.last_delete_at < TEARDOWN_RETRY_SECONDS
        ):
            return
        runtime.last_delete_at = now
        if runtime.first_delete_at is None:
            runtime.first_delete_at = now
        runtime.stop_detail = f"OVH still reports app {instance.app_id} in state {instance.state}"
        if now - runtime.first_delete_at >= TEARDOWN_CONFIRM_SECONDS:
            ilog.error("stop not confirmed: %s", runtime.stop_detail)
            await self._alert_failed_stop(instance, runtime, runtime.stop_detail)
        if instance.state == DELETING_STATE:
            return
        try:
            ilog.info("force-deleting OVH app %s (state %s)", instance.app_id, instance.state)
            await self._ovh.delete_app(instance.app_id)
        except Exception as error:
            ilog.exception("delete failed")
            runtime.stop_detail = str(error)
            await self._alert_failed_stop(instance, runtime, str(error))

    async def _alert_failed_stop(self, instance: Instance, runtime: Runtime, detail: str) -> None:
        runtime.delete_failures += 1
        await self._notifier.send(
            "TEARDOWN FAILED: billing continues",
            f"{instance.entry_id} ({instance.app_id}): attempt {runtime.delete_failures} "
            f"failed ({detail}). Retrying every minute.",
            urgent=True,
        )

    async def _delete_unused_tokens(self) -> None:
        """Delete per-app tokens whose app is gone. Only tokens scoped to one deployment."""
        live = {instance.deployment_id for instance in self._instances.values()}
        prefix = f"{DEPLOYMENT_LABEL}="
        for token in await self._ovh.list_tokens():
            selector = token.get("spec", {}).get("labelSelector") or ""
            if selector.startswith(prefix) and selector.removeprefix(prefix) not in live:
                await self._ovh.delete_token(token["id"])
                log.info("deleted the token of ended deployment %s", selector.removeprefix(prefix))

    async def _notify_budget(self) -> None:
        used = await self._ovh.current_usage_eur()
        self.last_usage_eur = used
        month = time.strftime("%Y-%m", time.gmtime(self._clock()))
        percent_used = 100 * used / self._config.budget_pool_eur
        for threshold in self._config.budget_notify_percent:
            if percent_used >= threshold and (month, threshold) not in self._notified_budget:
                self._notified_budget.add((month, threshold))
                log.info("budget at %d%%: OVH reports %.2f EUR", threshold, used)
                await self._notifier.send(
                    f"Budget at {threshold} %",
                    f"OVH reports {used:.2f} € of {self._config.budget_pool_eur:.2f} € this month.",
                    urgent=threshold >= 90,
                )
