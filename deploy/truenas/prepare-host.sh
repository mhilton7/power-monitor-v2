#!/usr/bin/env bash
set -Eeuo pipefail

# Prepare and verify the fixed host-path layout used by the signed release YAML.
# ZFS datasets must be created in the TrueNAS UI before this script is run.

readonly base="/mnt/Apps/PowerMeterV2"
assets=""
hostname="power-monitor.home.arpa"

usage() {
  printf 'Usage: %s --assets RELEASE_ASSET_DIRECTORY [--hostname DNS_NAME]\n' "$0" >&2
}

fail() {
  printf 'TrueNAS host preparation failed: %s\n' "$*" >&2
  exit 2
}

while (($#)); do
  case "$1" in
    --assets)
      assets="${2:-}"
      shift 2
      ;;
    --hostname)
      hostname="${2:-}"
      shift 2
      ;;
    *)
      usage
      exit 64
      ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || fail "run as root (use sudo)"
[[ -n "$assets" ]] || {
  usage
  exit 64
}
[[ "$hostname" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] ||
  fail "hostname is not a valid DNS name"

for command_name in awk base64 cmp findmnt getfacl mktemp openssl realpath setfacl sha256sum stat; do
  command -v "$command_name" >/dev/null || fail "required command is unavailable: $command_name"
done

assets="$(realpath -e -- "$assets")"
[[ -d "$assets" && ! -L "$assets" ]] || fail "release asset directory is not a real directory"
for asset in SHA256SUMS Caddyfile postgres-init-roles.sh; do
  [[ -f "$assets/$asset" && ! -L "$assets/$asset" ]] ||
    fail "release asset is missing or is a symbolic link: $asset"
done
(cd "$assets" && sha256sum --check --strict SHA256SUMS >/dev/null) ||
  fail "release asset checksum verification failed"

assert_dataset() {
  local path="$1"
  local target
  local filesystem
  [[ -d "$path" && ! -L "$path" ]] || fail "required dataset mount is missing: $path"
  [[ "$(realpath -e -- "$path")" == "$path" ]] || fail "dataset path does not resolve exactly: $path"
  target="$(findmnt -n -o TARGET -T "$path")"
  filesystem="$(findmnt -n -o FSTYPE -T "$path")"
  [[ "$target" == "$path" && "$filesystem" == "zfs" ]] ||
    fail "$path must be the mount point of its own ZFS dataset"
}

readonly -a dataset_paths=(
  "$base"
  "$base/postgres"
  "$base/config"
  "$base/firmware"
  "$base/backups"
  "$base/logs"
  "$base/rate-source-artifacts"
  "$base/bill-rate-source-artifacts"
  "$base/caddy-data"
  "$base/caddy-config"
  "$base/secrets"
)
for dataset_path in "${dataset_paths[@]}"; do
  assert_dataset "$dataset_path"
done

reset_directory() {
  local path="$1"
  local owner="$2"
  local group="$3"
  local mode="$4"
  setfacl -b -k -- "$path"
  chown "$owner:$group" "$path"
  chmod "$mode" "$path"
}

reset_directory "$base" 0 0 0755
reset_directory "$base/postgres" 70 70 0700
reset_directory "$base/config" 0 0 0755
reset_directory "$base/firmware" 10001 10001 0750
reset_directory "$base/backups" 568 568 0750
reset_directory "$base/logs" 0 0 0711
reset_directory "$base/rate-source-artifacts" 10001 10001 0750
reset_directory "$base/bill-rate-source-artifacts" 10001 10001 0750
reset_directory "$base/caddy-data" 1000 1000 0750
reset_directory "$base/caddy-config" 1000 1000 0750
reset_directory "$base/secrets" 0 0 0711

install -d -o 568 -g 568 -m 0750 "$base/backups/status"
reset_directory "$base/backups/status" 568 568 0750
setfacl -m u:10001:r-x,d:u:10001:r-x -- "$base/backups/status"
install -d -o 10001 -g 10001 -m 0750 "$base/logs/application"
reset_directory "$base/logs/application" 10001 10001 0750
install -d -o 1000 -g 1000 -m 0750 "$base/logs/gateway"
reset_directory "$base/logs/gateway" 1000 1000 0750

install -o 0 -g 0 -m 0644 -- "$assets/Caddyfile" "$base/config/Caddyfile"
install -o 0 -g 0 -m 0644 -- \
  "$assets/postgres-init-roles.sh" "$base/config/postgres-init-roles.sh"
cmp --silent -- "$assets/Caddyfile" "$base/config/Caddyfile" ||
  fail "installed Caddyfile differs from the verified release asset"
cmp --silent -- "$assets/postgres-init-roles.sh" "$base/config/postgres-init-roles.sh" ||
  fail "installed PostgreSQL role script differs from the verified release asset"

readonly -a database_secret_names=(
  postgres_bootstrap_password
  postgres_migrator_password
  postgres_api_password
  postgres_worker_password
  postgres_backup_password
  postgres_restore_password
)
readonly -a application_secret_names=(session_secret field_encryption_key ota_manifest_key)
readonly -a tls_secret_names=(tls.crt tls.key tls-ca.crt)
readonly -a all_secret_names=(
  "${database_secret_names[@]}"
  "${application_secret_names[@]}"
  backup_encryption_key
  "${tls_secret_names[@]}"
)

secret_value() {
  local path="$1"
  local value
  value="$(<"$path")"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] ||
    fail "secret contains more than one value: $(basename "$path")"
  printf '%s' "$value"
}

for secret_name in "${all_secret_names[@]}"; do
  secret_path="$base/secrets/$secret_name"
  [[ -f "$secret_path" && ! -L "$secret_path" ]] ||
    fail "required secret file is missing or is a symbolic link: $secret_name"
done

for secret_name in "${database_secret_names[@]}"; do
  value="$(secret_value "$base/secrets/$secret_name")"
  [[ "$value" =~ ^[0-9a-f]{64}$ ]] || fail "$secret_name must contain exactly 64 lowercase hex characters"
done

for secret_name in "${application_secret_names[@]}"; do
  value="$(secret_value "$base/secrets/$secret_name")"
  [[ "$value" =~ ^[A-Za-z0-9+/]+={0,2}$ ]] || fail "$secret_name is not valid Base64"
  if ! decoded_bytes="$(printf '%s' "$value" | base64 --decode 2>/dev/null | wc -c)"; then
    fail "$secret_name is not valid Base64"
  fi
  [[ "$decoded_bytes" -eq 32 ]] || fail "$secret_name must decode to exactly 32 bytes"
done

value="$(secret_value "$base/secrets/backup_encryption_key")"
backup_key_valid=false
if [[ "$value" =~ ^[A-Za-z0-9+/]+={0,2}$ ]]; then
  if decoded_bytes="$(printf '%s' "$value" | base64 --decode 2>/dev/null | wc -c)" &&
    [[ "$decoded_bytes" -ge 32 ]]; then
    backup_key_valid=true
  fi
fi
if [[ "$backup_key_valid" != true && "$(awk '{print NF}' <<<"$value")" -lt 6 ]]; then
  fail "backup_encryption_key must be 32+ random Base64 bytes or at least six Diceware words"
fi

set_secret_acl() {
  local path="$1"
  shift
  local acl_entries=""
  local uid
  setfacl -b -- "$path"
  chown 0:0 "$path"
  chmod 0400 "$path"
  for uid in "$@"; do
    acl_entries+="${acl_entries:+,}u:${uid}:r--"
  done
  setfacl -m "$acl_entries" -- "$path"
}

set_secret_acl "$base/secrets/postgres_bootstrap_password" 70
for secret_name in postgres_migrator_password postgres_api_password postgres_worker_password; do
  set_secret_acl "$base/secrets/$secret_name" 70 10001
done
for secret_name in postgres_backup_password postgres_restore_password; do
  set_secret_acl "$base/secrets/$secret_name" 70 568
done
for secret_name in "${application_secret_names[@]}"; do
  set_secret_acl "$base/secrets/$secret_name" 10001
done
set_secret_acl "$base/secrets/backup_encryption_key" 568
for secret_name in "${tls_secret_names[@]}"; do
  set_secret_acl "$base/secrets/$secret_name" 1000
done

assert_named_readers() {
  local path="$1"
  shift
  local expected
  local actual
  expected="$(printf '%s\n' "$@" | sort)"
  actual="$(getfacl -cpn -- "$path" | awk -F: '$1 == "user" && $2 != "" {print $2}' | sort)"
  [[ "$actual" == "$expected" ]] || fail "unexpected named reader ACL on $(basename "$path")"
  getfacl -cpn -- "$path" | grep -Fx 'group::---' >/dev/null ||
    fail "owning group has access to $(basename "$path")"
  getfacl -cpn -- "$path" | grep -Fx 'other::---' >/dev/null ||
    fail "other users have access to $(basename "$path")"
}

assert_named_readers "$base/secrets/postgres_bootstrap_password" 70
for secret_name in postgres_migrator_password postgres_api_password postgres_worker_password; do
  assert_named_readers "$base/secrets/$secret_name" 10001 70
done
for secret_name in postgres_backup_password postgres_restore_password; do
  assert_named_readers "$base/secrets/$secret_name" 568 70
done
for secret_name in "${application_secret_names[@]}"; do
  assert_named_readers "$base/secrets/$secret_name" 10001
done
assert_named_readers "$base/secrets/backup_encryption_key" 568
for secret_name in "${tls_secret_names[@]}"; do
  assert_named_readers "$base/secrets/$secret_name" 1000
done

openssl x509 -in "$base/secrets/tls.crt" -noout -checkhost "$hostname" >/dev/null ||
  fail "TLS certificate SAN does not contain $hostname"
openssl x509 -in "$base/secrets/tls.crt" -noout -checkend 604800 >/dev/null ||
  fail "TLS certificate expires in less than seven days"
chain_file="$(mktemp "${TMPDIR:-/tmp}/pm-tls-chain.XXXXXX")"
trap 'rm -f -- "$chain_file"' EXIT
awk '
  /-----BEGIN CERTIFICATE-----/ { certificate_count += 1 }
  certificate_count >= 2 { print }
' "$base/secrets/tls.crt" >"$chain_file"
if [[ -s "$chain_file" ]]; then
  openssl verify -CAfile "$base/secrets/tls-ca.crt" -untrusted "$chain_file" \
    "$base/secrets/tls.crt" >/dev/null ||
    fail "TLS certificate chain does not verify against tls-ca.crt"
else
  openssl verify -CAfile "$base/secrets/tls-ca.crt" "$base/secrets/tls.crt" >/dev/null ||
    fail "TLS certificate does not verify against tls-ca.crt"
fi
rm -f -- "$chain_file"
trap - EXIT
certificate_public_key="$(openssl x509 -in "$base/secrets/tls.crt" -pubkey -noout |
  openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)"
private_public_key="$(openssl pkey -in "$base/secrets/tls.key" -pubout -outform DER 2>/dev/null |
  sha256sum | cut -d' ' -f1)"
[[ "$certificate_public_key" == "$private_public_key" ]] ||
  fail "TLS certificate and private key do not match"

for directory_record in \
  "$base|0:0|755" \
  "$base/postgres|70:70|700" \
  "$base/config|0:0|755" \
  "$base/firmware|10001:10001|750" \
  "$base/backups|568:568|750" \
  "$base/backups/status|568:568|750" \
  "$base/logs|0:0|711" \
  "$base/logs/application|10001:10001|750" \
  "$base/logs/gateway|1000:1000|750" \
  "$base/rate-source-artifacts|10001:10001|750" \
  "$base/bill-rate-source-artifacts|10001:10001|750" \
  "$base/caddy-data|1000:1000|750" \
  "$base/caddy-config|1000:1000|750" \
  "$base/secrets|0:0|711"; do
  IFS='|' read -r path expected_owner expected_mode <<<"$directory_record"
  [[ "$(stat -c '%u:%g' -- "$path")" == "$expected_owner" ]] ||
    fail "wrong owner on $path"
  [[ "$(stat -c '%a' -- "$path")" == "$expected_mode" ]] ||
    fail "wrong mode on $path"
done

getfacl -cpn -- "$base/backups/status" | grep -Fx 'user:10001:r-x' >/dev/null ||
  fail "API read ACL is missing on backups/status"
getfacl -cpn -- "$base/backups/status" | grep -Fx 'default:user:10001:r-x' >/dev/null ||
  fail "API default read ACL is missing on backups/status"

printf 'TrueNAS host preparation passed for %s (%d ZFS datasets, %d secret files).\n' \
  "$hostname" "${#dataset_paths[@]}" "${#all_secret_names[@]}"
