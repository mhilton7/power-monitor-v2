#!/usr/bin/env bash
set -Eeuo pipefail
source /opt/powermeter/common.sh

usage() {
  printf 'Usage: restore.sh --archive PATH [--test-isolated | --target-database NAME --confirm RESTORE-NAME]\n' >&2
}

archive=""
mode=""
target_database=""
confirmation=""
while (($#)); do
  case "$1" in
    --archive) archive="${2:-}"; shift 2 ;;
    --test-isolated) mode="test"; shift ;;
    --target-database) target_database="${2:-}"; shift 2 ;;
    --confirm) confirmation="${2:-}"; shift 2 ;;
    *) usage; exit 64 ;;
  esac
done

database_env
gpg_env
require_file "$archive"
archive="$(realpath -e -- "$archive")"
readonly archive_directory="$(realpath -e -- "$ARCHIVE_DIR")"
[[ "$archive" == "$archive_directory"/* ]] || {
  printf 'archive must resolve inside %s\n' "$ARCHIVE_DIR" >&2
  exit 65
}
readonly archive_name="$(basename "$archive")"
[[ "$archive_name" =~ ^powermeter-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}\.dump\.gpg$ ]] || {
  printf 'archive filename is not a PowerMeter V2 backup artifact\n' >&2
  exit 65
}
readonly checksum_path="${archive}.sha256"
readonly manifest_path="${archive}.manifest.json"
require_file "$checksum_path"
require_file "$manifest_path"

readonly run_id="restore-$(date -u +'%Y%m%dT%H%M%SZ')-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
readonly started_at="$(json_now)"
readonly temporary_dir="$(mktemp -d "${TMP_ROOT}/pm-restore.${run_id}.XXXXXX")"
readonly dump_path="${temporary_dir}/database.dump"

cleanup() {
  rm -rf -- "$temporary_dir"
}
trap cleanup EXIT

failure() {
  local exit_code="$?"
  trap - ERR
  if [[ "$mode" == "test" ]]; then
    status_write "last-restore-test-attempt" "$(jq -cn \
      --arg format "$BACKUP_FORMAT_VERSION" --arg run_id "$run_id" \
      --arg started_at "$started_at" --arg completed_at "$(json_now)" \
      --arg state "failed" --arg error_code "RESTORE_VERIFICATION_FAILED" \
      --argjson exit_code "$exit_code" \
      '{format:$format,run_id:$run_id,started_at:$started_at,completed_at:$completed_at,state:$state,error_code:$error_code,exit_code:$exit_code}')"
  fi
  exit "$exit_code"
}
trap failure ERR

read -r declared_sha256 declared_name extra < "$checksum_path"
[[ -z "${extra:-}" && "$declared_sha256" =~ ^[0-9a-f]{64}$ && "$declared_name" == "$archive_name" ]]
readonly archive_sha256="$(sha256_file "$archive")"
[[ "$archive_sha256" == "$declared_sha256" ]]
jq -e --arg format "$BACKUP_FORMAT_VERSION" --arg archive "$archive_name" \
  --arg encrypted_sha256 "$archive_sha256" --arg database "$PM_DATABASE_NAME" \
  '.format == $format and .archive == $archive and .encrypted_sha256 == $encrypted_sha256 and .database == $database and (.plaintext_sha256 | test("^[0-9a-f]{64}$")) and (.migration_revision | type == "string" and length > 0) and (.public_table_count | type == "number" and . > 0)' \
  "$manifest_path" >/dev/null
gpg --batch --quiet --pinentry-mode loopback \
  --passphrase-file "$PM_BACKUP_ENCRYPTION_KEY_FILE" \
  --decrypt --output "$dump_path" "$archive"
pg_restore --list "$dump_path" >/dev/null
[[ "$(sha256_file "$dump_path")" == "$(jq -er '.plaintext_sha256' "$manifest_path")" ]]

if [[ "$mode" == "test" ]]; then
  target_database="pm_restore_test_${run_id//[^a-zA-Z0-9]/_}"
elif [[ -n "$target_database" && "$confirmation" == "RESTORE-${target_database}" ]]; then
  [[ "$target_database" =~ ^[a-z][a-z0-9_]{2,62}$ ]] || {
    printf 'target database must match ^[a-z][a-z0-9_]{2,62}$\n' >&2
    exit 64
  }
  [[ "$target_database" != "$PM_DATABASE_NAME" ]] || {
    printf 'in-place production restore is refused; use a new database and cut over after validation\n' >&2
    exit 66
  }
else
  usage
  exit 64
fi

readonly maintenance_db="${PM_MAINTENANCE_DATABASE:-postgres}"
restore_database_env
psql --host "$PM_DATABASE_HOST" --port "$PM_DATABASE_PORT" \
  --username "$PM_RESTORE_DATABASE_USER" \
  --dbname "$maintenance_db" --set=ON_ERROR_STOP=1 \
  --command "CREATE DATABASE \"${target_database}\" TEMPLATE template0"

drop_test_database() {
  psql --host "$PM_DATABASE_HOST" --port "$PM_DATABASE_PORT" \
    --username "$PM_RESTORE_DATABASE_USER" \
    --dbname "$maintenance_db" --set=ON_ERROR_STOP=1 \
    --command "DROP DATABASE IF EXISTS \"${target_database}\" WITH (FORCE)" >/dev/null || true
}
[[ "$mode" == "test" ]] && trap 'drop_test_database; cleanup' EXIT

pg_restore --host "$PM_DATABASE_HOST" --port "$PM_DATABASE_PORT" \
  --username "$PM_RESTORE_DATABASE_USER" \
  --dbname "$target_database" --exit-on-error --no-owner --no-privileges --jobs=2 "$dump_path"

readonly table_count="$(psql --host "$PM_DATABASE_HOST" --port "$PM_DATABASE_PORT" \
  --username "$PM_RESTORE_DATABASE_USER" --dbname "$target_database" --tuples-only --no-align \
  --set=ON_ERROR_STOP=1 --command "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")"
(( table_count > 0 ))
readonly database_version="$(psql --host "$PM_DATABASE_HOST" --port "$PM_DATABASE_PORT" \
  --username "$PM_RESTORE_DATABASE_USER" --dbname "$target_database" --tuples-only --no-align \
  --set=ON_ERROR_STOP=1 --command 'SHOW server_version')"
readonly migration_revision="$(psql --host "$PM_DATABASE_HOST" --port "$PM_DATABASE_PORT" \
  --username "$PM_RESTORE_DATABASE_USER" --dbname "$target_database" --tuples-only --no-align \
  --set=ON_ERROR_STOP=1 --command 'SELECT version_num FROM alembic_version LIMIT 1')"
[[ "$migration_revision" == "$(jq -er '.migration_revision' "$manifest_path")" ]]
(( table_count == $(jq -er '.public_table_count' "$manifest_path") ))
readonly archive_bytes="$(stat -c %s "$archive")"

readonly success_payload="$(jq -cn \
  --arg format "$BACKUP_FORMAT_VERSION" --arg run_id "$run_id" \
  --arg started_at "$started_at" --arg completed_at "$(json_now)" \
  --arg state "verified" --arg archive "$(basename "$archive")" \
  --arg restore_database "$target_database" --arg sha256 "$archive_sha256" \
  --arg database_version "$database_version" --arg migration_revision "$migration_revision" \
  --argjson byte_count "$archive_bytes" \
  --argjson public_table_count "$table_count" \
  '{format:$format,run_id:$run_id,started_at:$started_at,completed_at:$completed_at,state:$state,archive:$archive,restore_database:$restore_database,sha256:$sha256,byte_count:$byte_count,database_version:$database_version,migration_revision:$migration_revision,public_table_count:$public_table_count,verification_checks:["ciphertext checksum matched","OpenPGP decrypt succeeded","plaintext hash matched manifest","pg_restore completed with exit-on-error","restored schema query returned tables"]}')"

if [[ "$mode" == "test" ]]; then
  status_write "last-restore-test-attempt" "$success_payload"
  status_write "last-successful-restore-test" "$success_payload"
fi
printf '%s\n' "$success_payload"
