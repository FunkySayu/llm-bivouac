"""Settings read from the environment set in docker-compose.yaml."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    budget_pool_eur: float
    budget_refuse_at_eur: float
    budget_notify_percent: tuple[int, ...]
    max_gpus: int
    max_spend_ttl_multiplier: float
    cli_confirm_eur_per_hour: float
    owui_max_eur_per_hour: float
    ttl_seconds: int
    ttl_warning_seconds: int
    idle_teardown_seconds: int
    teardown_drain_seconds: int
    start_timeout_seconds: int
    listen_port: int
    metrics_port: int
    socket_path: str
    state_dir: str
    proxy_api_key: str
    backend_api_key: str
    database_url: str
    ovh_api_base_url: str
    ovh_token_url: str
    ovh_ai_api_url: str
    ovh_region: str
    ovh_project_id: str
    ovh_client_id: str
    ovh_client_secret: str
    authentik_url: str
    authentik_api_token: str
    authentik_group: str
    authentik_cache_seconds: int
    ntfy_url: str
    ntfy_topic: str
    ntfy_publish_token: str


def load_config(env: Mapping[str, str] | None = None) -> Config:
    source = os.environ if env is None else env

    def need(name: str) -> str:
        try:
            value = source[name]
        except KeyError:
            raise SystemExit(f"missing required environment variable {name}") from None
        if not value:
            raise SystemExit(f"environment variable {name} is empty")
        return value

    return Config(
        budget_pool_eur=float(need("AILAB_BUDGET_POOL_EUR")),
        budget_refuse_at_eur=float(need("AILAB_BUDGET_REFUSE_AT_EUR")),
        budget_notify_percent=tuple(
            int(p) for p in need("AILAB_BUDGET_NOTIFY_PERCENT").split(",")
        ),
        max_gpus=int(need("AILAB_MAX_GPUS")),
        max_spend_ttl_multiplier=float(need("AILAB_MAX_SPEND_TTL_MULTIPLIER")),
        cli_confirm_eur_per_hour=float(need("AILAB_CLI_CONFIRM_EUR_PER_HOUR")),
        owui_max_eur_per_hour=float(need("AILAB_OWUI_MAX_EUR_PER_HOUR")),
        ttl_seconds=int(need("AILAB_TTL_SECONDS")),
        ttl_warning_seconds=int(need("AILAB_TTL_WARNING_SECONDS")),
        idle_teardown_seconds=int(need("AILAB_IDLE_TEARDOWN_SECONDS")),
        teardown_drain_seconds=int(need("AILAB_TEARDOWN_DRAIN_SECONDS")),
        start_timeout_seconds=int(source.get("AILAB_START_TIMEOUT_SECONDS", "1200")),
        listen_port=int(need("AILAB_LISTEN_PORT")),
        metrics_port=int(need("AILAB_METRICS_PORT")),
        socket_path=need("AILAB_SOCKET_PATH"),
        state_dir=need("AILAB_STATE_DIR"),
        proxy_api_key=need("AILAB_PROXY_API_KEY"),
        backend_api_key=need("VLLM_API_KEY"),
        database_url=need("DATABASE_URL"),
        ovh_api_base_url=need("OVH_API_BASE_URL").rstrip("/"),
        ovh_token_url=source.get("OVH_TOKEN_URL", "https://www.ovh.com/auth/oauth2/token"),
        ovh_ai_api_url=source.get("OVH_AI_API_URL", "https://gra.ai.cloud.ovh.net").rstrip("/"),
        ovh_region=need("OVH_REGION"),
        ovh_project_id=need("OVH_PROJECT_ID"),
        ovh_client_id=need("OVH_CLIENT_ID"),
        ovh_client_secret=need("OVH_CLIENT_SECRET"),
        authentik_url=need("AUTHENTIK_URL").rstrip("/"),
        authentik_api_token=need("AUTHENTIK_API_TOKEN"),
        authentik_group=need("AUTHENTIK_GROUP"),
        authentik_cache_seconds=int(need("AUTHENTIK_CACHE_SECONDS")),
        ntfy_url=need("NTFY_URL").rstrip("/"),
        ntfy_topic=need("NTFY_TOPIC"),
        ntfy_publish_token=need("NTFY_PUBLISH_TOKEN"),
    )
