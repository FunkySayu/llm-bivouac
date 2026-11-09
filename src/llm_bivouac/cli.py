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


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=TIMEOUT_SECONDS)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(TIMEOUT_SECONDS)
        self.sock.connect(self._path)


def call(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
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
    if response.status >= 400:
        sys.exit(f"error: {data.get('error', response.status)}")
    return data


def _minutes_left(deadline: float) -> str:
    return f"{max(0, round((deadline - time.time()) / 60))} min"


def show_status(_: argparse.Namespace) -> None:
    deployments = call("GET", "/status")["deployments"]
    if not deployments:
        print("nothing running")
    for d in deployments:
        print(
            f"{d['entry_id']}  {d['status']}  ends in {_minutes_left(d['deadline'])}  "
            f"{d['price_eur_per_hour']:.3f} €/h  in flight: {d['inflight']}  app: {d['app_id']}"
        )
        if d["teardown_reason"]:
            print(f"  teardown: {d['teardown_reason']} ({d['teardown_failures']} failures)")


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
    deployment = call(
        "POST",
        f"/up/{args.entry}",
        {"max_spend_eur": args.max_spend, "keep_when_idle": args.keep_when_idle},
    )
    print(f"starting: app {deployment['app_id']}, follow with `bivouac status`")


def refresh(args: argparse.Namespace) -> None:
    deployment = call("POST", f"/refresh/{args.entry}")
    print(f"{args.entry} now ends in {_minutes_left(deployment['deadline'])}")


def down(args: argparse.Namespace) -> None:
    deployment = call("POST", f"/down/{args.entry}")
    print(f"tearing down app {deployment['app_id']}")


def budget(args: argparse.Namespace) -> None:
    if args.action == "history":
        history = call("GET", "/budget/history")
        for row in history["by_entry"]:
            print(f"{row['entry_id']}  {row['spent_eur']:.2f} €")
        for row in history["by_day"]:
            print(f"{row['day']}  {row['spent_eur']:.2f} €")
        return
    summary = call("POST", "/budget/renew") if args.action == "renew" else call("GET", "/budget")
    print(
        f"pool {summary['pool_eur']:.2f} €, spent {summary['spent_eur']:.2f} €, "
        f"reserved {summary['reserved_eur']:.2f} €"
    )


def quota(_: argparse.Namespace) -> None:
    print(json.dumps(call("GET", "/quota")))


def main() -> None:
    parser = argparse.ArgumentParser(prog="bivouac")
    commands = parser.add_subparsers(required=True)
    commands.add_parser("status", help="list deployments").set_defaults(run=show_status)
    commands.add_parser("catalog", help="list catalog entries").set_defaults(run=show_catalog)
    commands.add_parser("quota", help="show the OVH quota").set_defaults(run=quota)

    up_parser = commands.add_parser("up", help="start a deployment")
    up_parser.add_argument("entry")
    up_parser.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    up_parser.add_argument("--max-spend", type=float, help="spending cap in €")
    up_parser.add_argument(
        "--keep-when-idle", action="store_true", help="disable idle teardown, for benchmarks"
    )
    up_parser.set_defaults(run=up)

    for name, run, description in (
        ("refresh", refresh, "extend a deployment by a full TTL"),
        ("down", down, "stop a deployment"),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("entry")
        command.set_defaults(run=run)

    budget_parser = commands.add_parser("budget", help="show or renew the budget")
    budget_parser.add_argument("action", choices=("show", "history", "renew"))
    budget_parser.set_defaults(run=budget)

    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
