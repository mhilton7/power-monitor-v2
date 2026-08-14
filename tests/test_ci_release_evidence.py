from __future__ import annotations

import re
from pathlib import Path

from backend.app.bill_rate_import.parser import extract_rate_plan_from_pdf
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

    launcher = (
        ROOT / "backend/app/bill_rate_import/sandbox_launcher.py"
    ).read_text(encoding="utf-8")
    assert '"x86_64": Path("/lib/ld-musl-x86_64.so.1")' in launcher
    assert '"aarch64": Path("/lib/ld-musl-aarch64.so.1")' in launcher
    assert '"TESSDATA_PREFIX": "/usr/share/tessdata"' in launcher
    assert 'Path("/usr/share/tessdata")' in launcher

    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    image_gate = "Block HIGH and CRITICAL API image findings"
    assert image_gate in ci
    assert "version: v0.72.0" in ci
    assert "image-ref: local/power-monitor-v2-api:${{ github.sha }}" in ci
    assert "ignore-unfixed: false" in ci
    assert ci.index("Prove the API image PDF parser sandbox") < ci.index(image_gate)


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
