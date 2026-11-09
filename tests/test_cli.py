from __future__ import annotations

import argparse

import pytest

from llm_bivouac import cli

ENTRY = "smollm3-3b-q4-cpu"
APP = "9d12a800-ff22-49e7-8361-5a0ea7ddb40b"
ESTIMATE = {
    "display_name": "SmolLM3",
    "eur_per_hour": 0.132,
    "ttl_cost_eur": 0.132,
    "confirm_required": False,
}


def view(ready=False, state=None, health=None, reason=None, detail=None):
    return {
        "app_id": APP,
        "instance": "050db623",
        "ready": ready,
        "state": state,
        "info": None,
        "health": health,
        "stop_reason": reason,
        "stop_detail": detail,
    }


def fake_api(monkeypatch, polls):
    """Answer the CLI's calls; each /instance call consumes one entry of `polls`.

    None stands for the app no longer existing on OVH, which is a 404.
    """
    remaining = iter(polls)

    def call(method, path, _payload=None, *, missing_ok=False):
        if path.startswith("/estimate/"):
            return ESTIMATE
        if method == "POST":
            return view()
        answer = next(remaining)
        if answer is None and not missing_ok:
            raise SystemExit("error: app no longer exists on OVH: image pull failed")
        return answer

    monkeypatch.setattr(cli, "call", call)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)


def up_args(detach=False):
    return argparse.Namespace(
        entry=ENTRY, yes=True, detach=detach, max_spend=None, keep_when_idle=False
    )


def down_args():
    return argparse.Namespace(entry=ENTRY)


def test_up_waits_and_prints_each_change_once(monkeypatch, capsys):
    fake_api(
        monkeypatch,
        [
            view(state="QUEUED"),
            view(state="QUEUED"),
            view(state="RUNNING", health="HTTP 503 Loading model"),
            view(ready=True, state="RUNNING", health="healthy"),
        ],
    )
    cli.up(up_args())
    output = capsys.readouterr().out
    assert output.count("OVH QUEUED") == 1
    assert "health: HTTP 503 Loading model" in output
    assert output.rstrip().endswith(f"{ENTRY} is ready")


def test_up_detached_does_not_wait(monkeypatch, capsys):
    fake_api(monkeypatch, [])
    cli.up(up_args(detach=True))
    assert "bivouac status" in capsys.readouterr().out


def test_up_reports_why_it_is_being_stopped(monkeypatch):
    fake_api(monkeypatch, [view(reason="did not become ready")])
    with pytest.raises(SystemExit) as stop:
        cli.up(up_args())
    assert stop.value.code == "failed: did not become ready"


def test_up_reports_the_proxy_error_when_the_app_is_gone(monkeypatch):
    fake_api(monkeypatch, [None])
    with pytest.raises(SystemExit) as stop:
        cli.up(up_args())
    assert "image pull failed" in stop.value.code


def test_ctrl_c_stops_waiting_and_warns_that_it_is_still_billing(monkeypatch):
    fake_api(monkeypatch, [view(state="QUEUED")])

    def interrupted(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", interrupted)
    with pytest.raises(SystemExit) as stop:
        cli.up(up_args())
    assert "still starting and billing" in stop.value.code
    assert f"bivouac down {ENTRY}" in stop.value.code


def test_down_reports_success_only_after_ovh_stops_reporting_the_app(monkeypatch, capsys):
    fake_api(monkeypatch, [view(detail="OVH still reports app in state DELETING"), None])
    cli.down(down_args())
    output = capsys.readouterr().out
    assert "still reports app in state DELETING" in output
    assert output.rstrip().endswith("no longer exists")


def test_down_fails_if_ovh_never_confirms(monkeypatch, capsys):
    fake_api(monkeypatch, [view(detail="OVH still reports app in state RUNNING")] * 2)
    monkeypatch.setattr(cli, "DOWN_WAIT_SECONDS", 0)
    with pytest.raises(SystemExit) as stop:
        cli.down(down_args())
    assert "NOT confirmed stopped" in stop.value.code
    assert "stopped: OVH" not in capsys.readouterr().out
