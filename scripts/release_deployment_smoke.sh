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
readonly -a runtime_service_names=(postgres api worker frontend gateway backup)
failed_assertion="outside_instrumented_recovery"

compose() {
  PM_HOSTNAME="$hostname" docker compose -f "$COMPOSE_FILE" "$@"
}

wait_healthy() {
  local service="$1" expected_container_id="${2:-}" container_id attempt
  container_id="$(compose ps --quiet "$service")"
  [[ "$container_id" =~ ^[0-9a-f]{64}$ ]]
  [[ -z "$expected_container_id" || "$container_id" == "$expected_container_id" ]]
  for attempt in {1..72}; do
    if docker inspect "$container_id" | jq -e \
      '.[0].State.Running == true and .[0].State.Health.Status == "healthy"' >/dev/null; then
      return 0
    fi
    sleep 5
  done
  printf 'service did not become healthy after restart: %s\n' "$service" >&2
  return 1
}

project_compose_state() {
  jq -c '
    (if type == "array" then .[] else . end) |
    {service:.Service,state:.State,health:(if .Health == "" then null else .Health end),exit_code:.ExitCode} |
    . as $record |
    select(
      (["initialize","postgres","migrate","api","worker","frontend","gateway","backup"] | index($record.service)) != null and
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

collect_worker_cycle_health() {
  local container_id="$1"
  docker exec "$container_id" python -c '
import datetime
import json
import pathlib
import re

try:
    raw = json.loads(pathlib.Path("/tmp/worker-health.json").read_text(encoding="utf-8"))
    state = raw["state"]
    completed = datetime.datetime.fromisoformat(raw["completed_at"])
    error_code = raw.get("error_code")
    if state not in {"healthy", "degraded"}:
        raise ValueError
    if completed.utcoffset() != datetime.timedelta(0):
        raise ValueError
    if state == "healthy" and error_code is not None:
        raise ValueError
    if state == "degraded" and (
        not isinstance(error_code, str)
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", error_code) is None
    ):
        raise ValueError
except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)

print(
    json.dumps(
        {
            "state": state,
            "completed_at": completed.astimezone(datetime.UTC).isoformat().replace("+00:00", "Z"),
            "error_code": error_code,
        },
        allow_nan=False,
        separators=(",", ":"),
    )
)
' 2>/dev/null | jq -ce '
    select(
      type == "object" and
      (.state == "healthy" or .state == "degraded") and
      (.completed_at | type) == "string" and
      (.error_code == null or (.error_code | type) == "string")
    ) |
    {state,completed_at,error_code}
  '
}

collect_failure_diagnostics() {
  local exit_code="$1" service container_id readiness worker_cycle
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
    for service in initialize postgres migrate api worker frontend gateway backup; do
      container_id="$(compose ps --all --quiet "$service" 2>/dev/null | head -n 1)"
      [[ "$container_id" =~ ^[0-9a-f]{64}$ ]] || continue
      readiness="null"
      if [[ "$service" == "api" ]]; then
        readiness="$(collect_api_readiness "$container_id")" || readiness="null"
      fi
      worker_cycle="null"
      if [[ "$service" == "worker" ]]; then
        worker_cycle="$(collect_worker_cycle_health "$container_id")" || worker_cycle="null"
      fi
      if ! docker inspect "$container_id" 2>/dev/null | jq -c \
        --arg service "$service" --arg container_id "$container_id" \
        --argjson readiness "$readiness" --argjson worker_cycle "$worker_cycle" \
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
              ),readiness:$readiness,worker_cycle:$worker_cycle}
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
            "failed_assertion": sys.argv[6],
            "diagnostics": [
                "allowlisted service log event timeline",
                "Compose service state",
                "allowlisted container health state",
                "allowlisted worker cycle health state",
            ],
        },
        allow_nan=False,
        separators=(",", ":"),
    ) + "\n",
    encoding="utf-8",
)
' "$failure_evidence" "${GITHUB_REF_NAME:-unknown}" "${GITHUB_SHA:-unknown}" \
    "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$exit_code" "$failed_assertion"
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
for dataset in postgres config firmware backups logs rate-source-artifacts \
  caddy-data caddy-config secrets; do
  sudo install -d -o "$(id -u)" -g "$(id -g)" -m 0770 "$base/$dataset"
done

umask 077
for name in bootstrap migrator api worker backup restore; do
  printf '%s' "$(openssl rand -hex 32)" > "$work/postgres_${name}_password"
done
printf '%s' "$(openssl rand -base64 32)" > "$work/session_secret"
printf '%s' "$(openssl rand -base64 32)" > "$work/field_encryption_key"
printf '%s' "$(openssl rand -base64 32)" > "$work/ota_manifest_key"
printf '%s' "$(openssl rand -base64 48)" > "$work/backup_encryption_key"
openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 30 \
  -subj '/CN=PowerMeter V2 Release Test Root' \
  -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -addext 'subjectKeyIdentifier=hash' \
  -keyout "$work/tls-ca.key" -out "$work/tls-ca.crt"
openssl req -newkey rsa:3072 -sha256 -nodes -subj "/CN=${hostname}" \
  -keyout "$work/tls.key" -out "$work/tls.csr"
printf '%s\n' \
  'basicConstraints=critical,CA:FALSE' \
  'keyUsage=critical,digitalSignature,keyEncipherment' \
  'extendedKeyUsage=serverAuth' \
  "subjectAltName=DNS:${hostname}" \
  'subjectKeyIdentifier=hash' \
  'authorityKeyIdentifier=keyid:always' \
  > "$work/tls.ext"
openssl x509 -req -sha256 -days 30 -in "$work/tls.csr" \
  -CA "$work/tls-ca.crt" -CAkey "$work/tls-ca.key" -CAcreateserial \
  -extfile "$work/tls.ext" -out "$work/tls.crt"
openssl verify -x509_strict -purpose sslserver -CAfile "$work/tls-ca.crt" \
  -verify_hostname "$hostname" "$work/tls.crt"

for name in postgres_bootstrap_password postgres_migrator_password \
  postgres_api_password postgres_worker_password postgres_backup_password \
  postgres_restore_password session_secret field_encryption_key ota_manifest_key \
  backup_encryption_key tls.crt tls.key tls-ca.crt; do
  sudo install -o "$(id -u)" -g "$(id -g)" -m 0660 "$work/$name" "$base/secrets/$name"
done

compose config --quiet
mapfile -t services < <(compose config --services | sort)
[[ "${services[*]}" == "api backup frontend gateway initialize migrate postgres worker" ]]
compose up --detach --wait --wait-timeout 360
initializer_id="$(compose ps --all --quiet initialize)"
readonly initializer_id
[[ "$initializer_id" =~ ^[0-9a-f]{64}$ ]]
docker inspect "$initializer_id" | jq -e \
  '.[0].State.Status == "exited" and .[0].State.ExitCode == 0' >/dev/null
initializer_finished_at="$(docker inspect --format '{{.State.FinishedAt}}' "$initializer_id")"
readonly initializer_finished_at

curl_transport_common=(--silent --show-error --connect-timeout 5 --max-time 30 \
  --resolve "${hostname}:8443:127.0.0.1" --cacert "$work/tls-ca.crt")
curl_common=(--fail "${curl_transport_common[@]}")
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
failed_assertion="worker_healthy_after_authenticated_pricing"
wait_healthy worker
failed_assertion="outside_instrumented_recovery"
jq -e \
  '.schema == "pm-deployment-authenticated-evidence/1.0.0" and .status == "passed" and .enrollment == "authenticated" and .heartbeat == "authenticated_pzem" and .reading_sequence == 1 and .usage_source == "authenticated PZEM-004T sensor intervals only" and .rate_source == "reviewed_rate_only_pdf" and (.cost | tonumber) > 0 and .command.delivery == "authenticated" and .command.state == "succeeded"' \
  "$authenticated_evidence" >/dev/null
curl "${curl_common[@]}" --cookie "$cookie_jar" \
  "$endpoint/api/v1/system/health" | jq -e \
  --arg expected_version "${GITHUB_REF_NAME#v}" \
  '.version == $expected_version and .database == "reachable" and .backup.last_successful.state == "verified" and .restore_test.last_successful.state == "verified"' \
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
upload_status="$(curl "${curl_transport_common[@]}" --cookie "$cookie_jar" \
  -H "X-CSRF-Token: $csrf_token" -o "$work/upload-response.json" -w '%{http_code}' \
  -F "document=@$work/oversize.pdf;type=application/pdf" \
  "$endpoint/api/v1/bill-rate-imports")"
readonly upload_status
[[ "$upload_status" == "422" ]]
jq -e '.code == "BILL_RATE_IMPORT_REJECTED"' "$work/upload-response.json" >/dev/null

for service in postgres api worker frontend gateway backup; do
  compose restart "$service"
  wait_healthy "$service"
done
compose run --rm initialize
[[ "$(compose ps --all --quiet initialize)" == "$initializer_id" ]]
docker inspect "$initializer_id" | jq -e \
  '.[0].State.Status == "exited" and .[0].State.ExitCode == 0' >/dev/null
[[ "$(docker inspect --format '{{.State.FinishedAt}}' "$initializer_id")" == "$initializer_finished_at" ]]
compose run --rm --no-deps migrate
[[ "$(compose ps --all --quiet initialize)" == "$initializer_id" ]]
[[ "$(docker inspect --format '{{.State.FinishedAt}}' "$initializer_id")" == "$initializer_finished_at" ]]
declare -A runtime_container_ids=()
failed_assertion="capture_runtime_container_id"
for service in "${runtime_service_names[@]}"; do
  container_id="$(compose ps --quiet "$service")"
  [[ "$container_id" =~ ^[0-9a-f]{64}$ ]]
  runtime_container_ids["$service"]="$container_id"
done
[[ "${#runtime_container_ids[@]}" -eq "${#runtime_service_names[@]}" ]]
failed_assertion="runtime_container_stop"
compose stop "${runtime_service_names[@]}"
for service in "${runtime_service_names[@]}"; do
  expected_container_id="${runtime_container_ids[$service]}"
  failed_assertion="runtime_container_stopped"
  [[ "$(compose ps --all --quiet "$service")" == "$expected_container_id" ]]
  docker inspect "$expected_container_id" | jq -e \
    '.[0].State.Status == "exited" and .[0].State.Running == false' >/dev/null
  failed_assertion="runtime_container_identity_before_direct_start"
  [[ "$(compose ps --all --quiet "$service")" == "$expected_container_id" ]]
  failed_assertion="runtime_container_direct_start"
  docker start "$expected_container_id" >/dev/null
  failed_assertion="runtime_container_identity_after_direct_start"
  [[ "$(compose ps --all --quiet "$service")" == "$expected_container_id" ]]
  failed_assertion="runtime_container_healthy_after_direct_start"
  wait_healthy "$service" "$expected_container_id"
done
failed_assertion="initializer_id_unchanged_after_runtime_restart"
[[ "$(compose ps --all --quiet initialize)" == "$initializer_id" ]]
failed_assertion="initializer_exited_zero_after_runtime_restart"
docker inspect "$initializer_id" | jq -e \
  '.[0].State.Status == "exited" and .[0].State.ExitCode == 0' >/dev/null
failed_assertion="initializer_finished_at_unchanged_after_runtime_restart"
[[ "$(docker inspect --format '{{.State.FinishedAt}}' "$initializer_id")" == "$initializer_finished_at" ]]
failed_assertion="outside_instrumented_recovery"

curl "${curl_common[@]}" "$endpoint/healthz" >/dev/null
for evidence in last-backup-attempt last-successful-backup \
  last-restore-test-attempt last-successful-restore-test; do
  sudo jq -e '.state == "verified"' "$base/backups/status/${evidence}.json" >/dev/null
done
archive_path="$(sudo find "$base/backups/archives" -maxdepth 1 -type f -name 'powermeter-*.dump.gpg' -print -quit)"
readonly archive_path
[[ -n "$archive_path" ]]

compose ps --all --format json | project_compose_state > "$work/compose-ps.jsonl"
: > "$work/permissions.txt"
record_metadata() {
  local path="$1" owner="$2" mode="$3" kind="$4"
  [[ "$(sudo stat -c '%u:%g' -- "$path")" == "$owner" ]]
  [[ "$(sudo stat -c '%a' -- "$path")" == "$mode" ]]
  printf '%s|%s|%s|%s\n' "$kind" "$path" "$owner" "$mode" >> "$work/permissions.txt"
}
assert_exact_acl() {
  local path="$1" expected="$2" actual
  actual="$(sudo getfacl -cpn -- "$path" | sed '/^$/d' | LC_ALL=C sort)"
  [[ "$actual" == "$(printf '%s\n' "$expected" | LC_ALL=C sort)" ]]
}
record_secret() {
  local name="$1"
  shift
  local readers=("$@") expected reader rendered=""
  expected=$'user::r--\ngroup::---\nmask::r--\nother::---'
  for reader in "${readers[@]}"; do
    expected+=$'\n'"user:${reader}:r--"
    rendered+="${rendered:+,}${reader}"
  done
  record_metadata "$base/secrets/$name" "0:0" "440" secret
  assert_exact_acl "$base/secrets/$name" "$expected"
  printf 'readers|%s|%s\n' "$name" "$rendered" >> "$work/permissions.txt"
}

for record in \
  "postgres|70:70|700" \
  "config|0:0|755" \
  "firmware|10001:10001|750" \
  "backups|568:568|750" \
  "backups/status|568:568|750" \
  "logs|0:0|711" \
  "logs/application|10001:10001|750" \
  "logs/gateway|1000:1000|750" \
  "rate-source-artifacts|10001:10001|750" \
  "caddy-data|1000:1000|750" \
  "caddy-config|1000:1000|750" \
  "secrets|0:0|711"; do
  IFS='|' read -r relative owner mode <<< "$record"
  record_metadata "$base/$relative" "$owner" "$mode" directory
done
sudo cmp --silent deploy/caddy/Caddyfile "$base/config/Caddyfile"
sudo cmp --silent deploy/postgres/init-roles.sh "$base/config/postgres-init-roles.sh"
config_base_acl=$'user::r--\ngroup::---\nmask::r--\nother::---'
record_metadata "$base/config/Caddyfile" "0:0" "440" config
assert_exact_acl "$base/config/Caddyfile" "$config_base_acl"$'\nuser:1000:r--'
printf 'acl|%s|exact-caddy-read-only\n' "$base/config/Caddyfile" >> "$work/permissions.txt"
record_metadata "$base/config/postgres-init-roles.sh" "0:0" "440" config
assert_exact_acl "$base/config/postgres-init-roles.sh" "$config_base_acl"$'\nuser:70:r--'
printf 'acl|%s|exact-postgres-read-only\n' "$base/config/postgres-init-roles.sh" >> "$work/permissions.txt"
backups_acl=$'user::rwx\nuser:10001:--x\ngroup::r-x\nmask::r-x\nother::---'
assert_exact_acl "$base/backups" "$backups_acl"
printf 'acl|%s|exact-api-traverse-only\n' "$base/backups" >> "$work/permissions.txt"
status_acl=$'user::rwx\nuser:10001:r-x\ngroup::r-x\nmask::r-x\nother::---\ndefault:user::rwx\ndefault:user:10001:r-x\ndefault:group::r-x\ndefault:mask::r-x\ndefault:other::---'
assert_exact_acl "$base/backups/status" "$status_acl"
printf 'acl|%s|exact-api-read-default\n' "$base/backups/status" >> "$work/permissions.txt"
record_secret postgres_bootstrap_password 70
for name in postgres_migrator_password postgres_api_password postgres_worker_password; do
  record_secret "$name" 70 10001
done
for name in postgres_backup_password postgres_restore_password; do
  record_secret "$name" 70 568
done
for name in session_secret field_encryption_key ota_manifest_key; do
  record_secret "$name" 10001
done
record_secret backup_encryption_key 568
for name in tls.crt tls.key tls-ca.crt; do
  record_secret "$name" 1000
done
jq -cn \
  --arg version "$GITHUB_REF_NAME" --arg revision "$GITHUB_SHA" \
  --arg completed_at "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" \
  --arg services "${services[*]}" \
  --argjson backup "$(sudo cat "$base/backups/status/last-successful-backup.json")" \
  --argjson restore "$(sudo cat "$base/backups/status/last-successful-restore-test.json")" \
  --argjson authenticated "$(cat "$authenticated_evidence")" \
  --argjson pdf_sandbox "$(cat "$work/pdf-sandbox.json")" \
  '{schema:"pm-deployment-test/1.0.0",version:$version,revision:$revision,completed_at:$completed_at,status:"passed",services:($services|split(" ")),checks:["exact service set","digest-pinned image startup","one-shot host initializer first run","one-shot host initializer idempotent rerun","TLS chain and hostname","liveness and readiness","API image PDF sandbox self-test","authenticated owner login","authenticated sensor enrollment","authenticated PZEM heartbeat and reading","PZEM-only History","reviewed rate-only PDF","worker-produced sensor cost","worker healthy after authenticated pricing","authenticated command round trip","authenticated system health","SSE proxy streaming","oversize PDF rejection","per-service restarts without initializer restart","migration rerun","full-stack runtime restart without initializer restart","bind-mount access","encrypted backup","isolated restore"],rollback:"not_exercised_github_hosted_smoke",pdf_sandbox:$pdf_sandbox,authenticated_sensor_evidence:$authenticated,backup:$backup,restore_test:$restore}' \
  > "$EVIDENCE_FILE"
cp "$work/compose-ps.jsonl" "$compose_ps_evidence"
cp "$work/permissions.txt" "$permissions_evidence"
