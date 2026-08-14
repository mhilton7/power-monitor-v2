#!/usr/bin/env bash
set -Eeuo pipefail
umask 0077

readonly BACKUP_FORMAT_VERSION="pm-backup/1.0.0"
readonly BACKUP_DIR="${PM_BACKUP_DIR:-/backups}"
readonly ARCHIVE_DIR="${BACKUP_DIR}/archives"
readonly STATUS_DIR="${BACKUP_DIR}/status"
readonly TMP_ROOT="${TMPDIR:-/tmp}"

json_now() {
  date -u +'%Y-%m-%dT%H:%M:%SZ'
}

require_file() {
  local path="$1"
  [[ -r "$path" ]] || { printf 'required file is not readable: %s\n' "$path" >&2; return 1; }
  [[ -s "$path" ]] || { printf 'required file is empty: %s\n' "$path" >&2; return 1; }
}

database_env() {
  : "${PM_DATABASE_HOST:?PM_DATABASE_HOST is required}"
  : "${PM_DATABASE_PORT:?PM_DATABASE_PORT is required}"
  : "${PM_DATABASE_NAME:?PM_DATABASE_NAME is required}"
  : "${PM_DATABASE_USER:?PM_DATABASE_USER is required}"
  : "${PM_DATABASE_PASSWORD_FILE:?PM_DATABASE_PASSWORD_FILE is required}"
  require_file "$PM_DATABASE_PASSWORD_FILE"
  export PGPASSWORD
  PGPASSWORD="$(tr -d '\r\n' < "$PM_DATABASE_PASSWORD_FILE")"
  export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}"
}

restore_database_env() {
  : "${PM_RESTORE_DATABASE_USER:?PM_RESTORE_DATABASE_USER is required}"
  : "${PM_RESTORE_DATABASE_PASSWORD_FILE:?PM_RESTORE_DATABASE_PASSWORD_FILE is required}"
  require_file "$PM_RESTORE_DATABASE_PASSWORD_FILE"
  export PGPASSWORD
  PGPASSWORD="$(tr -d '\r\n' < "$PM_RESTORE_DATABASE_PASSWORD_FILE")"
  export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}"
}

gpg_env() {
  : "${PM_BACKUP_ENCRYPTION_KEY_FILE:?PM_BACKUP_ENCRYPTION_KEY_FILE is required}"
  require_file "$PM_BACKUP_ENCRYPTION_KEY_FILE"
  export GNUPGHOME="${TMP_ROOT}/gnupg"
  mkdir -p "$GNUPGHOME"
  chmod 0700 "$GNUPGHOME"
}

status_write() {
  local name="$1"
  local payload="$2"
  local temporary
  mkdir -p "$STATUS_DIR"
  temporary="$(mktemp "${STATUS_DIR}/.${name}.XXXXXX")"
  printf '%s\n' "$payload" > "$temporary"
  chmod 0640 "$temporary"
  mv -f "$temporary" "${STATUS_DIR}/${name}.json"
}

sha256_file() {
  sha256sum "$1" | awk '{print $1}'
}

pg_args() {
  printf '%s\n' \
    --host "$PM_DATABASE_HOST" \
    --port "$PM_DATABASE_PORT" \
    --username "$PM_DATABASE_USER" \
    --dbname "$PM_DATABASE_NAME"
}
