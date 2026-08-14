#!/usr/bin/env bash
set -Eeuo pipefail

# This destructive-looking test is restricted to a disposable GitHub-hosted
# runner. It never targets an operator's TrueNAS host.
[[ "${GITHUB_ACTIONS:-}" == "true" && "${RUNNER_ENVIRONMENT:-}" == "github-hosted" ]]
: "${COMPOSE_FILE:?COMPOSE_FILE is required}"
: "${EVIDENCE_FILE:?EVIDENCE_FILE is required}"
[[ -f "$COMPOSE_FILE" ]]
command -v docker >/dev/null
command -v setfacl >/dev/null

readonly hostname="power-monitor.home.arpa"
readonly base="/mnt/Apps/PowerMeterV2"
readonly work="$(mktemp -d "${RUNNER_TEMP:?}/pm-release-smoke.XXXXXX")"
readonly cookie_jar="$work/cookies.txt"
readonly endpoint="https://${hostname}:8443"
readonly hosts_marker="powermeter-v2-release-smoke-${GITHUB_RUN_ID:?}"
readonly authenticated_evidence="${EVIDENCE_FILE%.json}-authenticated.json"

compose() {
  PM_HOSTNAME="$hostname" docker compose -f "$COMPOSE_FILE" "$@"
}

cleanup() {
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  sudo sed -i "/# ${hosts_marker}$/d" /etc/hosts >/dev/null 2>&1 || true
  if [[ "$base" == "/mnt/Apps/PowerMeterV2" && -d "$base" ]]; then
    sudo rm -rf -- "$base"
  fi
  rm -rf -- "$work"
}
trap cleanup EXIT

[[ ! -e "$base" ]]
sudo install -d -o 0 -g 0 -m 0755 "$base"
sudo install -d -o 70 -g 70 -m 0700 "$base/postgres"
sudo install -d -o 0 -g 0 -m 0755 "$base/config"
sudo install -d -o 10001 -g 10001 -m 0750 \
  "$base/firmware" "$base/logs/application" \
  "$base/rate-source-artifacts" "$base/bill-rate-source-artifacts"
sudo install -d -o 568 -g 568 -m 0750 "$base/backups" "$base/backups/status"
sudo setfacl -m u:10001:rX,d:u:10001:rX "$base/backups/status"
sudo install -d -o 1000 -g 1000 -m 0750 \
  "$base/logs/gateway" "$base/caddy-data" "$base/caddy-config"
sudo install -d -o 0 -g 0 -m 0711 "$base/secrets"
sudo install -o 0 -g 0 -m 0644 deploy/caddy/Caddyfile "$base/config/Caddyfile"
sudo install -o 0 -g 0 -m 0644 deploy/postgres/init-roles.sh \
  "$base/config/postgres-init-roles.sh"

umask 077
for name in bootstrap migrator api worker backup restore; do
  openssl rand -hex 32 > "$work/postgres_${name}_password"
done
openssl rand -base64 48 > "$work/session_secret"
openssl rand -base64 32 > "$work/field_encryption_key"
openssl rand -base64 32 > "$work/ota_manifest_key"
openssl rand -base64 48 > "$work/backup_encryption_key"
openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 2 \
  -subj '/CN=PowerMeter V2 Release Test Root' \
  -keyout "$work/tls-ca.key" -out "$work/tls-ca.crt"
openssl req -newkey rsa:3072 -sha256 -nodes -subj "/CN=${hostname}" \
  -keyout "$work/tls.key" -out "$work/tls.csr"
printf 'subjectAltName=DNS:%s\nextendedKeyUsage=serverAuth\n' "$hostname" > "$work/tls.ext"
openssl x509 -req -sha256 -days 2 -in "$work/tls.csr" \
  -CA "$work/tls-ca.crt" -CAkey "$work/tls-ca.key" -CAcreateserial \
  -extfile "$work/tls.ext" -out "$work/tls.crt"
openssl verify -CAfile "$work/tls-ca.crt" -verify_hostname "$hostname" "$work/tls.crt"

for name in postgres_bootstrap_password postgres_migrator_password \
  postgres_api_password postgres_worker_password postgres_backup_password \
  postgres_restore_password session_secret field_encryption_key ota_manifest_key \
  backup_encryption_key tls.crt tls.key tls-ca.crt; do
  sudo install -o 0 -g 0 -m 0400 "$work/$name" "$base/secrets/$name"
done
sudo setfacl -m u:70:r "$base/secrets/postgres_bootstrap_password"
sudo setfacl -m u:70:r,u:10001:r \
  "$base/secrets/postgres_migrator_password" \
  "$base/secrets/postgres_api_password" \
  "$base/secrets/postgres_worker_password"
sudo setfacl -m u:70:r,u:568:r \
  "$base/secrets/postgres_backup_password" \
  "$base/secrets/postgres_restore_password"
sudo setfacl -m u:10001:r "$base/secrets/session_secret" \
  "$base/secrets/field_encryption_key" "$base/secrets/ota_manifest_key"
sudo setfacl -m u:568:r "$base/secrets/backup_encryption_key"
sudo setfacl -m u:1000:r "$base/secrets/tls.crt" \
  "$base/secrets/tls.key" "$base/secrets/tls-ca.crt"

compose config --quiet
mapfile -t services < <(compose config --services | sort)
[[ "${services[*]}" == "api backup frontend gateway migrate postgres worker" ]]
compose up --detach --wait --wait-timeout 360

curl_common=(--fail --silent --show-error --resolve "${hostname}:8443:127.0.0.1" \
  --cacert "$work/tls-ca.crt")
curl "${curl_common[@]}" "$endpoint/healthz" >/dev/null
curl "${curl_common[@]}" "$endpoint/health/live" | jq -e '.status == "live"' >/dev/null
curl "${curl_common[@]}" "$endpoint/health/ready" | jq -e '.status == "ready"' >/dev/null
compose exec -T api python -m backend.app.bill_rate_import.sandbox_check \
  | tee "$work/pdf-sandbox.json"
jq -e \
  '.schema_id == "pm-pdf-sandbox-health/1.0.0" and .pdf_sandbox == "enforced"' \
  "$work/pdf-sandbox.json" >/dev/null

readonly test_password="Release-smoke-only-$(openssl rand -hex 12)Aa1!"
jq -cn --arg email 'release-smoke@example.invalid' --arg password "$test_password" \
  '{email:$email,display_name:"Release Smoke",password:$password,home_name:"Release Test Home",timezone:"America/Los_Angeles"}' \
  > "$work/bootstrap.json"
curl "${curl_common[@]}" --cookie-jar "$cookie_jar" \
  -H 'Content-Type: application/json' --data-binary "@$work/bootstrap.json" \
  "$endpoint/api/v1/auth/bootstrap" | jq -e '.user.email == "release-smoke@example.invalid"' >/dev/null
printf '127.0.0.1 %s # %s\n' "$hostname" "$hosts_marker" | sudo tee -a /etc/hosts >/dev/null
python backend/tests/deployment_evidence_probe.py \
  --base-url "$endpoint" --ca-file "$work/tls-ca.crt" \
  --email release-smoke@example.invalid --password "$test_password" \
  --output "$authenticated_evidence"
jq -e \
  '.schema == "pm-deployment-authenticated-evidence/1.0.0" and .status == "passed" and .enrollment == "authenticated" and .heartbeat == "authenticated_pzem" and .reading_sequence == 1 and .usage_source == "authenticated PZEM-004T sensor intervals only" and .rate_source == "reviewed_rate_only_pdf" and (.cost | tonumber) > 0 and .command.delivery == "authenticated" and .command.state == "succeeded"' \
  "$authenticated_evidence" >/dev/null
curl "${curl_common[@]}" --cookie "$cookie_jar" \
  "$endpoint/api/v1/system/health" | jq -e \
  '.database == "reachable" and .backup.last_successful.state == "verified" and .restore_test.last_successful.state == "verified"' \
  >/dev/null

set +e
curl "${curl_common[@]}" --cookie "$cookie_jar" --no-buffer --max-time 8 \
  "$endpoint/api/v1/events" > "$work/events.txt"
sse_status=$?
set -e
[[ "$sse_status" -eq 0 || "$sse_status" -eq 28 ]]
grep -Eq '^event: refresh$' "$work/events.txt"

readonly csrf_token="$(awk '$6 == "pm_csrf" { print $7 }' "$cookie_jar")"
[[ -n "$csrf_token" ]]
head -c 10485761 /dev/zero > "$work/oversize.pdf"
readonly upload_status="$(curl "${curl_common[@]}" --cookie "$cookie_jar" \
  -H "X-CSRF-Token: $csrf_token" -o "$work/upload-response.json" -w '%{http_code}' \
  -F "document=@$work/oversize.pdf;type=application/pdf" \
  "$endpoint/api/v1/bill-rate-imports")"
[[ "$upload_status" == "422" ]]
jq -e '.code == "BILL_RATE_IMPORT_REJECTED"' "$work/upload-response.json" >/dev/null

for service in postgres api worker frontend gateway backup; do
  compose restart "$service"
  compose up --detach --wait --wait-timeout 360
done
compose run --rm migrate
compose stop
compose start
compose up --detach --wait --wait-timeout 360

curl "${curl_common[@]}" "$endpoint/healthz" >/dev/null
for evidence in last-backup-attempt last-successful-backup \
  last-restore-test-attempt last-successful-restore-test; do
  sudo jq -e '.state == "verified"' "$base/backups/status/${evidence}.json" >/dev/null
done
[[ -n "$(find "$base/backups/archives" -maxdepth 1 -type f -name 'powermeter-*.dump.gpg' -print -quit)" ]]

compose ps --format json > "$work/compose-ps.jsonl"
sudo find "$base" -maxdepth 3 -printf '%M %u:%g %p\n' | sort > "$work/permissions.txt"
jq -cn \
  --arg version "$GITHUB_REF_NAME" --arg revision "$GITHUB_SHA" \
  --arg completed_at "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" \
  --arg services "${services[*]}" \
  --argjson backup "$(sudo cat "$base/backups/status/last-successful-backup.json")" \
  --argjson restore "$(sudo cat "$base/backups/status/last-successful-restore-test.json")" \
  --argjson authenticated "$(cat "$authenticated_evidence")" \
  --argjson pdf_sandbox "$(cat "$work/pdf-sandbox.json")" \
  '{schema:"pm-deployment-test/1.0.0",version:$version,revision:$revision,completed_at:$completed_at,status:"passed",services:($services|split(" ")),checks:["exact service set","digest-pinned image startup","TLS chain and hostname","liveness and readiness","API image PDF sandbox self-test","authenticated owner login","authenticated sensor enrollment","authenticated PZEM heartbeat and reading","PZEM-only History","reviewed rate-only PDF","worker-produced sensor cost","authenticated command round trip","authenticated system health","SSE proxy streaming","oversize PDF rejection","per-service restarts","migration rerun","full-stack restart","bind-mount access","encrypted backup","isolated restore"],rollback:"not_applicable_initial_release_candidate",pdf_sandbox:$pdf_sandbox,authenticated_sensor_evidence:$authenticated,backup:$backup,restore_test:$restore}' \
  > "$EVIDENCE_FILE"
cp "$work/compose-ps.jsonl" "${EVIDENCE_FILE%.json}-compose-ps.jsonl"
cp "$work/permissions.txt" "${EVIDENCE_FILE%.json}-permissions.txt"
