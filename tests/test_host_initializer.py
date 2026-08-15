from __future__ import annotations

import base64
import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from scripts.render_truenas_release import ReleaseError, validate_compose
from scripts.validate_release import validate_compose as validate_static_compose

ROOT = Path(__file__).resolve().parents[1]
INITIALIZER_PATH = ROOT / "deploy/truenas/initialize_host.py"
COMPOSE_PATH = ROOT / "deploy/truenas/power-monitor-v2.yaml"


def _load_initializer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pm_host_initializer", INITIALIZER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INITIALIZER = _load_initializer()


def _secret_values() -> dict[str, bytes]:
    values: dict[str, bytes] = {}
    for index, name in enumerate(INITIALIZER.DATABASE_SECRETS, start=1):
        values[name] = f"{index:064x}".encode()
    for index, name in enumerate(INITIALIZER.APPLICATION_SECRETS, start=21):
        values[name] = base64.b64encode(bytes([index]) * 32)
    values["backup_encryption_key"] = base64.b64encode(bytes([42]) * 32)
    values["tls.crt"] = b"certificate fixture"
    values["tls.key"] = b"private key fixture"
    values["tls-ca.crt"] = b"CA fixture"
    return values


def test_initializer_accepts_exact_independent_secret_formats() -> None:
    INITIALIZER._validate_secret_values(_secret_values())


@pytest.mark.parametrize("mutation", ["duplicate", "newline", "short_base64", "weak_backup"])
def test_initializer_rejects_invalid_secret_values(mutation: str) -> None:
    values = _secret_values()
    if mutation == "duplicate":
        values["postgres_api_password"] = values["postgres_migrator_password"]
    elif mutation == "newline":
        values["postgres_api_password"] += b"\n"
    elif mutation == "short_base64":
        values["session_secret"] = base64.b64encode(b"short")
    else:
        values["backup_encryption_key"] = b"not enough words"
    with pytest.raises(INITIALIZER.InitializationError):
        INITIALIZER._validate_secret_values(values)


def test_initializer_compares_normalized_secret_key_material() -> None:
    values = _secret_values()
    database_bytes = bytes.fromhex(values["postgres_api_password"].decode())
    values["session_secret"] = base64.b64encode(database_bytes)
    with pytest.raises(INITIALIZER.InitializationError, match="independent"):
        INITIALIZER._validate_secret_values(values)


@pytest.mark.parametrize(
    "hostname",
    ["192.168.0.175", "localhost", "power-monitor.home.arpa.", "-bad.home.arpa"],
)
def test_initializer_rejects_non_dns_or_unsafe_hostnames(hostname: str) -> None:
    with pytest.raises(INITIALIZER.InitializationError):
        INITIALIZER._validate_hostname(hostname)
    INITIALIZER._validate_hostname("power-monitor.home.arpa")


def test_initializer_requires_each_explicit_host_mount_without_claiming_zfs_proof() -> None:
    test_root = ROOT / ".test-runtime" / f"mountinfo-{uuid.uuid4()}"
    test_root.mkdir(parents=True)
    target = Path("/host/postgres")
    try:
        valid = test_root / "valid-mountinfo"
        valid.write_text(
            "42 31 0:45 / /host/postgres rw,relatime - zfs Apps/PowerMeterV2/postgres rw\n",
            encoding="utf-8",
        )
        INITIALIZER._assert_explicit_host_mount(target, valid)

        for root, filesystem in (("/PowerMeterV2/postgres", "zfs"), ("/", "ext4")):
            invalid = test_root / f"invalid-{filesystem}-{len(root)}"
            invalid.write_text(
                f"42 31 0:45 {root} /host/postgres rw,relatime - {filesystem} source rw\n",
                encoding="utf-8",
            )
            INITIALIZER._assert_explicit_host_mount(target, invalid)
        missing = test_root / "missing"
        missing.write_text(
            "42 31 0:45 / /host/config rw,relatime - zfs source rw\n",
            encoding="utf-8",
        )
        with pytest.raises(INITIALIZER.InitializationError, match="explicit"):
            INITIALIZER._assert_explicit_host_mount(target, missing)
    finally:
        for path in test_root.iterdir():
            path.unlink()
        test_root.rmdir()


def test_config_assets_are_readable_by_only_their_runtime_uid(monkeypatch) -> None:
    path = Path("/host/config/Caddyfile")
    metadata_calls: list[tuple[str, int, int]] = []
    acl_calls: list[list[str]] = []

    def fake_chown(_path: Path, uid: int, gid: int, *, follow_symlinks: bool) -> None:
        metadata_calls.append(("chown", uid, gid))
        assert follow_symlinks is False

    def fake_chmod(_path: Path, mode: int, *, follow_symlinks: bool) -> None:
        metadata_calls.append(("chmod", mode, 0))
        assert follow_symlinks is False

    def fake_run(command: list[str], _failure: str, **_kwargs) -> bytes:
        acl_calls.append(command)
        return b""

    monkeypatch.setattr(INITIALIZER.os, "chown", fake_chown, raising=False)
    monkeypatch.setattr(INITIALIZER.os, "chmod", fake_chmod)
    monkeypatch.setattr(INITIALIZER, "_run", fake_run)

    INITIALIZER._set_asset_metadata(path, 1000)

    assert metadata_calls == [("chown", 0, 0), ("chmod", 0o400, 0)]
    assert acl_calls == [
        ["setfacl", "-b", "--", str(path)],
        ["setfacl", "-m", "u:1000:r--", "--", str(path)],
    ]
    assert INITIALIZER._expected_read_acl((1000,)) == {
        "user::r--",
        "user:1000:r--",
        "group::---",
        "mask::r--",
        "other::---",
    }
    assert {name: uid for name, (_source, uid) in INITIALIZER.CONFIG_ASSETS.items()} == {
        "Caddyfile": 1000,
        "postgres-init-roles.sh": 70,
    }


def test_source_only_host_preparer_matches_restricted_config_asset_contract() -> None:
    source = (ROOT / "deploy/truenas/prepare-host.sh").read_text(encoding="utf-8")
    assert 'install -o 0 -g 0 -m 0400 -- "$assets/Caddyfile" "$base/config/Caddyfile"' in source
    assert "install -o 0 -g 0 -m 0400 -- \\\n" in source
    assert '"$assets/postgres-init-roles.sh" "$base/config/postgres-init-roles.sh"' in source
    assert 'set_read_acl "$base/config/Caddyfile" 1000' in source
    assert 'set_read_acl "$base/config/postgres-init-roles.sh" 70' in source
    assert '"$base/config/Caddyfile|0:0|440"' in source
    assert '"$base/config/postgres-init-roles.sh|0:0|440"' in source
    assert "-m 0644" not in source


def test_tls_verification_rejects_encryption_and_binds_strict_hostname(monkeypatch) -> None:
    test_root = ROOT / ".test-runtime" / f"tls-initializer-{uuid.uuid4()}"
    test_root.mkdir(parents=True)
    certificate = b"-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n"
    (test_root / "tls.crt").write_bytes(certificate)
    (test_root / "tls-ca.crt").write_bytes(certificate)
    key_path = test_root / "tls.key"
    key_path.write_bytes(
        b"-----BEGIN ENCRYPTED PRIVATE KEY-----\nZmFrZQ==\n-----END ENCRYPTED PRIVATE KEY-----\n"
    )
    try:
        with pytest.raises(INITIALIZER.InitializationError, match="unencrypted"):
            INITIALIZER._validate_tls("power-monitor.home.arpa", test_root)

        key_path.write_bytes(b"-----BEGIN PRIVATE KEY-----\nZmFrZQ==\n-----END PRIVATE KEY-----\n")
        commands: list[list[str]] = []

        def fake_run(command: list[str], _failure: str, **_kwargs) -> bytes:
            commands.append(command)
            return b"same-public-key" if "-pubkey" in command or "-pubout" in command else b""

        monkeypatch.setattr(INITIALIZER, "_run", fake_run)
        monkeypatch.setattr(INITIALIZER.time, "time", lambda: 1_700_000_000)
        INITIALIZER._validate_tls("power-monitor.home.arpa", test_root)
        verifies = [command for command in commands if command[1] == "verify"]
        assert len(verifies) == 2
        assert "-attime" not in verifies[0]
        assert verifies[1][verifies[1].index("-attime") + 1] == "1700604800"
        for verify in verifies:
            assert "-x509_strict" in verify
            assert verify[verify.index("-verify_hostname") + 1] == "power-monitor.home.arpa"
    finally:
        for path in test_root.iterdir():
            path.unlink()
        test_root.rmdir()


def test_backup_status_acl_verification_is_exact(monkeypatch) -> None:
    path = Path("/host/backups/status")
    monkeypatch.setattr(INITIALIZER, "_numeric_acl", lambda _path: INITIALIZER.EXPECTED_STATUS_ACL)
    INITIALIZER._verify_status_acl(path)
    unexpected = set(INITIALIZER.EXPECTED_STATUS_ACL) | {"user:999:r-x"}
    monkeypatch.setattr(INITIALIZER, "_numeric_acl", lambda _path: unexpected)
    with pytest.raises(INITIALIZER.InitializationError, match="backup-status"):
        INITIALIZER._verify_status_acl(path)


def test_backups_traversal_acl_verification_is_exact(monkeypatch) -> None:
    path = Path("/host/backups")
    monkeypatch.setattr(INITIALIZER, "_numeric_acl", lambda _path: INITIALIZER.EXPECTED_BACKUPS_ACL)
    INITIALIZER._verify_backups_acl(path)
    unexpected = set(INITIALIZER.EXPECTED_BACKUPS_ACL) | {"default:user:10001:--x"}
    monkeypatch.setattr(INITIALIZER, "_numeric_acl", lambda _path: unexpected)
    with pytest.raises(INITIALIZER.InitializationError, match="traversal"):
        INITIALIZER._verify_backups_acl(path)


def test_release_compose_has_one_shot_initializer_and_runtime_secret_isolation() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    validate_compose(compose, published=False)
    assert validate_static_compose(COMPOSE_PATH) == []
    services = compose["services"]
    initializer = services["initialize"]
    assert initializer["network_mode"] == "none"
    assert initializer["read_only"] is True
    assert initializer["cap_drop"] == ["ALL"]
    assert set(initializer["cap_add"]) == {"CHOWN", "FOWNER", "DAC_OVERRIDE"}
    assert len(initializer["secrets"]) == 13
    assert services["postgres"]["volumes"][1]["source"].endswith("/config")
    assert services["gateway"]["volumes"][0]["source"].endswith("/config")
    for name, service in services.items():
        if name == "initialize":
            continue
        assert service["depends_on"]["initialize"] == {
            "condition": "service_completed_successfully"
        }
        assert all(volume.get("target") != "/host/secrets" for volume in service.get("volumes", []))


def test_release_renderer_rejects_initializer_privilege_or_gate_drift() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    compose["services"]["initialize"]["cap_add"] = ["ALL"]
    with pytest.raises(ReleaseError, match="capabilities"):
        validate_compose(compose, published=False)

    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    del compose["services"]["gateway"]["depends_on"]["initialize"]
    with pytest.raises(ReleaseError, match="gateway"):
        validate_compose(compose, published=False)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("initializer_target", "exact required host datasets"),
        ("initializer_read_only", "exact required host datasets"),
        ("initializer_create_host_path", "may create an unexpected host path"),
        ("runtime_secret_directory", "must not mount the secrets dataset"),
        ("runtime_secret_short_syntax", "not a long-form bind mount"),
        ("runtime_child_bind", "exact UI-created dataset root"),
        ("writable_gateway_config", "must mount the config dataset read-only"),
    ],
)
def test_both_release_validators_reject_host_mount_boundary_drift(
    mutation: str, message: str
) -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    services = compose["services"]
    if mutation == "initializer_target":
        services["initialize"]["volumes"][0]["target"] = "/host/wrong"
    elif mutation == "initializer_read_only":
        services["initialize"]["volumes"][0]["read_only"] = True
    elif mutation == "initializer_create_host_path":
        services["initialize"]["volumes"][0]["bind"]["create_host_path"] = True
    elif mutation == "runtime_secret_directory":
        services["api"]["volumes"].append(
            {
                "type": "bind",
                "source": "/mnt/Apps/PowerMeterV2/secrets",
                "target": "/unexpected",
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        )
    elif mutation == "runtime_secret_short_syntax":
        services["api"]["volumes"].append("/mnt/Apps/PowerMeterV2/secrets:/unexpected:ro")
    elif mutation == "runtime_child_bind":
        services["api"]["volumes"][2]["source"] = "/mnt/Apps/PowerMeterV2/logs/application"
    else:
        services["gateway"]["volumes"][0]["read_only"] = False
    with pytest.raises(ReleaseError):
        validate_compose(compose, published=False)

    test_root = ROOT / ".test-runtime" / f"compose-mutation-{uuid.uuid4()}"
    test_root.mkdir(parents=True)
    mutated = test_root / "power-monitor-v2.yaml"
    try:
        mutated.write_text(yaml.safe_dump(compose), encoding="utf-8")
        assert any(message in error for error in validate_static_compose(mutated))
    finally:
        mutated.unlink()
        test_root.rmdir()


def test_windows_stager_is_bounded_non_generating_and_commits_marker_last() -> None:
    source = (ROOT / "deploy/truenas/Stage-PowerMeterTrueNAS.ps1").read_text(encoding="utf-8")
    for prohibited in (
        "RandomNumberGenerator",
        "openssl rand",
        "Write-Secret",
        "-Recurse",
        "ForceRotate",
    ):
        assert prohibited not in source
    assert (
        "[Parameter(Mandatory)]\n    [System.Management.Automation.PSCredential]$Credential"
        in source
    )
    assert "SourceDirectory must be a trusted local path" in source
    assert "The SMB destination must be empty" in source
    assert "[System.IO.File]::Copy" in source
    assert "[System.IO.File]::Move" in source
    assert "$movedNames = [System.Collections.Generic.List[string]]::new()" in source
    assert "[void]$movedNames.Add($name)" in source
    assert "if (-not $stagingSucceeded -and $null -ne $remoteRoot)" in source
    assert "foreach ($movedName in $movedNames)" in source
    assert "$movedPath = Join-Path $remoteRoot $movedName" in source
    assert "Failed to clean a file published by this staging invocation" in source
    assert "[DateTimeOffset]::UtcNow.AddSeconds(604800).ToUnixTimeSeconds()" in source
    assert "'-attime', $minimumValidEpoch" in source
    assert "Test-SameFile" in source
    already_verified = source.index("exact 13 files are already staged")
    assert source.index("return", already_verified) < source.index("[System.IO.File]::Move")
    marker = source.index(".powermeter-stage-complete")
    final_verification = source.index("The final SMB destination did not verify")
    marker_removal = source.index("Remove-Item -LiteralPath $marker", final_verification)
    assert marker < final_verification < marker_removal
    write_host_lines = [line for line in source.splitlines() if "Write-Host" in line]
    assert write_host_lines == [
        "        Write-Host 'The exact 13 files are already staged and byte-for-byte verified.'",
        "    Write-Host 'Staging passed: exactly 13 secret/TLS files were preserved and verified.'",
    ]


def test_no_shell_installation_uses_the_exact_nine_dataset_model() -> None:
    source = (ROOT / "deploy/truenas/INSTALLATION.md").read_text(encoding="utf-8")
    assert "these nine child ZFS datasets" in source
    assert "reports 9 mounts, 13 preserved files" in source
    assert "reports 10 mounts" not in source
