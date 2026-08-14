#!/usr/bin/env bash
set -Eeuo pipefail
umask 0077

read_secret() {
  local path="$1"
  [[ -r "$path" && -s "$path" ]] || {
    printf 'required PostgreSQL role secret is missing: %s\n' "$path" >&2
    exit 1
  }
  tr -d '\r\n' < "$path"
}

readonly migrator_password="$(
  read_secret "${PM_POSTGRES_MIGRATOR_PASSWORD_FILE:-/run/secrets/postgres_migrator_password}"
)"
readonly api_password="$(
  read_secret "${PM_POSTGRES_API_PASSWORD_FILE:-/run/secrets/postgres_api_password}"
)"
readonly worker_password="$(
  read_secret "${PM_POSTGRES_WORKER_PASSWORD_FILE:-/run/secrets/postgres_worker_password}"
)"
readonly backup_password="$(
  read_secret "${PM_POSTGRES_BACKUP_PASSWORD_FILE:-/run/secrets/postgres_backup_password}"
)"
readonly restore_password="$(
  read_secret "${PM_POSTGRES_RESTORE_PASSWORD_FILE:-/run/secrets/postgres_restore_password}"
)"

psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=migrator_password="$migrator_password" \
  --set=api_password="$api_password" \
  --set=worker_password="$worker_password" \
  --set=backup_password="$backup_password" \
  --set=restore_password="$restore_password" <<'SQL'
CREATE ROLE pm_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  PASSWORD :'migrator_password';
CREATE ROLE pm_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  PASSWORD :'api_password';
CREATE ROLE pm_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  PASSWORD :'worker_password';
CREATE ROLE pm_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  PASSWORD :'backup_password';
CREATE ROLE pm_restore_test LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOREPLICATION
  PASSWORD :'restore_password';

REVOKE ALL ON DATABASE powermeter FROM PUBLIC;
GRANT CONNECT ON DATABASE powermeter TO pm_migrator, pm_api, pm_worker, pm_backup;
ALTER DATABASE powermeter OWNER TO pm_migrator;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO pm_migrator;
GRANT USAGE ON SCHEMA public TO pm_api, pm_worker, pm_backup;

ALTER DEFAULT PRIVILEGES FOR ROLE pm_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pm_api, pm_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE pm_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO pm_api, pm_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE pm_migrator IN SCHEMA public
  GRANT SELECT ON TABLES TO pm_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE pm_migrator IN SCHEMA public
  GRANT SELECT ON SEQUENCES TO pm_backup;

ALTER ROLE pm_api SET search_path = public, pg_catalog;
ALTER ROLE pm_worker SET search_path = public, pg_catalog;
ALTER ROLE pm_backup SET search_path = public, pg_catalog;
ALTER ROLE pm_migrator SET search_path = public, pg_catalog;
ALTER ROLE pm_restore_test SET search_path = public, pg_catalog;

-- The image bootstrap principal is never available to application containers and
-- cannot be used for a network login after the empty-cluster initialization ends.
ALTER ROLE pm_bootstrap NOLOGIN;
SQL
