#!/usr/bin/env python3
"""Fail-closed, one-shot TrueNAS host-path initializer.

The release API image embeds this script and the two configuration assets.  It
does not generate, rotate, replace, print, or hash secret values.  Its only
write operations are host-path ownership/mode/ACL repair and atomic installation
of the image-embedded public configuration files.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import ipaddress
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path

HOST_ROOT = Path("/host")
SECRET_MOUNT_ROOT = Path("/run/secrets")
ASSET_ROOT = Path("/opt/powermeter/host-assets")

DATABASE_SECRETS = (
    "postgres_bootstrap_password",
    "postgres_migrator_password",
    "postgres_api_password",
    "postgres_worker_password",
    "postgres_backup_password",
    "postgres_restore_password",
)
APPLICATION_SECRETS = ("session_secret", "field_encryption_key", "ota_manifest_key")
TLS_SECRETS = ("tls.crt", "tls.key", "tls-ca.crt")
ALL_SECRETS = DATABASE_SECRETS + APPLICATION_SECRETS + ("backup_encryption_key",) + TLS_SECRETS

DATASET_DIRECTORIES = {
    "postgres": (70, 70, 0o700),
    "config": (0, 0, 0o755),
    "firmware": (10001, 10001, 0o750),
    "backups": (568, 568, 0o750),
    "logs": (0, 0, 0o711),
    "rate-source-artifacts": (10001, 10001, 0o750),
    "caddy-data": (1000, 1000, 0o750),
    "caddy-config": (1000, 1000, 0o750),
    "secrets": (0, 0, 0o711),
}
CHILD_DIRECTORIES = {
    "backups/status": (568, 568, 0o750),
    "logs/application": (10001, 10001, 0o750),
    "logs/gateway": (1000, 1000, 0o750),
}
SECRET_READERS = {
    "postgres_bootstrap_password": (70,),
    "postgres_migrator_password": (70, 10001),
    "postgres_api_password": (70, 10001),
    "postgres_worker_password": (70, 10001),
    "postgres_backup_password": (70, 568),
    "postgres_restore_password": (70, 568),
    "session_secret": (10001,),
    "field_encryption_key": (10001,),
    "ota_manifest_key": (10001,),
    "backup_encryption_key": (568,),
    "tls.crt": (1000,),
    "tls.key": (1000,),
    "tls-ca.crt": (1000,),
}
CONFIG_ASSETS = {
    "Caddyfile": (ASSET_ROOT / "Caddyfile", 1000),
    "postgres-init-roles.sh": (ASSET_ROOT / "postgres-init-roles.sh", 70),
}
EXPECTED_STATUS_ACL = {
    "user::rwx",
    "user:10001:r-x",
    "group::r-x",
    "mask::r-x",
    "other::---",
    "default:user::rwx",
    "default:user:10001:r-x",
    "default:group::r-x",
    "default:mask::r-x",
    "default:other::---",
}
EXPECTED_BACKUPS_ACL = {
    "user::rwx",
    "user:10001:--x",
    "group::r-x",
    "mask::r-x",
    "other::---",
}


class InitializationError(RuntimeError):
    """A host-path invariant was not satisfied."""


def _fail(message: str) -> None:
    raise InitializationError(message)


def _run(command: list[str], failure: str, *, input_bytes: bytes | None = None) -> bytes:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed executable/arguments only
            command,
            input=input_bytes,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise InitializationError(failure) from exc
    if completed.returncode != 0:
        _fail(failure)
    return completed.stdout


def _assert_real_directory(path: Path) -> None:
    try:
        value = path.lstat()
    except OSError as exc:
        raise InitializationError(f"required host dataset mount is missing: {path.name}") from exc
    if not stat.S_ISDIR(value.st_mode) or path.is_symlink():
        _fail(f"required host dataset mount is not a real directory: {path.name}")
    if path.resolve(strict=True) != path:
        _fail(f"host dataset path does not resolve exactly: {path.name}")


def _unescape_mountinfo(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _assert_explicit_host_mount(path: Path, mountinfo: Path = Path("/proc/self/mountinfo")) -> None:
    """Require an explicit container mount at the fixed target.

    The mount namespace cannot reliably distinguish a ZFS dataset root from a
    bind of a directory within that dataset. TrueNAS ZFS dataset identity is an
    explicit UI/operator precondition, while this check proves the bind exists.
    """
    target = path.as_posix()
    try:
        lines = mountinfo.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise InitializationError("cannot inspect container mount boundaries") from exc
    for line in lines:
        before, separator, after = line.partition(" - ")
        fields = before.split()
        if separator and after and len(fields) >= 5 and _unescape_mountinfo(fields[4]) == target:
            return
    _fail(f"host path is not an explicit container bind mount: {path.name}")


def _assert_regular_file(path: Path, label: str, *, maximum_bytes: int = 1024 * 1024) -> bytes:
    try:
        value = path.lstat()
    except OSError as exc:
        raise InitializationError(f"required {label} is missing") from exc
    if path.is_symlink() or not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        _fail(f"required {label} is not a single real regular file")
    if value.st_size <= 0 or value.st_size > maximum_bytes:
        _fail(f"required {label} has an invalid size")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise InitializationError(f"required {label} cannot be read") from exc


def _single_ascii_value(value: bytes, name: str) -> str:
    if not value or len(value) > 4096 or b"\n" in value or b"\r" in value or b"\x00" in value:
        _fail(f"secret has an invalid single-value encoding: {name}")
    try:
        return value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise InitializationError(f"secret is not ASCII: {name}") from exc


def _decode_base64(value: str, name: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InitializationError(f"secret is not canonical Base64: {name}") from exc
    if base64.b64encode(decoded).decode("ascii") != value:
        _fail(f"secret is not canonical Base64: {name}")
    return decoded


def _validate_secret_values(host_values: dict[str, bytes]) -> None:
    normalized_values: list[bytes] = []
    for name in DATABASE_SECRETS:
        value = _single_ascii_value(host_values[name], name)
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            _fail(f"database secret must be exactly 64 lowercase hexadecimal characters: {name}")
        normalized_values.append(bytes.fromhex(value))
    for name in APPLICATION_SECRETS:
        value = _single_ascii_value(host_values[name], name)
        decoded = _decode_base64(value, name)
        if len(decoded) != 32:
            _fail(f"application secret must decode to exactly 32 bytes: {name}")
        normalized_values.append(decoded)
    backup = _single_ascii_value(host_values["backup_encryption_key"], "backup_encryption_key")
    backup_valid = False
    try:
        backup_valid = len(_decode_base64(backup, "backup_encryption_key")) >= 32
    except InitializationError:
        backup_valid = False
    if not backup_valid and re.fullmatch(r"[!-~]+(?: [!-~]+){5,}", backup) is None:
        _fail("backup_encryption_key must be 32+ random Base64 bytes or six Diceware words")
    normalized_values.append(
        _decode_base64(backup, "backup_encryption_key") if backup_valid else backup.encode()
    )
    for index, normalized_value in enumerate(normalized_values):
        if any(hmac.compare_digest(normalized_value, prior) for prior in normalized_values[:index]):
            _fail("application and database secret values must all be independent")


def _validate_hostname(hostname: str) -> None:
    if len(hostname) > 253 or hostname.endswith("."):
        _fail("PM_HOSTNAME must be an unqualified DNS name without a trailing dot")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        _fail("PM_HOSTNAME must be a DNS name, not an IP address")
    labels = hostname.split(".")
    if len(labels) < 2 or any(
        len(label) > 63 or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label) is None
        for label in labels
    ):
        _fail("PM_HOSTNAME is not a valid DNS name")


def _validate_tls(hostname: str, secret_root: Path) -> None:
    certificate = secret_root / "tls.crt"
    private_key = secret_root / "tls.key"
    ca_certificate = secret_root / "tls-ca.crt"
    private_key_bytes = private_key.read_bytes()
    if re.search(rb"(?im)^-----BEGIN ENCRYPTED PRIVATE KEY-----$", private_key_bytes) or re.search(
        rb"(?im)^(Proc-Type:\s*4,ENCRYPTED|DEK-Info:)", private_key_bytes
    ):
        _fail("TLS private key must be an unencrypted PEM key")
    _run(
        ["openssl", "pkey", "-in", str(private_key), "-passin", "pass:", "-check", "-noout"],
        "TLS private key is invalid or encrypted",
    )
    _run(
        ["openssl", "x509", "-in", str(certificate), "-noout", "-checkhost", hostname],
        "TLS certificate SAN does not contain PM_HOSTNAME",
    )
    _run(
        ["openssl", "x509", "-in", str(certificate), "-noout", "-checkend", "604800"],
        "TLS certificate expires in less than seven days",
    )
    certificate_bytes = certificate.read_bytes()
    blocks = re.findall(
        rb"-----BEGIN CERTIFICATE-----\s+.*?-----END CERTIFICATE-----\s*",
        certificate_bytes,
        flags=re.DOTALL,
    )
    if not blocks or b"".join(blocks).strip() != certificate_bytes.strip():
        _fail("tls.crt is not an exact PEM certificate chain")
    command = [
        "openssl",
        "verify",
        "-x509_strict",
        "-purpose",
        "sslserver",
        "-CAfile",
        str(ca_certificate),
        "-verify_hostname",
        hostname,
    ]
    temporary_chain: str | None = None
    try:
        if len(blocks) > 1:
            with tempfile.NamedTemporaryFile(prefix="pm-tls-chain-", delete=False) as stream:
                stream.write(b"".join(blocks[1:]))
                stream.flush()
                os.fsync(stream.fileno())
                temporary_chain = stream.name
            command.extend(["-untrusted", temporary_chain])
        command.append(str(certificate))
        _run(command, "TLS certificate chain does not verify strictly against tls-ca.crt")
        future_command = [
            *command[:-1],
            "-attime",
            str(int(time.time()) + 604800),
            str(certificate),
        ]
        _run(
            future_command,
            "TLS certificate chain expires in less than seven days",
        )
    finally:
        if temporary_chain is not None:
            Path(temporary_chain).unlink(missing_ok=True)
    certificate_key = _run(
        ["openssl", "x509", "-in", str(certificate), "-pubkey", "-noout"],
        "cannot read TLS certificate public key",
    )
    private_public_key = _run(
        ["openssl", "pkey", "-in", str(private_key), "-passin", "pass:", "-pubout"],
        "cannot derive TLS private-key public key",
    )
    if not hmac.compare_digest(certificate_key, private_public_key):
        _fail("TLS certificate and private key do not match")


def _reset_directory(path: Path, uid: int, gid: int, mode: int) -> None:
    _run(["setfacl", "-b", "-k", "--", str(path)], f"cannot reset ACL on {path.name}")
    os.chown(path, uid, gid, follow_symlinks=False)
    os.chmod(path, mode, follow_symlinks=False)


def _ensure_child_directory(relative: str, uid: int, gid: int, mode: int) -> Path:
    path = HOST_ROOT / relative
    try:
        path.mkdir(mode=mode, exist_ok=True)
    except OSError as exc:
        raise InitializationError(f"cannot create required runtime directory: {relative}") from exc
    _assert_real_directory(path)
    _reset_directory(path, uid, gid, mode)
    return path


def _set_asset_metadata(path: Path, gid: int) -> None:
    os.chown(path, 0, gid, follow_symlinks=False)
    os.chmod(path, 0o440, follow_symlinks=False)


def _verify_asset_metadata(path: Path, gid: int) -> None:
    try:
        value = path.lstat()
    except OSError as exc:
        raise InitializationError(f"cannot verify config asset metadata: {path.name}") from exc
    if (value.st_uid, value.st_gid, stat.S_IMODE(value.st_mode)) != (0, gid, 0o440):
        _fail(f"config asset owner or mode verification failed: {path.name}")


def _install_asset(source: Path, destination: Path, gid: int) -> None:
    source_bytes = _assert_regular_file(source, f"image asset {source.name}")
    if destination.exists() or destination.is_symlink():
        _assert_regular_file(destination, f"existing config file {destination.name}")
    temporary = destination.parent / f".{destination.name}.initializer-{os.getpid()}"
    descriptor = -1
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(source_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        _set_asset_metadata(temporary, gid)
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise InitializationError(
            f"cannot install verified config asset: {destination.name}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    if not hmac.compare_digest(source_bytes, _assert_regular_file(destination, destination.name)):
        _fail(f"installed config asset differs from image asset: {destination.name}")
    _verify_asset_metadata(destination, gid)


def _set_secret_acl(path: Path, readers: tuple[int, ...]) -> None:
    _run(["setfacl", "-b", "--", str(path)], f"cannot reset secret ACL: {path.name}")
    os.chown(path, 0, 0, follow_symlinks=False)
    os.chmod(path, 0o400, follow_symlinks=False)
    entries = ",".join(f"u:{uid}:r--" for uid in readers)
    _run(["setfacl", "-m", entries, "--", str(path)], f"cannot set secret ACL: {path.name}")


def _numeric_acl(path: Path) -> set[str]:
    output = _run(["getfacl", "-cpn", "--", str(path)], f"cannot verify ACL: {path.name}")
    try:
        return {
            line
            for line in output.decode("ascii").splitlines()
            if line and not line.startswith("#")
        }
    except UnicodeDecodeError as exc:
        raise InitializationError(f"cannot verify ACL: {path.name}") from exc


def _verify_secret_acl(path: Path, readers: tuple[int, ...]) -> None:
    value = path.lstat()
    if (value.st_uid, value.st_gid, stat.S_IMODE(value.st_mode)) != (0, 0, 0o440):
        _fail(f"secret owner or mode verification failed: {path.name}")
    expected = {"user::r--", "group::---", "mask::r--", "other::---"}
    expected.update(f"user:{uid}:r--" for uid in readers)
    if _numeric_acl(path) != expected:
        _fail(f"secret ACL verification failed: {path.name}")


def _verify_directory(path: Path, uid: int, gid: int, mode: int) -> None:
    value = path.lstat()
    if (value.st_uid, value.st_gid, stat.S_IMODE(value.st_mode)) != (uid, gid, mode):
        _fail(f"directory owner or mode verification failed: {path.relative_to(HOST_ROOT)}")


def _verify_status_acl(path: Path) -> None:
    if _numeric_acl(path) != EXPECTED_STATUS_ACL:
        _fail("backup-status API ACL verification failed")


def _verify_backups_acl(path: Path) -> None:
    if _numeric_acl(path) != EXPECTED_BACKUPS_ACL:
        _fail("backups API traversal ACL verification failed")


def initialize() -> None:
    os.umask(0o077)
    hostname = os.environ.get("PM_HOSTNAME", "power-monitor.home.arpa")
    _validate_hostname(hostname)

    for name in DATASET_DIRECTORIES:
        path = HOST_ROOT / name
        _assert_real_directory(path)
        _assert_explicit_host_mount(path)

    secret_root = HOST_ROOT / "secrets"
    try:
        actual_secret_names = {entry.name for entry in secret_root.iterdir()}
    except OSError as exc:
        raise InitializationError("cannot enumerate the staged secret dataset") from exc
    if actual_secret_names != set(ALL_SECRETS):
        _fail("secret dataset must contain exactly the required 13 files")

    host_values: dict[str, bytes] = {}
    for name in ALL_SECRETS:
        host_value = _assert_regular_file(
            secret_root / name, f"host secret {name}", maximum_bytes=65536
        )
        mounted_value = _assert_regular_file(
            SECRET_MOUNT_ROOT / name,
            f"individual Compose secret {name}",
            maximum_bytes=65536,
        )
        if not hmac.compare_digest(host_value, mounted_value):
            _fail(f"individual Compose secret does not match its host source: {name}")
        host_values[name] = host_value
    _validate_secret_values(host_values)
    _validate_tls(hostname, secret_root)

    config_root = HOST_ROOT / "config"
    config_entries = {entry.name for entry in config_root.iterdir()}
    if not config_entries.issubset(CONFIG_ASSETS):
        _fail("config dataset contains an unexpected file or directory")

    for name, (uid, gid, mode) in DATASET_DIRECTORIES.items():
        _reset_directory(HOST_ROOT / name, uid, gid, mode)
    for relative, (uid, gid, mode) in CHILD_DIRECTORIES.items():
        _ensure_child_directory(relative, uid, gid, mode)
    status = HOST_ROOT / "backups/status"
    backups = HOST_ROOT / "backups"
    _run(
        ["setfacl", "-m", "u:10001:--x", "--", str(backups)],
        "cannot grant the API traversal-only backups ACL",
    )
    _run(
        ["setfacl", "-m", "u:10001:r-x,d:u:10001:r-x", "--", str(status)],
        "cannot grant the API read-only backup-status ACL",
    )

    for name, (source, gid) in CONFIG_ASSETS.items():
        _install_asset(source, config_root / name, gid)
    for name, readers in SECRET_READERS.items():
        _set_secret_acl(secret_root / name, readers)

    for name, expected in DATASET_DIRECTORIES.items():
        _verify_directory(HOST_ROOT / name, *expected)
    for relative, expected in CHILD_DIRECTORIES.items():
        _verify_directory(HOST_ROOT / relative, *expected)
    _verify_backups_acl(backups)
    _verify_status_acl(status)
    if {entry.name for entry in config_root.iterdir()} != set(CONFIG_ASSETS):
        _fail("config dataset does not contain the exact embedded asset set")
    for name, readers in SECRET_READERS.items():
        host_path = secret_root / name
        _verify_secret_acl(host_path, readers)
        if not hmac.compare_digest(host_values[name], _assert_regular_file(host_path, name)):
            _fail(f"secret content changed during metadata initialization: {name}")


def main() -> int:
    try:
        initialize()
    except (InitializationError, OSError) as exc:
        print(f"TrueNAS host initialization failed: {exc}", flush=True)
        return 2
    print(
        "TrueNAS host initialization passed: 9 mounted datasets, "
        "13 preserved secret files, and 2 embedded config assets.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
