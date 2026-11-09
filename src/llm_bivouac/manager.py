"""Starts, refreshes and tears down deployments, and enforces their limits."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .app_spec import (
    DEADLINE_LABEL,
    DEPLOYMENT_LABEL,
    MANAGED_LABEL,
    WATCHDOG_MARGIN_SECONDS,
    build_app_spec,
)
from .catalog import CATALOG, CatalogEntry
from .config import Config
from .ledger import LedgerUnavailable
from .state import READY, STARTING, TEARING_DOWN, Deployment, EndedDeployment, StateStore

log = logging.getLogger(__name__)

TICK_SECONDS = 15
TEARDOWN_RETRY_SECONDS = 60
RECONCILE_SECONDS = 60
USAGE_CHECK_SECONDS = 900
DRIFT_NOTICE_SECONDS = 6 * 3600
DRIFT_TOLERANCE_EUR = 1.0
DRIFT_TOLERANCE_RATIO = 0.2
FAILED_APP_STATES = {"FAILED", "ERROR", "DELETED", "DELETING", "STOPPED", "STOPPING"}
GONE_APP_STATES = {"DELETED", "DELETING"}

Probe = Callable[[Deployment], Awaitable[bool]]


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class Notifications(Protocol):
    async def send(self, title: str, message: str, *, urgent: bool = False) -> None: ...


def _is_gone(app: dict[str, Any]) -> bool:
    if app.get("spec", {}).get("deletionRequested"):
        return True
    return app.get("status", {}).get("state") in GONE_APP_STATES


class Manager:
    def __init__(
        self,
        config: Config,
        store: StateStore,
        ledger: Any,
        ovh: Any,
        notifier: Notifications,
        probe: Probe,
        clock: Callable[[], float] = time.time,
    ):
        self._config = config
        self._store = store
        self._ledger = ledger
        self._ovh = ovh
        self._notifier = notifier
        self._probe = probe
        self._clock = clock
        self._start_lock = asyncio.Lock()
        self._inflight: dict[str, int] = {}
        self._last_reconcile = 0.0
        self._last_usage_check = 0.0
        self._last_drift_notice = 0.0
        # In-flight counts do not survive a restart, so idle time starts over.
        now = clock()
        for deployment in self.deployments:
            deployment.last_activity = now

    @property
    def deployments(self) -> list[Deployment]:
        return list(self._store.state.deployments.values())

    def inflight(self, deployment_id: str) -> int:
        return self._inflight.get(deployment_id, 0)

    # --- Starting and refreshing ---

    def estimate(self, entry_id: str) -> dict[str, Any]:
        entry = self._entry(entry_id)
        return {
            "entry_id": entry.id,
            "display_name": entry.display_name,
            "eur_per_hour": entry.price_eur_per_hour,
            "ttl_cost_eur": entry.price_eur_per_hour * self._config.ttl_seconds / 3600,
            "confirm_required": entry.price_eur_per_hour >= self._config.cli_confirm_eur_per_hour,
        }

    async def up(
        self,
        entry_id: str,
        requested_by: str,
        *,
        max_spend_eur: float | None = None,
        keep_when_idle: bool = False,
    ) -> Deployment:
        entry = self._entry(entry_id)
        async with self._start_lock:
            if self.find_active(entry_id):
                raise Conflict(f"{entry_id} is already running")
            self._check_gpu_capacity(entry)
            now = self._clock()
            deployment_id = str(uuid.uuid4())
            deadline = now + self._config.ttl_seconds
            await self._ledger.reserve(
                deployment_id=deployment_id,
                entry_id=entry.id,
                price_eur_per_hour=entry.price_eur_per_hour,
                requested_by=requested_by,
                started_at=now,
                deadline=deadline,
            )
            try:
                token_id, token_value = await self._ovh.create_operator_token(
                    f"ailab-{deployment_id}", f"{DEPLOYMENT_LABEL}={deployment_id}"
                )
            except Exception:
                await self._record_end(deployment_id, now, "operator token creation failed")
                raise
            deployment = Deployment(
                id=deployment_id,
                entry_id=entry.id,
                price_eur_per_hour=entry.price_eur_per_hour,
                started_at=now,
                deadline=deadline,
                last_activity=now,
                max_spend_eur=max_spend_eur or self._default_max_spend(entry),
                requested_by=requested_by,
                token_id=token_id,
                token_value=token_value,
                keep_when_idle=keep_when_idle,
            )
            # Saved before the app exists so a crash here cannot leave it untracked.
            self._store.state.deployments[deployment_id] = deployment
            self._store.save()
            spec = build_app_spec(
                deployment_id=deployment_id,
                entry=entry,
                region=self._config.ovh_region,
                deadline=int(deadline),
                self_destruct_grace=self._config.teardown_drain_seconds + WATCHDOG_MARGIN_SECONDS,
                operator_token=token_value,
                backend_api_key=self._config.backend_api_key,
                ai_api_url=self._config.ovh_ai_api_url,
            )
            try:
                app = await self._ovh.create_app(spec)
            except Exception as error:
                # A timeout may still have created the app, so teardown searches for it.
                self._request_teardown(deployment, f"start failed: {error}")
                await self._teardown_step(deployment)
                raise
            deployment.app_id = app["id"]
            self._store.save()
            return deployment

    def _entry(self, entry_id: str) -> CatalogEntry:
        try:
            return CATALOG[entry_id]
        except KeyError:
            raise NotFound(f"unknown catalog entry {entry_id}") from None

    def _default_max_spend(self, entry: CatalogEntry) -> float:
        if entry.max_spend_eur is not None:
            return entry.max_spend_eur
        return entry.price_eur_per_hour * self._config.max_spend_ttl_multiplier

    def _check_gpu_capacity(self, entry: CatalogEntry) -> None:
        if entry.gpu_count == 0:
            return
        running = [(d, CATALOG[d.entry_id]) for d in self.deployments if d.entry_id in CATALOG]
        used = sum(e.gpu_count for _, e in running)
        if used + entry.gpu_count > self._config.max_gpus:
            listing = ", ".join(f"{e.id} ({e.gpu_count} GPU)" for _, e in running) or "nothing"
            raise Conflict(
                f"{entry.id} needs {entry.gpu_count} GPU and {used}/{self._config.max_gpus} "
                f"are in use by: {listing}"
            )

    def find_active(self, entry_id: str) -> Deployment | None:
        for deployment in self.deployments:
            if deployment.entry_id == entry_id and deployment.status != TEARING_DOWN:
                return deployment
        return None

    def ready_deployment(self, entry_id: str) -> Deployment | None:
        deployment = self.find_active(entry_id)
        return deployment if deployment and deployment.status == READY else None

    def _running(self, entry_id: str) -> Deployment:
        deployment = self.find_active(entry_id)
        if deployment is None:
            raise NotFound(f"{entry_id} is not running")
        return deployment

    async def refresh(self, entry_id: str) -> Deployment:
        deployment = self._running(entry_id)
        if deployment.app_id is None:
            raise Conflict(f"{entry_id} is still being created")
        now = self._clock()
        new_deadline = now + self._config.ttl_seconds
        previous = deployment.deadline
        extra = deployment.price_eur_per_hour * (new_deadline - previous) / 3600
        await self._ledger.extend(deployment.id, new_deadline, extra)
        try:
            # The in-app self-destruct reads this label, so a refresh that
            # fails to set it must not be reported as having worked.
            await self._ovh.set_app_label(deployment.app_id, DEADLINE_LABEL, str(int(new_deadline)))
        except Exception:
            await self._ledger.set_deadline(deployment.id, previous)
            raise
        deployment.deadline = new_deadline
        deployment.ttl_warned = False
        self._store.save()
        return deployment

    async def down(self, entry_id: str) -> Deployment:
        deployment = self._running(entry_id)
        self._request_teardown(deployment, "stopped manually")
        await self._advance_teardown(deployment, self._clock())
        return deployment

    # --- Requests in flight ---

    def begin_request(self, deployment: Deployment) -> None:
        self._inflight[deployment.id] = self.inflight(deployment.id) + 1
        deployment.last_activity = self._clock()

    def end_request(self, deployment: Deployment) -> None:
        self._inflight[deployment.id] = max(0, self.inflight(deployment.id) - 1)
        deployment.last_activity = self._clock()

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
        for deployment in self.deployments:
            try:
                await self._tick_deployment(deployment, now)
            except Exception:
                log.exception("could not process deployment %s", deployment.id)
        await self._flush_ledger_ends()
        if now - self._last_reconcile >= RECONCILE_SECONDS:
            self._last_reconcile = now
            await self._guarded(self._delete_orphan_apps)
        await self._guarded(self._notify_budget)
        if now - self._last_usage_check >= USAGE_CHECK_SECONDS:
            self._last_usage_check = now
            await self._guarded(self._compare_with_ovh)

    async def _guarded(self, step: Callable[[], Awaitable[None]]) -> None:
        """Run an optional step; its failure must not stop deadline enforcement."""
        try:
            await step()
        except LedgerUnavailable:
            pass
        except Exception:
            log.exception("controller step %s failed", step.__name__)

    async def _tick_deployment(self, deployment: Deployment, now: float) -> None:
        if deployment.status == TEARING_DOWN:
            await self._advance_teardown(deployment, now)
            return
        reason = self._teardown_reason(deployment, now)
        if reason:
            self._request_teardown(deployment, reason)
            await self._advance_teardown(deployment, now)
            return
        if deployment.status == STARTING:
            await self._advance_start(deployment, now)
        if (
            not deployment.ttl_warned
            and deployment.deadline - now <= self._config.ttl_warning_seconds
        ):
            deployment.ttl_warned = True
            self._store.save()
            minutes = max(0, round((deployment.deadline - now) / 60))
            await self._notifier.send(
                "Deployment ending soon",
                f"{deployment.entry_id} ends in {minutes} min unless refreshed.",
            )

    def _teardown_reason(self, deployment: Deployment, now: float) -> str | None:
        if now >= deployment.deadline:
            return "TTL expired"
        if deployment.cost_so_far(now) >= deployment.max_spend_eur:
            return f"spending cap of {deployment.max_spend_eur:.2f} € reached"
        if deployment.status == STARTING:
            if now - deployment.started_at >= self._config.start_timeout_seconds:
                return "did not become ready in time"
            return None
        idle_for = now - deployment.last_activity
        if (
            not deployment.keep_when_idle
            and self.inflight(deployment.id) == 0
            and idle_for >= self._config.idle_teardown_seconds
        ):
            return "idle"
        return None

    async def _advance_start(self, deployment: Deployment, now: float) -> None:
        if deployment.app_id is None:
            return
        app = await self._ovh.get_app(deployment.app_id)
        status = app.get("status", {})
        if status.get("state") in FAILED_APP_STATES:
            self._request_teardown(deployment, f"app entered state {status.get('state')}")
            await self._advance_teardown(deployment, now)
            return
        if status.get("state") != "RUNNING" or not status.get("url"):
            return
        deployment.url = status["url"]
        if await self._probe(deployment):
            deployment.status = READY
            deployment.last_activity = now
            self._store.save()
            log.info("deployment %s is ready", deployment.id)

    def _request_teardown(self, deployment: Deployment, reason: str) -> None:
        deployment.status = TEARING_DOWN
        deployment.teardown_reason = reason
        deployment.teardown_requested_at = self._clock()
        self._store.save()

    async def _advance_teardown(self, deployment: Deployment, now: float) -> None:
        drain_limit = deployment.teardown_requested_at + self._config.teardown_drain_seconds
        if self.inflight(deployment.id) > 0 and now < drain_limit:
            return
        last_attempt = deployment.last_teardown_attempt
        if last_attempt is not None and now - last_attempt < TEARDOWN_RETRY_SECONDS:
            return
        deployment.last_teardown_attempt = now
        self._store.save()
        await self._teardown_step(deployment)

    async def _teardown_step(self, deployment: Deployment) -> None:
        try:
            await self._delete_app(deployment)
        except Exception as error:
            deployment.teardown_failures += 1
            self._store.save()
            log.exception("teardown of %s failed", deployment.id)
            await self._notifier.send(
                "TEARDOWN FAILED: billing continues",
                f"{deployment.entry_id} ({deployment.app_id}): attempt "
                f"{deployment.teardown_failures} failed ({error}). Retrying every minute.",
                urgent=True,
            )
            return
        try:
            await self._ovh.delete_token(deployment.token_id)
        except Exception:
            # The token can only reach this now-deleted app, so this is not worth an alert.
            log.warning("could not delete token %s", deployment.token_id, exc_info=True)
        ended_at = self._clock()
        reason = deployment.teardown_reason or "unknown"
        del self._store.state.deployments[deployment.id]
        self._inflight.pop(deployment.id, None)
        self._store.state.pending_ledger_ends.append(
            EndedDeployment(id=deployment.id, ended_at=ended_at, reason=reason)
        )
        self._store.save()
        await self._flush_ledger_ends()
        await self._notifier.send("Deployment stopped", f"{deployment.entry_id}: {reason}.")

    async def _delete_app(self, deployment: Deployment) -> None:
        selector = f"{DEPLOYMENT_LABEL}={deployment.id}"
        if deployment.app_id:
            app_ids = [deployment.app_id]
        else:
            app_ids = [app["id"] for app in await self._ovh.list_apps(selector)]
        for app_id in app_ids:
            await self._ovh.delete_app(app_id)
        remaining = [a for a in await self._ovh.list_apps(selector) if not _is_gone(a)]
        if remaining:
            raise RuntimeError(f"{len(remaining)} app(s) still present after delete")

    async def _record_end(self, deployment_id: str, ended_at: float, reason: str) -> None:
        self._store.state.pending_ledger_ends.append(
            EndedDeployment(id=deployment_id, ended_at=ended_at, reason=reason)
        )
        self._store.save()
        await self._flush_ledger_ends()

    async def _flush_ledger_ends(self) -> None:
        state = self._store.state
        while state.pending_ledger_ends:
            ended = state.pending_ledger_ends[0]
            try:
                await self._ledger.end(ended.id, ended.ended_at, ended.reason)
            except LedgerUnavailable:
                return
            state.pending_ledger_ends.pop(0)
            self._store.save()

    async def _delete_orphan_apps(self) -> None:
        """Delete managed apps the proxy has no record of, such as after lost state."""
        known = set(self._store.state.deployments)
        for app in await self._ovh.list_apps(f"{MANAGED_LABEL}=true"):
            if _is_gone(app):
                continue
            if app.get("spec", {}).get("labels", {}).get(DEPLOYMENT_LABEL) not in known:
                await self._ovh.delete_app(app["id"])
                await self._notifier.send(
                    "Deleted untracked app",
                    f"App {app['id']} had no deployment record and was deleted.",
                    urgent=True,
                )

    async def _notify_budget(self) -> None:
        summary = await self._ledger.summary()
        state = self._store.state
        if state.notified_period != summary.period_id:
            state.notified_period = summary.period_id
            state.notified_percent = []
            self._store.save()
        percent_used = 100 * summary.spent_eur / summary.pool_eur
        for threshold in self._config.budget_notify_percent:
            if percent_used >= threshold and threshold not in state.notified_percent:
                state.notified_percent.append(threshold)
                self._store.save()
                await self._notifier.send(
                    f"Budget at {threshold} %",
                    f"{summary.spent_eur:.2f} € of {summary.pool_eur:.2f} € spent.",
                    urgent=threshold >= 90,
                )

    async def _compare_with_ovh(self) -> None:
        ledger_eur = await self._ledger.spent_this_month()
        ovh_eur = await self._ovh.current_usage_eur()
        tolerance = max(DRIFT_TOLERANCE_EUR, ledger_eur * DRIFT_TOLERANCE_RATIO)
        now = self._clock()
        if ovh_eur > ledger_eur + tolerance:
            log.warning("OVH reports %.2f EUR, ledger %.2f EUR", ovh_eur, ledger_eur)
            if now - self._last_drift_notice >= DRIFT_NOTICE_SECONDS:
                self._last_drift_notice = now
                await self._notifier.send(
                    "OVH billing exceeds the ledger",
                    f"OVH reports {ovh_eur:.2f} € this month, the ledger {ledger_eur:.2f} €.",
                    urgent=True,
                )
