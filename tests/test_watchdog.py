"""Runs the real in-app shell script against a fake curl and sleep."""

from __future__ import annotations

import shutil
import subprocess
import time

import pytest

from llm_bivouac.app_spec import WATCHDOG

APP = "5e189919-0b4a-4158-b353-46f9e140c6ea"
FAKE_CURL = r"""#!/bin/sh
for arg in "$@"; do [ "$arg" = DELETE ] && { echo "DELETE ${!#}" >> "$LOG"; exit 0; }; done
[ -f "$RESPONSE" ] || exit 7
cat "$RESPONSE"
"""
FAKE_SLEEP = r"""#!/bin/sh
count=$(cat "$COUNT" 2>/dev/null || echo 0)
echo $((count + 1)) > "$COUNT"
[ "$DROP_API" = 1 ] && rm -f "$RESPONSE"
[ "$count" -ge "$MAX_LOOPS" ] && kill -TERM "$PPID"
exit 0
"""


@pytest.fixture
def sh():
    path = shutil.which("bash") or shutil.which("sh")
    if path is None:
        pytest.skip("no POSIX shell available")
    return path


def run_watchdog(sh, tmp_path, *, deadline, grace, first=None, drop_api=False, loops=2):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text("#!/bin/bash\n" + FAKE_CURL.split("\n", 1)[1], newline="\n")
    sleep = bin_dir / "sleep"
    sleep.write_text(FAKE_SLEEP, newline="\n")
    log = tmp_path / "log"
    response = tmp_path / "response"
    if first is not None:
        response.write_text(first)
    script = tmp_path / "script.sh"
    script.write_text(WATCHDOG + "\nwatchdog\n", newline="\n")

    def posix(path):
        text = str(path).replace("\\", "/")
        return f"/{text[0].lower()}{text[2:]}" if text[1:2] == ":" else text

    env_script = tmp_path / "launch.sh"
    env_script.write_text(
        "\n".join(
            [
                f'export PATH="{posix(bin_dir)}:$PATH"',
                f"export LOG='{posix(log)}' RESPONSE='{posix(response)}'",
                f"export DROP_API={int(drop_api)} COUNT='{posix(tmp_path / 'count')}'",
                f"export MAX_LOOPS={loops}",
                f"export AILAB_DEADLINE={deadline} AILAB_SELF_DESTRUCT_GRACE={grace}",
                "export AILAB_OPERATOR_TOKEN=token AILAB_AI_API=https://ai.example",
                "export AILAB_DEPLOYMENT=abc AILAB_DEPLOYMENT_LABEL=ailab-deployment",
                "export AILAB_DEADLINE_LABEL=ailab-deadline AILAB_WATCHDOG_INTERVAL=0",
                f"chmod +x '{posix(curl)}' '{posix(sleep)}'",
                f". '{posix(script)}'",
            ]
        ),
        newline="\n",
    )
    subprocess.run([sh, posix(env_script)], timeout=30, check=False, capture_output=True)
    return log.read_text() if log.exists() else ""


def body(deadline):
    return (
        f'[{{"id": "{APP}", "spec": {{"labels": '
        f'{{"ailab-deadline": "{deadline}", "ailab-deployment": "abc"}}}}}}]\n'
    )


def test_delete_is_forced_because_ovh_will_not_otherwise_kill_a_running_app():
    assert '"$apps/$app_id?force=true"' in WATCHDOG


def test_deletes_itself_after_the_deadline(sh, tmp_path):
    past = int(time.time()) - 1000
    out = run_watchdog(sh, tmp_path, deadline=past, grace=60, first=body(past))
    assert f"https://ai.example/v1/app/{APP}" in out


def test_leaves_the_app_alone_before_the_deadline(sh, tmp_path):
    future = int(time.time()) + 1000
    assert run_watchdog(sh, tmp_path, deadline=future, grace=60, first=body(future)) == ""


def test_a_refreshed_label_postpones_the_deletion(sh, tmp_path):
    past = int(time.time()) - 1000
    future = int(time.time()) + 1000
    assert run_watchdog(sh, tmp_path, deadline=past, grace=60, first=body(future)) == ""


def test_an_expired_label_overrides_the_start_deadline(sh, tmp_path):
    past = int(time.time()) - 1000
    future = int(time.time()) + 1000
    out = run_watchdog(sh, tmp_path, deadline=future, grace=60, first=body(past))
    assert APP in out


def test_keeps_the_last_deadline_when_the_api_goes_away(sh, tmp_path):
    past = int(time.time()) - 1000
    future = int(time.time()) + 1000
    # The start deadline has passed, but the last value read before OVH's API
    # became unreachable is in the future, and that is the one to honour.
    out = run_watchdog(sh, tmp_path, deadline=past, grace=60, first=body(future), drop_api=True)
    assert out == ""
    assert int((tmp_path / "count").read_text()) >= 2


def test_does_nothing_without_an_app_id(sh, tmp_path):
    # With the API down from the start the app cannot find its own ID to delete.
    past = int(time.time()) - 1000
    assert run_watchdog(sh, tmp_path, deadline=past, grace=60) == ""
