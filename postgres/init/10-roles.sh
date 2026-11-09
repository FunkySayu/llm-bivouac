#!/bin/sh
# Runs once, when the Postgres data volume is first initialised.
set -eu

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v ailab_pw="$AILAB_DB_PASSWORD" \
  -v litellm_pw="$LITELLM_DB_PASSWORD" \
  -v grafana_pw="$GRAFANA_DB_PASSWORD" <<'SQL'
CREATE ROLE ailab LOGIN PASSWORD :'ailab_pw';
CREATE ROLE litellm LOGIN PASSWORD :'litellm_pw';
CREATE ROLE grafana_ro LOGIN PASSWORD :'grafana_pw';

CREATE DATABASE ailab OWNER ailab;
CREATE DATABASE litellm OWNER litellm;
REVOKE ALL ON DATABASE ailab FROM PUBLIC;
REVOKE ALL ON DATABASE litellm FROM PUBLIC;
GRANT CONNECT ON DATABASE ailab TO grafana_ro;

\connect ailab
GRANT USAGE ON SCHEMA public TO grafana_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE ailab IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
SQL
