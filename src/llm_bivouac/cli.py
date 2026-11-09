"""`bivouac`: thin client for the proxy's admin socket.

Standard library only, so it also runs from the host outside the image. It holds
no credentials: access is whatever the Unix socket's permissions allow.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import sys
import time
from typing import Any

SOCKET_PATH = os.environ.get("AILAB_SOCKET_PATH", "/run/ailab/ailab.sock")
TIMEOUT_SECONDS = 120
POLL_SECONDS = 3
# Covers the 5 min drain for running responses and OVH's own deletion time.
DOWN_WAIT_SECONDS = 600


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=TIMEOUT_SECONDS)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(TIMEOUT_SECONDS)
        self.sock.connect(self._path)


def call(
    method: str, path: str, payload: dict[str, Any] | None = None, *, missing_ok: bool = False
) -> Any:
    connection = _UnixConnection(SOCKET_PATH)
    try:
        body = json.dumps(payload or {}) if method == "POST" else None
        connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read() or b"{}")
    except OSError as error:
        sys.exit(f"cannot reach the proxy at {SOCKET_PATH}: {error}")
    finally:
        connection.close()
    if response.status == 404 and missing_ok:
        return None
    if response.status >= 400:
        sys.exit(f"error: {data.get('error', response.status)}")
    return data


def _minutes_left(deadline: float) -> str:
    return f"{max(0, round((deadline - time.time()) / 60))} min"


def _phase(instance: dict[str, Any]) -> str:
    if instance["stop_reason"]:
        return "stopping"
    return "ready" if instance["ready"] else "starting"


def _progress(instance: dict[str, Any]) -> str:
    parts = [_phase(instance)]
    if instance["state"]:
        parts.append(f"OVH {instance['state']}")
    if instance["info"]:
        parts.append(instance["info"])
    if instance["health"]:
        parts.append(f"health: {instance['health']}")
    return " · ".join(parts)


def _wait_until_ready(app_id: str) -> None:
    """Print each change of a starting app. Exits with an error if it fails."""
    started = time.monotonic()
    last = None
    while True:
        current = call("GET", f"/instance/{app_id}")
        progress = _progress(current)
        if progress != last:
            print(f"[{int(time.monotonic() - started):4d} s] {progress}", flush=True)
            last = progress
        if current["stop_reason"]:
            sys.exit(f"failed: {current['stop_reason']}")
        if current["ready"]:
            return
        time.sleep(POLL_SECONDS)


def _wait_until_gone(app_id: str, entry: str) -> None:
    """Return only once OVH no longer reports the app."""
    started = time.monotonic()
    last = None
    while True:
        current = call("GET", f"/instance/{app_id}", missing_ok=True)
        if current is None:
            return
        detail = current["stop_detail"]
        if detail and detail != last:
            print(f"[{int(time.monotonic() - started):4d} s] {detail}", flush=True)
            last = detail
        if time.monotonic() - started >= DOWN_WAIT_SECONDS:
            sys.exit(
                f"error: {entry} is NOT confirmed stopped and may still be billing: "
                f"{detail or 'no answer from OVH yet'}. The proxy keeps retrying."
            )
        time.sleep(POLL_SECONDS)


def show_status(_: argparse.Namespace) -> None:
    instances = call("GET", "/status")["instances"]
    if not instances:
        print("nothing running")
    for i in instances:
        print(
            f"{i['entry_id']}  {_phase(i)}  ends in {_minutes_left(i['deadline'])}  "
            f"{i['eur_per_hour']:.3f} €/h  in flight: {i['inflight']}  "
            f"instance: {i['instance']}  app: {i['app_id']}"
        )
        if _phase(i) == "starting":
            print(f"  progress: {_progress(i)}")
        if i["stop_reason"]:
            print(f"  stopping: {i['stop_reason']}")


def show_catalog(_: argparse.Namespace) -> None:
    for entry in call("GET", "/catalog")["entries"]:
        print(f"{entry['entry_id']}  {entry['eur_per_hour']:.3f} €/h  {entry['display_name']}")


def up(args: argparse.Namespace) -> None:
    estimate = call("GET", f"/estimate/{args.entry}")
    print(
        f"{estimate['display_name']}\n"
        f"{estimate['eur_per_hour']:.3f} €/h, {estimate['ttl_cost_eur']:.3f} € for a full TTL"
    )
    if estimate["confirm_required"] and not args.yes:
        if input("Start it? [y/N] ").strip().lower() != "y":
            sys.exit("cancelled")
    started = call(
        "POST",
        f"/up/{args.entry}",
        {"max_spend_eur": args.max_spend, "keep_when_idle": args.keep_when_idle},
    )
    print(f"starting: instance {started['instance']}, app {started['app_id']}", flush=True)
    if args.detach:
        print("follow with `bivouac status`")
        return
    print("waiting until ready (Ctrl-C stops waiting, not the deployment)", flush=True)
    try:
        _wait_until_ready(started["app_id"])
    except KeyboardInterrupt:
        sys.exit(
            f"\nstopped waiting: {args.entry} is still starting and billing. "
            f"Follow it with `bivouac status`, stop it with `bivouac down {args.entry}`."
        )
    print(f"{args.entry} is ready")


def refresh(args: argparse.Namespace) -> None:
    instance = call("POST", f"/refresh/{args.entry}")
    print(f"{args.entry} now ends in {_minutes_left(instance['deadline'])}")


def down(args: argparse.Namespace) -> None:
    instance = call("POST", f"/down/{args.entry}")
    print(f"tearing down app {instance['app_id']}, waiting for OVH to confirm", flush=True)
    try:
        _wait_until_gone(instance["app_id"], args.entry)
    except KeyboardInterrupt:
        sys.exit(
            f"\nstopped waiting: {args.entry} is NOT confirmed stopped and may still be billing. "
            "The proxy keeps retrying; check with `bivouac status`."
        )
    print(f"stopped: OVH confirms app {instance['app_id']} no longer exists")


def budget(_: argparse.Namespace) -> None:
    summary = call("GET", "/budget")
    print(
        f"OVH reports {summary['used_eur']:.2f} € this month, "
        f"{summary['reserved_eur']:.2f} € reserved by running apps; "
        f"pool {summary['pool_eur']:.2f} €, new starts refused past "
        f"{summary['refuse_at_eur']:.2f} €"
    )


def quota(_: argparse.Namespace) -> None:
    print(json.dumps(call("GET", "/quota")))


def main() -> None:
    parser = argparse.ArgumentParser(prog="bivouac")
    commands = parser.add_subparsers(required=True)
    commands.add_parser("status", help="list deployments").set_defaults(run=show_status)
    commands.add_parser("catalog", help="list catalog entries").set_defaults(run=show_catalog)
    commands.add_parser("quota", help="show the OVH quota").set_defaults(run=quota)
    commands.add_parser("budget", help="show this month's spending").set_defaults(run=budget)

    up_parser = commands.add_parser("up", help="start a deployment and wait until it is ready")
    up_parser.add_argument("entry")
    up_parser.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    up_parser.add_argument(
        "-d", "--detach", action="store_true", help="return without waiting until it is ready"
    )
    up_parser.add_argument("--max-spend", type=float, help="spending cap in €")
    up_parser.add_argument(
        "--keep-when-idle", action="store_true", help="disable idle teardown, for benchmarks"
    )
    up_parser.set_defaults(run=up)

    for name, run, description in (
        ("refresh", refresh, "extend a deployment by a full TTL"),
        ("down", down, "stop a deployment and wait until OVH confirms it is gone"),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("entry")
        command.set_defaults(run=run)

    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
