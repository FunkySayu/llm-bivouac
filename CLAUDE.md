# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
python -m venv .venv && . .venv/bin/activate   # Windows Git Bash: .venv/Scripts/activate
pip install -e ".[test]"

python -m pytest                                        # all tests
python -m pytest tests/test_manager.py::test_idle_teardown   # one test

docker build -t llm-bivouac-proxy .                     # proxy image only
./gen-secrets.sh                                        # create .env, fill generated secrets
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

- **:8000** OpenAI-compatible API. Requires the proxy API key and an `X-OpenWebUI-User-Email` header, which is checked against the Authentik group. It only forwards to running deployments and never starts one.
- **Unix socket** admin API, the only way to start or stop anything. `cli.py` is its client and must stay standard-library only, because it also runs on the host outside the image.
- **:9100** Prometheus metrics.

`Manager` (`manager.py`) owns the lifecycle. A 15-second `tick()` advances each deployment through `starting`, `ready` and `tearing_down`, and runs the periodic reconcile, budget and billing-drift checks.

### Two stores with different jobs

- `state.py` (`deadlines.json`) holds live deployments and is what the controller acts on.
- `ledger.py` (Postgres) is the source of truth for spending. Cost is derived from wall-clock time and price, and a live deployment reserves everything up to its deadline.

Ends that Postgres could not record wait in `pending_ledger_ends` and are flushed later.

### Invariants to preserve

- **Teardown must never depend on Postgres or ntfy.** Without Postgres, new starts and refreshes are refused but deadlines are still enforced. Optional controller steps go through `_guarded` so their failure cannot stop enforcement. This is also why `proxy` deliberately has no `depends_on: postgres`.
- **Persist before the external side effect.** A deployment is saved to the state file before its OVH app is created, and teardown looks apps up by label because a timed-out create may still have succeeded.
- **The in-app watchdog is the backstop if the proxy or server dies.** `WATCHDOG` in `app_spec.py` is POSIX shell run inside the llama.cpp image. A refresh reaches it only through the `ailab-deadline` app label, so a refresh that cannot set the label must fail and roll back the ledger.
- **Per-deployment OVH tokens are secrets.** They live in the state file (mode 0600) and `_describe` strips them from admin responses.
- **Network isolation is intentional.** The `core` and `clients` networks are internal, and the comments in `docker-compose.yaml` explain each service's placement. Do not add a service to a network without reading them.

### Making common changes

- **New model:** add a `CatalogEntry` in `catalog.py` and a named route in `litellm/config.yaml`. A wildcard route does not work, because LiteLLM then answers `/v1/models` from its own cost map.
- **New setting:** add it to `Config` and `load_config` in `config.py`, to `docker-compose.yaml`, and to `ENV` in `tests/conftest.py`. Most settings are required, so a missing one stops the process at startup.
- Compose sets some variables the code does not read yet (`AILAB_REFRESH_PROMPT_SECONDS`, `AILAB_WEIGHTS_RETENTION_DAYS`, `HF_TOKEN`, `OVH_S3_*`, `OVH_DATASTORE_ALIAS`), and `owui_max_eur_per_hour` is loaded but unused. Check for a reader before assuming a setting has any effect.
- `VLLM_API_KEY` is the key for the model backend (`config.backend_api_key`), even though the only engine today is llama.cpp.

### Tests

`Manager` receives its ledger, OVH client, notifier, health probe and clock as arguments. `tests/conftest.py` provides fakes and a `Harness`, so lifecycle tests advance a fake `Clock` and call `tick()` instead of sleeping; `Harness.restart()` simulates a process restart from the state file.

`tests/test_watchdog.py` runs the real `WATCHDOG` script under bash with fake `curl` and `sleep`, so it is skipped when no POSIX shell is available.
