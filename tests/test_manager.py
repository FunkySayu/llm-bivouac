from __future__ import annotations

import asyncio

import pytest

from llm_bivouac.app_spec import DEADLINE_LABEL, DEPLOYMENT_LABEL, MANAGED_LABEL
from llm_bivouac.catalog import CATALOG
from llm_bivouac.ledger import BudgetExceeded, LedgerUnavailable
from llm_bivouac.manager import Conflict
from llm_bivouac.ovh import GpuQuotaError, OvhError
from llm_bivouac.state import READY, TEARING_DOWN

ENTRY = "smollm3-3b-q4-cpu"


def run(coroutine):
    return asyncio.run(coroutine)


def start(h, **kwargs):
    deployment = run(h.manager.up(ENTRY, "tester", **kwargs))
    h.ovh.run(deployment.app_id)
    run(h.manager.tick())
    return deployment


def test_catalog_entry_is_cpu_only():
    entry = CATALOG[ENTRY]
    assert (entry.flavor, entry.flavor_count, entry.gpu_count) == ("ai1-1-cpu", 4, 0)
    assert entry.price_eur_per_hour == pytest.approx(0.132)


def test_start_creates_a_labelled_app_with_a_scoped_token(harness):
    deployment = run(harness.manager.up(ENTRY, "tester"))
    app = harness.ovh.apps[deployment.app_id]
    labels = app["spec"]["labels"]
    assert labels[MANAGED_LABEL] == "true"
    assert labels[DEPLOYMENT_LABEL] == deployment.id
    assert labels[DEADLINE_LABEL] == str(int(deployment.deadline))
    assert harness.ovh.tokens[deployment.token_id] == f"{DEPLOYMENT_LABEL}={deployment.id}"
    assert app["spec"]["resources"] == {"flavor": "ai1-1-cpu", "flavorCount": 4}
    assert harness.ledger.reservations[deployment.id] == pytest.approx(0.132)


def test_deployment_becomes_ready_only_when_healthy(harness):
    deployment = run(harness.manager.up(ENTRY, "tester"))
    harness.ovh.run(deployment.app_id)
    harness.healthy = False
    run(harness.manager.tick())
    assert harness.manager.ready_deployment(ENTRY) is None
    harness.healthy = True
    run(harness.manager.tick())
    assert harness.manager.ready_deployment(ENTRY).status == READY


def test_second_start_of_same_entry_is_refused(harness):
    start(harness)
    with pytest.raises(Conflict):
        run(harness.manager.up(ENTRY, "tester"))


def test_budget_refusal_creates_nothing(harness):
    harness.ledger.refuse = True
    with pytest.raises(BudgetExceeded):
        run(harness.manager.up(ENTRY, "tester"))
    assert not harness.ovh.apps and not harness.ovh.tokens


def test_postgres_down_refuses_starts(harness):
    harness.ledger.available = False
    with pytest.raises(LedgerUnavailable):
        run(harness.manager.up(ENTRY, "tester"))
    assert not harness.ovh.apps


def test_quota_refusal_cleans_up_and_is_explained(harness):
    harness.ovh.fail_create = GpuQuotaError(402, "quota exceeded, please contact us")
    with pytest.raises(GpuQuotaError, match="GPU quota"):
        run(harness.manager.up(ENTRY, "tester"))
    assert not harness.ovh.tokens
    assert not harness.manager.deployments
    assert "start failed" in next(iter(harness.ledger.ended.values()))


def test_unrefreshed_deployment_is_gone_at_the_deadline(harness):
    deployment = start(harness, keep_when_idle=True)
    harness.clock.advance(3599)
    run(harness.manager.tick())
    assert deployment.app_id in harness.ovh.apps
    harness.clock.advance(1)
    run(harness.manager.tick())
    assert deployment.app_id not in harness.ovh.apps
    assert not harness.ovh.tokens
    assert harness.ledger.ended[deployment.id] == "TTL expired"


def test_deadline_is_enforced_with_postgres_down(harness):
    deployment = start(harness, keep_when_idle=True)
    harness.ledger.available = False
    harness.clock.advance(3600)
    run(harness.manager.tick())
    assert deployment.app_id not in harness.ovh.apps
    assert deployment.id not in harness.ledger.ended
    harness.ledger.available = True
    run(harness.manager.tick())
    assert harness.ledger.ended[deployment.id] == "TTL expired"


def test_deadline_is_enforced_after_a_restart(harness):
    deployment = start(harness, keep_when_idle=True)
    harness.restart()
    harness.clock.advance(3600)
    run(harness.manager.tick())
    assert deployment.app_id not in harness.ovh.apps


def test_refresh_moves_deadline_and_label(harness):
    deployment = start(harness)
    harness.clock.advance(1000)
    run(harness.manager.refresh(ENTRY))
    expected = harness.clock.now + 3600
    assert deployment.deadline == expected
    labels = harness.ovh.apps[deployment.app_id]["spec"]["labels"]
    assert labels[DEADLINE_LABEL] == str(int(expected))
    assert harness.ledger.deadlines[deployment.id] == expected


def test_refresh_is_undone_if_the_label_cannot_be_set(harness):
    deployment = start(harness)
    original = deployment.deadline
    harness.clock.advance(1000)
    harness.ovh.fail_label = True
    with pytest.raises(OvhError):
        run(harness.manager.refresh(ENTRY))
    assert deployment.deadline == original
    assert harness.ledger.deadlines[deployment.id] == original


def test_idle_teardown(harness):
    deployment = start(harness)
    harness.clock.advance(1199)
    run(harness.manager.tick())
    assert deployment.app_id in harness.ovh.apps
    harness.clock.advance(1)
    run(harness.manager.tick())
    assert harness.ledger.ended[deployment.id] == "idle"


def test_a_generating_response_delays_teardown_up_to_the_drain(harness):
    deployment = start(harness, keep_when_idle=True)
    harness.manager.begin_request(deployment)
    harness.clock.advance(3600)
    run(harness.manager.tick())
    assert deployment.status == TEARING_DOWN
    assert deployment.app_id in harness.ovh.apps
    harness.clock.advance(299)
    run(harness.manager.tick())
    assert deployment.app_id in harness.ovh.apps
    harness.clock.advance(1)
    run(harness.manager.tick())
    assert deployment.app_id not in harness.ovh.apps


def test_spending_cap_tears_down(harness):
    deployment = start(harness, keep_when_idle=True, max_spend_eur=0.05)
    harness.clock.advance(1400)
    run(harness.manager.tick())
    assert "cap" in harness.ledger.ended[deployment.id]


def test_failed_teardown_retries_every_minute_and_alerts(harness):
    deployment = start(harness, keep_when_idle=True)
    harness.ovh.fail_delete = True
    harness.clock.advance(3600)
    run(harness.manager.tick())
    assert harness.notifier.sent[-1] == ("TEARDOWN FAILED: billing continues", True)
    alerts = len(harness.notifier.sent)
    harness.clock.advance(30)
    run(harness.manager.tick())
    assert len(harness.notifier.sent) == alerts
    harness.clock.advance(30)
    run(harness.manager.tick())
    assert len(harness.notifier.sent) == alerts + 1
    harness.ovh.fail_delete = False
    harness.clock.advance(60)
    run(harness.manager.tick())
    assert deployment.app_id not in harness.ovh.apps


def test_failed_app_is_torn_down(harness):
    deployment = run(harness.manager.up(ENTRY, "tester"))
    harness.ovh.apps[deployment.app_id]["status"] = {"state": "FAILED"}
    run(harness.manager.tick())
    assert deployment.app_id not in harness.ovh.apps
    assert "FAILED" in harness.ledger.ended[deployment.id]


def test_app_never_ready_is_torn_down(harness):
    deployment = run(harness.manager.up(ENTRY, "tester"))
    harness.clock.advance(1200)
    run(harness.manager.tick())
    assert harness.ledger.ended[deployment.id] == "did not become ready in time"


def test_untracked_managed_app_is_deleted(harness):
    run(
        harness.ovh.create_app(
            {"labels": {MANAGED_LABEL: "true", DEPLOYMENT_LABEL: "unknown-deployment"}}
        )
    )
    tracked = start(harness)
    harness.clock.advance(60)
    run(harness.manager.tick())
    assert list(harness.ovh.apps) == [tracked.app_id]
    assert "Deleted untracked app" in harness.notifier.titles()


def test_budget_thresholds_notify_once(harness):
    harness.ledger.spent = 85.0
    run(harness.manager.tick())
    run(harness.manager.tick())
    titles = harness.notifier.titles()
    assert titles.count("Budget at 50 %") == 1
    assert titles.count("Budget at 80 %") == 1
    assert "Budget at 90 %" not in titles


def test_ovh_billing_above_ledger_alerts(harness):
    harness.ovh.usage = 5.0
    run(harness.manager.tick())
    assert ("OVH billing exceeds the ledger", True) in harness.notifier.sent


def test_token_secret_is_never_lost_across_a_restart(harness):
    deployment = start(harness)
    harness.restart()
    assert harness.manager.ready_deployment(ENTRY).token_value == deployment.token_value
