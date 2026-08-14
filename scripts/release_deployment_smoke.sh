#!/usr/bin/env bash
set -Eeuo pipefail

# This destructive-looking test is restricted to a disposable GitHub-hosted
# runner. It never targets an operator's TrueNAS host.
readonly hostname="power-monitor.home.arpa"
readonly base="/mnt/Apps/PowerMeterV2"
readonly supplied_compose_file="${COMPOSE_FILE:-}"
readonly supplied_evidence_file="${EVIDENCE_FILE:-}"
readonly COMPOSE_FILE="$supplied_compose_file"
readonly EVIDENCE_FILE="${supplied_evidence_file:-release/smoke/deployment-test-report.json}"
work=""
cookie_jar=""
runner_authorized=false
base_owned=false
hosts_entry_added=false
readonly endpoint="https://${hostname}:8443"
readonly hosts_marker="powermeter-v2-release-smoke-${GITHUB_RUN_ID:-unknown}"
readonly smoke_email="release-smoke@example.com"
readonly authenticated_evidence="${EVIDENCE_FILE%.json}-authenticated.json"
readonly compose_ps_evidence="${EVIDENCE_FILE%.json}-compose-ps.jsonl"
readonly permissions_evidence="${EVIDENCE_FILE%.json}-permissions.txt"
readonly failure_evidence="${EVIDENCE_FILE%.json}-failure.json"
readonly failure_compose_ps="${EVIDENCE_FILE%.json}-failure-compose-ps.jsonl"
readonly failure_health="${EVIDENCE_FILE%.json}-failure-health.jsonl"
readonly failure_logs="${EVIDENCE_FILE%.json}-failure-log-events.jsonl"

compose() {
  PM_HOSTNAME="$hostname" docker compose -f "$COMPOSE_FILE" "$@"
}

project_compose_state() {
  jq -c '
    (if type == "array" then .[] else . end) |
    {service:.Service,state:.State,health:(if .Health == "" then null else .Health end),exit_code:.ExitCode} |
    . as $record |
    select(
      (["postgres","migrate","api","worker","frontend","gateway","backup"] | index($record.service)) != null and
      (["created","running","paused","restarting","removing","exited","dead"] | index($record.state)) != null and
      ($record.health == null or (["starting","healthy","unhealthy"] | index($record.health)) != null) and
      ($record.exit_code | type) == "number"
    )
  '
}

collect_api_readiness() {
  local container_id="$1"
  docker exec "$container_id" python -c '
import json
import urllib.error
import urllib.request

try:
    response = urllib.request.urlopen("http://127.0.0.1:8000/health/ready", timeout=10)
except urllib.error.HTTPError as error:
    response = error
try:
    payload = json.loads(response.read(4096))
    payload["http_status"] = response.status
    print(json.dumps(payload, allow_nan=False, separators=(",", ":")))
finally:
    response.close()
' 2>/dev/null | jq -ce '
    select(
      (type == "object") and
      (.http_status == 200 or .http_status == 503) and
      (.status == "ready" or .status == "not_ready") and
      (.database == "ready" or .database == "unavailable") and
      (.pdf_sandbox == "enforced" or .pdf_sandbox == "unavailable")
    ) |
    {http_status,status,database,pdf_sandbox}
  '
}

collect_failure_diagnostics() {
  local exit_code="$1" service container_id readiness
  mkdir -p -- "$(dirname -- "$EVIDENCE_FILE")"
  rm -f -- "$EVIDENCE_FILE" "$authenticated_evidence" \
    "$compose_ps_evidence" "$permissions_evidence"
  : > "$failure_compose_ps"
  if [[ "$runner_authorized" == "true" ]]; then
    compose ps --all --format json 2>/dev/null \
      | project_compose_state > "$failure_compose_ps" || true
  fi
  if [[ ! -s "$failure_compose_ps" ]]; then
    printf '%s\n' '{"service":null,"state":null,"health":null,"exit_code":null}' \
      > "$failure_compose_ps"
  fi
  : > "$failure_health"
  if [[ "$runner_authorized" == "true" ]]; then
    for service in postgres migrate api worker frontend gateway backup; do
      container_id="$(compose ps --all --quiet "$service" 2>/dev/null | head -n 1)"
      [[ "$container_id" =~ ^[0-9a-f]{64}$ ]] || continue
      readiness="null"
      if [[ "$service" == "api" ]]; then
        readiness="$(collect_api_readiness "$container_id")" || readiness="null"
      fi
      if ! docker inspect "$container_id" 2>/dev/null | jq -c \
        --arg service "$service" --arg container_id "$container_id" --argjson readiness "$readiness" \
        '.[0].State as $state |
          {service:$service,container_id:$container_id,state:(
            if
              (["created","running","paused","restarting","removing","exited","dead"] | index($state.Status)) != null and
              ($state.Running | type) == "boolean" and
              ($state.Restarting | type) == "boolean" and
              ($state.OOMKilled | type) == "boolean" and
              ($state.Dead | type) == "boolean" and
              ($state.ExitCode | type) == "number"
            then
              {status:$state.Status,running:$state.Running,restarting:$state.Restarting,oom_killed:$state.OOMKilled,dead:$state.Dead,exit_code:$state.ExitCode,health:(
                if
                  ($state.Health | type) == "object" and
                  (["starting","healthy","unhealthy"] | index($state.Health.Status)) != null and
                  ($state.Health.FailingStreak | type) == "number"
                then {status:$state.Health.Status,failing_streak:$state.Health.FailingStreak}
                else null
                end
              ),readiness:$readiness}
            else null
            end
          )}' \
        >> "$failure_health"; then
        jq -cn --arg service "$service" --arg container_id "$container_id" \
          '{service:$service,container_id:$container_id,state:null}' >> "$failure_health"
      fi
    done
  fi
  if [[ ! -s "$failure_health" ]]; then
    printf '%s\n' '{"service":null,"container_id":null,"state":null}' \
      > "$failure_health"
  fi
  : > "$failure_logs"
  if [[ "$runner_authorized" == "true" ]]; then
    compose logs --tail 2000 --no-color --timestamps 2>&1 \
      | python scripts/redact_deployment_logs.py > "$failure_logs" || true
  fi
  if [[ ! -s "$failure_logs" ]]; then
    printf '%s\n' \
      '{"line_number":0,"service":null,"timestamp":null,"event":"unavailable"}' \
      > "$failure_logs"
  fi
  python -c '
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(
    json.dumps(
        {
            "schema": "pm-deployment-failure/1.0.0",
            "version": sys.argv[2],
            "revision": sys.argv[3],
            "completed_at": sys.argv[4],
            "status": "failed",
            "exit_code": int(sys.argv[5]),
            "diagnostics": [
                "allowlisted service log event timeline",
                "Compose service state",
                "allowlisted container health state",
            ],
        },
        allow_nan=False,
        separators=(",", ":"),
    ) + "\n",
    encoding="utf-8",
)
' "$failure_evidence" "${GITHUB_REF_NAME:-unknown}" "${GITHUB_SHA:-unknown}" \
    "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$exit_code"
}

cleanup() {
  local exit_code="$?"
  trap - EXIT
  set +e
  if [[ "$exit_code" -ne 0 ]]; then
    collect_failure_diagnostics "$exit_code"
  fi
  if [[ "$runner_authorized" == "true" && -n "$COMPOSE_FILE" && -f "$COMPOSE_FILE" ]]; then
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  fi
  if [[ "$hosts_entry_added" == "true" ]]; then
    sudo sed -i "/# ${hosts_marker}$/d" /etc/hosts >/dev/null 2>&1 || true
  fi
  if [[ "$base_owned" == "true" && "$base" == "/mnt/Apps/PowerMeterV2" && -d "$base" ]]; then
    sudo rm -rf -- "$base"
  fi
  if [[ -n "$work" && -d "$work" ]]; then
    rm -rf -- "$work"
  fi
  exit "$exit_code"
}
trap cleanup EXIT

[[ "${GITHUB_ACTIONS:-}" == "true" && "${RUNNER_ENVIRONMENT:-}" == "github-hosted" ]]
[[ -n "$supplied_compose_file" && -n "$supplied_evidence_file" ]]
[[ -f "$COMPOSE_FILE" ]]
command -v python >/dev/null
command -v jq >/dev/null
command -v docker >/dev/null
command -v setfacl >/dev/null
runner_authorized=true
work="$(mktemp -d "${RUNNER_TEMP:?}/pm-release-smoke.XXXXXX")"
readonly work
cookie_jar="$work/cookies.txt"
readonly cookie_jar

[[ ! -e "$base" ]]
base_owned=true
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
jq -cn --arg email "$smoke_email" --arg password "$test_password" \
  '{email:$email,display_name:"Release Smoke",password:$password,home_name:"Release Test Home",timezone:"America/Los_Angeles"}' \
  > "$work/bootstrap.json"
curl "${curl_common[@]}" --cookie-jar "$cookie_jar" \
  -H 'Content-Type: application/json' --data-binary "@$work/bootstrap.json" \
  "$endpoint/api/v1/auth/bootstrap" \
  | jq -e --arg email "$smoke_email" '.user.email == $email' >/dev/null
printf '127.0.0.1 %s # %s\n' "$hostname" "$hosts_marker" | sudo tee -a /etc/hosts >/dev/null
hosts_entry_added=true
python backend/tests/deployment_evidence_probe.py \
  --base-url "$endpoint" --ca-file "$work/tls-ca.crt" \
  --email "$smoke_email" --password "$test_password" \
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

compose ps --format json | project_compose_state > "$work/compose-ps.jsonl"
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
cp "$work/compose-ps.jsonl" "$compose_ps_evidence"
cp "$work/permissions.txt" "$permissions_evidence"
