# `bivouac` CLI

`bivouac` starts, extends and stops model deployments and shows what they cost. Open WebUI can only chat with a model that is already running, so this CLI is the only way to start one.

What it shows comes from the OVH API, which is the only record of what is running.

## Running it

The CLI talks to the proxy over a Unix socket and holds no credentials of its own. The socket is `/run/ailab/ailab.sock` with mode `0600`, owned by the proxy's user (UID 990).

From the directory holding `docker-compose.yaml`, on the server:

```sh
docker compose exec proxy bivouac status
```

The CLI uses only the standard library, so you can also run it directly on the host as root:

```sh
sudo python3 src/llm_bivouac/cli.py status
```

Set `AILAB_SOCKET_PATH` if the socket is somewhere else.

The examples below shorten the command to `bivouac`.

## Typical session

```console
$ bivouac catalog
smollm3-3b-q4-cpu  0.132 €/h  SmolLM3 3B · Q4_K_M · llama.cpp · 4×ai1-1-cpu · ctx M

$ bivouac up smollm3-3b-q4-cpu
SmolLM3 3B · Q4_K_M · llama.cpp · 4×ai1-1-cpu · ctx M
0.132 €/h, 0.132 € for a full TTL
starting: instance 050db623, app <app-id>
waiting until ready (Ctrl-C stops waiting, not the deployment)
[   2 s] starting · OVH QUEUED
[  17 s] starting · OVH INITIALIZING · Pulling app's data
[  62 s] starting · OVH RUNNING · App is running · health: HTTP 503 Loading model
[ 182 s] ready · OVH RUNNING · App is running · health: healthy
smollm3-3b-q4-cpu is ready

$ bivouac status
smollm3-3b-q4-cpu  ready  ends in 57 min  0.132 €/h  in flight: 0  instance: 050db623  app: <app-id>

$ bivouac down smollm3-3b-q4-cpu
tearing down app <app-id>, waiting for OVH to confirm
stopped: OVH confirms app <app-id> no longer exists
```

The states and timings above are only an example. Once `up` reports `ready`, the model can be used from Open WebUI. A message sent before that gets a reply saying how far the startup has got.

## Deployments

### `bivouac catalog`

Lists the models that can be started, with their hourly price.

### `bivouac status`

Lists the apps OVH reports, with their phase, time left, price, requests in flight, instance ID and OVH app ID. A deployment that is still starting also gets a line showing its progress.

| Phase | Meaning |
|---|---|
| `starting` | OVH is creating the app or the model is loading. |
| `ready` | The model is serving requests. |
| `stopping` | The app is being deleted. A line below it gives the reason. |

Prints `nothing running` if OVH reports no managed apps.

### `bivouac up <entry>`

Starts a catalog entry and waits until it is ready, printing each change in OVH's state and the health check. It exits with status 1 and the reason if the deployment fails.

Ctrl-C stops the waiting only. The deployment keeps starting and billing until you run `bivouac down`.

The start is refused if this month's OVH usage, plus the time reserved by running apps and one full TTL for this one, would go over the budget limit.

| Option | Effect |
|---|---|
| `-d`, `--detach` | Returns as soon as the app is created, without waiting. Follow it with `bivouac status`. |
| `--yes` | Skips the confirmation prompt, which appears for entries costing €3/h or more. |
| `--max-spend <eur>` | Sets this deployment's spending cap in euros, replacing the default. |
| `--keep-when-idle` | Disables idle teardown, for example during a benchmark. The TTL and spending cap still apply. |

Only one deployment per catalog entry can be active at a time.

### `bivouac refresh <entry>`

Moves the deadline to one full TTL from now. It sets the deadline rather than adding to it, so refreshing early does not stack time. The refresh is refused if the extra time doesn't fit in the budget.

> **The spending cap still applies.** The cap counts from the original start, so by default a deployment stops after about 2 hours of runtime however often you refresh it. To run longer, start it with a higher `--max-spend`.

### `bivouac down <entry>`

Stops a deployment and waits until OVH no longer reports the app. Responses still being generated get up to 5 minutes to finish, then the app is force-deleted.

The command reports success only after that confirmation. If it isn't confirmed within 10 minutes, it exits with status 1 and says the deployment may still be billing. The proxy keeps retrying every minute and sends an urgent notification on each failure.

Ctrl-C stops the waiting only. Running `down` again on a deployment that is already stopping follows the same teardown.

## Automatic stops

A deployment is torn down without any command when one of these applies. Each stop sends an ntfy notification.

| Trigger | Default | Setting in `docker-compose.yaml` |
|---|---|---|
| TTL expires (warning 10 min before) | 1 hour | `AILAB_TTL_SECONDS`, `AILAB_TTL_WARNING_SECONDS` |
| No requests | 20 min | `AILAB_IDLE_TEARDOWN_SECONDS` |
| Spending cap reached | 2 hours of the entry's price, unless the catalog entry sets its own cap | `AILAB_MAX_SPEND_TTL_MULTIPLIER` |
| Not ready in time | 20 min | `AILAB_START_TIMEOUT_SECONDS` |
| The OVH app fails | Next check, within 15 s | None |
| The app has labels the proxy can't read, or its model left the catalog | Next check, within 15 s | None |

If the proxy or the whole server is down, the app deletes itself about 7 minutes after its deadline.

A proxy restart loses nothing important: the deadline, cap and model are stored on the app. It only restarts the idle timer.

## Budget and quota

### `bivouac budget`

Prints what OVH reports for this calendar month, the amount reserved by running apps, the pool and the limit.

The pool is €100 per month, and new starts and refreshes are refused once usage plus reserved time would pass €90. ntfy notifications go out at 50, 80 and 90% of the pool.

The usage figure covers the whole OVH project, including anything not started by `bivouac`, and OVH may update it with some delay.

### `bivouac quota`

Prints OVH's quota for the project as raw JSON.

## Logs

The proxy logs to the container's output, and every line about a deployment starts with its instance ID:

```sh
docker compose logs -f proxy               # everything
docker compose logs proxy | grep 050db623  # one deployment
```

| Event | Example |
|---|---|
| Commands and refusals | `admin POST /up/<entry> -> 200` |
| Start and OVH state changes | `[050db623] OVH app state: INITIALIZING (Pulling app's data)` |
| Health and reachability, when they change | `[050db623] health: HTTP 503 Loading model (was: None)` |
| Incoming message | `[050db623] incoming message: user=<email> chat=<id> messages=3 stream=True` |
| The model finished | `[050db623] conversation ended by the model: user=<email> chat=<id> finish_reason=stop tokens=120 duration=8.4s` |
| The client or backend dropped | `[050db623] conversation ended by the client: ...`, `[050db623] conversation lost: ...` |
| Stops | `[050db623] stop requested: idle (0 requests in flight)`, `[050db623] ended after 3600 s, OVH no longer reports app <app-id>: TTL expired` |

Message content is never logged, only who sent it, the chat ID and the number of messages.

`finish_reason=stop` means the model finished on its own, and `length` means it hit the token limit.

## Errors

Errors are printed as `error: <message>` and the command exits with status 1.

| Message | Cause | What to do |
|---|---|---|
| `cannot reach the proxy at ...` | The proxy is down, or you lack permission on the socket. | Check the `proxy` container, and use `docker compose exec` or root. |
| `unknown catalog entry ...` | The entry ID is wrong. | Check `bivouac catalog`. |
| `... is not running` | OVH reports no app for that entry. | Check `bivouac status`. |
| `... is already running` | That entry already has an active deployment. | Use `refresh` or `down`. |
| `... no longer exists on OVH: ...` | The app disappeared while `up` was waiting. The reason follows. | Read the reason, or grep the logs for the instance ID. |
| `... is NOT confirmed stopped ...` | OVH still reports the app 10 minutes after `down`. It may still be billing. | Check the app in OVH's console. |
| `... needs N GPU and ...` | The start would exceed the limit of 4 GPUs. | Stop one of the listed deployments. |
| `Refused: ...` | The budget limit would be exceeded. | Stop something, or wait for next month's usage to start. |
| `OVH refused the app because the project's GPU quota ...` | The OVH project has no GPU quota left. | CPU entries still work. |
| `OVH API returned ...` | Any command needs OVH, so an OVH error stops it. | Read the message and retry. |
