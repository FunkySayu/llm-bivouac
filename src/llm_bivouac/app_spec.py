"""Builds the AI Deploy app for a catalog entry, including its self-destruct.

AI Deploy has no maximum run time, so each app deletes itself if the proxy (or
the whole server) disappears. The watchdog re-reads the deadline from the app's
own label, which is how a refresh reaches it, and keeps the last value it saw
if OVH's API cannot be reached. The app's ID is not known when it is created,
so the watchdog finds itself through the deployment label its token is limited to.
"""

from __future__ import annotations

from .catalog import CatalogEntry

MANAGED_LABEL = "ailab-managed"
DEPLOYMENT_LABEL = "ailab-deployment"
DEADLINE_LABEL = "ailab-deadline"
ENGINE_PORT = 8080

# Past this margin after the deadline the proxy has clearly failed to act.
WATCHDOG_MARGIN_SECONDS = 120

WATCHDOG = r"""
watchdog() {
  deadline="$AILAB_DEADLINE"
  app_id=""
  apps="$AILAB_AI_API/v1/app"
  while :; do
    if body=$(curl -fsS --max-time 20 -H "Authorization: Bearer $AILAB_OPERATOR_TOKEN" \
        "$apps?labelSelector=$AILAB_DEPLOYMENT_LABEL%3D$AILAB_DEPLOYMENT" 2>/dev/null); then
      flat=$(printf %s "$body" | tr -d ' \n')
      seen=$(printf %s "$flat" | sed -n "s/.*\"$AILAB_DEADLINE_LABEL\":\"\([0-9][0-9]*\)\".*/\1/p")
      [ -n "$seen" ] && deadline="$seen"
      seen=$(printf %s "$flat" | sed -n 's/^\[{"id":"\([0-9a-f-]*\)".*/\1/p')
      [ -n "$seen" ] && app_id="$seen"
    fi
    if [ "$(date +%s)" -gt "$((deadline + AILAB_SELF_DESTRUCT_GRACE))" ] && [ -n "$app_id" ]; then
      echo "ailab watchdog: deadline passed, deleting app $app_id" >&2
      curl -fsS --max-time 20 -X DELETE -H "Authorization: Bearer $AILAB_OPERATOR_TOKEN" \
        "$apps/$app_id" >/dev/null 2>&1
    fi
    sleep "${AILAB_WATCHDOG_INTERVAL:-60}"
  done
}
"""

# The operator token is accepted as a key too: OVH's gateway and llama-server both
# read Authorization, and whether the gateway forwards that header is still untested.
# X-Api-Key carries the second lock if the gateway strips it.
START_LLAMA_CPP = r"""
watchdog &
exec /app/llama-server --host 0.0.0.0 --port "$AILAB_ENGINE_PORT" \
  --hf-repo "$AILAB_HF_REPO" --hf-file "$AILAB_HF_FILE" \
  --ctx-size "$AILAB_CTX_TOKENS" --parallel "$AILAB_PARALLEL" --threads "$AILAB_THREADS" \
  --api-key "$AILAB_BACKEND_API_KEY,$AILAB_OPERATOR_TOKEN" --jinja --metrics --no-webui
"""


def build_app_spec(
    *,
    deployment_id: str,
    entry: CatalogEntry,
    region: str,
    deadline: int,
    self_destruct_grace: int,
    operator_token: str,
    backend_api_key: str,
    ai_api_url: str,
) -> dict:
    env = {
        "AILAB_DEADLINE": str(deadline),
        "AILAB_SELF_DESTRUCT_GRACE": str(self_destruct_grace),
        "AILAB_OPERATOR_TOKEN": operator_token,
        "AILAB_AI_API": ai_api_url,
        "AILAB_DEPLOYMENT": deployment_id,
        "AILAB_DEPLOYMENT_LABEL": DEPLOYMENT_LABEL,
        "AILAB_DEADLINE_LABEL": DEADLINE_LABEL,
        "AILAB_BACKEND_API_KEY": backend_api_key,
        "AILAB_ENGINE_PORT": str(ENGINE_PORT),
        "AILAB_HF_REPO": entry.hf_repo,
        "AILAB_HF_FILE": entry.hf_file,
        "AILAB_CTX_TOKENS": str(entry.ctx_tokens * entry.parallel),
        "AILAB_PARALLEL": str(entry.parallel),
        "AILAB_THREADS": str(entry.flavor_count),
        # AI Deploy runs images as an unprivileged user; the image's own cache path is not writable.
        "LLAMA_CACHE": "/tmp/llama-cache",
    }
    return {
        "name": f"ailab-{entry.id}-{deployment_id[:8]}",
        "image": entry.image,
        "region": region,
        "command": ["sh", "-c", WATCHDOG + START_LLAMA_CPP],
        "defaultHttpPort": ENGINE_PORT,
        "unsecureHttp": False,
        "resources": {"flavor": entry.flavor, "flavorCount": entry.flavor_count},
        "scalingStrategy": {"fixed": {"replicas": 1}},
        "labels": {
            MANAGED_LABEL: "true",
            DEPLOYMENT_LABEL: deployment_id,
            DEADLINE_LABEL: str(deadline),
        },
        "envVars": [{"name": name, "value": value} for name, value in env.items()],
    }
