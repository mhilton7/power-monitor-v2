from __future__ import annotations

import re
from pathlib import Path

import yaml
from backend.app.bill_rate_import.parser import extract_rate_plan_from_pdf
from backend.app.schemas.api import BootstrapRequest
from backend.tests.deployment_evidence_probe import _rate_source_pdf

ROOT = Path(__file__).resolve().parents[1]


def test_deployment_probe_pdf_contains_rates_but_no_usage_evidence() -> None:
    draft, ignored_categories = extract_rate_plan_from_pdf(_rate_source_pdf())
    assert draft.rate_plan_name == "TOU-D-4-9PM"
    assert len(draft.periods) == 13
    assert draft.review_required is True
    assert set(ignored_categories).isdisjoint(
        {"BILL_USAGE", "BILL_TOTAL", "CUSTOMER_IDENTITY", "ACCOUNT_IDENTIFIER"}
    )


def test_ci_and_release_gates_use_postgres_roles_and_production_browser_e2e() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    gates = (ROOT / ".github/workflows/release-gates.yml").read_text(encoding="utf-8")
    for workflow in (ci, gates):
        assert 'POSTGRES_USER: pm_bootstrap' in workflow
        assert 'PM_REQUIRE_POSTGRES_TESTS: "1"' in workflow
        assert "deploy/postgres/init-roles.sh" in workflow
        assert "PM_TEST_MIGRATOR_DATABASE_URL=postgresql+asyncpg://pm_migrator:" in workflow
        assert "PM_DATABASE_URL=postgresql+asyncpg://pm_api:" in workflow
        assert "permission denied|not permitted" in workflow
        assert "npm run test:e2e -- --reporter=line,junit" in workflow
        assert workflow.index("alembic -c backend/alembic.ini upgrade head") < workflow.index(
            "pytest"
        )
    assert "python -m backend.app.bill_rate_import.sandbox_check" in ci

    package = (ROOT / "frontend/package.json").read_text(encoding="utf-8")
    assert '"serve:e2e": "npm run build && npm run preview:test"' in package


def test_release_preserves_role_initializer_and_smoke_uses_role_scoped_secrets() -> None:
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    promotion = (ROOT / ".github/workflows/stable-promotion.yml").read_text(encoding="utf-8")
    smoke = (ROOT / "scripts/release_deployment_smoke.sh").read_text(encoding="utf-8")
    assert "cp deploy/postgres/init-roles.sh release/assets/postgres-init-roles.sh" in release
    assert "cp deploy/truenas/prepare-host.sh release/assets/prepare-host.sh" in release
    assert "cp docs/FIRST_RUN.md release/assets/FIRST_RUN.md" in release
    assert "cp docs/BACKUPS_AND_RESTORE.md release/assets/BACKUPS_AND_RESTORE.md" in release
    assert "postgres-init-roles.sh INSTALLATION.md" in release
    assert "prepare-host.sh DATASET_ACLS.md SECRETS.md" in release
    assert 'done < release/promotion/server-candidate/SHA256SUMS' in promotion
    assert "server-candidate/postgres-init-roles.sh" in promotion
    assert "stable/postgres-init-roles.sh" in promotion

    assert (
        'setfacl -m u:70:r "$base/secrets/postgres_bootstrap_password"' in smoke
    )
    application_acl = smoke[
        smoke.index("sudo setfacl -m u:70:r,u:10001:r") : smoke.index(
            "sudo setfacl -m u:70:r,u:568:r"
        )
    ]
    for secret in (
        "postgres_migrator_password",
        "postgres_api_password",
        "postgres_worker_password",
    ):
        assert secret in application_acl
    backup_start = smoke.index("sudo setfacl -m u:70:r,u:568:r")
    backup_acl = smoke[
        backup_start : smoke.index("sudo setfacl -m u:10001:r", backup_start)
    ]
    for secret in ("postgres_backup_password", "postgres_restore_password"):
        assert secret in backup_acl
    assert '"$work/postgres_password"' not in smoke
    assert "backend/tests/deployment_evidence_probe.py" in smoke
    assert "backend.app.bill_rate_import.sandbox_check" in smoke


def test_release_gate_stages_only_flat_frontend_evidence_files() -> None:
    gates = (ROOT / ".github/workflows/release-gates.yml").read_text(encoding="utf-8")
    assert (
        "install -m 0644 frontend/playwright-results.xml frontend-playwright-results.xml"
        in gates
    )
    assert "install -m 0644 frontend/npm-audit.json frontend-npm-audit.json" in gates
    assert "install -d -m 0755 frontend/test-results" in gates
    assert "-czf frontend-test-results.tar.gz -C frontend test-results" in gates

    upload = gates.split("name: release-gate-reports", maxsplit=1)[1]
    upload = upload.split("if-no-files-found: error", maxsplit=1)[0]
    uploaded_paths = [
        line.strip()
        for line in upload.splitlines()
        if line.startswith("            ") and line.strip()
    ]
    assert "frontend-playwright-results.xml" in uploaded_paths
    assert "frontend-npm-audit.json" in uploaded_paths
    assert "frontend-test-results.tar.gz" in uploaded_paths
    assert all(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", path) for path in uploaded_paths
    )


def test_release_checksums_only_flat_regular_files_and_publishes_every_asset() -> None:
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "sha256sum -- *" not in release
    assert "find . -mindepth 1 -maxdepth 1 ! -type f -print -quit" in release
    assert '[[ "$asset" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]' in release
    assert 'sha256sum -- "${release_assets[@]}" > SHA256SUMS' in release
    assert "sha256sum --check --strict SHA256SUMS" in release
    assert (
        "frontend-playwright-results.xml frontend-npm-audit.json "
        "frontend-test-results.tar.gz"
        in release
    )
    assert "files: release/assets/*" in release


def test_contract_workflows_install_exact_server_lock_before_validator() -> None:
    workflows = (
        (
            ROOT / ".github/workflows/release.yml",
            "-r backend/requirements.lock jsonschema==4.25.1",
            "python scripts/validate_firmware_contract.py",
        ),
        (
            ROOT / ".github/workflows/firmware-contract.yml",
            "-r server/backend/requirements.lock jsonschema==4.25.1",
            "python server/scripts/validate_firmware_contract.py",
        ),
        (
            ROOT / ".github/workflows/stable-promotion.yml",
            "-r backend/requirements.lock jsonschema==4.25.1",
            "python scripts/validate_firmware_contract.py",
        ),
    )

    for path, install, validator in workflows:
        workflow = path.read_text(encoding="utf-8")
        assert install in workflow
        assert workflow.index(install) < workflow.index(validator)


def test_backup_image_removes_unused_inherited_privilege_helper() -> None:
    dockerfile = (ROOT / "backup/Dockerfile").read_text(encoding="utf-8")
    removal = "rm -f /usr/local/bin/gosu"
    assert removal in dockerfile
    assert "test ! -e /usr/local/bin/gosu" in dockerfile
    assert dockerfile.index(removal) < dockerfile.index("USER 568:568")
    assert 'ENTRYPOINT ["/opt/powermeter/entrypoint.sh"]' in dockerfile

    for script in (ROOT / "backup").glob("*.sh"):
        assert "gosu" not in script.read_text(encoding="utf-8")

    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    runtime_check = "Reject the unused inherited backup privilege helper"
    assert runtime_check in ci
    assert "test ! -e /usr/local/bin/gosu" in ci
    assert ci.index(runtime_check) < ci.index("Prove the API image PDF parser sandbox")


def test_api_image_uses_the_zero_finding_alpine_base_and_pinned_ocr() -> None:
    dockerfile = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    base = (
        "python:3.13.14-alpine3.23@"
        "sha256:9fdbf2e3e82628351513560b121e2ee6ce31cac212be9e070c5a5e2769fb5e76"
    )
    assert dockerfile.count(f"FROM {base}") == 2
    assert "slim-bookworm" not in dockerfile
    assert "/sbin/apk add --no-cache" in dockerfile
    assert "tesseract-ocr=5.5.1-r0" in dockerfile
    assert "tesseract-ocr-data-eng=5.5.1-r0" in dockerfile
    assert "font-dejavu=2.37-r6" in dockerfile
    assert "timeout=10" in dockerfile
    assert "HEALTHCHECK --interval=15s --timeout=15s" in dockerfile
    assert '"--loop", "asyncio"' in dockerfile

    launcher = (
        ROOT / "backend/app/bill_rate_import/sandbox_launcher.py"
    ).read_text(encoding="utf-8")
    assert '"x86_64": Path("/lib/ld-musl-x86_64.so.1")' in launcher
    assert '"aarch64": Path("/lib/ld-musl-aarch64.so.1")' in launcher
    assert '"TESSDATA_PREFIX": "/usr/share/tessdata"' in launcher
    assert 'Path("/usr/share/tessdata")' in launcher

    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    image_gate = "Block HIGH and CRITICAL final-image findings"
    assert image_gate in ci
    assert "version: v0.72.0" in ci
    assert "image-ref: local/power-monitor-v2-${{ matrix.name }}:${{ github.sha }}" in ci
    assert "ignore-unfixed: false" in ci
    assert ci.index("Prove the API image PDF parser sandbox") < ci.index(image_gate)


def test_gateway_image_removes_unneeded_file_capability_and_is_release_owned() -> None:
    dockerfile = (ROOT / "gateway/Dockerfile").read_text(encoding="utf-8")
    main_go = (ROOT / "gateway/main.go").read_text(encoding="utf-8")
    go_mod = (ROOT / "gateway/go.mod").read_text(encoding="utf-8")
    base = (
        "caddy:2.11.4-alpine@"
        "sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
    )
    builder = (
        "golang:1.26.6-alpine3.23@"
        "sha256:5978cc992ad5ef96a7469713c8af849c1433824761ce3be2c56381403cd8d9a3"
    )
    assert dockerfile.count(f"FROM {base}") == 1
    assert dockerfile.count(f"FROM {builder} AS builder") == 1
    assert "GOTOOLCHAIN=local" in dockerfile
    assert "go mod download && go mod verify" in dockerfile
    assert "go mod tidy -diff" in dockerfile
    assert dockerfile.count("CGO_ENABLED=0 go build -mod=readonly -trimpath") == 2
    assert dockerfile.count("-tags=nobadger,nomysql,nopgx") == 2
    assert "cmp -s /out/caddy.first /out/caddy.second" in dockerfile
    assert "-buildid=" in dockerfile
    assert "go1.26.6" in dockerfile
    assert "v2.11.4-pmv2.1" in dockerfile
    assert "h1:XKxkMTgNSizEvKG6QHue6cAsFOteU2qA61w2tKkCWi0=" in dockerfile
    for module in (
        "golang.org/x/net v0.56.0",
        "golang.org/x/text v0.39.0",
        "google.golang.org/grpc v1.82.1",
    ):
        assert module in go_mod
        assert module in dockerfile
    assert go_mod.startswith("module github.com/mhilton7/power-monitor-v2/gateway\n\ngo 1.26.6\n")
    assert '_ "time/tzdata"' in main_go
    for package in ("c-ares=1.34.8-r0", "curl=8.20.0-r0", "libcurl=8.20.0-r0"):
        assert package in dockerfile
    for license_file in (
        "CADDY-LICENSE.txt",
        "X-NET-LICENSE.txt",
        "X-TEXT-LICENSE.txt",
        "GRPC-LICENSE.txt",
        "GO-STDLIB-LICENSE.txt",
        "POWER-METER-V2-LICENSE.txt",
    ):
        assert license_file in dockerfile
    assert "install -d -o 1000 -g 1000 -m 0750 /var/log/powermeter" in dockerfile
    assert "chown -R 1000:1000 /data /config" in dockerfile
    assert "/usr/sbin/setcap -r /usr/bin/caddy" in dockerfile
    assert 'test -z "$(/usr/sbin/getcap /usr/bin/caddy)"' in dockerfile
    assert dockerfile.rstrip().endswith("USER 1000:1000")

    truenas = (ROOT / "deploy/truenas/power-monitor-v2.yaml").read_text(encoding="utf-8")
    gateway = truenas.split("\n  gateway:", maxsplit=1)[1].split("\n  backup:", maxsplit=1)[0]
    assert (
        "ghcr.io/mhilton7/power-monitor-v2-gateway:0.0.0-unpublished@"
        "sha256:UNPUBLISHED_GATEWAY_DIGEST"
    ) in gateway
    assert "cap_drop: [ALL]" in gateway
    assert "security_opt: [no-new-privileges:true]" in gateway
    assert 'user: "1000:1000"' in gateway
    assert "cap_add:" not in gateway
    assert "curl --fail --silent --show-error --cacert /run/secrets/tls_ca" in gateway
    assert "wget -q --spider --ca-certificate" not in gateway

    local_compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    local_gateway = local_compose["services"]["gateway"]
    assert local_gateway["image"] == "${PM_GATEWAY_IMAGE:-power-meter-v2-gateway:local}"
    assert local_gateway["build"] == {"context": ".", "dockerfile": "gateway/Dockerfile"}
    assert local_gateway["user"] == "1000:1000"
    for secret in local_gateway["secrets"]:
        assert secret["uid"] == "1000"
        assert secret["gid"] == "1000"
        assert secret["mode"] == 0o440
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Compose file-backed secrets do not remap host" in readme
    assert "never world-readable" in readme

    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    runtime = ci.split("- name: Prove the gateway image under release restrictions", maxsplit=1)[1]
    runtime = runtime.split("- name: Block HIGH and CRITICAL final-image findings", maxsplit=1)[0]
    for value in (
        "--read-only",
        "--user 1000:1000",
        "--cap-drop ALL",
        "--security-opt no-new-privileges:true",
        "caddy version",
        "caddy list-modules --skip-standard",
        "caddy validate",
        "v2.11.4-pmv2.1 v2.11.4 h1:XKxkMTgNSizEvKG6QHue6cAsFOteU2qA61w2tKkCWi0=",
    ):
        assert value in runtime

    dependabot = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))
    gateway_ecosystems = {
        update["package-ecosystem"]
        for update in dependabot["updates"]
        if update["directory"] == "/gateway"
    }
    assert gateway_ecosystems == {"docker", "gomod"}

    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    promotion = (ROOT / ".github/workflows/stable-promotion.yml").read_text(encoding="utf-8")
    assert "image: ghcr.io/mhilton7/power-monitor-v2-gateway" in release
    assert "dockerfile: gateway/Dockerfile" in release
    assert release.count("for component in api frontend gateway backup; do") == 4
    assert promotion.count("for component in api frontend gateway backup; do") == 4
    for asset in (
        "gateway.spdx.json",
        "gateway-security.json",
        "candidate-gateway-image.json",
    ):
        assert asset in release
    assert 'test -s "$sbom"' in promotion
    assert 'test -s "$security"' in promotion
    assert 'select(.Severity == "HIGH" or .Severity == "CRITICAL")' in promotion


def test_release_smoke_bootstrap_identity_is_schema_valid_and_consistent() -> None:
    smoke = (ROOT / "scripts/release_deployment_smoke.sh").read_text(encoding="utf-8")
    match = re.search(r'^readonly smoke_email="([^"]+)"$', smoke, flags=re.MULTILINE)
    assert match is not None
    smoke_email = match.group(1)

    payload = BootstrapRequest.model_validate(
        {
            "email": smoke_email,
            "display_name": "Release Smoke",
            "password": "Release-smoke-only-0123456789abcdef01234567Aa1!",
            "home_name": "Release Test Home",
            "timezone": "America/Los_Angeles",
        }
    )
    assert str(payload.email) == "release-smoke@example.com"
    assert not smoke_email.endswith(".invalid")
    assert smoke.count("release-smoke@example.com") == 1
    assert "release-smoke@example.invalid" not in smoke
    for use in (
        '--arg email "$smoke_email"',
        "'.user.email == $email'",
        '--email "$smoke_email"',
    ):
        assert use in smoke


def test_release_smoke_and_operator_tls_validation_are_strict() -> None:
    smoke = (ROOT / "scripts/release_deployment_smoke.sh").read_text(encoding="utf-8")
    preflight = (ROOT / "deploy/truenas/prepare-host.sh").read_text(encoding="utf-8")
    secrets = (ROOT / "deploy/truenas/SECRETS.md").read_text(encoding="utf-8")

    extension_counts = {
        "-addext 'basicConstraints=critical,CA:TRUE,pathlen:0'": 1,
        "-addext 'keyUsage=critical,keyCertSign,cRLSign'": 1,
        "'basicConstraints=critical,CA:FALSE'": 1,
        "'keyUsage=critical,digitalSignature,keyEncipherment'": 1,
        "'extendedKeyUsage=serverAuth'": 1,
        '"subjectAltName=DNS:${hostname}"': 1,
        "'subjectKeyIdentifier=hash'": 2,
        "'authorityKeyIdentifier=keyid:always'": 1,
    }
    for extension, expected_count in extension_counts.items():
        assert smoke.count(extension) == expected_count

    strict_smoke_verify = (
        'openssl verify -x509_strict -purpose sslserver -CAfile "$work/tls-ca.crt"'
    )
    assert smoke.count(strict_smoke_verify) == 1
    assert preflight.count("openssl verify -x509_strict -purpose sslserver") == 2
    assert secrets.count("openssl verify -x509_strict -purpose sslserver") == 2


def test_release_smoke_preserves_redacted_failure_diagnostics() -> None:
    smoke = (ROOT / "scripts/release_deployment_smoke.sh").read_text(encoding="utf-8")
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    truenas = (ROOT / "deploy/truenas/power-monitor-v2.yaml").read_text(encoding="utf-8")

    assert "collect_failure_diagnostics" in smoke
    assert 'rm -f -- "$EVIDENCE_FILE" "$authenticated_evidence"' in smoke
    assert ".[0].State as $state" in smoke
    assert "failing_streak:$state.Health.FailingStreak" in smoke
    assert "readiness:$readiness" in smoke
    assert "$state.Error" not in smoke
    assert "$state.Health.Log" not in smoke
    assert "project_compose_state" in smoke
    assert "--tail 2000" in smoke
    assert "python scripts/redact_deployment_logs.py" in smoke
    assert "sed -E" not in smoke
    assert smoke.index("trap cleanup EXIT") < smoke.index(
        '[[ "${GITHUB_ACTIONS:-}" == "true"'
    )
    assert 'runner_authorized" == "true"' in smoke
    assert 'base_owned" == "true"' in smoke
    assert smoke.index('collect_failure_diagnostics "$exit_code"') < smoke.index(
        "compose down --volumes --remove-orphans"
    )

    deployment = release.split("  deployment-smoke:", maxsplit=1)[1]
    deployment = deployment.split("  public-distribution:", maxsplit=1)[0]
    upload = deployment.split("uses: actions/upload-artifact@", maxsplit=1)[1]
    assert "if: always()" in upload
    assert "continue-on-error" not in deployment
    assert "id: smoke" in deployment
    assert "Require complete deployment or failure evidence" in deployment
    assert "SMOKE_OUTCOME: ${{ steps.smoke.outcome }}" in deployment
    assert "EXPECTED_VERSION: ${{ github.ref_name }}" in deployment
    assert "EXPECTED_REVISION: ${{ github.sha }}" in deployment
    assert "python scripts/validate_deployment_evidence.py" in deployment
    assert '--outcome "$SMOKE_OUTCOME"' in deployment
    assert '--expected-version "$EXPECTED_VERSION"' in deployment
    assert '--expected-revision "$EXPECTED_REVISION"' in deployment
    for suffix in (
        "failure.json",
        "failure-compose-ps.jsonl",
        "failure-health.jsonl",
        "failure-log-events.jsonl",
    ):
        assert f"deployment-test-report-{suffix}" in upload

    assemble = release.split("  assemble:", maxsplit=1)[1]
    assert "needs: [release-gates, build-images, deployment-smoke, public-distribution]" in assemble

    assert "timeout=10" in truenas
    assert "timeout: 15s" in truenas


def test_frontend_uses_clean_runtime_base_and_ci_scans_every_final_image() -> None:
    dockerfile = (ROOT / "frontend/Dockerfile").read_text(encoding="utf-8")
    runtime_base = (
        "nginxinc/nginx-unprivileged:1.30.4-alpine3.24@"
        "sha256:44e36330f74d4f3a1d4e222acca9e23b401fb87811a7597024502bb759c4dd49"
    )
    assert dockerfile.count(f"FROM {runtime_base}") == 1
    assert "1.28.0-alpine3.21" not in dockerfile
    assert dockerfile.index(f"FROM {runtime_base}") < dockerfile.index("USER 101")

    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    container_matrix = ci.split("\n  containers:", maxsplit=1)[1]
    container_matrix = container_matrix.split("    steps:", maxsplit=1)[0]
    for image_name in ("api", "frontend", "gateway", "backup"):
        assert f"- name: {image_name}" in container_matrix

    gate_name = "Block HIGH and CRITICAL final-image findings"
    assert ci.count(gate_name) == 1
    gate = ci.split(f"- name: {gate_name}", maxsplit=1)[1]
    gate = gate.split("- uses: actions/upload-artifact", maxsplit=1)[0]
    assert "if:" not in gate
    assert "uses: aquasecurity/trivy-action@57a97c7e7821a5776cebc9bb87c984fa69cba8f1" in gate
    assert "version: v0.72.0" in gate
    assert "scan-type: image" in gate
    assert "image-ref: local/power-monitor-v2-${{ matrix.name }}:${{ github.sha }}" in gate
    assert "severity: HIGH,CRITICAL" in gate
    assert "ignore-unfixed: false" in gate
    assert "exit-code: 1" in gate
    assert "ignorefile:" not in gate


def test_truenas_operator_bundle_is_fail_closed_and_complete() -> None:
    installation = (ROOT / "deploy/truenas/INSTALLATION.md").read_text(encoding="utf-8")
    datasets = (ROOT / "deploy/truenas/DATASET_ACLS.md").read_text(encoding="utf-8")
    secrets = (ROOT / "deploy/truenas/SECRETS.md").read_text(encoding="utf-8")
    preflight = (ROOT / "deploy/truenas/prepare-host.sh").read_text(encoding="utf-8")
    template = (ROOT / "deploy/truenas/power-monitor-v2.yaml").read_text(encoding="utf-8")

    assert "Install via YAML" in installation
    assert "gh attestation verify" in installation
    assert "sha256sum --check --strict SHA256SUMS" in installation
    assert "scripts/verify_release_artifacts.py" not in installation
    assert "docker exec --user 568:568" in installation
    assert "pm-protocol/1.0.0" in installation
    assert "authenticated PZEM-004T readings" in installation

    for dataset in (
        "postgres",
        "config",
        "firmware",
        "backups",
        "logs",
        "rate-source-artifacts",
        "bill-rate-source-artifacts",
        "caddy-data",
        "caddy-config",
        "secrets",
    ):
        assert f"Apps/PowerMeterV2/{dataset}" in datasets
    assert "Generic/POSIX" in datasets
    assert "0777" not in datasets

    for secret in (
        "postgres_bootstrap_password",
        "postgres_migrator_password",
        "postgres_api_password",
        "postgres_worker_password",
        "postgres_backup_password",
        "postgres_restore_password",
        "session_secret",
        "field_encryption_key",
        "ota_manifest_key",
        "backup_encryption_key",
        "tls.crt",
        "tls.key",
        "tls-ca.crt",
    ):
        assert secret in secrets
        assert secret in preflight

    assert 'readonly base="/mnt/Apps/PowerMeterV2"' in preflight
    assert "sha256sum --check --strict SHA256SUMS" in preflight
    assert "must be the mount point of its own ZFS dataset" in preflight
    assert "getfacl -cpn" in preflight
    assert "UNPUBLISHED_API_DIGEST" in template
    assert "UNPUBLISHED_FRONTEND_DIGEST" in template
    assert "UNPUBLISHED_GATEWAY_DIGEST" in template
    assert "UNPUBLISHED_BACKUP_DIGEST" in template


def test_candidate_notes_describe_workflow_output_without_claiming_source_publication() -> None:
    notes = (ROOT / "release/RELEASE_NOTES.md").read_text(encoding="utf-8")
    normalized = " ".join(notes.split())
    assert "presence alone does not prove a release workflow ran" in normalized
    assert "power-monitor-v2-v0.1.0-rc.1.yaml" in normalized
    assert "hardware-certification-status.json` remains `pending`" in normalized
    assert "at least 72 hours" in normalized
    assert "repositories do not yet exist" not in normalized
    assert "Current `gh` authentication is invalid" not in normalized
