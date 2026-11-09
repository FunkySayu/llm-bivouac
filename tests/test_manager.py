from __future__ import annotations

import asyncio
import logging

import pytest

from llm_bivouac.app_spec import (
    DEADLINE_LABEL,
    DEPLOYMENT_LABEL,
    ENTRY_LABEL,
    KEEP_WHEN_IDLE_LABEL,
    MANAGED_LABEL,
    MAX_SPEND_LABEL,
)
from llm_bivouac.catalog import CATALOG
from llm_bivouac.manager import BudgetExceeded, Conflict, NotFound
from llm_bivouac.ovh import GpuQuotaError, OvhError

ENTRY = "smollm3-3b-q4-cpu"
FAILED_STOP = "TEARDOWN FAILED: billing continues"


def run(coroutine):
    return asyncio.run(coroutine)


def up(h, **kwargs):
    return run(h.manager.up(ENTRY, **kwargs))


def start(h, **kwargs):
    instance = up(h, **kwargs)
    h.ovh.run(instance.app_id)
    run(h.manager.tick())
    return instance


def labels(h, instance):
    return h.ovh.apps[instance.app_id]["spec"]["labels"]


def test_catalog_entry_is_cpu_only():
    entry = CATALOG[ENTRY]
    assert (entry.flavor, entry.flavor_count, entry.gpu_count) == ("ai1-1-cpu", 4, 0)
    assert entry.price_eur_per_hour == pytest.approx(0.132)


def test_up_writes_everything_it_must_remember_on_the_app(harness):
    instance = up(harness, keep_when_idle=True, max_spend_eur=5)
    written = labels(harness, instance)
    assert written[MANAGED_LABEL] == "true"
    assert written[ENTRY_LABEL] == ENTRY
    assert written[DEADLINE_LABEL] == str(int(instance.deadline))
    assert written[MAX_SPEND_LABEL] == "5.0000"
    assert written[KEEP_WHEN_IDLE_LABEL] == "true"
    selector = f"{DEPLOYMENT_LABEL}={instance.deployment_id}"
    assert [t["spec"]["labelSelector"] for t in harness.ovh.tokens.values()] == [selector]
    assert instance.token.startswith("secret-")


def test_becomes_ready_only_when_healthy(harness):
    instance = up(harness)
    harness.ovh.run(instance.app_id)
    harness.healthy = False
    run(harness.manager.tick())
    assert harness.manager.ready_instance(ENTRY) is None
    harness.healthy = True
    run(harness.manager.tick())
    assert harness.manager.ready_instance(ENTRY).app_id == instance.app_id


def test_restart_recovers_everything_from_ovh(harness):
    instance = start(harness)
    harness.restart()
    with pytest.raises(Conflict):
        up(harness)
    run(harness.manager.tick())
    recovered = harness.manager.ready_instance(ENTRY)
    assert recovered.app_id == instance.app_id
    assert recovered.token == instance.token


def test_deadline_is_enforced_after_a_restart(harness):
    instance = start(harness)
    harness.restart()
    harness.clock.advance(3601)
    run(harness.manager.tick())
    assert instance.app_id not in harness.ovh.apps


def test_second_start_of_same_entry_is_refused(harness):
    start(harness)
    with pytest.raises(Conflict):
        up(harness)


def test_budget_comes_from_ovh_usage_and_creates_nothing_when_refused(harness):
    harness.ovh.usage = 89.95
    with pytest.raises(BudgetExceeded):
        up(harness)
    assert harness.ovh.apps == {} and harness.ovh.tokens == {}


def test_reserved_time_of_running_apps_counts_against_the_budget(harness):
    instance = start(harness)
    harness.ovh.usage = 89.9  # under the limit alone, over it with the app's 0.132 € reserved
    with pytest.raises(BudgetExceeded):
        run(harness.manager.refresh(ENTRY))
    assert labels(harness, instance)[DEADLINE_LABEL] == str(int(instance.deadline))


def test_start_is_refused_when_ovh_usage_is_unavailable(harness):
    harness.ovh.fail_usage = True
    with pytest.raises(OvhError):
        up(harness)
    assert harness.ovh.apps == {}


def test_quota_refusal_is_raised_and_leaves_no_app(harness):
    harness.ovh.fail_create = GpuQuotaError(402, "quota")
    with pytest.raises(GpuQuotaError):
        up(harness)
    assert harness.ovh.apps == {}
    assert harness.manager.instances == []


def test_token_of_a_failed_start_is_cleaned_up(harness):
    harness.ovh.fail_create = GpuQuotaError(402, "quota")
    with pytest.raises(GpuQuotaError):
        up(harness)
    harness.clock.advance(61)
    run(harness.manager.tick())
    assert harness.ovh.tokens == {}


def test_unrefreshed_app_is_deleted_at_the_deadline(harness):
    instance = start(harness, keep_when_idle=True)
    harness.clock.advance(3599)
    run(harness.manager.tick())
    assert instance.app_id in harness.ovh.apps
    harness.clock.advance(2)
    run(harness.manager.tick())
    assert harness.ovh.apps == {}
    harness.clock.advance(15)
    run(harness.manager.tick())
    assert harness.manager.instances == []
    assert harness.manager.ended_reason(instance.app_id) == "TTL expired"
    assert "Deployment stopped" in harness.notifier.titles()


def test_refresh_moves_the_label_on_ovh(harness):
    instance = start(harness)
    harness.clock.advance(1800)
    refreshed = run(harness.manager.refresh(ENTRY))
    assert refreshed.deadline == pytest.approx(harness.clock() + 3600)
    assert labels(harness, instance)[DEADLINE_LABEL] == str(int(refreshed.deadline))


def test_refresh_that_cannot_set_the_label_fails(harness):
    start(harness)
    harness.ovh.fail_label = True
    with pytest.raises(OvhError):
        run(harness.manager.refresh(ENTRY))


def test_idle_app_is_stopped(harness):
    instance = start(harness)
    harness.clock.advance(1201)
    run(harness.manager.tick())
    assert instance.app_id not in harness.ovh.apps


def test_keep_when_idle_is_read_from_the_label(harness):
    instance = start(harness, keep_when_idle=True)
    harness.restart()
    run(harness.manager.tick())
    harness.clock.advance(1201)
    run(harness.manager.tick())
    assert instance.app_id in harness.ovh.apps


def test_running_response_delays_the_stop_up_to_the_drain(harness):
    instance = start(harness)
    harness.manager.begin_request(instance)
    harness.clock.advance(3601)
    run(harness.manager.tick())
    assert instance.app_id in harness.ovh.apps
    harness.clock.advance(301)
    run(harness.manager.tick())
    assert instance.app_id not in harness.ovh.apps


def test_spending_cap_is_read_from_the_label(harness):
    instance = start(harness, max_spend_eur=0.01)
    harness.clock.advance(301)
    run(harness.manager.tick())
    assert instance.app_id not in harness.ovh.apps
    run(harness.manager.tick())
    assert "spending cap" in harness.manager.ended_reason(instance.app_id)


def test_failed_app_reason_includes_what_ovh_said(harness):
    instance = up(harness)
    harness.ovh.apps[instance.app_id]["status"] = {
        "state": "ERROR",
        "info": {"message": "image pull failed"},
    }
    run(harness.manager.tick())
    assert instance.app_id not in harness.ovh.apps
    run(harness.manager.tick())
    assert "image pull failed" in harness.manager.ended_reason(instance.app_id)


def test_app_never_ready_is_stopped(harness):
    instance = up(harness)
    harness.clock.advance(1201)
    run(harness.manager.tick())
    assert instance.app_id not in harness.ovh.apps


def test_app_with_an_unknown_entry_is_deleted(harness):
    instance = start(harness)
    labels(harness, instance)[ENTRY_LABEL] = "removed-model"
    run(harness.manager.tick())
    assert harness.ovh.apps == {}


def test_app_with_an_unreadable_deadline_is_deleted(harness):
    instance = start(harness)
    labels(harness, instance)[DEADLINE_LABEL] = "not-a-number"
    run(harness.manager.tick())
    assert harness.ovh.apps == {}


def test_tick_survives_ovh_being_unavailable(harness):
    instance = start(harness)
    harness.ovh.fail_list = True
    run(harness.manager.tick())
    assert [i.app_id for i in harness.manager.instances] == [instance.app_id]


def test_failed_delete_retries_every_minute_and_alerts(harness):
    instance = start(harness)
    harness.ovh.fail_delete = True
    run(harness.manager.down(ENTRY))
    assert harness.notifier.sent[-1] == (FAILED_STOP, True)
    harness.clock.advance(15)
    run(harness.manager.tick())
    assert harness.notifier.titles().count(FAILED_STOP) == 1
    harness.clock.advance(60)
    run(harness.manager.tick())
    assert harness.notifier.titles().count(FAILED_STOP) == 2
    harness.ovh.fail_delete = False
    harness.clock.advance(60)
    run(harness.manager.tick())
    assert instance.app_id not in harness.ovh.apps


def test_down_writes_the_deadline_so_it_survives_a_restart(harness):
    instance = start(harness)
    harness.manager.begin_request(instance)
    run(harness.manager.down(ENTRY))
    assert labels(harness, instance)[DEADLINE_LABEL] == str(int(harness.clock()))
    harness.restart()
    run(harness.manager.tick())
    assert instance.app_id not in harness.ovh.apps


def test_down_is_not_finished_while_ovh_still_reports_the_app(harness):
    instance = start(harness)
    harness.ovh.ignore_delete = True
    run(harness.manager.down(ENTRY))
    run(harness.manager.tick())
    assert [i.app_id for i in harness.manager.instances] == [instance.app_id]
    assert "Deployment stopped" not in harness.notifier.titles()

    harness.ovh.ignore_delete = False
    harness.clock.advance(60)
    run(harness.manager.tick())
    harness.clock.advance(15)
    run(harness.manager.tick())
    assert harness.manager.instances == []
    assert harness.manager.ended_reason(instance.app_id) == "stopped manually"
    assert "Deployment stopped" in harness.notifier.titles()


def test_deleting_app_is_waited_for_and_not_deleted_again(harness):
    instance = start(harness)
    harness.ovh.apps[instance.app_id]["status"] = {"state": "DELETING"}
    run(harness.manager.tick())
    assert harness.ovh.delete_calls == []
    assert instance.app_id in harness.ovh.apps


def test_unconfirmed_stop_raises_the_alarm(harness):
    start(harness)
    harness.ovh.ignore_delete = True
    run(harness.manager.down(ENTRY))
    assert FAILED_STOP not in harness.notifier.titles()
    harness.clock.advance(181)
    run(harness.manager.tick())
    assert FAILED_STOP in harness.notifier.titles()
    assert len(harness.manager.instances) == 1


def test_second_down_follows_the_stop_already_running(harness):
    instance = start(harness)
    harness.ovh.ignore_delete = True
    run(harness.manager.down(ENTRY))
    assert run(harness.manager.down(ENTRY)).app_id == instance.app_id
    assert harness.ovh.delete_calls == [instance.app_id]


def test_down_on_nothing_is_not_found(harness):
    with pytest.raises(NotFound):
        run(harness.manager.down(ENTRY))


def test_token_is_deleted_once_its_app_is_gone(harness):
    start(harness)
    assert len(harness.ovh.tokens) == 1
    run(harness.manager.down(ENTRY))
    harness.clock.advance(61)
    run(harness.manager.tick())
    assert harness.ovh.tokens == {}


def test_other_tokens_are_left_alone(harness):
    harness.ovh.tokens["mine"] = {
        "id": "mine",
        "spec": {"name": "ailab-read", "labelSelector": "team=mine"},
    }
    run(harness.manager.tick())
    assert "mine" in harness.ovh.tokens


def test_budget_thresholds_notify_once_per_month(harness):
    harness.ovh.usage = 85.0
    run(harness.manager.tick())
    assert harness.notifier.titles() == ["Budget at 50 %", "Budget at 80 %"]
    harness.clock.advance(901)
    run(harness.manager.tick())
    assert harness.notifier.titles() == ["Budget at 50 %", "Budget at 80 %"]


def test_startup_progress_is_logged_with_the_instance(harness, caplog):
    instance = up(harness)
    harness.ovh.apps[instance.app_id]["status"] = {
        "state": "INITIALIZING",
        "info": {"code": "APP_INITIALIZING", "message": "pulling image"},
    }
    harness.healthy = False
    with caplog.at_level(logging.INFO, logger="llm_bivouac"):
        run(harness.manager.tick())
        run(harness.manager.tick())  # unchanged, so nothing new is logged
        harness.ovh.run(instance.app_id)
        run(harness.manager.tick())
        harness.healthy = True
        run(harness.manager.tick())
    lines = [r.getMessage() for r in caplog.records]
    prefix = f"[{instance.deployment_id[:8]}] "
    assert all(line.startswith(prefix) for line in lines)
    assert sum("OVH app state: INITIALIZING (pulling image)" in line for line in lines) == 1
    assert any("health: HTTP 503 Loading model" in line for line in lines)
    assert any("ready after" in line for line in lines)


def test_lost_health_while_serving_is_logged_but_does_not_stop_the_app(harness, caplog):
    instance = start(harness)
    harness.clock.advance(61)
    harness.healthy = False
    with caplog.at_level(logging.WARNING, logger="llm_bivouac"):
        run(harness.manager.tick())
    assert instance.app_id in harness.ovh.apps
    assert any("health: HTTP 503" in r.getMessage() for r in caplog.records)
