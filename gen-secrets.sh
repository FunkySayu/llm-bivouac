#!/usr/bin/env bash
# Creates .env from .env.example and fills every __GENERATE__ value.
# Safe to re-run: a value that is already set is never touched.
set -euo pipefail
cd "$(dirname "$0")"
umask 077

[ -f .env ] || cp .env.example .env

tmp="$(mktemp .env.XXXXXX)"
trap 'rm -f "$tmp"' EXIT

generated=0
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    *=__GENERATE__)
      name="${line%%=*}"
      value="$(openssl rand -hex 32)"
      # LiteLLM rejects master and virtual keys that do not start with sk-.
      case "$name" in
        LITELLM_MASTER_KEY|LITELLM_SALT_KEY|LITELLM_OWUI_KEY) value="sk-$value" ;;
      esac
      printf '%s=%s\n' "$name" "$value"
      generated=$((generated + 1))
      ;;
    *)
      printf '%s\n' "$line"
      ;;
  esac
done < .env > "$tmp"

mv "$tmp" .env
trap - EXIT
chmod 600 .env

remaining="$(grep -c '=__FILL__$' .env || true)"
echo "Generated $generated value(s). $remaining __FILL__ value(s) remain in .env."
