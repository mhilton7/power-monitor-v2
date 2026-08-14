from __future__ import annotations

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
