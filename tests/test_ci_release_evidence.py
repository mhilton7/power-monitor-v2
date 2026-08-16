from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest
import yaml
from backend.app.bill_rate_import.parser import extract_rate_plan_from_pdf
from backend.app.schemas.api import BootstrapRequest
from backend.tests.deployment_evidence_probe import _rate_source_pdf
from scripts.validate_deployment_evidence import FAILURE_ASSERTIONS

ROOT = Path(__file__).resolve().parents[1]


def test_gitleaks_private_key_allowlist_is_exactly_scoped_to_synthetic_fixture() -> None:
    policy = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    assert set(policy) == {"title", "extend", "rules"}
    private_key_rules = [rule for rule in policy["rules"] if rule["id"] == "private-key"]
    assert len(private_key_rules) == 1
    rule = private_key_rules[0]
    assert set(rule) == {"id", "allowlists"}
    assert len(rule["allowlists"]) == 1
    allowlist = rule["allowlists"][0]
    assert allowlist["description"] == (
        "Allow only the exact historical synthetic encrypted-key rejection fixture"
    )
    assert allowlist["condition"] == "AND"
    assert allowlist["regexTarget"] == "match"
    assert allowlist["commits"] == ["7b025b031c12e3760ffad1f4471ce4b3bb69ccfb"]
    assert allowlist["paths"] == [r"^tests/test_host_initializer\.py$"]
    assert len(allowlist["regexes"]) == 1
    assert hashlib.sha256(allowlist["regexes"][0].encode()).hexdigest() == (
        "4e2149c40b0053568b7fd2aca8fe990e12a4b731d342e30366460e7a6e72a2b7"
    )

    ignored_fingerprints = [
        line
        for line in (ROOT / ".gitleaksignore").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert ignored_fingerprints == [
        "ec5cd5b66affcf7a155f36b74aac3654e6636c94:tests/test_ci_release_evidence.py:private-key:39"
    ]


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
        assert "POSTGRES_USER: pm_bootstrap" in workflow
        assert 'PM_REQUIRE_POSTGRES_TESTS: "1"' in workflow
        assert "deploy/postgres/init-roles.sh" in workflow
        assert "PM_TEST_MIGRATOR_DATABASE_URL=postgresql+asyncpg://pm_migrator:" in workflow
        assert "PM_DATABASE_URL=postgresql+asyncpg://pm_api:" in workflow
        assert "permission denied|not permitted" in workflow
        assert "npm run test:e2e -- --reporter=line,junit" in workflow
        assert "python -m ruff format --check" in workflow
        assert "backend/app backend/tests worker tests scripts" in workflow
        assert "python -m mypy --platform linux deploy/truenas/initialize_host.py" in workflow
        assert "Stage-PowerMeterTrueNAS.ps1" in workflow
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
    assert "Stage-PowerMeterTrueNAS.ps1" in release
    assert "cp deploy/truenas/initialize_host.py release/assets/initialize-host.py" in release
    assert "release/assets/prepare-host.sh" not in release
    assert "cp docs/FIRST_RUN.md release/assets/FIRST_RUN.md" in release
    assert "cp docs/BACKUPS_AND_RESTORE.md release/assets/BACKUPS_AND_RESTORE.md" in release
    assert "postgres-init-roles.sh INSTALLATION.md" in release
    assert "Stage-PowerMeterTrueNAS.ps1 initialize-host.py" in release
    assert "done < release/promotion/server-candidate/SHA256SUMS" in promotion
    assert "server-candidate/postgres-init-roles.sh" in promotion
    assert "stable/postgres-init-roles.sh" in promotion
    assert "server-candidate/Stage-PowerMeterTrueNAS.ps1" in promotion
    assert "stable/initialize-host.py" in promotion

    first_up = smoke[: smoke.index("compose up --detach --wait --wait-timeout 360")]
    assert 'sudo install -o "$(id -u)" -g "$(id -g)" -m 0660' in first_up
    secret_staging = first_up[first_up.index("for name in postgres_bootstrap_password") :]
    assert "setfacl" not in secret_staging
    assert "sudo setfacl" not in first_up
    assert (
        "for dataset in postgres config firmware backups logs rate-source-artifacts \\" in first_up
    )
    for child in ("backups/status", "logs/application", "logs/gateway"):
        assert f'"$base/{child}"' not in first_up
    assert '"$base/config/Caddyfile"' not in first_up
    assert '"$base/config/postgres-init-roles.sh"' not in first_up
    assert "assert_exact_acl" in smoke
    assert "record_secret postgres_bootstrap_password 70" in smoke
    assert 'sudo cmp --silent deploy/caddy/Caddyfile "$base/config/Caddyfile"' in smoke
    assert '"$work/postgres_password"' not in smoke
    assert "backend/tests/deployment_evidence_probe.py" in smoke
    assert "backend.app.bill_rate_import.sandbox_check" in smoke


def test_release_gate_stages_only_flat_frontend_evidence_files() -> None:
    gates = (ROOT / ".github/workflows/release-gates.yml").read_text(encoding="utf-8")
    assert (
        "install -m 0644 frontend/playwright-results.xml frontend-playwright-results.xml" in gates
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
    assert all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", path) for path in uploaded_paths)


def test_release_gate_archives_latest_same_major_public_release_not_newest_tag() -> None:
    gates = (ROOT / ".github/workflows/release-gates.yml").read_text(encoding="utf-8")
    step_name = "Verify an upgrade from the latest public same-major V2 release"
    assert gates.index("Require a public source repository") < gates.index(step_name)
    step = gates.split(
        f"- name: {step_name}",
        maxsplit=1,
    )[1].split("- name: Create evidence-backed reports", maxsplit=1)[0]

    assert "GH_TOKEN: ${{ github.token }}" in step
    assert "gh api --paginate --slurp" in step
    assert "repos/${GITHUB_REPOSITORY}/releases?per_page=100" in step
    assert "[.[][]" in step
    assert ".draft == false" in step
    assert ".published_at != null" in step
    assert ".tag_name != $current" in step
    assert "(.tag_name | startswith($major_prefix))" in step
    assert "sort_by([.published_at, .tag_name])" in step
    assert "| last" in step
    assert '[[ "$previous_tag" =~ ^v[0-9]+\\.[0-9]+\\.[0-9]+(-rc\\.[1-9][0-9]*)?$ ]]' in step
    assert 'python - "$previous_tag" "$GITHUB_REF_NAME" <<\'PY\'' in step
    assert "from packaging.version import InvalidVersion, Version" in step
    assert "if previous >= current:" in step
    assert 'previous_ref="refs/tags/${previous_tag}"' in step
    assert 'git show-ref --verify --quiet "$previous_ref"' in step
    assert '"repos/${GITHUB_REPOSITORY}/git/ref/tags/${previous_tag}"' in step
    assert 'select(.ref == $expected_ref and .object.type == "tag")' in step
    assert '[[ "$(git cat-file -t "$local_tag_object")" == tag ]]' in step
    assert '[[ "$local_tag_object" == "$remote_tag_object" ]]' in step
    assert '"repos/${GITHUB_REPOSITORY}/git/tags/${remote_tag_object}"' in step
    assert ".sha == $expected_tag_object" in step
    assert ".tag == $expected_tag" in step
    assert '.object.type == "commit"' in step
    assert ".verification.verified == true" in step
    assert '.verification.reason == "valid"' in step
    assert 'local_commit="$(git rev-parse "${previous_ref}^{commit}")"' in step
    assert '[[ "$local_commit" == "$remote_commit" ]]' in step
    assert '[[ "$local_commit" != "$GITHUB_SHA" ]]' in step
    assert 'git merge-base --is-ancestor "$local_commit" "$GITHUB_SHA"' in step
    assert 'git archive --format=tar "$local_commit"' in step
    assert "No prior non-draft public GitHub Release exists" in step

    assert "git tag --list" not in step
    assert "--sort=-v:refname" not in step
    assert "not_applicable_initial_release" not in step
    assert "|| true" not in step


def _release_upgrade_step() -> str:
    gates = (ROOT / ".github/workflows/release-gates.yml").read_text(encoding="utf-8")
    return gates.split(
        "- name: Verify an upgrade from the latest public same-major V2 release",
        maxsplit=1,
    )[1].split("- name: Create evidence-backed reports", maxsplit=1)[0]


def _between(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker) + len(start_marker)
    return source[start : source.index(end_marker, start)]


def _run_jq(program: str, value: object, *arguments: str) -> subprocess.CompletedProcess[str]:
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq is supplied by the tagged Linux release runner")
    return subprocess.run(  # noqa: S603 - fixed local jq with test-only arguments
        [jq, "-er", *arguments, program],
        input=json.dumps(value),
        text=True,
        capture_output=True,
        check=False,
    )


def test_public_release_selector_executes_against_paginated_fail_closed_fixtures() -> None:
    step = _release_upgrade_step()
    program = _between(
        step,
        'jq -er --arg current "$GITHUB_REF_NAME" --arg major_prefix "$major_prefix" \'\n',
        '\n            \' "$release_metadata"',
    )
    arguments = (
        "--arg",
        "current",
        "v0.1.0-rc.9",
        "--arg",
        "major_prefix",
        "v0.",
    )
    pages = [
        [
            {
                "draft": True,
                "published_at": "2026-08-15T06:00:00Z",
                "prerelease": True,
                "tag_name": "v0.2.0-rc.1",
            },
            {
                "draft": False,
                "published_at": "2026-08-16T08:06:33Z",
                "prerelease": True,
                "tag_name": "v0.1.0-rc.8",
            },
            {
                "draft": False,
                "published_at": "2026-08-16T01:14:49Z",
                "prerelease": True,
                "tag_name": "v0.1.0-rc.6",
            },
            {
                "draft": False,
                "published_at": "2026-08-15T05:00:00Z",
                "prerelease": True,
                "tag_name": "v1.0.0-rc.1",
            },
        ],
        [
            {
                "draft": False,
                "published_at": "2026-08-15T18:07:19Z",
                "prerelease": True,
                "tag_name": "v0.1.0-rc.5",
            },
            {
                "draft": False,
                "published_at": "2026-08-15T08:34:29Z",
                "prerelease": True,
                "tag_name": "v0.1.0-rc.3",
            },
            {
                "draft": False,
                "published_at": "2026-08-14T18:35:05Z",
                "prerelease": True,
                "tag_name": "v0.1.0-rc.1",
            },
        ],
    ]
    # Failed RC2 and RC4 have Git tags but no Releases, so they are intentionally absent.
    selected = _run_jq(program, pages, *arguments)
    assert selected.returncode == 0, selected.stderr
    assert selected.stdout.strip() == "v0.1.0-rc.8"

    for rejected in ([[]], [[{}]], {"not": "slurped release pages"}):
        result = _run_jq(program, rejected, *arguments)
        assert result.returncode != 0


def test_release_predecessor_version_check_rejects_equal_or_newer_versions() -> None:
    step = _release_upgrade_step()
    program = textwrap.dedent(
        _between(
            step,
            'python - "$previous_tag" "$GITHUB_REF_NAME" <<\'PY\'\n',
            "\n          PY",
        )
    )

    def validate(previous: str, current: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed interpreter with test-only input
            [sys.executable, "-", previous, current],
            input=program,
            text=True,
            capture_output=True,
            check=False,
        )

    assert validate("v0.1.0-rc.8", "v0.1.0-rc.9").returncode == 0
    assert validate("v0.1.0-rc.9", "v0.1.0-rc.9").returncode != 0
    assert validate("v0.1.0-rc.10", "v0.1.0-rc.9").returncode != 0
    assert validate("v0.1.0", "v0.1.0-rc.9").returncode != 0
    assert validate("not-a-version", "v0.1.0-rc.9").returncode != 0


def test_release_tag_metadata_filters_execute_and_reject_invalid_verification() -> None:
    step = _release_upgrade_step()
    ref_program = _between(
        step,
        'jq -er --arg expected_ref "$previous_ref" \'\n',
        '\n            \' "$tag_ref_metadata"',
    )
    tag_program = _between(
        step,
        "              select(\n                .sha == $expected_tag_object",
        '\n            \' "$tag_object_metadata"',
    )
    tag_program = "select(\n  .sha == $expected_tag_object" + tag_program
    tag_object = "a" * 40
    commit = "b" * 40
    ref = {
        "ref": "refs/tags/v0.1.0-rc.1",
        "object": {"sha": tag_object, "type": "tag"},
    }
    ref_result = _run_jq(
        ref_program,
        ref,
        "--arg",
        "expected_ref",
        "refs/tags/v0.1.0-rc.1",
    )
    assert ref_result.returncode == 0, ref_result.stderr
    assert ref_result.stdout.strip() == tag_object
    ref["object"]["type"] = "commit"
    assert (
        _run_jq(
            ref_program,
            ref,
            "--arg",
            "expected_ref",
            "refs/tags/v0.1.0-rc.1",
        ).returncode
        != 0
    )

    metadata = {
        "sha": tag_object,
        "tag": "v0.1.0-rc.1",
        "object": {"sha": commit, "type": "commit"},
        "verification": {
            "payload": "signed payload",
            "reason": "valid",
            "signature": "SSH signature",
            "verified": True,
        },
    }
    tag_arguments = (
        "--arg",
        "expected_tag",
        "v0.1.0-rc.1",
        "--arg",
        "expected_tag_object",
        tag_object,
    )
    tag_result = _run_jq(tag_program, metadata, *tag_arguments)
    assert tag_result.returncode == 0, tag_result.stderr
    assert tag_result.stdout.strip() == commit
    metadata["verification"]["verified"] = False
    assert _run_jq(tag_program, metadata, *tag_arguments).returncode != 0


def test_release_checksums_only_flat_regular_files_and_publishes_every_asset() -> None:
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "sha256sum -- *" not in release
    assert "find . -mindepth 1 -maxdepth 1 ! -type f -print -quit" in release
    assert '[[ "$asset" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]' in release
    assert 'sha256sum -- "${release_assets[@]}" > SHA256SUMS' in release
    assert "sha256sum --check --strict SHA256SUMS" in release
    assert (
        "frontend-playwright-results.xml frontend-npm-audit.json "
        "frontend-test-results.tar.gz" in release
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

    launcher = (ROOT / "backend/app/bill_rate_import/sandbox_launcher.py").read_text(
        encoding="utf-8"
    )
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
    assert "/var/log/powermeter /var/log/powermeter/gateway" in dockerfile
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
    initializer = (ROOT / "deploy/truenas/initialize_host.py").read_text(encoding="utf-8")
    staging = (ROOT / "deploy/truenas/Stage-PowerMeterTrueNAS.ps1").read_text(encoding="utf-8")

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
    assert smoke.count("-days 30") == 2
    assert "-days 2 " not in smoke
    assert '"-checkend", "604800"' in initializer
    assert '"-attime"' in initializer

    strict_smoke_verify = (
        'openssl verify -x509_strict -purpose sslserver -CAfile "$work/tls-ca.crt"'
    )
    assert smoke.count(strict_smoke_verify) == 1
    assert '"-x509_strict"' in initializer
    assert '"-verify_hostname"' in initializer
    assert "'verify', '-x509_strict', '-purpose', 'sslserver'" in staging
    assert "'-attime', $minimumValidEpoch" in staging
    assert "'-verify_hostname', $HostName" in staging


def test_release_smoke_expected_upload_rejection_is_fail_closed() -> None:
    smoke = (ROOT / "scripts/release_deployment_smoke.sh").read_text(encoding="utf-8")
    transport = smoke.split("curl_transport_common=(", maxsplit=1)[1].split(")", maxsplit=1)[0]
    for required in (
        "--silent",
        "--show-error",
        "--connect-timeout 5",
        "--max-time 30",
        '--resolve "${hostname}:8443:127.0.0.1"',
        '--cacert "$work/tls-ca.crt"',
    ):
        assert required in transport
    assert "--fail" not in transport
    assert 'curl_common=(--fail "${curl_transport_common[@]}")' in smoke

    upload = smoke.split('upload_status="$(curl', maxsplit=1)[1].split(
        "jq -e '.code == \"BILL_RATE_IMPORT_REJECTED\"'", maxsplit=1
    )[0]
    assert '"${curl_transport_common[@]}"' in upload
    assert '"${curl_common[@]}"' not in upload
    assert '-o "$work/upload-response.json"' in upload
    assert "-w '%{http_code}'" in upload
    assert '[[ "$upload_status" == "422" ]]' in upload
    assert 'readonly upload_status="$(curl' not in smoke
    assert re.search(
        r'^upload_status="\$\(curl .*?\)"\nreadonly upload_status$',
        smoke,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert smoke.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail\n")


def test_release_smoke_archive_lookup_is_privileged_and_fail_closed() -> None:
    smoke = (ROOT / "scripts/release_deployment_smoke.sh").read_text(encoding="utf-8")
    expected_find = (
        'sudo find "$base/backups/archives" -maxdepth 1 -type f '
        "-name 'powermeter-*.dump.gpg' -print -quit"
    )
    expected_lookup = (
        f'archive_path="$({expected_find})"\n'
        + "readonly archive_path\n"
        + '[[ -n "$archive_path" ]]'
    )
    archive_offset = smoke.index(expected_lookup)
    archive_path_reads = [line for line in smoke.splitlines() if '"$base/backups/archives"' in line]

    assert archive_path_reads == [f'archive_path="$({expected_find})"']
    assert smoke.count(expected_find) == 1
    assert '$(find "$base/backups/archives"' not in smoke
    assert f'readonly archive_path="$({expected_find})"' not in smoke
    assert f'[[ -n "$({expected_find})" ]]' not in smoke
    assert smoke.rfind("set -e", 0, archive_offset) > smoke.rfind("set +e", 0, archive_offset)


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
    assert smoke.index("trap cleanup EXIT") < smoke.index('[[ "${GITHUB_ACTIONS:-}" == "true"')
    assert 'runner_authorized" == "true"' in smoke
    assert 'base_owned" == "true"' in smoke
    assert smoke.index('collect_failure_diagnostics "$exit_code"') < smoke.index(
        "compose down --volumes --remove-orphans"
    )
    assert '--arg expected_version "${GITHUB_REF_NAME#v}"' in smoke
    assert ".version == $expected_version" in smoke
    assert 'rollback:"not_exercised_github_hosted_smoke"' in smoke
    assert "compose run --rm initialize" in smoke
    assert "initializer_finished_at" in smoke
    assert "declare -A runtime_container_ids=()" in smoke
    assert 'compose stop "${runtime_service_names[@]}"' in smoke
    assert "compose start postgres api worker frontend gateway backup" not in smoke
    assert 'docker start "$expected_container_id"' in smoke
    assert 'wait_healthy "$service" "$expected_container_id"' in smoke
    assert smoke.index('runtime_container_ids["$service"]="$container_id"') < smoke.index(
        'compose stop "${runtime_service_names[@]}"'
    )
    assert 'failed_assertion="initializer_finished_at_unchanged_after_runtime_restart"' in smoke
    assert '"failed_assertion": sys.argv[6]' in smoke
    assigned_failure_assertions = set(
        re.findall(r'^[ \t]*failed_assertion="([a-z_]+)"$', smoke, flags=re.MULTILINE)
    )
    assert assigned_failure_assertions == FAILURE_ASSERTIONS
    restart_loop = smoke.split(
        "for service in postgres api worker frontend gateway backup; do", maxsplit=1
    )[1].split("done", maxsplit=1)[0]
    assert 'compose restart "$service"' in restart_loop
    assert "initialize" not in restart_loop

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
    initializer = (ROOT / "deploy/truenas/initialize_host.py").read_text(encoding="utf-8")
    staging = (ROOT / "deploy/truenas/Stage-PowerMeterTrueNAS.ps1").read_text(encoding="utf-8")
    template = (ROOT / "deploy/truenas/power-monitor-v2.yaml").read_text(encoding="utf-8")
    normalized_installation = " ".join(installation.replace(">", " ").split())

    assert "Install via YAML" in installation
    assert "gh attestation verify" in installation
    assert "SHA256SUMS" in installation
    assert "scripts/verify_release_artifacts.py" not in installation
    assert "docker exec" not in installation
    assert "sudo " not in installation
    assert "System > Shell" not in installation
    assert "prepare-host.sh" not in installation
    assert "pm-protocol/1.0.0" in installation
    assert "authenticated PZEM-004T readings" in installation
    assert "$Tag = 'v0.1.0-rc.9'" in installation
    assert "$env:TEMP" in installation
    assert "[guid]::NewGuid().ToString('N')" in installation
    assert "Join-Path $HOME" not in installation
    assert "signed v0.1.0-rc.9 release" in normalized_installation
    assert "Stage-PowerMeterTrueNAS.ps1" in installation
    assert "power-monitor.home.arpa -> 192.168.0.175" in installation
    assert "Direct-IP HTTPS is not supported" in installation
    assert "unless a coordinated certificate" not in installation
    assert "disable or delete the SMB share" in installation
    assert "complete" in installation.lower()

    for dataset in (
        "postgres",
        "config",
        "firmware",
        "backups",
        "logs",
        "rate-source-artifacts",
        "caddy-data",
        "caddy-config",
        "secrets",
    ):
        assert dataset in datasets
    assert "POSIX ACLs" in datasets
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
        assert secret in staging or secret in initializer

    assert "create_host_path: false" in datasets
    assert "mount namespace cannot conclusively prove" in datasets
    assert "network_mode: none" in template
    assert "service_completed_successfully" in template
    assert "never generates, rotates, replaces" in secrets
    assert "UNPUBLISHED_API_DIGEST" in template
    assert "UNPUBLISHED_FRONTEND_DIGEST" in template
    assert "UNPUBLISHED_GATEWAY_DIGEST" in template
    assert "UNPUBLISHED_BACKUP_DIGEST" in template


def test_candidate_notes_describe_workflow_output_without_claiming_source_publication() -> None:
    notes = (ROOT / "release/RELEASE_NOTES.md").read_text(encoding="utf-8")
    normalized = " ".join(notes.split())
    assert "source copy alone is not publication evidence" in normalized
    assert "power-monitor-v2-v0.1.0-rc.9.yaml" in normalized
    assert "Keep all existing application secrets" in normalized
    assert "Alembic head `20260816_0013`" in normalized
    assert "Revision 0009 adds the exact-home rate-candidate" in normalized
    assert "permanent-loss rows immutable" in normalized
    assert "firmware `v0.1.0-rc.9`" in normalized
    assert "41dcb941227367b2097b4b16d8c43d0312bc9a3794e1fa96e7a2c89b77f37c63" in normalized
    assert "failed release run" in normalized
    assert "31866197054" in normalized
    assert "There is no server rc.2 GitHub Release" in normalized
    assert "Public server rc.8 remains installable" in normalized
    assert "Firmware rc.9 must be published and verified" in normalized
    assert "31893354667" in normalized
    assert "no server rc.4 GitHub Release" in normalized
    assert "deterministically" in normalized
    assert "docker compose start" in normalized
    assert "captures the six runtime container IDs" in normalized
    assert "never persisted" in normalized
    assert "nine Generic/POSIX child ZFS datasets" in normalized
    assert "not_exercised_github_hosted_smoke" in normalized
    assert "proves only the forward upgrade" in normalized
    assert "Application-only rollback is not authorized" in normalized
    assert "matching pre-upgrade snapshot" in normalized
    assert "migration report can permit application rollback" not in normalized
    assert "Hardware status is honestly `pending`" in normalized
    assert "at least 72 hours" in normalized
    assert "repositories do not yet exist" not in normalized
    assert "Current `gh` authentication is invalid" not in normalized


def test_rc3_recovery_docs_separate_failed_rc2_forward_upgrade_and_publication() -> None:
    rollback = " ".join((ROOT / "deploy/truenas/ROLLBACK.md").read_text(encoding="utf-8").split())
    release_process = " ".join(
        (ROOT / "docs/RELEASE_PROCESS.md").read_text(encoding="utf-8").split()
    )
    testing = " ".join((ROOT / "docs/TESTING.md").read_text(encoding="utf-8").split())
    firmware = " ".join((ROOT / "docs/FIRMWARE_RELEASES.md").read_text(encoding="utf-8").split())
    traceability = " ".join(
        (ROOT / "docs/REQUIREMENTS_TRACEABILITY.md").read_text(encoding="utf-8").split()
    )

    assert "proved only forward rc.1-to-rc.3 upgrade" in rollback
    assert "Never attach old binaries to the current post-upgrade database" in rollback
    assert "matching pre-upgrade ZFS snapshot or verified encrypted backup" in rollback
    assert "migration report can permit" not in rollback
    assert "Historical rc.3 evidence proved only forward rc.1-to-rc.3 upgrade" in release_process
    assert "latest lower same-major non-draft public release" in release_process
    assert "therefore selects public rc.8" in release_process
    assert "exercises only the forward path" in testing

    assert "Historical published v0.1.0-rc.1 evidence" in firmware
    assert "55/55 host tests" in firmware
    assert "02e0c46a0bfee4fcf35a0bf82de191bf04e69a65d387fbbdbb78e6876b6b06da" in firmware
    assert "signed, public firmware" in firmware
    assert "v0.1.0-rc.2" in firmware
    assert "No server rc.2 Release, image set, TrueNAS YAML, or deployment smoke" in firmware
    assert "coordinated public firmware rc.3 release" in firmware
    assert "7caada9c6295f4c201fd7ce7d383822e6b5785a960022de8355e3b6acc9a4e2c" in firmware
    assert "matching server rc.5 release completed publication" in firmware
    assert "firmware rc.1 through rc.5 crash in the main stack before provisioning" in firmware
    assert "Candidate firmware rc.9" in firmware

    assert "Signed public firmware `v0.1.0-rc.2` is historical" in traceability
    assert "Signed server tag `v0.1.0-rc.2` and failed run `31866197054`" in traceability
    assert "Coordinated server and firmware `v0.1.0-rc.3` are public" in traceability
    assert "Coordinated server and firmware rc.5 and rc.6 are public" in traceability
    assert "target repos are absent" not in traceability
    assert "invalid `gh` authentication" not in traceability
    assert "no signed public release" not in traceability
