# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
python -m venv .venv && . .venv/bin/activate   # Windows Git Bash: .venv/Scripts/activate
pip install -e ".[test]"

python -m pytest                                        # all tests
python -m pytest tests/test_manager.py::test_idle_app_is_stopped   # one test

docker build -t llm-bivouac-proxy .                     # proxy image only
bash gen-secrets.sh                                     # create .env, fill generated secrets
```

Python 3.12 or later. On the Windows dev machine `python` is the Microsoft Store stub, so create the venv with `py -3.12`. No linter or formatter is configured.

`docker compose up` needs a filled `.env`, the external `monitoring` network, and a host directory `/run/ailab` writable by UID 990.

CI (`.github/workflows/publish-image.yaml`) only builds and publishes the image; it does not run the tests. Compose pulls `ghcr.io/funkysayu/llm-bivouac-proxy:main`, so a push to `main` is what changes the server after its next pull.

Operator commands are documented in [docs/cli.md](docs/cli.md).

## Architecture

The stack rents model servers on OVH AI Deploy only while they are needed. Request path:

```
Open WebUI -> LiteLLM -> proxy (src/llm_bivouac) -> OVH app running llama-server
```

The proxy is one asyncio process with three HTTP surfaces, wired in `__main__.py` and defined in `server.py`:

- **:8000** OpenAI-compatible API. Requires the proxy API key and an `X-OpenWebUI-User-Email` header, which is checked against the Authentik group. It only forwards to running apps and never starts one.
- **Unix socket** admin API, the only way to start or stop anything. `cli.py` is its client and must stay standard-library only, because it also runs on the host outside the image.
- **:9100** Prometheus metrics.

### OVH is the source of truth

The proxy has no database and no state file. Every operation is a translation of the OVH API:

- `app_spec.py` translates both ways. `build_app_spec` writes what must be remembered onto the app (labels for entry, deadline, spending cap and idle policy; the gateway token in `envVars`), and `parse_instance` reads it back as an `Instance`.
- `up`, `refresh`, `down` and the `/status`, `/instance` and `/budget` endpoints all query OVH directly.
- The budget is OVH's month-to-date usage plus the time reserved by running apps, up to their deadline labels.
- `Manager.tick()` lists the managed apps every 15 seconds and enforces limits on them. An app has ended when OVH stops reporting it, and that is the only thing that triggers "ended".

Adding a local record of something OVH already knows goes against this design. `Runtime` in `manager.py` holds only what OVH cannot know (requests in flight, last activity, health results, why a stop was requested) and may be lost on restart. The snapshot in `Manager._instances` exists only so the chat path avoids a call to OVH per message.

### Invariants to preserve

- **A stop is finished only when OVH says so.** An app is gone only on a 404 or state `DELETED`; an accepted `DELETE` proves nothing, and `DELETING` may still bill. Deletes must pass `force=true`, in both `ovh.py` and the watchdog, or OVH will not kill a running app. `bivouac down` must not report success before this.
- **Never probe `/health` through OVH's gateway.** The gateway answers it with a 204 itself, even for an app that does not exist.
- **The in-app watchdog is the backstop if the proxy or server dies.** `WATCHDOG` in `app_spec.py` is POSIX shell run inside the llama.cpp image. It reads only the `ailab-deadline` label, so a refresh that cannot set the label must fail, and `down` lowers it to now.
- **Unreadable means stop.** A missing or invalid deadline or spending-cap label parses as 0, and an unknown catalog entry is deleted.
- **Per-app tokens are secrets.** They live in the app's env on OVH and in `Instance.token`; `Manager.view` is what reaches the CLI and must not include them. The unused-token cleanup only touches tokens whose selector is `ailab-deployment=<id>`, so other tokens in the project are left alone.
- **Network isolation is intentional.** The `core` and `clients` networks are internal, and the comments in `docker-compose.yaml` explain each service's placement. Do not add a service to a network without reading them.

### Logging

Anything about one deployment goes through `instance_log(instance.deployment_id)` from `logs.py`, so a grep on the instance ID follows it from start to teardown. Log metadata only: never message content or token values.

Status messages meant for the person chatting are returned by `_chat_notice` as successful completions, because LiteLLM rewrites upstream errors into its own exception text.

### Making common changes

- **New model:** add a `CatalogEntry` in `catalog.py` and a named route in `litellm/config.yaml`. A wildcard route does not work, because LiteLLM then answers `/v1/models` from its own cost map.
- **New setting:** add it to `Config` and `load_config` in `config.py`, to `docker-compose.yaml`, and to `ENV` in `tests/conftest.py`. Most settings are required, so a missing one stops the process at startup.
- Compose sets some variables the code does not read yet (`AILAB_REFRESH_PROMPT_SECONDS`, `AILAB_WEIGHTS_RETENTION_DAYS`, `HF_TOKEN`, `OVH_S3_*`, `OVH_DATASTORE_ALIAS`). Check for a reader before assuming a setting has any effect.
- `VLLM_API_KEY` is the key for the model backend (`config.backend_api_key`), even though the only engine today is llama.cpp.

### Tests

`Manager` receives its OVH client, notifier, health probe and clock as arguments. In `tests/conftest.py`, `FakeOvh` is the only place app state lives, so lifecycle tests advance a fake `Clock` and call `tick()` instead of sleeping, and `Harness.restart()` builds a new `Manager` with nothing but what OVH holds.

`tests/test_watchdog.py` runs the real `WATCHDOG` script under bash with fake `curl` and `sleep`, so it is skipped when no POSIX shell is available.
