#!/usr/bin/env bash
set -Eeuo pipefail
source /opt/powermeter/common.sh

database_env
gpg_env
mkdir -p "$ARCHIVE_DIR" "$STATUS_DIR"

readonly run_id="$(date -u +'%Y%m%dT%H%M%SZ')-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
readonly started_at="$(json_now)"
readonly temporary_dir="$(mktemp -d "${TMP_ROOT}/pm-backup.${run_id}.XXXXXX")"
readonly dump_path="${temporary_dir}/database.dump"
readonly encrypted_name="powermeter-${run_id}.dump.gpg"
readonly encrypted_path="${ARCHIVE_DIR}/${encrypted_name}"
readonly checksum_path="${encrypted_path}.sha256"
readonly manifest_path="${encrypted_path}.manifest.json"
readonly partial_encrypted_path="${ARCHIVE_DIR}/.${encrypted_name}.partial"
readonly partial_checksum_path="${ARCHIVE_DIR}/.${encrypted_name}.sha256.partial"
readonly partial_manifest_path="${ARCHIVE_DIR}/.${encrypted_name}.manifest.json.partial"
readonly verify_dump="${temporary_dir}/verify.dump"
archive_published=0

cleanup() {
  rm -rf -- "$temporary_dir"
}
trap cleanup EXIT

failure() {
  local exit_code="$?"
  trap - ERR
  rm -f -- "$partial_encrypted_path" "$partial_checksum_path" "$partial_manifest_path"
  if (( archive_published == 0 )); then
    rm -f -- "$encrypted_path" "$checksum_path" "$manifest_path"
  fi
  status_write "last-backup-attempt" "$(jq -cn \
    --arg format "$BACKUP_FORMAT_VERSION" \
    --arg run_id "$run_id" \
    --arg started_at "$started_at" \
    --arg completed_at "$(json_now)" \
    --arg state "failed" \
    --arg error_code "BACKUP_PIPELINE_FAILED" \
    --argjson exit_code "$exit_code" \
    '{format:$format,run_id:$run_id,started_at:$started_at,completed_at:$completed_at,state:$state,error_code:$error_code,exit_code:$exit_code}')"
  exit "$exit_code"
}
trap failure ERR

mapfile -t connection_args < <(pg_args)
readonly database_version="$(psql "${connection_args[@]}" --tuples-only --no-align --command 'SHOW server_version')"
readonly migration_revision="$(psql "${connection_args[@]}" --tuples-only --no-align --command 'SELECT version_num FROM alembic_version LIMIT 1')"
readonly public_table_count="$(psql "${connection_args[@]}" --tuples-only --no-align \
  --command "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")"
(( public_table_count > 0 ))
pg_dump "${connection_args[@]}" --format=custom --compress=9 --no-owner --no-privileges --file="$dump_path"
pg_restore --list "$dump_path" >/dev/null

gpg --batch --yes --pinentry-mode loopback \
  --passphrase-file "$PM_BACKUP_ENCRYPTION_KEY_FILE" \
  --symmetric --cipher-algo AES256 --s2k-digest-algo SHA512 --s2k-count 65011712 \
  --output "$partial_encrypted_path" "$dump_path"
chmod 0640 "$partial_encrypted_path"

readonly encrypted_sha256="$(sha256_file "$partial_encrypted_path")"
readonly encrypted_bytes="$(stat -c %s "$partial_encrypted_path")"
printf '%s  %s\n' "$encrypted_sha256" "$encrypted_name" > "$partial_checksum_path"
chmod 0640 "$partial_checksum_path"

gpg --batch --quiet --pinentry-mode loopback \
  --passphrase-file "$PM_BACKUP_ENCRYPTION_KEY_FILE" \
  --decrypt --output "$verify_dump" "$partial_encrypted_path"
pg_restore --list "$verify_dump" >/dev/null
readonly plaintext_sha256="$(sha256_file "$dump_path")"
[[ "$(sha256_file "$verify_dump")" == "$plaintext_sha256" ]]

jq -cn \
  --arg format "$BACKUP_FORMAT_VERSION" \
  --arg run_id "$run_id" \
  --arg created_at "$started_at" \
  --arg database "$PM_DATABASE_NAME" \
  --arg archive "$encrypted_name" \
  --arg encrypted_sha256 "$encrypted_sha256" \
  --arg plaintext_sha256 "$plaintext_sha256" \
  --arg database_version "$database_version" \
  --arg migration_revision "$migration_revision" \
  --arg encryption "OpenPGP symmetric AES-256/SHA-512" \
  --arg verification "decrypt, byte-compare, pg_restore --list" \
  --argjson encrypted_bytes "$encrypted_bytes" \
  --argjson public_table_count "$public_table_count" \
  '{format:$format,run_id:$run_id,created_at:$created_at,database:$database,archive:$archive,encrypted_sha256:$encrypted_sha256,plaintext_sha256:$plaintext_sha256,encrypted_bytes:$encrypted_bytes,database_version:$database_version,migration_revision:$migration_revision,public_table_count:$public_table_count,encryption:$encryption,verification:$verification,secrets_included:false}' \
  > "$partial_manifest_path"
chmod 0640 "$partial_manifest_path"

readonly retention_days="${PM_BACKUP_RETENTION_DAYS:-35}"
[[ "$retention_days" =~ ^[0-9]+$ ]] && (( retention_days >= 1 ))
mapfile -t successful_archives < <(
  find "$ARCHIVE_DIR" -maxdepth 1 -type f -name 'powermeter-*.dump.gpg' -printf '%T@ %p\n' |
    sort -nr | cut -d' ' -f2-
)
for old_archive in "${successful_archives[@]:1}"; do
  if find "$old_archive" -maxdepth 0 -mtime "+${retention_days}" -print -quit | grep -q .; then
    rm -f -- "$old_archive" "${old_archive}.sha256" "${old_archive}.manifest.json"
  fi
done

# Publish the archive last. A power loss before this point can leave only hidden
# staging/metadata files, never a discoverable archive falsely presented as complete.
mv -f -- "$partial_checksum_path" "$checksum_path"
mv -f -- "$partial_manifest_path" "$manifest_path"
mv -f -- "$partial_encrypted_path" "$encrypted_path"
archive_published=1

readonly success_payload="$(jq -cn \
  --arg format "$BACKUP_FORMAT_VERSION" \
  --arg run_id "$run_id" \
  --arg started_at "$started_at" \
  --arg completed_at "$(json_now)" \
  --arg state "verified" \
  --arg archive "$encrypted_name" \
  --arg manifest "$(basename "$manifest_path")" \
  --arg sha256 "$encrypted_sha256" \
  --arg database_version "$database_version" \
  --arg migration_revision "$migration_revision" \
  --argjson byte_count "$encrypted_bytes" \
  --argjson public_table_count "$public_table_count" \
  '{format:$format,run_id:$run_id,started_at:$started_at,completed_at:$completed_at,state:$state,archive:$archive,manifest:$manifest,sha256:$sha256,byte_count:$byte_count,database_version:$database_version,migration_revision:$migration_revision,public_table_count:$public_table_count,verification_checks:["pg_dump completed","pg_restore catalog parsed","OpenPGP decrypt succeeded","plaintext byte hash matched"]}')"
status_write "last-backup-attempt" "$success_payload"
status_write "last-successful-backup" "$success_payload"

printf '%s\n' "$manifest_path"
