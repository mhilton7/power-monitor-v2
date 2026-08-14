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
    assert "postgres-init-roles.sh INSTALLATION.md" in release
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
