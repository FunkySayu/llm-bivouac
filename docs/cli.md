# `bivouac` CLI

`bivouac` starts, extends and stops model deployments and shows what they cost. Open WebUI can only chat with a model that is already running, so this CLI is the only way to start one.

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
starting: app <app-id>, follow with `bivouac status`

$ bivouac status
smollm3-3b-q4-cpu  ready  ends in 57 min  0.132 €/h  in flight: 0  app: <app-id>

$ bivouac down smollm3-3b-q4-cpu
tearing down app <app-id>
```

Once the status is `ready`, the model can be used from Open WebUI.

## Deployments

### `bivouac catalog`

Lists the models that can be started, with their hourly price.

### `bivouac status`

Lists current deployments with their state, time left, price, requests in flight and OVH app ID.

| State | Meaning |
|---|---|
| `starting` | The app is being created or the model is loading. |
| `ready` | The model is serving requests. |
| `tearing_down` | The app is being deleted. A line below it gives the reason and the number of failed attempts. |

Prints `nothing running` if there are no deployments.

### `bivouac up <entry>`

Starts a catalog entry and returns immediately. Follow its progress with `bivouac status`.

Before starting, the proxy reserves the cost of one full TTL from the budget. The start is refused if that would go over the limit.

| Option | Effect |
|---|---|
| `--yes` | Skips the confirmation prompt, which appears for entries costing €3/h or more. |
| `--max-spend <eur>` | Sets this deployment's spending cap in euros, replacing the default. |
| `--keep-when-idle` | Disables idle teardown, for example during a benchmark. The TTL and spending cap still apply. |

Only one deployment per catalog entry can be active at a time.

### `bivouac refresh <entry>`

Moves the deadline to one full TTL from now. It sets the deadline rather than adding to it, so refreshing early does not stack time.

The extra time is reserved from the budget, and the refresh is refused if it doesn't fit.

> **The spending cap still applies.** The cap counts from the original start, so by default a deployment stops after about 2 hours of runtime however often you refresh it. To run longer, start it with a higher `--max-spend`.

### `bivouac down <entry>`

Stops a deployment. Responses still being generated get up to 5 minutes to finish, then the app is deleted. If deletion fails, the proxy retries every minute and sends an urgent notification on each failure.

The command returns immediately. Check `bivouac status` to see it disappear. A deployment that is already `tearing_down` counts as not running, so a second `down` returns an error.

## Automatic stops

A deployment is torn down without any command when one of these applies. Each stop sends an ntfy notification.

| Trigger | Default | Setting in `docker-compose.yaml` |
|---|---|---|
| TTL expires (warning 10 min before) | 1 hour | `AILAB_TTL_SECONDS`, `AILAB_TTL_WARNING_SECONDS` |
| No requests | 20 min | `AILAB_IDLE_TEARDOWN_SECONDS` |
| Spending cap reached | 2 hours of the entry's price, unless the catalog entry sets its own cap | `AILAB_MAX_SPEND_TTL_MULTIPLIER` |
| Not ready in time | 20 min | `AILAB_START_TIMEOUT_SECONDS` |
| The OVH app fails | Immediately | None |

If the proxy or the whole server is down, the app deletes itself about 7 minutes after its deadline.

## Budget and quota

### `bivouac budget show`

Prints the pool, the amount spent, and the amount reserved by running deployments for the current budget period.

The pool is €100, and new starts and refreshes are refused once spent plus reserved would pass €90. ntfy notifications go out at 50, 80 and 90% of the pool.

### `bivouac budget history`

Prints two lists without headings: spending per catalog entry for the current period, then spending per day (UTC) for up to the last 60 days with spending, across all periods.

### `bivouac budget renew`

Starts a new budget period with a full pool.

> **This runs without asking for confirmation.** Past spending stops counting toward the limit and nothing carries over. It changes only the proxy's budget, not what OVH bills you.

### `bivouac quota`

Prints OVH's quota for the project as raw JSON.

## Errors

Errors are printed as `error: <message>` and the command exits with status 1.

| Message | Cause | What to do |
|---|---|---|
| `cannot reach the proxy at ...` | The proxy is down, or you lack permission on the socket. | Check the `proxy` container, and use `docker compose exec` or root. |
| `unknown catalog entry ...` | The entry ID is wrong. | Check `bivouac catalog`. |
| `... is not running` | Nothing is active for that entry, or it is already tearing down. | Check `bivouac status`. |
| `... is already running` | That entry already has an active deployment. | Use `refresh` or `down`. |
| `... is still being created` | A refresh arrived before OVH had created the app. | Retry shortly. |
| `... needs N GPU and ...` | The start would exceed the limit of 4 GPUs. | Stop one of the listed deployments. |
| `Refused: ...` | The budget limit would be exceeded. | Stop something, or run `bivouac budget renew` if a reset is justified. |
| `Postgres is unavailable ...` | The budget ledger can't be reached. Running deployments are still stopped on time. | Check the `postgres` container. |
| `OVH refused the app because the project's GPU quota ...` | The OVH project has no GPU quota left. | CPU entries still work. |
| `OVH API returned ...` | Another error from OVH. | Read the message and retry. |
